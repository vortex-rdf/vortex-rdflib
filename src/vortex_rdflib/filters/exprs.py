"""Walking FILTER expression trees: the variables an expression names, whether
it holds a MINUS or an EXISTS, an impure builtin, and the top-level
conjuncts of a ``&&`` chain. An EXISTS body is walked in its translated
form.
"""

from collections.abc import Iterator

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import BNode, Variable

_IMPURE = frozenset({"Builtin_RAND", "Builtin_UUID", "Builtin_STRUUID", "Builtin_BNODE"})


_VAR_LIKE = (Variable, BNode)


_EXISTS = ("Builtin_EXISTS", "Builtin_NOTEXISTS")


def _walk(node) -> Iterator[object]:
    """Every node reachable from ``node``, itself first: through an
    expression's arguments (a ``CompValue``'s items but its ``_vars``),
    lists, tuples and a dict's keys (a VALUES row names its variables
    there).

    An EXISTS body is walked in its translated form, the instance attribute
    rdflib keeps beside the parse tree under the key: rdflib pulls the
    body's FILTERs out of that parse tree, so only the translation still
    holds them."""
    yield node
    if isinstance(node, CompValue):
        body = getattr(node, "graph", None) if node.name in _EXISTS else None
        if isinstance(body, CompValue):
            yield from _walk(body)
            return
        for key in node.keys():
            if key != "_vars":
                yield from _walk(dict.__getitem__(node, key))
    elif isinstance(node, dict):
        for key in node:
            yield from _walk(key)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _named(node, names) -> bool:
    return isinstance(node, CompValue) and node.name in names


def expr_vars(node) -> list:
    """The variable-like terms an expression references, in first-seen
    order, an EXISTS body's included."""
    return list(dict.fromkeys(n for n in _walk(node) if isinstance(n, _VAR_LIKE)))


def has_minus(node) -> bool:
    """Whether an algebra tree or an expression holds a MINUS, one in an
    EXISTS body included."""
    return any(_named(n, ("Minus",)) for n in _walk(node))


def has_exists(node) -> bool:
    """Whether an expression holds an EXISTS or a NOT EXISTS."""
    return any(_named(n, _EXISTS) for n in _walk(node))


def _is_impure(node) -> bool:
    """Whether an expression calls a builtin that draws anew on each call
    (``RAND``, ``UUID``, ``STRUUID``, ``BNODE``), one in an EXISTS body
    included."""
    return any(_named(n, _IMPURE) for n in _walk(node))


def _flatten_and(expr, out: list) -> list:
    if isinstance(expr, CompValue) and expr.name == "ConditionalAndExpression" and expr.other:
        for x in [expr.expr, *expr.other]:
            _flatten_and(x, out)
    else:
        out.append(expr)
    return out


def exists_shape(expr):
    """``(negate, body)`` when a conjunct is ``EXISTS {..}``, ``NOT EXISTS
    {..}`` or either under one ``!``; ``None`` otherwise. ``body`` is the
    translated group, which rdflib keeps as an instance attribute."""
    negate = False
    node = expr
    if isinstance(node, CompValue) and node.name == "UnaryNot":
        negate = True
        node = node.expr
    if isinstance(node, CompValue) and node.name in _EXISTS:
        body = getattr(node, "graph", None)
        if not isinstance(body, CompValue):
            return None
        return (negate != (node.name == "Builtin_NOTEXISTS"), body)
    return None
