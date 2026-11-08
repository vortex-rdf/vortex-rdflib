"""``vortex_rdflib.nodes``: rdflib's query nodes and terms told apart as
``isinstance`` would, and a node's field read as rdflib answers it, without
rdflib's slow paths: no ``abc`` check for a node of a class a query holds,
no class lookup at all for a term the spellers turn into text."""

import abc

import pytest
from pyparsing import ParseResults
from rdflib import BNode, Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath, ZeroOrMore
from rdflib.plugins.sparql.parserutils import CompValue, Expr
from rdflib.term import Genid

from vortex_rdflib import nodes, terms

P = URIRef("http://ex.org/p")
TERMS = [P, Literal("x"), BNode("b"), Variable("v")]
#: The other classes a query's nodes have: none is a term.
NON_TERMS = [
    None,
    ParseResults(),
    SequencePath(P, P),
    AlternativePath(P, P),
    InvPath(P),
    MulPath(P, ZeroOrMore),
    NegatedPath(P),
    CompValue("BGP"),
    Expr("Builtin_STR"),
    "*",
    1,
    1.5,
    True,
    [],
    (),
    {},
]


class _Iri(URIRef):
    pass


class _Var(Variable):
    pass


@pytest.mark.parametrize(
    "node", [*TERMS, *NON_TERMS, Genid("http://ex.org/.well-known/genid/x"), _Iri("a"), _Var("w")]
)
def test_term_class_and_var_like_answer_as_isinstance(node):
    expected = next((cls for cls in nodes.TERM_CLASSES if isinstance(node, cls)), None)
    assert nodes.term_class(node) is expected
    assert nodes.var_like(node) is isinstance(node, nodes.VAR_LIKE)


def test_a_query_node_of_a_known_class_is_told_without_abc(monkeypatch):
    checks: list = []
    test = abc.ABCMeta.__instancecheck__
    monkeypatch.setattr(
        abc.ABCMeta,
        "__instancecheck__",
        lambda cls, instance: (
            checks.append((cls.__name__, type(instance).__name__)) or test(cls, instance)
        ),
    )
    for node in [*TERMS, *NON_TERMS]:
        nodes.term_class(node)
        nodes.var_like(node)
    assert checks == []


def test_an_exact_term_is_spelled_without_a_class_lookup(monkeypatch):
    def unused(node):
        raise AssertionError(f"a class lookup for {node!r}")

    monkeypatch.setattr(terms, "term_class", unused)
    assert terms.canonical_spelling(P) == "<http://ex.org/p>"
    assert terms.canonical_spelling(BNode("b")) == "_:b"
    assert terms.canonical_spelling(Literal("x", lang="EN")) == '"x"@en'
    assert terms.view_of(P) == terms.TermView(terms.IRI, "http://ex.org/p")
    assert terms.view_of(Literal("x")) == terms.TermView(terms.LITERAL, "x")
    assert terms.unparseable_iri(URIRef("relative")) is True
    assert terms.unparseable_iri(P) is False
    assert terms.unparseable_iri(Literal("relative")) is False


def test_a_term_of_a_subclass_is_spelled_as_its_class():
    assert terms.canonical_spelling(_Iri("http://ex.org/a")) == "<http://ex.org/a>"
    assert terms.view_of(_Iri("http://ex.org/a")) == terms.TermView(terms.IRI, "http://ex.org/a")
    assert terms.unparseable_iri(_Iri("relative")) is True
    with pytest.raises(TypeError):
        terms.canonical_spelling(Variable("v"))


def test_a_field_is_read_as_rdflib_reads_it():
    """An attribute rdflib set on the node comes before the field it
    shadows (an EXISTS keeps its translated graph so); a missing field is
    ``None``."""
    node = CompValue("Builtin_EXISTS", graph="parsed")
    assert nodes.field(node, "graph") == "parsed" == node.graph
    node.graph = "translated"
    assert nodes.field(node, "graph") == "translated" == node.graph
    assert nodes.field(node, "absent") is None is node.absent
