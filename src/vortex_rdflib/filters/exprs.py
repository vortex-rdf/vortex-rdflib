"""Walking FILTER expression trees: the variables an expression names, whether
it holds a MINUS or an EXISTS, an impure builtin, and the top-level
conjuncts of a ``&&`` chain. An EXISTS body is walked in its translated
form.
"""

from collections.abc import Iterator

from rdflib.plugins.sparql.parserutils import CompValue

from ..nodes import field, var_like
from ..terms import BLANK, IRI, LITERAL

_IMPURE = frozenset({"Builtin_RAND", "Builtin_UUID", "Builtin_STRUUID", "Builtin_BNODE"})


#: The builtins that test a term's kind, and the kind each is true for.
KIND_BUILTINS = {
    "Builtin_isIRI": IRI,
    "Builtin_isURI": IRI,
    "Builtin_isBLANK": BLANK,
    "Builtin_isLITERAL": LITERAL,
}


_EXISTS = ("Builtin_EXISTS", "Builtin_NOTEXISTS")


def _walk(node) -> Iterator[object]:
    """Every node reachable from ``node``, itself first, depth first in the
    order the tree holds them: through an expression's arguments (a
    ``CompValue``'s items but its ``_vars``), lists, tuples and a dict's
    keys (a VALUES row names its variables there). One generator walks the
    whole tree, off a stack, so a FILTER compiled per query pays no frame
    per node.

    An EXISTS body is walked in its translated form, the instance attribute
    rdflib keeps beside the parse tree under the key: rdflib pulls the
    body's FILTERs out of that parse tree, so only the translation still
    holds them."""
    stack = [node]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, CompValue):
            body = getattr(node, "graph", None) if node.name in _EXISTS else None
            if isinstance(body, CompValue):
                stack.append(body)
                continue
            stack.extend(
                dict.__getitem__(node, key) for key in reversed(node.keys()) if key != "_vars"
            )
        elif isinstance(node, dict):
            stack.extend(reversed(node))
        elif isinstance(node, (list, tuple)):
            stack.extend(reversed(node))


def _named(node, names) -> bool:
    return isinstance(node, CompValue) and node.name in names


def expr_vars(node) -> list:
    """The variable-like terms an expression references, in first-seen
    order, an EXISTS body's included."""
    return list(dict.fromkeys(n for n in _walk(node) if var_like(n)))


def has_minus(node) -> bool:
    """Whether an algebra tree or an expression holds a MINUS, one in an
    EXISTS body included."""
    return any(_named(n, ("Minus",)) for n in _walk(node))


def has_exists(node) -> bool:
    """Whether an expression holds an EXISTS or a NOT EXISTS."""
    return any(_named(n, _EXISTS) for n in _walk(node))


def is_impure(node) -> bool:
    """Whether an expression calls a builtin that draws anew on each call
    (``RAND``, ``UUID``, ``STRUUID``, ``BNODE``), one in an EXISTS body
    included."""
    return any(_named(n, _IMPURE) for n in _walk(node))


def flatten_and(expr, out: list) -> list:
    if (
        isinstance(expr, CompValue)
        and expr.name == "ConditionalAndExpression"
        and (other := field(expr, "other"))
    ):
        for x in [field(expr, "expr"), *other]:
            flatten_and(x, out)
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
        node = field(node, "expr")
    if isinstance(node, CompValue) and node.name in _EXISTS:
        body = getattr(node, "graph", None)
        if not isinstance(body, CompValue):
            return None
        return (negate != (node.name == "Builtin_NOTEXISTS"), body)
    return None
