"""The fast route: a whitelist of expression shapes compiled to a predicate
over the term views of a conjunct's variables (:func:`compile_fast`). Every
leaf answers as SPARQL 1.1 does (:mod:`.values`); a comparison the
specification leaves to the implementation (values of an XML Schema type it
names no operator for) is left ``UNKNOWN`` for rdflib's evaluator.
"""

from collections.abc import Callable

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.sparql import SPARQLError
from rdflib.term import Literal, URIRef

from ..terms import BLANK, IRI, LITERAL, TermView
from . import exprs, values
from .exprs import _VAR_LIKE
from .values import (
    _RDF_LANGSTRING,
    _RDF_NIL,
    _XSD_BOOLEAN,
    _XSD_STRING,
    ERROR,
    UNKNOWN,
    constant_view,
)

_KIND_LEAVES = frozenset(
    {"Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL", "Builtin_BOUND"}
)

_TRUE = TermView(LITERAL, "true", dt=_XSD_BOOLEAN)
_FALSE = TermView(LITERAL, "false", dt=_XSD_BOOLEAN)


class _NotFast(Exception):
    """The expression is outside the whitelist; the conjunct takes the
    generic route for every value."""


def _const(value):
    return lambda env: value


def _as_term(value):
    """A comparison's value as the ``xsd:boolean`` term it is, any other
    value unchanged."""
    if value is True:
        return _TRUE
    if value is False:
        return _FALSE
    return value


def _rdflib_fold(node):
    """A variable-free subexpression the whitelist does not cover, evaluated
    once by rdflib (the generic route's semantics); an error stays one."""
    try:
        result = node.eval({})
    except Exception as error:
        raise _NotFast from error
    if isinstance(result, (Literal, URIRef)):
        return constant_view(result)
    if isinstance(result, SPARQLError):
        return ERROR
    raise _NotFast


def _compile(node, slots: dict, consts: dict):
    """Compile an expression node to ``env -> value`` where ``env`` is the
    tuple of term views of the conjunct's block variables (``None`` for an
    unbound variable) and the value is a term view, a bool, ``None``
    (unbound), ``ERROR`` or ``UNKNOWN``. A variable-free subexpression is
    evaluated once, here."""
    if isinstance(node, _VAR_LIKE):
        if node in slots:
            index = slots[node]
            return lambda env: env[index]
        return _const(consts.get(node))
    if isinstance(node, (Literal, URIRef)):
        return _const(constant_view(node))
    if not isinstance(node, CompValue):
        raise _NotFast
    if not exprs.expr_vars(node):
        try:
            value = _compile_node(node, slots, consts)(())
        except _NotFast:
            value = UNKNOWN
        return _const(_rdflib_fold(node) if value is UNKNOWN else value)
    return _compile_node(node, slots, consts)


def _compile_node(node, slots: dict, consts: dict):
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
        return lambda env: values.logical_not(values.truth(inner(env)))
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
    """``&&`` (§17.2): false when any operand is false, else an error when
    any errs, else true."""

    def fn(env):
        pending = None
        for part in parts:
            r = values.truth(part(env))
            if r is False:
                return False
            if r is not True and pending is not UNKNOWN:
                pending = r
        return True if pending is None else pending

    return fn


def _or(parts):
    """``||`` (§17.2): true when any operand is true, else an error when any
    errs, else false."""

    def fn(env):
        pending = None
        for part in parts:
            r = values.truth(part(env))
            if r is True:
                return True
            if r is not False and pending is not UNKNOWN:
                pending = r
        return False if pending is None else pending

    return fn


def _compile_additive(node, slots, consts):
    """Compile ``+`` and ``-`` (§17.3): numbers promoted to the wider of
    their types (:func:`values.add`), anything else a type error."""
    if node.other is None:
        # The add-expr production wraps a bare operand; rdflib's own
        # evaluator returns `expr` unchanged, so compile straight through.
        return _compile(node.expr, slots, consts)
    others = node.other if isinstance(node.other, list) else [node.other]
    operators = node.op if isinstance(node.op, list) else [node.op]
    if len(others) != len(operators) or any(op not in ("+", "-") for op in operators):
        raise _NotFast
    first = _compile(node.expr, slots, consts)
    signed = [
        (op == "-", _compile(part, slots, consts))
        for op, part in zip(operators, others, strict=True)
    ]

    def fn(env):
        total = values.number(first(env))
        if total is ERROR or total is UNKNOWN:
            return total
        for subtract, operand in signed:
            value = values.number(operand(env))
            if value is ERROR or value is UNKNOWN:
                return value
            total = values.add(total, value, subtract)
        return values.number_term(total)

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
        items = [_compile(x, slots, consts) for x in others]
        negate = op == "NOT IN"

        def fn_in(env):
            # A IN (B, ...) is A = B || ...: true when one comparison holds,
            # an error when none does and one errs (§17.4.1.9); NOT IN is its
            # negation, an error included.
            a = left(env)
            if a is None or a is ERROR:
                return ERROR
            if a is UNKNOWN:
                return UNKNOWN
            a = _as_term(a)
            pending = None
            for item in items:
                b = item(env)
                if b is None or b is ERROR:
                    r = ERROR
                elif b is UNKNOWN:
                    r = UNKNOWN
                else:
                    r = values.value_eq(a, _as_term(b))
                if r is True:
                    return not negate
                if r is not False and pending is not UNKNOWN:
                    pending = r
            return negate if pending is None else pending

        return fn_in
    if op not in ("=", "!=", "<", ">", "<=", ">="):
        raise _NotFast
    right = _compile(node.other, slots, consts)

    def fn(env):
        a, b = left(env), right(env)
        if a is None or b is None or a is ERROR or b is ERROR:
            return ERROR
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        return values.compare(op, _as_term(a), _as_term(b))

    return fn


def _is_kind(kind: str):
    def fn(a):
        if a is None or a is ERROR:
            return ERROR
        if a is UNKNOWN:
            return UNKNOWN
        return _as_term(a).kind == kind

    return fn


def _is_numeric(a):
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    return values.numeric_value(_as_term(a)) is not None


def _bound(a):
    if a is UNKNOWN:
        return UNKNOWN
    return a is not None


def _datatype(a):
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    a = _as_term(a)
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
    a = _as_term(a)
    if a.kind != LITERAL:
        return ERROR
    return TermView(LITERAL, a.lang or "")


def _str(a):
    """``STR``: an IRI's string or a literal's lexical form as written; a
    blank node has none (§17.4.2.5)."""
    if a is None or a is ERROR:
        return ERROR
    if a is UNKNOWN:
        return UNKNOWN
    a = _as_term(a)
    if a.kind == BLANK:
        return ERROR
    return TermView(LITERAL, a.lex)


def _string_arg(a):
    """A string function's argument: a simple, ``xsd:string`` or
    language-tagged literal, or ``ERROR``."""
    if a is UNKNOWN:
        return UNKNOWN
    if values._string_like(a):
        return a
    return ERROR


def _simple_arg(a):
    """A simple literal (or ``xsd:string``) argument, or ``ERROR``."""
    a = _string_arg(a)
    if isinstance(a, TermView) and a.lang:
        return ERROR
    return a


def _lang_matches(tag, range_):
    """``langMatches``: RFC 4647 basic filtering (§17.4.3.13). ``*`` matches
    any non-empty tag; another range matches a tag equal to it or starting
    with it and a hyphen, case-insensitively."""
    tag, range_ = _simple_arg(tag), _simple_arg(range_)
    if tag is UNKNOWN or range_ is UNKNOWN:
        return UNKNOWN
    if tag is ERROR or range_ is ERROR:
        return ERROR
    text, wanted = tag.lex.lower(), range_.lex.lower()
    if text == "":
        return False
    if wanted == "*":
        return True
    return text == wanted or text.startswith(wanted + "-")


def _str_op(op: Callable[[str, str], bool]):
    def fn(a, b):
        a, b = _string_arg(a), _string_arg(b)
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        if a is ERROR or b is ERROR:
            return ERROR
        if b.lang and a.lang != b.lang:
            return ERROR  # argument compatibility (§17.4.3.1.2)
        return op(a.lex, b.lex)

    return fn


def _same_term(a, b):
    if a is None or b is None or a is ERROR or b is ERROR:
        return ERROR
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    return values.same_term(_as_term(a), _as_term(b))


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
    """``REGEX`` with a constant pattern and flags, read as XPath's
    ``fn:matches`` (:func:`.values.compile_regex`). A pattern or flags that
    are not simple literals, invalid flags or an invalid pattern make every
    call an error; a pattern ``re`` cannot compile leaves the conjunct to
    rdflib's evaluator, which raises where it reads one."""
    pattern, flags = node.pattern, node.flags
    if not isinstance(pattern, Literal) or (flags is not None and not isinstance(flags, Literal)):
        raise _NotFast
    text = _compile(node.text, slots, consts)
    pattern_view = constant_view(pattern)
    flags_view = None if flags is None else constant_view(flags)
    if _simple_arg(pattern_view) is ERROR or (
        flags_view is not None and _simple_arg(flags_view) is ERROR
    ):
        return _const(ERROR)
    try:
        regex = values.compile_regex(str(pattern), None if flags is None else str(flags))
    except values.COMPILE_ERRORS as error:
        raise _NotFast from error
    if regex is ERROR:
        return _const(ERROR)

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
        r = values.truth(node(env))
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
