"""The fast route: a whitelist of expression shapes compiled to a predicate
over the term views of a conjunct's variables (:func:`compile_fast`). Every
leaf answers as SPARQL 1.1 does (:mod:`.values`); a comparison the
specification leaves to the implementation (values of an XML Schema type it
names no operator for) is left ``UNKNOWN`` for rdflib's evaluator.
"""

from collections.abc import Callable
from functools import partial

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Literal, URIRef

from ..terms import BLANK, IRI, LITERAL, TermView, view_of
from . import exprs, values
from .exprs import VAR_LIKE
from .values import (
    ERROR,
    RDF_LANGSTRING,
    RDF_NIL,
    UNKNOWN,
    XSD_BOOLEAN,
    XSD_STRING,
)

_KIND_LEAVES = frozenset({*exprs.KIND_BUILTINS, "Builtin_BOUND"})

_TRUE = TermView(LITERAL, "true", dt=XSD_BOOLEAN)
_FALSE = TermView(LITERAL, "false", dt=XSD_BOOLEAN)


class _NotFast(Exception):
    """The expression is outside the whitelist; the conjunct takes the
    generic route for every value."""


def _const(value):
    return lambda env: value


def _as_term(value):
    """An operand as a function reads it — the operand protocol: an unbound
    operand or an error is ``ERROR``, a comparison's bool the
    ``xsd:boolean`` term it is, and a term view or ``UNKNOWN`` itself."""
    if value is None or value is ERROR:
        return ERROR
    if value is True:
        return _TRUE
    if value is False:
        return _FALSE
    return value


def _strict(fn, arg):
    """``fn``, a function of one term view, over the operand ``arg``
    computes (:func:`_as_term`): an error is an error, and an operand this
    route cannot evaluate leaves the call to rdflib's evaluator."""

    def apply(env):
        a = _as_term(arg(env))
        return a if a is ERROR or a is UNKNOWN else fn(a)

    return apply


def _strict2(fn, left, right):
    """``fn``, a function of two term views, over two operands: an error in
    either is an error (a function of an error is one, §17.3), else an
    operand this route cannot evaluate leaves the call to rdflib's
    evaluator."""

    def apply(env):
        a, b = left(env), right(env)
        if a is None or a is ERROR or b is None or b is ERROR:
            return ERROR
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        return fn(_as_term(a), _as_term(b))

    return apply


def _view(term):
    """A context-bound term's view; a value no view holds leaves the
    conjunct to rdflib's evaluator."""
    try:
        return view_of(term)
    except TypeError as error:
        raise _NotFast from error


def _folded(node, consts: dict):
    """A subexpression without block variables the whitelist leaves
    undecided, evaluated once by rdflib (:func:`values.fold`); one rdflib
    cannot evaluate leaves the conjunct to its evaluator."""
    value = values.fold(node, consts)
    if value is None:
        raise _NotFast
    return value if value is ERROR else view_of(value)


def _compile(node, slots: dict, consts: dict):
    """Compile an expression node to ``env -> value`` where ``env`` is the
    tuple of term views of the conjunct's block variables (``None`` for an
    unbound variable) and the value is a term view, a bool, ``None``
    (unbound), ``ERROR`` or ``UNKNOWN``. ``consts`` maps the context-bound
    variables to rdflib terms. A subexpression that reads no block variable
    is evaluated once, here: by the whitelist, else by rdflib."""
    if isinstance(node, VAR_LIKE):
        if node in slots:
            index = slots[node]
            return lambda env: env[index]
        term = consts.get(node)
        return _const(None if term is None else _view(term))
    if isinstance(node, (Literal, URIRef)):
        return _const(view_of(node))
    if not isinstance(node, CompValue):
        raise _NotFast
    if not any(v in slots for v in exprs.expr_vars(node)):
        try:
            value = _compile_node(node, slots, consts)(())
        except _NotFast:
            value = UNKNOWN
        return _const(_folded(node, consts) if value is UNKNOWN else value)
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
    if name == "Builtin_BOUND":
        if not isinstance(node.arg, VAR_LIKE):
            raise _NotFast
        arg = _compile(node.arg, slots, consts)
        return lambda env: _bound(arg(env))
    one_arg = _ONE_ARG.get(name)
    if one_arg is not None:
        return _strict(one_arg, _compile(node.arg, slots, consts))
    two_args = _TWO_ARGS.get(name)
    if two_args is not None:
        a, b = _compile(node.arg1, slots, consts), _compile(node.arg2, slots, consts)
        return _strict2(two_args, a, b)
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
        total = _number(first(env))
        if total is ERROR or total is UNKNOWN:
            return total
        for subtract, operand in signed:
            value = _number(operand(env))
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
            others = [] if others == RDF_NIL else [others]
        items = [_compile(x, slots, consts) for x in others]
        negate = op == "NOT IN"

        def fn_in(env):
            # A IN (B, ...) is A = B || ...: true when one comparison holds,
            # an error when none does and one errs (§17.4.1.9); NOT IN is its
            # negation, an error included.
            a = _as_term(left(env))
            if a is ERROR or a is UNKNOWN:
                return a
            pending = None
            for item in items:
                b = _as_term(item(env))
                r = b if b is ERROR or b is UNKNOWN else values.value_eq(a, b)
                if r is True:
                    return not negate
                if r is not False and pending is not UNKNOWN:
                    pending = r
            return negate if pending is None else pending

        return fn_in
    if op not in ("=", "!=", "<", ">", "<=", ">="):
        raise _NotFast
    right = _compile(node.other, slots, consts)
    return _strict2(partial(values.compare, op), left, right)


def _is_kind(kind: str):
    return lambda a: a.kind == kind


def _is_numeric(a):
    return values.numeric_value(a) is not None


def _bound(a):
    """``BOUND``, the one function of an unbound operand."""
    if a is UNKNOWN:
        return UNKNOWN
    return a is not None


def _datatype(a):
    if a.kind != LITERAL:
        return ERROR
    if a.lang:
        return TermView(IRI, RDF_LANGSTRING)
    return TermView(IRI, a.dt or XSD_STRING)


def _lang(a):
    if a.kind != LITERAL:
        return ERROR
    return TermView(LITERAL, a.lang or "")


def _str(a):
    """``STR``: an IRI's string or a literal's lexical form as written; a
    blank node has none (§17.4.2.5)."""
    if a.kind == BLANK:
        return ERROR
    return TermView(LITERAL, a.lex)


def _number(value):
    """An operand of ``+`` or ``-`` as the ``(rank, value)`` it contributes:
    only a well-typed numeric literal is a number (§17.3), so any other
    value — unbound, an error, a comparison's bool — is ``ERROR``; one this
    route cannot evaluate stays ``UNKNOWN``."""
    if value is UNKNOWN:
        return UNKNOWN
    operand = values.numeric_operand(value) if isinstance(value, TermView) else None
    return ERROR if operand is None else operand


def _lang_matches(tag, range_):
    """``langMatches``: RFC 4647 basic filtering (§17.4.3.13), over two
    simple literals. ``*`` matches any non-empty tag; another range matches a
    tag equal to it or starting with it and a hyphen, case-insensitively."""
    if not (values.simple_literal(tag) and values.simple_literal(range_)):
        return ERROR
    text, wanted = tag.lex.lower(), range_.lex.lower()
    if text == "":
        return False
    if wanted == "*":
        return True
    return text == wanted or text.startswith(wanted + "-")


def _str_op(op: Callable[[str, str], bool]):
    """A string function of two string literals (§17.4.3), the second's
    language tag, if any, the first's (argument compatibility,
    §17.4.3.1.2)."""

    def fn(a, b):
        if not (values.string_like(a) and values.string_like(b)):
            return ERROR
        if b.lang and a.lang != b.lang:
            return ERROR
        return op(a.lex, b.lex)

    return fn


_ONE_ARG = {
    **{name: _is_kind(kind) for name, kind in exprs.KIND_BUILTINS.items()},
    "Builtin_isNUMERIC": _is_numeric,
    "Builtin_DATATYPE": _datatype,
    "Builtin_LANG": _lang,
    "Builtin_STR": _str,
}


_TWO_ARGS = {
    "Builtin_LANGMATCHES": _lang_matches,
    "Builtin_STRSTARTS": _str_op(str.startswith),
    "Builtin_STRENDS": _str_op(str.endswith),
    "Builtin_CONTAINS": _str_op(lambda a, b: b in a),
    "Builtin_sameTerm": values.same_term,
}


def _compile_regex(node, slots, consts):
    """``REGEX`` with a pattern and flags that read no block variable, read
    as XPath's ``fn:matches`` (:func:`.values.regex_spec`): a pattern or
    flags every call errs on make it an error; a pattern ``re`` cannot
    compile, or one read per row, leaves the conjunct to rdflib's
    evaluator."""
    spec = values.regex_spec(node, consts)
    if spec is None:
        raise _NotFast
    if spec is ERROR:
        return _const(ERROR)
    text = _compile(node.text, slots, consts)
    search = spec.regex.search

    def fn(env):
        # Only a string literal has a text (§17.4.3.14): any other value —
        # unbound, an error, a comparison's bool — is an error.
        a = text(env)
        if a is UNKNOWN:
            return UNKNOWN
        return search(a.lex) is not None if values.string_like(a) else ERROR

    return fn


def compile_fast(expr, variables: tuple, consts: dict) -> Callable | None:
    """Compile ``expr`` into a predicate ``env -> True | False | UNKNOWN``
    over the term views of ``variables`` (in order), or ``None`` when the
    expression is outside the whitelist. ``consts`` maps the visible
    ctx-bound variables to their rdflib terms; every other variable is
    unbound."""
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
    if isinstance(expr, VAR_LIKE):
        return False  # a bare variable is evaluated as a term
    if not isinstance(expr, CompValue):
        return True
    name = expr.name
    if name in _KIND_LEAVES:
        return isinstance(expr.arg, VAR_LIKE)
    if name in ("ConditionalAndExpression", "ConditionalOrExpression"):
        return expr.other is not None and all(is_kind_only(x) for x in [expr.expr, *expr.other])
    if name == "UnaryNot":
        return is_kind_only(expr.expr)
    return False
