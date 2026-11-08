"""rdflib's query nodes and terms, read as rdflib answers them, without its
slow paths.

A parse, algebra or expression node (``CompValue``) is a dict of its fields.
Read as an attribute, rdflib answers with an attribute set on the node — it
sets a few, an algebra node's ``no_isolated_scope`` and an EXISTS's
translated graph among them — else the field resolved against the context of
an expression being evaluated: outside an evaluation, the stored value, after
a lookup of that context that misses and a ``KeyError`` caught. That is about
a microsecond a read, where :func:`field` takes a tenth of it.

rdflib's term classes test an instance through ``abc.ABCMeta``: an
``isinstance`` costs a few hundred nanoseconds for each class a node is not.
A node whose class is exactly one a query holds — a term, a node or a path
rdflib's parser builds, ``None``, a plain Python value — is told by its class
(:func:`term_class`, :func:`var_like`); any other, a term's subclass say, by
``isinstance``.
"""

from typing import Any

from pyparsing import ParseResults
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql.parserutils import CompValue, Expr
from rdflib.term import BNode, Literal, URIRef, Variable

#: The term classes a pattern or an expression holds.
TERM_CLASSES = (URIRef, Literal, BNode, Variable)
#: The term classes that act as variables in a pattern, as rdflib's
#: ``evalBGP`` treats a query's blank nodes.
VAR_LIKE = (Variable, BNode)

#: The term class of a node by its exact class, for the classes a query's
#: nodes have: a term class for itself, ``None`` for every other.
_EXACT: dict[type, type | None] = {cls: cls for cls in TERM_CLASSES}
_EXACT.update(
    dict.fromkeys(
        [
            *(type(None), CompValue, Expr, ParseResults),
            *(SequencePath, AlternativePath, InvPath, MulPath, NegatedPath),
            *(str, int, float, bool, list, tuple, dict),
        ]
    )
)


def field(node: CompValue, name: str) -> Any:
    """``node.<name>`` for a field of the ``CompValue`` ``node`` — not the
    name of one of its methods — as rdflib answers it: an attribute set on the
    node, else the field, ``None`` where the node has none; or, while rdflib
    evaluates the node, the field resolved as rdflib resolves it."""
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
    if cls in _EXACT:
        return _EXACT[cls]
    return next((term for term in TERM_CLASSES if isinstance(node, term)), None)


def var_like(node) -> bool:
    """Whether ``node`` is a variable or a blank node (:data:`VAR_LIKE`)."""
    cls = type(node)
    cls = _EXACT[cls] if cls in _EXACT else term_class(node)
    return cls is Variable or cls is BNode
