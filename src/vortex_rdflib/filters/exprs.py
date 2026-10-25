"""Walking FILTER expression trees: the variables an expression names, whether
it holds a MINUS or an EXISTS, an impure builtin, and the top-level
conjuncts of a ``&&`` chain. An EXISTS body is walked in its translated
form.
"""

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import BNode, Variable

_IMPURE = frozenset({"Builtin_RAND", "Builtin_UUID", "Builtin_STRUUID", "Builtin_BNODE"})


_VAR_LIKE = (Variable, BNode)


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
