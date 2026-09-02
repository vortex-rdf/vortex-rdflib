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
"""

import re
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

from .terms import BLANK, IRI, LITERAL, TermView, kind_of, parse_spelling

# Force the generic route everywhere (VORTEX_RDF_FILTER_FAST=0): the
# equivalence tests run the matrix both ways.
_FAST_ENABLED: bool = True


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
_RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
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
        return _const(view_of_node(node))
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


def _integer_view(value: int) -> TermView:
    """An arithmetic result in SPARQL's integer value space."""
    return TermView(LITERAL, str(value), dt=_XSD + "integer")


def _compile_additive(node, slots, consts):
    """Compile integer-only ``+`` and ``-`` without rdflib term allocation.

    SPARQL promotes arithmetic over integer-derived datatypes to xsd:integer.
    Any unbound, ill-typed, non-integer or unexpected shape returns UNKNOWN,
    so the caller uses rdflib's evaluator for that distinct binding tuple.
    """
    if node.other is None or node.op is None:
        raise _NotFast
    others = node.other if isinstance(node.other, list) else [node.other]
    operators = node.op if isinstance(node.op, list) else [node.op]
    if len(others) != len(operators) or any(op not in ("+", "-") for op in operators):
        raise _NotFast
    first = _compile(node.expr, slots, consts)
    rest = [_compile(part, slots, consts) for part in others]

    def integer(value):
        if not isinstance(value, TermView) or value.kind != LITERAL or value.dt not in _INT_LIKE:
            return UNKNOWN
        converted = numeric_value(value)
        return converted if isinstance(converted, int) else UNKNOWN

    def fn(env):
        value = integer(first(env))
        if value is UNKNOWN:
            return UNKNOWN
        for operator, operand in zip(operators, rest, strict=True):
            other = integer(operand(env))
            if other is UNKNOWN:
                return UNKNOWN
            value = value + other if operator == "+" else value - other
        return _integer_view(value)

    return fn


def _compile_relational(node, slots, consts):
    op = node.op
    if node.other is None:
        raise _NotFast
    left = _compile(node.expr, slots, consts)
    if op in ("IN", "NOT IN"):
        others = node.other if isinstance(node.other, list) else [node.other]
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
        if a is None or b is None or a is ERROR or b is ERROR:
            return ERROR
        if a is UNKNOWN or b is UNKNOWN or isinstance(a, bool) or isinstance(b, bool):
            return UNKNOWN
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
    if a is None or b is None:
        return ERROR
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
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


def _compile_regex(node, slots, consts):
    pattern, flags = node.pattern, node.flags
    if not isinstance(pattern, Literal) or (flags is not None and not isinstance(flags, Literal)):
        raise _NotFast
    if not _string_like(view_of_node(pattern)) or (
        flags is not None and not _string_like(view_of_node(flags))
    ):
        raise _NotFast
    cflags = 0
    for flag in str(flags or ""):
        cflags |= _REGEX_FLAGS.get(flag, 0)
    try:
        regex = re.compile(str(pattern), cflags)
    except re.error as error:
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


# --- analysis ----------------------------------------------------------------


def expr_vars(node, out: list | None = None) -> list:
    """The variable-like terms an expression references, in first-seen
    order. An EXISTS body is walked in its translated form (the instance
    attribute): rdflib pulls the body's FILTERs out of the parse tree it
    keeps under the key, so only the translation still names their
    variables."""
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
    elif isinstance(node, (list, tuple)):
        for x in node:
            expr_vars(x, out)
    return out


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


def analyze_expr(expr, block_vars, ctx, visible: dict) -> FilterPlan:
    """Route the conjuncts of a boolean expression over ``block_vars``;
    ``visible`` maps the context-bound variables the expression may see to
    their terms, every other non-block variable is unbound."""
    block_set = set(block_vars)
    plan = FilterPlan([], {}, [], [])
    for conjunct_expr in _flatten_and(expr, []):
        expr = conjunct_expr
        if _is_impure(expr):
            raise NotImplementedError
        referenced = expr_vars(expr)
        variables = tuple(v for v in referenced if v in block_set)
        consts = {v: visible[v] for v in referenced if v in visible and v not in block_set}
        fast = None
        if _FAST_ENABLED:
            try:
                const_views = {v: view_of_node(term) for v, term in consts.items()}
            except _NotFast:
                const_views = None
            if const_views is not None:
                fast = compile_fast(expr, variables, const_views)
        conjunct = Conjunct(
            expr, variables, fast, fast is not None and is_kind_only(expr), consts, ctx
        )
        shape = exists_shape(expr)
        if shape is not None:
            plan.exists.append(ExistsConjunct(conjunct, *shape))
        elif not variables:
            plan.constant.append(conjunct)
        elif len(variables) == 1:
            plan.per_var.setdefault(variables[0], []).append(conjunct)
        else:
            plan.tuples.append(conjunct)
    return plan


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
    conjunct. Kind-only predicates never decode; the others decode the
    surviving codes once, in one batch, and parse their spellings."""
    remaining = set(codes)
    views: dict = {}
    bounds = None
    for conjunct in conjuncts:
        if not remaining:
            break
        fast = conjunct.fast
        passed: set = set()
        unknown: list = []
        if fast is None:
            unknown = list(remaining)
        elif conjunct.kind_only:
            if bounds is None:
                bounds = store._term_kind_bounds()
            for code in remaining:
                r = fast((TermView(kind_of(code, bounds), None),))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
        else:
            missing = [code for code in remaining if code not in views]
            if missing:
                for code, spelling in zip(missing, store._dict.decode_many(missing), strict=True):
                    if spelling is None:
                        raise ValueError(f"term code {code} is not in the store dictionary")
                    views[code] = parse_spelling(spelling)
            for code in remaining:
                r = fast((views[code],))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
        if unknown:
            var = conjunct.vars[0]
            for code in unknown:
                if conjunct.generic({var: store._decode_term(code)}):
                    passed.add(code)
        remaining = passed
    return remaining


def tuple_predicate(store, conjunct: Conjunct, positions: list) -> Callable[[tuple], bool]:
    """A row predicate for a conjunct over several variables, memoized per
    distinct code tuple; ``positions`` are the variables' row indexes."""
    memo: dict = {}
    views: dict = {}
    fast = conjunct.fast
    variables = conjunct.vars

    def view_of(code):
        view = views.get(code)
        if view is None:
            if code < 0:
                view = views[code] = view_of_node(store._foreign[code])
                return view
            spelling = store._dict.decode(code)
            if spelling is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            view = views[code] = parse_spelling(spelling)
        return view

    def predicate(row) -> bool:
        key = tuple(row[i] for i in positions)
        r = memo.get(key)
        if r is None:
            r = UNKNOWN
            if fast is not None:
                r = fast(tuple(None if c is None else view_of(c) for c in key))
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
