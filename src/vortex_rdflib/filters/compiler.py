"""The fast route: a whitelist of expression shapes compiled to a predicate
over the term views of a conjunct's variables (:func:`compile_fast`), every
leaf mirroring rdflib's own code path, any value it cannot reproduce exactly
left ``UNKNOWN`` for rdflib's evaluator.
"""

import re
from collections.abc import Callable

from rdflib.plugins.sparql.operators import _lang_range_check
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.sparql import SPARQLError
from rdflib.term import BNode, Literal, URIRef

from ..terms import BLANK, IRI, LITERAL, TermView
from . import exprs, values
from .exprs import _VAR_LIKE
from .values import (
    _COMPILE_ERRORS,
    _INT_LIKE,
    _RDF_LANGSTRING,
    _RDF_NIL,
    _XSD_BOOLEAN,
    _XSD_DTS,
    _XSD_INTEGER,
    _XSD_STRING,
    ERROR,
    NUMERIC_TYPES,
    UNKNOWN,
)

_KIND_LEAVES = frozenset(
    {"Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL", "Builtin_BOUND"}
)


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
        and values.numeric_value(view) is None
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
    if not exprs.expr_vars(node):
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
        return lambda env: values._not(values._truth(inner(env)))
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
            r = values._truth(part(env))
            if r is not True:
                return r  # False, ERROR (rdflib raises there) or UNKNOWN
        return True

    return fn


def _or(parts):
    def fn(env):
        unknown = error = False
        for part in parts:
            r = values._truth(part(env))
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
    converted = values.numeric_value(value)
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
                r = values.term_eq(view, a)
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
        return values._value_eq(a, b)
    if op == "!=":
        return values._not(values._value_eq(a, b))
    if a.kind != LITERAL or b.kind != LITERAL:
        return ERROR
    if a.dt and a.dt not in _XSD_DTS and b.dt and b.dt not in _XSD_DTS:
        return ERROR
    gt = values._gt(a, b)
    if op == ">":
        return gt
    if gt is True:
        # __lt__ is `not gt and not eq`, __ge__ is `gt or eq`, __le__ is
        # `lt or eq`: a true gt settles all three without computing eq.
        return op == ">="
    if gt is not False:
        return gt  # ERROR or UNKNOWN
    eq = values._eq(a, b)
    if op == "<":
        return values._not(eq)
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
    if a.kind != LITERAL or values._string_like(a):
        return TermView(LITERAL, a.lex)
    # Other datatypes carry rdflib's normalized lexical form ("042" -> "42").
    return UNKNOWN


def _string_arg(a):
    """rdflib's ``string()`` on an evaluated value: the view, or ERROR."""
    if a is UNKNOWN:
        return UNKNOWN
    if values._string_like(a):
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
    return values.term_eq(a, b)


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


def _compile_regex(node, slots, consts):
    pattern, flags = node.pattern, node.flags
    if not isinstance(pattern, Literal) or (flags is not None and not isinstance(flags, Literal)):
        raise _NotFast
    if not values._string_like(view_of_node(pattern)) or (
        flags is not None and not values._string_like(view_of_node(flags))
    ):
        raise _NotFast
    _, compile_flags = values._regex_flags(flags)
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
        r = values._truth(node(env))
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
