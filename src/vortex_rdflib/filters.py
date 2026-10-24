"""FILTER expressions evaluated over term codes.

A ``Filter`` node's expression is split into its top-level conjuncts (rdflib
folds every FILTER of a group into one ``ConditionalAndExpression``; a row
passes iff every conjunct is true, so the split is exact). Each conjunct is
classified by the block variables it references: none (evaluated once), one
(a per-variable predicate applied to the pattern scan that binds it, before
any join), or several (applied on the joined rows). Either way a conjunct is
evaluated **per distinct value**, never per row, through two routes:

- **fast**: a whitelist of expression shapes compiled to a predicate over the
  parsed spelling (:class:`~vortex_rdflib.terms.TermView`). Every leaf mirrors
  the exact rdflib code path (``operators.py``, ``Literal.__gt__``/``eq``)
  and answers ``True``, ``False``, ``ERROR`` (rdflib would raise, which a
  filter turns into false) or ``UNKNOWN`` — the value is outside the domain
  the fast path reproduces exactly (an ill-typed number, a datatype rdflib
  orders by its own rules, ...) and is deferred, alone, to
- **generic**: rdflib's own evaluator (``_ebv``) on a ``FrozenBindings`` with
  the value decoded to an rdflib term. Semantics-exact by construction, at
  rdflib's per-evaluation cost, but paid once per distinct value.

``UNKNOWN`` is sticky through ``&&``/``||``/``!`` except where rdflib itself
short-circuits first (``false && ?`` is false, ``true || ?`` is true).

A single-variable conjunct of a shape vortex-rdf evaluates (:func:`native_shape`
lists them) also carries that predicate (``Conjunct.native``, a
:class:`~vortex_rdflib.candidates.NativeShape`). A kind test is its kind's
code range and an IRI equality its IRIs' codes, which the pushdown turns into
a ``keep`` on the native match (:func:`native_restriction`), so the rows that
fail never cross into Python. The one exception is a probed pattern: it
carries a range into each probe and tests a code set on the rows the probe
returns.
Every other native shape is answered by vortex-rdf over the distinct codes a
query produced (:func:`candidates.evaluate`, called from
:func:`evaluate_column`), never over the whole dictionary, and only the codes
it leaves undecided go down the two routes above.
"""

import re
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from rdflib.plugins.sparql.datatypes import XSD_DTs
from rdflib.plugins.sparql.evalutils import _ebv as rdflib_ebv
from rdflib.plugins.sparql.operators import _lang_range_check
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.sparql import FrozenBindings, SPARQLError
from rdflib.term import BNode, Literal, URIRef, Variable
from vortex_rdf import U64Column

from . import candidates
from .candidates import IRI_IN, NativeShape
from .settings import SETTINGS
from .terms import BLANK, IRI, LITERAL, TermView, canonical_spelling, kind_of, parse_spelling


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


ERROR = _Sentinel("ERROR")
UNKNOWN = _Sentinel("UNKNOWN")

_XSD = "http://www.w3.org/2001/XMLSchema#"
_XSD_STRING = _XSD + "string"
_XSD_BOOLEAN = _XSD + "boolean"
_XSD_INTEGER = _XSD + "integer"
_RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
#: How rdflib's parser spells ``IN ()``; its evaluator reads it back as the
#: empty list.
_RDF_NIL = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
_XML_COMPARABLE = frozenset(
    {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML",
    }
)
_XSD_DTS = frozenset(str(dt) for dt in XSD_DTs)

# rdflib's numeric datatypes with the converter Literal.__new__ applies
# (XSDToPython) and the well-formedness check that decides `ill_typed`.
_INT_LIKE = frozenset(
    _XSD + name
    for name in (
        "integer",
        "nonPositiveInteger",
        "negativeInteger",
        "nonNegativeInteger",
        "positiveInteger",
        "long",
        "unsignedLong",
        "int",
        "short",
        "byte",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    )
)
_DECIMAL = _XSD + "decimal"
_FLOATS = frozenset({_XSD + "float", _XSD + "double"})
NUMERIC_TYPES = _INT_LIKE | {_DECIMAL} | _FLOATS
_RANGES = {
    _XSD + "int": (-2147483648, 2147483647),
    _XSD + "short": (-32768, 32767),
    _XSD + "byte": (-128, 127),
    _XSD + "unsignedInt": (0, 4294967295),
    _XSD + "unsignedShort": (0, 65535),
    _XSD + "unsignedByte": (0, 255),
}
_SIGNS: dict[str, Callable[[int], bool]] = {
    _XSD + "nonNegativeInteger": lambda v: v >= 0,
    _XSD + "positiveInteger": lambda v: v > 0,
    _XSD + "nonPositiveInteger": lambda v: v <= 0,
    _XSD + "negativeInteger": lambda v: v < 0,
    _XSD + "unsignedLong": lambda v: v >= 0,
}

_IMPURE = frozenset({"Builtin_RAND", "Builtin_UUID", "Builtin_STRUUID", "Builtin_BNODE"})
_KIND_LEAVES = frozenset(
    {"Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL", "Builtin_BOUND"}
)
_VAR_LIKE = (Variable, BNode)


# --- values ---------------------------------------------------------------


def convert(view: TermView):
    """The raw converter result rdflib stores as ``Literal.value`` for a
    numeric datatype, ``None`` where the conversion fails."""
    dt, lex = view.dt, view.lex
    if lex is None:
        return None
    try:
        if dt in _INT_LIKE:
            return int(lex)
        if dt == _DECIMAL:
            return Decimal(lex)
        if dt in _FLOATS:
            return float(lex)
    except (ValueError, InvalidOperation, TypeError):
        return None
    return None


def numeric_value(view: TermView):
    """The value a literal contributes to rdflib's numeric fast path — a
    numeric datatype, a converting lexical form and a well-formed value —
    or ``None`` when rdflib would leave that path."""
    if view.kind != LITERAL or view.dt not in NUMERIC_TYPES:
        return None
    value = convert(view)
    if value is None:
        return None
    dt = view.dt
    bounds = _RANGES.get(dt)
    if bounds is not None and not (bounds[0] <= value <= bounds[1]):
        return None
    sign = _SIGNS.get(dt)
    if sign is not None and not sign(value):
        return None
    return value


def _bool_value(view: TermView) -> bool:
    # rdflib's _parseBoolean: anything but 1/true (case-insensitive) is False.
    return (view.lex or "").lower() in ("1", "true")


def _string_like(view) -> bool:
    """rdflib's ``string()``: a plain, language-tagged or xsd:string literal."""
    return (
        isinstance(view, TermView)
        and view.kind == LITERAL
        and (view.dt is None or view.dt == _XSD_STRING)
    )


def _num_compare(a, b, op: str):
    # A NaN meeting a Decimal raises decimal.InvalidOperation in Python and
    # rdflib propagates it; those values are left to rdflib.
    nan = any(v != v for v in (a, b))
    if nan and any(isinstance(v, Decimal) for v in (a, b)):
        return UNKNOWN
    if op == ">":
        return a > b
    return a == b


def _gt(a: TermView, b: TermView):
    """``Literal.__gt__`` for two literals: the numeric fast path, else the
    datatype-IRI order; everything after that is rdflib's own business."""
    va, vb = numeric_value(a), numeric_value(b)
    if va is not None and vb is not None:
        return _num_compare(va, vb, ">")
    dta, dtb = a.dt or _XSD_STRING, b.dt or _XSD_STRING
    if dta != dtb:
        return dta > dtb
    return UNKNOWN


def _eq(a: TermView, b: TermView):
    """``Literal.eq`` for two literals (value equality)."""
    va, vb = numeric_value(a), numeric_value(b)
    if va is not None and vb is not None:
        return _num_compare(va, vb, "==")
    if (a.lang or "").lower() != (b.lang or "").lower():
        return False
    dta, dtb = a.dt or _XSD_STRING, b.dt or _XSD_STRING
    if dta == _XSD_STRING and dtb == _XSD_STRING:
        return a.lex == b.lex
    if dta in _XML_COMPARABLE and dtb in _XML_COMPARABLE:
        return UNKNOWN
    if dta != dtb:
        return False
    if dta == _XSD_BOOLEAN:
        return _bool_value(a) == _bool_value(b)
    if dta in NUMERIC_TYPES:
        ca, cb = convert(a), convert(b)
        if ca is not None and cb is not None:
            return _num_compare(ca, cb, "==")
        # Without both values rdflib falls back to the lexical forms and
        # raises ("cannot know") when they differ.
        return True if a.lex == b.lex else ERROR
    # Other datatypes: equal spellings are equal values; anything else
    # depends on rdflib's parser for that datatype.
    return True if a.lex == b.lex else UNKNOWN


def _value_eq(a, b):
    """``x.eq(y)`` for two terms of any kind (the ``=`` operator)."""
    if a.kind == LITERAL and b.kind == LITERAL:
        return _eq(a, b)
    if a.kind != b.kind:
        return False  # a literal equals no other node; IRIs and bnodes are type-strict
    return a.lex == b.lex


def term_eq(a, b):
    """``x == y`` (RDF term equality, ``__eq__``). Exact for IRIs, blank
    nodes and string-like literals; other literals normalize their lexical
    form in rdflib, so unequal spellings may still be equal terms."""
    if a.kind != b.kind:
        return False
    if a.kind != LITERAL:
        return a.lex == b.lex
    if a.dt != b.dt or (a.lang or "").lower() != (b.lang or "").lower():
        return False
    if a.lex == b.lex:
        return True
    if a.dt is None or a.dt == _XSD_STRING:
        return False
    return UNKNOWN


def _truth(val):
    """rdflib's ``EBV`` of an evaluated value (bool, term view, unbound)."""
    if val is True or val is False or val is ERROR or val is UNKNOWN:
        return val
    if val is None or val.kind != LITERAL:
        return ERROR
    dt = val.dt
    if dt == _XSD_BOOLEAN:
        return _bool_value(val)
    if dt is None or dt == _XSD_STRING:
        return len(val.lex) > 0
    if dt in NUMERIC_TYPES:
        value = convert(val)
        return ERROR if value is None else bool(value)
    return UNKNOWN


def _not(val):
    if val is True or val is False:
        return not val
    return val


# --- the fast compiler -----------------------------------------------------


class _NotFast(Exception):
    """The expression is outside the whitelist; the conjunct takes the
    generic route for every value."""


def view_of_node(node) -> TermView:
    """A query constant as a term view (rdflib's normalized lexical form)."""
    if isinstance(node, Literal):
        return TermView(
            LITERAL,
            str(node),
            dt=str(node.datatype) if node.datatype is not None else None,
            lang=node.language,
        )
    if isinstance(node, URIRef):
        return TermView(IRI, str(node))
    if isinstance(node, BNode):
        return TermView(BLANK, str(node))
    raise _NotFast


def _const(value):
    return lambda env: value


def _constant_view(node):
    """A query constant's view — or ``UNKNOWN`` for a numeric constant rdflib
    compares by a value the fast route refuses.

    rdflib's SPARQL parser leaves a typed constant's ``ill_typed`` unset, so
    ``Literal.__gt__``/``eq`` take its numeric fast path whenever its value
    converts — ``"300"^^xsd:byte`` compares as 300 — where a data literal
    out of its type's bounds is ill-typed and ordered by its datatype IRI
    (``numeric_value`` refuses it). Such a constant sends every value to
    rdflib's own evaluator.
    """
    view = view_of_node(node)
    if (
        isinstance(node, Literal)
        and view.dt in NUMERIC_TYPES
        and node.ill_typed is not True
        and node.value is not None
        and numeric_value(view) is None
    ):
        return UNKNOWN
    return view


def _fold_constant(node):
    """A variable-free subexpression (``-1``, ``1 + 2``, ``str(5)``) evaluated
    once by rdflib itself; an error value stays an error."""
    try:
        result = node.eval({})
    except Exception as error:
        raise _NotFast from error
    if isinstance(result, (Literal, URIRef)):
        return view_of_node(result)
    if isinstance(result, SPARQLError):
        return ERROR
    raise _NotFast


def _compile(node, slots: dict, consts: dict):
    """Compile an expression node to ``env -> value`` where ``env`` is the
    tuple of term views of the conjunct's block variables (``None`` for an
    unbound variable) and the value is a term view, a bool, ``None``
    (unbound), ``ERROR`` or ``UNKNOWN``."""
    if isinstance(node, _VAR_LIKE):
        if node in slots:
            index = slots[node]
            return lambda env: env[index]
        return _const(consts.get(node))
    if isinstance(node, (Literal, URIRef)):
        return _const(_constant_view(node))
    if not isinstance(node, CompValue):
        raise _NotFast
    if not expr_vars(node):
        return _const(_fold_constant(node))
    name = node.name
    if name == "RelationalExpression":
        return _compile_relational(node, slots, consts)
    if name == "AdditiveExpression":
        return _compile_additive(node, slots, consts)
    if name in ("ConditionalAndExpression", "ConditionalOrExpression"):
        if node.other is None:
            raise _NotFast
        parts = [_compile(x, slots, consts) for x in [node.expr, *node.other]]
        return _and(parts) if name == "ConditionalAndExpression" else _or(parts)
    if name == "UnaryNot":
        inner = _compile(node.expr, slots, consts)
        return lambda env: _not(_truth(inner(env)))
    one_arg = _ONE_ARG.get(name)
    if one_arg is not None:
        if name == "Builtin_BOUND":
            if not isinstance(node.arg, _VAR_LIKE):
                raise _NotFast
        arg = _compile(node.arg, slots, consts)
        return lambda env: one_arg(arg(env))
    two_args = _TWO_ARGS.get(name)
    if two_args is not None:
        a, b = _compile(node.arg1, slots, consts), _compile(node.arg2, slots, consts)
        return lambda env: two_args(a(env), b(env))
    if name == "Builtin_REGEX":
        return _compile_regex(node, slots, consts)
    raise _NotFast


def _and(parts):
    def fn(env):
        for part in parts:
            r = _truth(part(env))
            if r is not True:
                return r  # False, ERROR (rdflib raises there) or UNKNOWN
        return True

    return fn


def _or(parts):
    def fn(env):
        unknown = error = False
        for part in parts:
            r = _truth(part(env))
            if r is True:
                return True
            if r is UNKNOWN:
                unknown = True
            elif r is ERROR:
                error = True
        if unknown:
            return UNKNOWN
        return ERROR if error else False

    return fn


def _integer(value):
    """The Python ``int`` an evaluated operand contributes to integer
    arithmetic, or ``UNKNOWN``.

    Narrower than rdflib's ``numeric()``, which computes with
    ``Literal.value`` whatever its well-formedness — an out-of-range
    ``xsd:byte`` still has one. ``numeric_value`` refuses those, so they
    defer instead of being reproduced from a different rule.
    """
    if not isinstance(value, TermView) or value.dt not in _INT_LIKE:
        return UNKNOWN
    converted = numeric_value(value)
    return UNKNOWN if converted is None else converted


def _compile_additive(node, slots, consts):
    """Compile ``+`` and ``-`` over integer-derived literals.

    rdflib's ``type_promotion`` sends every integer-derived datatype to
    ``xsd:integer``, and ``Literal(int, datatype=xsd:integer)`` spells its
    value with ``str``, so an all-integer sum is reproduced exactly — value
    *and* lexical form — without building one rdflib term. Every other
    operand (unbound, ill-typed, decimal, double, dateTime, a shape outside
    the whitelist) yields ``UNKNOWN``, and that binding alone goes to rdflib.
    """
    if node.other is None:
        # The add-expr production wraps a bare operand; rdflib's own
        # evaluator returns `expr` unchanged, so compile straight through.
        return _compile(node.expr, slots, consts)
    others = node.other if isinstance(node.other, list) else [node.other]
    operators = node.op if isinstance(node.op, list) else [node.op]
    if len(others) != len(operators) or any(op not in ("+", "-") for op in operators):
        raise _NotFast
    first = _compile(node.expr, slots, consts)
    # The sign is settled at compile time, so the loop below only ever adds.
    signed = [
        (1 if op == "+" else -1, _compile(part, slots, consts))
        for op, part in zip(operators, others, strict=True)
    ]

    def fn(env):
        total = _integer(first(env))
        if total is UNKNOWN:
            return UNKNOWN
        for sign, operand in signed:
            value = _integer(operand(env))
            if value is UNKNOWN:
                return UNKNOWN
            total += sign * value
        return TermView(LITERAL, str(total), dt=_XSD_INTEGER)

    return fn


def _compile_relational(node, slots, consts):
    op = node.op
    if node.other is None:
        raise _NotFast
    left = _compile(node.expr, slots, consts)
    if op in ("IN", "NOT IN"):
        others = node.other
        if not isinstance(others, list):
            others = [] if others == _RDF_NIL else [others]
        views = [view_of_node(x) for x in others]
        negate = op == "NOT IN"

        def fn_in(env):
            a = left(env)
            if a is None:
                return ERROR  # an unbound variable raises before the loop
            if a is UNKNOWN:
                return UNKNOWN
            if a is ERROR or isinstance(a, bool):
                return negate  # an error value equals nothing
            unknown = False
            for view in views:
                r = term_eq(view, a)
                if r is True:
                    return not negate
                if r is UNKNOWN:
                    unknown = True
            return UNKNOWN if unknown else negate

        return fn_in
    if op not in ("=", "!=", "<", ">", "<=", ">="):
        raise _NotFast
    right = _compile(node.other, slots, consts)

    def fn(env):
        a, b = left(env), right(env)
        # UNKNOWN first: rdflib evaluates the operands before comparing them,
        # so one the fast route cannot reproduce may raise there — outside
        # `_ebv`'s SPARQLError catch — before an unbound operand is reached.
        if a is UNKNOWN or b is UNKNOWN or isinstance(a, bool) or isinstance(b, bool):
            return UNKNOWN
        if a is None or b is None or a is ERROR or b is ERROR:
            return ERROR
        return _relational(op, a, b)

    return fn


def _relational(op: str, a: TermView, b: TermView):
    if op == "=":
        return _value_eq(a, b)
    if op == "!=":
        return _not(_value_eq(a, b))
    if a.kind != LITERAL or b.kind != LITERAL:
        return ERROR
    if a.dt and a.dt not in _XSD_DTS and b.dt and b.dt not in _XSD_DTS:
        return ERROR
    gt = _gt(a, b)
    if op == ">":
        return gt
    if gt is True:
        # __lt__ is `not gt and not eq`, __ge__ is `gt or eq`, __le__ is
        # `lt or eq`: a true gt settles all three without computing eq.
        return op == ">="
    if gt is not False:
        return gt  # ERROR or UNKNOWN
    eq = _eq(a, b)
    if op == "<":
        return _not(eq)
    if op == ">=":
        return eq
    # op == "<=": lt or eq == (not eq) or eq
    return True if eq in (True, False) else eq


def _is_kind(kind: str):
    def fn(a):
        if a is None:
            return ERROR
        if a is UNKNOWN:
            return UNKNOWN
        if a is ERROR:
            return False
        if isinstance(a, bool):
            return kind == LITERAL
        return a.kind == kind

    return fn


def _is_numeric(a):
    if a is None or a is ERROR or isinstance(a, bool):
        return False
    if a is UNKNOWN:
        return UNKNOWN
    return a.kind == LITERAL and a.dt in NUMERIC_TYPES


def _bound(a):
    if a is UNKNOWN:
        return UNKNOWN
    return a is not None


def _datatype(a):
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    if isinstance(a, bool):
        return TermView(IRI, _XSD_BOOLEAN)
    if a.kind != LITERAL:
        return ERROR
    if a.lang:
        return TermView(IRI, _RDF_LANGSTRING)
    return TermView(IRI, a.dt or _XSD_STRING)


def _lang(a):
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    if isinstance(a, bool):
        return TermView(LITERAL, "")
    if a.kind != LITERAL:
        return ERROR
    return TermView(LITERAL, a.lang or "")


def _str(a):
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    if isinstance(a, bool):
        return TermView(LITERAL, "true" if a else "false")
    if a.kind != LITERAL or _string_like(a):
        return TermView(LITERAL, a.lex)
    # Other datatypes carry rdflib's normalized lexical form ("042" -> "42").
    return UNKNOWN


def _string_arg(a):
    """rdflib's ``string()`` on an evaluated value: the view, or ERROR."""
    if a is UNKNOWN:
        return UNKNOWN
    if _string_like(a):
        return a
    return ERROR


def _lang_matches(tag, range_):
    tag, range_ = _string_arg(tag), _string_arg(range_)
    if tag is UNKNOWN or range_ is UNKNOWN:
        return UNKNOWN
    if tag is ERROR or range_ is ERROR:
        return ERROR
    if tag.lex == "":
        return False
    return _lang_range_check(range_.lex, tag.lex)


def _str_op(op: Callable[[str, str], bool]):
    def fn(a, b):
        a, b = _string_arg(a), _string_arg(b)
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        if a is ERROR or b is ERROR:
            return ERROR
        if b.lang and a.lang != b.lang:
            return ERROR  # _compatibleStrings
        return op(a.lex, b.lex)

    return fn


def _same_term(a, b):
    # UNKNOWN before unbound, as in `_compile_relational`: an argument the
    # fast route cannot reproduce is evaluated first and may raise there.
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    if a is None or b is None:
        return ERROR
    if a is ERROR or b is ERROR or isinstance(a, bool) or isinstance(b, bool):
        return False
    return term_eq(a, b)


_ONE_ARG = {
    "Builtin_isIRI": _is_kind(IRI),
    "Builtin_isURI": _is_kind(IRI),
    "Builtin_isBLANK": _is_kind(BLANK),
    "Builtin_isLITERAL": _is_kind(LITERAL),
    "Builtin_isNUMERIC": _is_numeric,
    "Builtin_BOUND": _bound,
    "Builtin_DATATYPE": _datatype,
    "Builtin_LANG": _lang,
    "Builtin_STR": _str,
}
_TWO_ARGS = {
    "Builtin_LANGMATCHES": _lang_matches,
    "Builtin_STRSTARTS": _str_op(str.startswith),
    "Builtin_STRENDS": _str_op(str.endswith),
    "Builtin_CONTAINS": _str_op(lambda a, b: b in a),
    "Builtin_sameTerm": _same_term,
}
_REGEX_FLAGS = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}
#: What ``re.compile`` raises for a pattern it cannot compile: a syntax error,
#: a repetition count past its limit, nesting past the recursion limit.
_COMPILE_ERRORS = (re.error, OverflowError, RecursionError)


def _regex_flags(flags) -> tuple[str, int]:
    """The flags of a REGEX flags literal (``None`` for none) that rdflib maps
    (``i``, ``s``, ``m``; it ignores every other letter), as those letters and
    as ``re`` flags."""
    kept = "".join(flag for flag in ("" if flags is None else str(flags)) if flag in _REGEX_FLAGS)
    compile_flags = 0
    for flag in kept:
        compile_flags |= _REGEX_FLAGS[flag]
    return kept, compile_flags


def _compile_regex(node, slots, consts):
    pattern, flags = node.pattern, node.flags
    if not isinstance(pattern, Literal) or (flags is not None and not isinstance(flags, Literal)):
        raise _NotFast
    if not _string_like(view_of_node(pattern)) or (
        flags is not None and not _string_like(view_of_node(flags))
    ):
        raise _NotFast
    _, compile_flags = _regex_flags(flags)
    try:
        regex = re.compile(str(pattern), compile_flags)
    except _COMPILE_ERRORS as error:
        raise _NotFast from error
    text = _compile(node.text, slots, consts)

    def fn(env):
        a = _string_arg(text(env))
        if a is UNKNOWN or a is ERROR:
            return a
        return regex.search(a.lex) is not None

    return fn


def compile_fast(expr, variables: tuple, consts: dict) -> Callable | None:
    """Compile ``expr`` into a predicate ``env -> True | False | UNKNOWN``
    over the term views of ``variables`` (in order), or ``None`` when the
    expression is outside the whitelist. ``consts`` maps the visible
    ctx-bound variables to their views; every other variable is unbound."""
    slots = {v: i for i, v in enumerate(variables)}
    try:
        node = _compile(expr, slots, consts)
    except _NotFast:
        return None

    def predicate(env):
        r = _truth(node(env))
        return False if r is ERROR else r

    return predicate


def is_kind_only(expr) -> bool:
    """Whether the fast predicate only needs each variable's term kind."""
    if isinstance(expr, _VAR_LIKE):
        return False  # a bare variable is evaluated as a term
    if not isinstance(expr, CompValue):
        return True
    name = expr.name
    if name in _KIND_LEAVES:
        return isinstance(expr.arg, _VAR_LIKE)
    if name in ("ConditionalAndExpression", "ConditionalOrExpression"):
        return expr.other is not None and all(is_kind_only(x) for x in [expr.expr, *expr.other])
    if name == "UnaryNot":
        return is_kind_only(expr.expr)
    return False


# --- native predicates ------------------------------------------------------

#: The relational operators as native numeric predicates, and the operator
#: that reads the same comparison with its operands swapped.
_NUM_KINDS = {
    "<": "num_lt",
    "<=": "num_le",
    ">": "num_gt",
    ">=": "num_ge",
    "=": "num_eq",
    "!=": "num_ne",
}
_SWAPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}
#: Language ranges on which the native basic filtering (RFC 4647 §3.3.1) and
#: rdflib's `_lang_range_check` agree: `*`, or ASCII alphanumeric subtags.
_LANG_RANGE = re.compile(r"\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _call_on(node, name: str, var) -> bool:
    """Whether ``node`` is the builtin ``name`` applied to ``var`` itself."""
    return isinstance(node, CompValue) and node.name == name and node.arg == var


def _string_literal(term) -> bool:
    """rdflib's ``string()``: a plain, ``xsd:string`` or language-tagged
    literal."""
    return isinstance(term, Literal) and (
        term.datatype is None or str(term.datatype) == _XSD_STRING
    )


def _string_constant(term) -> bool:
    """A plain or ``xsd:string`` literal: what the native string predicates
    take as their argument."""
    return _string_literal(term) and term.language is None


#: The string functions with a native kind, by rdflib's builtin name.
_STRING_FUNCTIONS = {
    "Builtin_CONTAINS": "contains",
    "Builtin_STRSTARTS": "strstarts",
    "Builtin_STRENDS": "strends",
}
#: The case wrappers a native string kind takes as its ``case``.
_CASE_WRAPPERS = {"Builtin_LCASE": "lower", "Builtin_UCASE": "upper"}


def _text_shape(node, var) -> tuple[str | None, bool] | None:
    """How a string function's text reads ``var``: ``(case, as_str)`` for
    ``var``, ``STR(var)`` and ``LCASE``/``UCASE`` of either — ``None`` for
    anything else."""
    case = None
    if isinstance(node, CompValue) and node.name in _CASE_WRAPPERS:
        case, node = _CASE_WRAPPERS[node.name], node.arg
    if isinstance(node, _VAR_LIKE) and node == var:
        return case, False
    if _call_on(node, "Builtin_STR", var):
        return case, True
    return None


def _string_function_shape(expr, var, consts) -> NativeShape | None:
    """CONTAINS, STRSTARTS or STRENDS of ``var``'s text and a string constant.

    rdflib's ``_compatibleStrings`` makes a tagged constant demand the text's
    own tag, compared as written: ``STR()`` text has none (an error for every
    value) and a stored tag is lower-case, so a tag that is not lower-case
    never matches either. Both stay with the Python routes."""
    text = _text_shape(expr.arg1, var)
    constant = _constant_term(expr.arg2, consts)
    if text is None or not _string_literal(constant):
        return None
    case, as_str = text
    language = constant.language
    if language is not None and (as_str or language != language.lower()):
        return None
    kind = _STRING_FUNCTIONS[expr.name]
    return NativeShape(kind, canonical_spelling(constant), case=case, as_str=as_str)


def _regex_shape(expr, var, consts) -> NativeShape | None:
    """REGEX of ``var``'s text with a constant string pattern and flags.

    rdflib compiles the pattern with Python's ``re`` and raises for every
    string text when that fails, so such a pattern stays with rdflib. Only
    the flags rdflib maps (``i``, ``s``, ``m``) are kept; it ignores the
    rest."""
    text = _text_shape(expr.text, var)
    pattern = _constant_term(expr.pattern, consts)
    flags = None if expr.flags is None else _constant_term(expr.flags, consts)
    if text is None or not _string_literal(pattern):
        return None
    if expr.flags is not None and not _string_literal(flags):
        return None
    kept, compile_flags = _regex_flags(flags)
    try:
        re.compile(str(pattern), compile_flags)
    except _COMPILE_ERRORS:
        return None
    case, as_str = text
    return NativeShape("regex", str(pattern), flags=kept, case=case, as_str=as_str)


#: IRIs ``canonical_spelling`` spells as an N-Triples IRIREF.
_SPELLABLE_IRI = re.compile(r'[^\x00-\x20<>"{}|^`\\]*')


def _iri_spelling(node, consts) -> str | None:
    """The spelling of the IRI an operand without block variables evaluates
    to, or ``None`` when it is not an IRI the dictionary could hold."""
    iri = _constant_term(node, consts)
    if not isinstance(iri, URIRef) or not _SPELLABLE_IRI.fullmatch(str(iri)):
        return None
    return canonical_spelling(iri)


def _iri_equality(left, right, var, consts) -> NativeShape | None:
    """``var = <iri>`` or ``<iri> = var`` (``=`` and ``sameTerm`` alike): true
    for exactly that IRI — rdflib's ``eq`` and ``==`` are term equality for
    an IRI, and no literal or blank node equals one."""
    for side, other in ((left, right), (right, left)):
        if isinstance(side, _VAR_LIKE) and side == var:
            spelling = _iri_spelling(other, consts)
            return None if spelling is None else NativeShape(IRI_IN, (spelling,))
    return None


def _iri_list(left, right, var, consts) -> NativeShape | None:
    """``var IN (<iri>, ...)``: true for exactly those IRIs. rdflib's parser
    spells an empty list as rdf:nil, which its evaluator reads back as the
    empty list."""
    if not (isinstance(left, _VAR_LIKE) and left == var):
        return None
    if not isinstance(right, list):
        return NativeShape(IRI_IN, ()) if right == _RDF_NIL else None
    spellings = []
    for node in right:
        spelling = _iri_spelling(node, consts)
        if spelling is None:
            return None
        spellings.append(spelling)
    return NativeShape(IRI_IN, tuple(dict.fromkeys(spellings)))


def _constant_term(node, consts: dict):
    """The rdflib term an operand without block variables evaluates to: a
    constant, a visible ctx-bound variable, or an expression over those,
    which rdflib's own evaluator computes. ``None`` for an error or a value
    that is not a term."""
    if isinstance(node, (Literal, URIRef)):
        return node
    if isinstance(node, _VAR_LIKE):
        term = consts.get(node)
        return term if isinstance(term, (Literal, URIRef)) else None
    if not isinstance(node, CompValue) or any(v not in consts for v in expr_vars(node)):
        return None
    try:
        result = node.eval(dict(consts))
    except Exception:  # noqa: BLE001 - not a value this route can rely on
        return None
    return result if isinstance(result, (Literal, URIRef)) else None


def native_shape(expr, var, consts: dict) -> NativeShape | None:
    """The native predicate (:class:`~vortex_rdflib.candidates.NativeShape`)
    that ``expr`` is as a test of ``var`` alone — any other variable seen only
    through ``consts`` — or ``None``.

    The shapes: ``isIRI``/``isURI``/``isBlank``/``isLiteral`` of ``var``;
    ``datatype(var) = <iri>`` and ``lang(var) = "tag"`` (either way round);
    ``langMatches(lang(var), "range")``; ``strstarts(str(var), "prefix")``;
    ``REGEX``, ``CONTAINS``, ``STRSTARTS`` and ``STRENDS`` of ``var`` or
    ``STR(var)``, either under ``LCASE``/``UCASE``, with a constant pattern or
    string; ``var = <iri>``, ``<iri> = var``, ``sameTerm`` of ``var`` and an
    IRI, and ``var IN (<iri>, ...)``; and ``var`` compared with a well-formed
    numeric literal by any of ``< <= > >= = !=`` (either way round). vortex-rdf
    answers each over the codes a query produced with rdflib's own rules
    wherever it gives a definite answer (``tests/test_native_filters.py`` pins
    that against rdflib's evaluator) and leaves every other code undecided.
    """
    if not isinstance(expr, CompValue):
        return None
    name = expr.name
    if name in ("Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL"):
        if expr.arg != var:
            return None
        kind = {"Builtin_isBLANK": "is_blank", "Builtin_isLITERAL": "is_literal"}.get(
            name, "is_iri"
        )
        return NativeShape(kind, "")
    if name == "Builtin_LANGMATCHES":
        if not _call_on(expr.arg1, "Builtin_LANG", var):
            return None
        # rdflib's `string()` admits a language-tagged range: its lexical
        # form is the range either way.
        rng = _constant_term(expr.arg2, consts)
        if not _string_literal(rng):
            return None
        return NativeShape("lang_matches", str(rng)) if _LANG_RANGE.fullmatch(str(rng)) else None
    if name == "Builtin_STRSTARTS" and _call_on(expr.arg1, "Builtin_STR", var):
        prefix = _constant_term(expr.arg2, consts)
        if _string_constant(prefix):
            return NativeShape("str_prefix", str(prefix))
    if name in _STRING_FUNCTIONS:
        return _string_function_shape(expr, var, consts)
    if name == "Builtin_REGEX":
        return _regex_shape(expr, var, consts)
    if name == "Builtin_sameTerm":
        return _iri_equality(expr.arg1, expr.arg2, var, consts)
    if name != "RelationalExpression" or expr.other is None:
        return None
    op, left, right = expr.op, expr.expr, expr.other
    if op == "IN":
        return _iri_list(left, right, var, consts)
    if isinstance(right, list):
        return None
    if op == "=":
        for call, other in ((left, right), (right, left)):
            if _call_on(call, "Builtin_DATATYPE", var):
                datatype = _constant_term(other, consts)
                if not isinstance(datatype, URIRef):
                    return None
                return NativeShape("datatype", str(datatype))
            if _call_on(call, "Builtin_LANG", var):
                tag = _constant_term(other, consts)
                return NativeShape("lang", str(tag)) if _string_constant(tag) else None
        iri = _iri_equality(left, right, var, consts)
        if iri is not None:
            return iri
    kind = _NUM_KINDS.get(op)
    if kind is None:
        return None
    if isinstance(left, _VAR_LIKE) and left == var:
        constant = _constant_term(right, consts)
    elif isinstance(right, _VAR_LIKE) and right == var:
        constant = _constant_term(left, consts)
        kind = _NUM_KINDS[_SWAPPED[op]]
    else:
        return None
    # A constant inside the value model both sides share — rdflib compares a
    # typed query constant by its value even out of its type's bounds (see
    # `_constant_view`), which the native layer refuses to.
    if (
        not isinstance(constant, Literal)
        or str(constant.datatype) not in NUMERIC_TYPES
        or numeric_value(view_of_node(constant)) is None
    ):
        return None
    return NativeShape(kind, canonical_spelling(constant))


def _sorted_contains(view, code: int) -> bool:
    index = bisect_left(view, code)
    return index < len(view) and view[index] == code


def native_restriction(store, conjuncts: list) -> tuple:
    """``(keep, residual)`` for one variable's conjuncts. ``keep`` admits
    exactly the codes passing every conjunct vortex-rdf answers with a keep
    (:func:`candidates.keep_of`) — ``None`` when there is none — and
    ``residual`` are the other conjuncts, evaluated over the codes the query
    produces (:func:`evaluate_column`)."""
    if not (SETTINGS.fast_filters and SETTINGS.native_filters):
        return None, conjuncts
    keeps, residual = [], []
    for conjunct in conjuncts:
        keep = None
        if conjunct.native is not None:
            try:
                keep = candidates.keep_of(store, conjunct.native)
            except ValueError:
                keep = None  # a constant the native parser refuses
        if keep is None:
            residual.append(conjunct)
        else:
            keeps.append(keep)
    return (intersect_keeps(keeps) if keeps else None), residual


def intersect_keeps(keeps: list):
    """The intersection of native keeps: ranges intersect as ranges, code
    columns (ascending, unique) through the native merge join, and a range
    over a column slices it."""
    ranges = [keep for keep in keeps if isinstance(keep, tuple)]
    columns = sorted((keep for keep in keeps if not isinstance(keep, tuple)), key=len)
    if ranges:
        lo = max(lo for lo, _ in ranges)
        hi = max(lo, min(hi for _, hi in ranges))
    if not columns:
        return (lo, hi)
    result = columns[0]
    for other in columns[1:]:
        if not len(result):
            break
        left, _ = result.join_indices(other)
        result = result.take(left)
    if ranges:
        view = memoryview(result).cast("Q")
        start, stop = bisect_left(view, lo), bisect_left(view, hi)
        if (start, stop) != (0, len(view)):
            result = U64Column(view[start:stop])
    return result


def keep_member(keep) -> Callable[[int], bool]:
    """A native keep as a test of one code, for rows that did not come
    through a keep-narrowed native match."""
    if isinstance(keep, tuple):
        lo, hi = keep
        return lambda code: lo <= code < hi
    view = memoryview(keep).cast("Q")
    return lambda code: _sorted_contains(view, code)


# --- analysis ----------------------------------------------------------------


def expr_vars(node, out: list | None = None) -> list:
    """The variable-like terms an expression references, in first-seen
    order. An EXISTS body is walked in its translated form (the instance
    attribute): rdflib pulls the body's FILTERs out of the parse tree it
    keeps under the key, so only the translation still names their
    variables, and a VALUES table in it names its own in each row's keys."""
    if out is None:
        out = []
    if isinstance(node, _VAR_LIKE):
        if node not in out:
            out.append(node)
    elif isinstance(node, CompValue):
        if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
            body = getattr(node, "graph", None)
            if isinstance(body, CompValue):
                return expr_vars(body, out)
        for key in node.keys():
            if key != "_vars":
                expr_vars(dict.__getitem__(node, key), out)
    elif isinstance(node, dict):
        expr_vars(list(node), out)
    elif isinstance(node, (list, tuple)):
        for x in node:
            expr_vars(x, out)
    return out


def has_minus(node) -> bool:
    """Whether an algebra tree or an expression holds a MINUS, one in an
    EXISTS body included (walked in its translated form, as
    :func:`expr_vars` does)."""
    if isinstance(node, (list, tuple)):
        return any(has_minus(item) for item in node)
    if not isinstance(node, CompValue):
        return False
    if node.name == "Minus":
        return True
    if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
        return has_minus(getattr(node, "graph", None))
    return any(has_minus(dict.__getitem__(node, key)) for key in node.keys() if key != "_vars")


def has_exists(node) -> bool:
    """Whether an expression holds an EXISTS or a NOT EXISTS."""
    if isinstance(node, (list, tuple)):
        return any(has_exists(item) for item in node)
    if not isinstance(node, CompValue):
        return False
    if node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
        return True
    return any(has_exists(dict.__getitem__(node, key)) for key in node.keys() if key != "_vars")


def _is_impure(node) -> bool:
    if isinstance(node, CompValue):
        if node.name in _IMPURE:
            return True
        return any(_is_impure(dict.__getitem__(node, k)) for k in node.keys() if k != "_vars")
    if isinstance(node, list):
        return any(_is_impure(x) for x in node)
    return False


def _flatten_and(expr, out: list) -> list:
    if isinstance(expr, CompValue) and expr.name == "ConditionalAndExpression" and expr.other:
        for x in [expr.expr, *expr.other]:
            _flatten_and(x, out)
    else:
        out.append(expr)
    return out


@dataclass(slots=True)
class Conjunct:
    expr: Any
    vars: tuple  # the block variables it references, in order
    fast: Callable | None  # env -> True | False | UNKNOWN
    kind_only: bool
    consts: dict = field(default_factory=dict)  # visible ctx-bound vars -> rdflib terms
    ctx: Any = None
    # The native predicate a single-variable conjunct is (see `native_shape`),
    # or None.
    native: NativeShape | None = None

    def generic(self, bound: dict) -> bool:
        """rdflib's own answer for one value assignment (``bound`` maps the
        conjunct's variables to decoded terms; unbound ones are absent)."""
        bindings = dict(self.consts)
        bindings.update(bound)
        return rdflib_ebv(self.expr, FrozenBindings(self.ctx, bindings))


@dataclass(slots=True)
class ExistsConjunct:
    conjunct: Conjunct
    negate: bool
    body: Any  # the translated algebra of the EXISTS group


@dataclass(slots=True)
class FilterPlan:
    constant: list  # conjuncts referencing no block variable
    per_var: dict  # block variable -> conjuncts referencing only it
    tuples: list  # conjuncts referencing several block variables
    exists: list  # ExistsConjunct: (NOT) EXISTS conjuncts, semi/anti-joins


def exists_shape(expr):
    """``(negate, body)`` when a conjunct is ``EXISTS {..}``, ``NOT EXISTS
    {..}`` or either under one ``!``; ``None`` otherwise. ``body`` is the
    translated group, which rdflib keeps as an instance attribute."""
    negate = False
    node = expr
    if isinstance(node, CompValue) and node.name == "UnaryNot":
        negate = True
        node = node.expr
    if isinstance(node, CompValue) and node.name in ("Builtin_EXISTS", "Builtin_NOTEXISTS"):
        body = getattr(node, "graph", None)
        if not isinstance(body, CompValue):
            return None
        return (negate != (node.name == "Builtin_NOTEXISTS"), body)
    return None


def analyze_filter(node, block_vars, ctx) -> FilterPlan:
    """Split a ``Filter`` node's expression into conjuncts and route them.

    Visibility follows rdflib's ``evalFilter``: the expression sees the block's
    own variables, the context's bindings that are in the node's ``_vars`` or
    in the query's ``initBindings`` (the rest was forgotten), and everything
    when the filter sits directly inside an EXISTS body
    (``no_isolated_scope``).
    """
    outer = dict(ctx.bindings.items())
    init = ctx.initBindings or {}
    if getattr(node, "no_isolated_scope", False):
        visible = outer
    else:
        allowed = set(node._vars or ()) | set(init)
        visible = {v: term for v, term in outer.items() if v in allowed}
    return analyze_expr(node.expr, block_vars, ctx, visible)


def _compile_conjunct(
    expr, variables: tuple, consts: dict, ctx, native: NativeShape | None
) -> Conjunct:
    """``expr`` as a conjunct of ``variables``, every other variable it reads
    fixed to the term ``consts`` gives it, or unbound: its fast route compiled
    over those constants (none when the fast routes are off, a constant has no
    term view or the expression is outside the whitelist), kind-only when it
    reads nothing but term kinds. ``native`` is the predicate vortex-rdf
    decides it by, if any."""
    fast = None
    if SETTINGS.fast_filters:
        try:
            const_views = {v: view_of_node(term) for v, term in consts.items()}
        except _NotFast:
            const_views = None
        if const_views is not None:
            fast = compile_fast(expr, variables, const_views)
    return Conjunct(
        expr, variables, fast, fast is not None and is_kind_only(expr), consts, ctx, native
    )


def analyze_expr(expr, block_vars, ctx, visible: dict) -> FilterPlan:
    """Route the conjuncts of a boolean expression over ``block_vars``;
    ``visible`` maps the context-bound variables the expression may see to
    their terms, every other non-block variable is unbound.

    A conjunct holding an EXISTS under another operator, with a MINUS in its
    body, is left to rdflib (``NotImplementedError``): it would be decided
    with only the conjunct's variables bound, where rdflib binds the whole
    row, which that MINUS then always shares."""
    block_set = set(block_vars)
    plan = FilterPlan([], {}, [], [])
    for conjunct_expr in _flatten_and(expr, []):
        expr = conjunct_expr
        if _is_impure(expr):
            raise NotImplementedError
        shape = exists_shape(expr)
        if shape is None and has_minus(expr):
            raise NotImplementedError
        referenced = expr_vars(expr)
        variables = tuple(v for v in referenced if v in block_set)
        consts = {v: visible[v] for v in referenced if v in visible and v not in block_set}
        native = None
        if len(variables) == 1 and SETTINGS.fast_filters and SETTINGS.native_filters:
            native = native_shape(expr, variables[0], consts)
        conjunct = _compile_conjunct(expr, variables, consts, ctx, native)
        if shape is not None:
            plan.exists.append(ExistsConjunct(conjunct, *shape))
        elif not variables:
            plan.constant.append(conjunct)
        elif len(variables) == 1:
            plan.per_var.setdefault(variables[0], []).append(conjunct)
        else:
            plan.tuples.append(conjunct)
    return plan


def specialize(conjunct: Conjunct, var, consts: dict) -> Conjunct | None:
    """``conjunct`` as a test of ``var`` alone, its other variables fixed to
    the terms ``consts`` gives them: a single-variable conjunct carrying the
    native shape that makes it (:func:`native_shape`), its fast route compiled
    over those constants — or ``None`` when it has no native shape that way.
    Its generic route is rdflib's evaluator over the same constants, so every
    value it decides is rdflib's answer."""
    shape = native_shape(conjunct.expr, var, consts)
    if shape is None:
        return None
    return _compile_conjunct(conjunct.expr, (var,), consts, conjunct.ctx, shape)


# --- evaluation ----------------------------------------------------------------


def evaluate_constant(conjunct: Conjunct) -> bool:
    """A conjunct without block variables, evaluated once."""
    if conjunct.fast is not None:
        r = conjunct.fast(())
        if r is not UNKNOWN:
            return r
    return conjunct.generic({})


def evaluate_column(store, conjuncts: list, codes) -> set:
    """The subset of ``codes`` (distinct codes of one variable) passing every
    conjunct — ``codes`` itself when it is a set and no conjunct runs.
    Kind-only predicates never decode; a conjunct with a native shape is
    answered by vortex-rdf over the codes still in play
    (:func:`candidates.evaluate`), and only the codes it leaves undecided go
    on; the others decode the surviving codes once, in one batch, and parse
    their spellings."""
    remaining = codes if isinstance(codes, set) else set(codes)
    views: dict = {}
    bounds = None
    for conjunct in conjuncts:
        if not remaining:
            break
        fast = conjunct.fast
        passed: set = set()
        unknown: list = []
        pending = remaining
        if conjunct.native is not None and not conjunct.kind_only and SETTINGS.native_filters:
            passed, pending = candidates.evaluate(store, conjunct.native, remaining)
        if not pending:
            pass
        elif fast is None:
            unknown = list(pending)
        elif conjunct.kind_only:
            if bounds is None:
                bounds = store._term_kind_bounds()
            for code in pending:
                r = fast((TermView(kind_of(code, bounds), None),))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
        else:
            decode_views(store, pending, views)
            for code in pending:
                r = fast((views[code],))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
        if unknown:
            var = conjunct.vars[0]
            # One decode_many; the loop reads the terms it returns.
            terms = store._prime_decode_cache(unknown)
            for code in unknown:
                if conjunct.generic({var: terms[code]}):
                    passed.add(code)
        remaining = passed
    return remaining


def _fast_route(store, fast, views: dict) -> Callable[[tuple], Any]:
    """``fast`` over a code tuple: each code parsed into a view once, through
    ``views``, a dictionary code decoded and a negative (query-constant) one
    taken from the query's foreign terms; an unbound ``None`` stays unbound."""

    def view_of(code):
        view = views.get(code)
        if view is None:
            if code < 0:
                view = views[code] = view_of_node(store._terms().foreign[code])
            else:
                spelling = store._dict.decode(code)
                if spelling is None:
                    raise ValueError(f"term code {code} is not in the store dictionary")
                view = views[code] = parse_spelling(spelling)
        return view

    def answer(key: tuple):
        return fast(tuple(None if code is None else view_of(code) for code in key))

    return answer


def tuple_predicate(
    store, conjunct: Conjunct, positions: list, shared_views: dict | None = None
) -> Callable[[tuple], bool]:
    """A row predicate for a conjunct over several variables, memoized per
    distinct code tuple; ``positions`` are the variables' row indexes and
    ``shared_views`` a parsed-term cache shared with the caller."""
    memo: dict = {}
    fast = conjunct.fast
    views = {} if shared_views is None else shared_views
    answer = None if fast is None else _fast_route(store, fast, views)
    variables = conjunct.vars

    def predicate(row) -> bool:
        key = tuple(row[i] for i in positions)
        r = memo.get(key)
        if r is None:
            r = UNKNOWN if answer is None else answer(key)
            if r is UNKNOWN:
                r = conjunct.generic(
                    {
                        v: store._decode_term(c)
                        for v, c in zip(variables, key, strict=True)
                        if c is not None
                    }
                )
            memo[key] = r
        return r is True

    return predicate


def constant_predicate(conjunct: Conjunct) -> Callable[[Any], bool]:
    """A single-variable conjunct as a test of a query constant (a VALUES
    table's term, as rdflib binds it), memoized per term: the fast route over
    the constant's view, rdflib's evaluator where that is unknown, a number
    the fast route refuses included (see :func:`_constant_view`)."""
    memo: dict = {}
    fast = conjunct.fast
    var = conjunct.vars[0]

    def predicate(term) -> bool:
        r = memo.get(term)
        if r is None:
            r = UNKNOWN
            if fast is not None:
                view = _constant_view(term)
                if view is not UNKNOWN:
                    r = fast((view,))
            if r is UNKNOWN:
                r = conjunct.generic({var: term})
            memo[term] = r
        return r is True

    return predicate


def tuple_fast_reject_predicate(
    store, conjunct: Conjunct, positions: list, shared_views: dict | None = None
) -> Callable[[tuple], bool]:
    """Keep rows unless the exact fast route proves a conjunct false.

    UNKNOWN remains visible to the normal final predicate, preserving the
    generic RDFLib fallback for values outside the fast path's domain.
    """
    fast = conjunct.fast
    if fast is None:
        raise ValueError("tuple_fast_reject_predicate requires a fast conjunct")
    memo: dict = {}
    answer = _fast_route(store, fast, {} if shared_views is None else shared_views)

    def predicate(row) -> bool:
        key = tuple(row[index] for index in positions)
        result = memo.get(key)
        if result is None:
            result = memo[key] = answer(key)
        return result is not False

    return predicate


def decode_views(store, codes, views: dict) -> None:
    """Parse into ``views`` every dictionary code of ``codes`` it lacks,
    decoding them in one batch."""
    missing = [code for code in codes if code not in views]
    if not missing:
        return
    for code, spelling in zip(missing, store._dict.decode_many(missing), strict=True):
        if spelling is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        views[code] = parse_spelling(spelling)


def prime_tuple_views(store, rows, positions, views: dict) -> None:
    """Decode, in one batch, the dictionary codes ``rows`` hold at
    ``positions`` that ``views`` lacks — for tuple predicates about to read
    every row, which would otherwise decode them one call at a time."""
    codes = {row[position] for row in rows for position in positions}
    codes.discard(None)
    decode_views(store, [code for code in codes if code >= 0], views)
