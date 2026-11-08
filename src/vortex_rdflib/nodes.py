"""rdflib's query nodes and terms, read as rdflib answers them, without its
slow paths.

A parse, algebra or expression node (``CompValue``) is a dict of its fields.
Read as an attribute, rdflib answers with an attribute set on the node, else
the field resolved against the context of an expression being evaluated —
outside an evaluation, the stored value, after a lookup of that context that
misses and a ``KeyError`` caught: about a microsecond a read, where
:func:`field` takes a tenth of it.

rdflib's term classes test an instance through ``abc.ABCMeta``: an
``isinstance`` costs a few hundred nanoseconds for each class a term is not.
A node of exactly one of the classes a query holds — every term and node
rdflib's parser builds — is told by its class (:func:`term_class`,
:func:`var_like`); any other, by ``isinstance``.
"""

from typing import Any

from rdflib.plugins.sparql.parserutils import CompValue, Expr
from rdflib.term import BNode, Literal, URIRef, Variable

#: The term classes a pattern or an expression holds.
TERM_CLASSES = (URIRef, Literal, BNode, Variable)
#: The term classes that act as variables in a pattern, as rdflib's
#: ``evalBGP`` treats a query's blank nodes.
VAR_LIKE = (Variable, BNode)

#: The other classes a query's nodes have, exactly: none is a term class.
_NOT_TERMS = frozenset([CompValue, Expr, str, int, float, bool, list, tuple, dict])
#: Whether a node of one of the classes a query holds, exactly, is
#: variable-like.
_VAR_LIKE_CLASS: dict[type, bool] = {cls: cls in VAR_LIKE for cls in TERM_CLASSES}
_VAR_LIKE_CLASS.update(dict.fromkeys(_NOT_TERMS, False))


def field(node: CompValue, name: str) -> Any:
    """``node.<name>`` as rdflib answers it: an attribute set on the node
    (rdflib sets a few on parse nodes, an EXISTS's translated graph among
    them), else the field, ``None`` where the node has none — or, while
    rdflib evaluates the node, the field resolved as rdflib resolves it."""
    attrs = node.__dict__
    if name in attrs:
        return attrs[name]
    if attrs.get("ctx") is not None:
        return getattr(node, name)
    return dict.get(node, name)


def term_class(node) -> type | None:
    """The term class of :data:`TERM_CLASSES` ``node`` is an instance of, or
    ``None``."""
    cls = type(node)
    if cls in _VAR_LIKE_CLASS:
        return None if cls in _NOT_TERMS else cls
    return next((term for term in TERM_CLASSES if isinstance(node, term)), None)


def var_like(node) -> bool:
    """Whether ``node`` is a variable or a blank node (:data:`VAR_LIKE`)."""
    known = _VAR_LIKE_CLASS.get(type(node))
    return isinstance(node, VAR_LIKE) if known is None else known
