"""A store over a quad file: its named graphs, the default graph, and the
union of every graph — a set of triples, each with the contexts of the
graphs that hold it."""

import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, ConjunctiveGraph

from conftest import GRAPH_1, GRAPH_2, LAYOUTS
from vortex_rdflib import VortexRdflibStore

FOAF_NAME = URIRef("http://xmlns.com/foaf/0.1/name")
FOAF_KNOWS = URIRef("http://xmlns.com/foaf/0.1/knows")
ALICE = URIRef("http://ex.org/alice")
BOB = URIRef("http://ex.org/bob")
G1 = URIRef(GRAPH_1)
G2 = URIRef(GRAPH_2)


@pytest.fixture(params=LAYOUTS)
def quad_store(vortex_quad_files, request):
    return VortexRdflibStore(str(vortex_quad_files[request.param]))


def test_contexts_are_the_files_graphs(quad_store):
    names = {context.identifier for context in quad_store.contexts()}
    assert names == {G1, G2, DATASET_DEFAULT_GRAPH_ID}


def test_contexts_of_a_triple(quad_store):
    # Alice's name is in both named graphs, her `knows` in only the first.
    assert {c.identifier for c in quad_store.contexts((ALICE, FOAF_NAME, Literal("Alice")))} == {
        G1,
        G2,
    }
    assert {c.identifier for c in quad_store.contexts((ALICE, FOAF_KNOWS, BOB))} == {G1}
    assert list(quad_store.contexts((ALICE, FOAF_NAME, Literal("nobody")))) == []


def test_len_per_graph(quad_store):
    dataset = Dataset(store=quad_store)
    assert len(dataset.graph(G1)) == 2
    assert len(dataset.graph(G2)) == 2
    assert len(dataset.default_graph) == 2
    assert len(quad_store) == 5  # the union counts distinct triples, as rdflib's Memory store


def test_len_of_an_absent_graph(quad_store):
    assert len(Dataset(store=quad_store).graph(URIRef("http://ex.org/g/absent"))) == 0


def test_quads_carry_their_graph(quad_store):
    quads = set(Dataset(store=quad_store).quads((None, None, None, None)))
    assert (ALICE, FOAF_NAME, Literal("Alice"), G1) in quads
    assert (ALICE, FOAF_NAME, Literal("Alice"), G2) in quads
    assert (ALICE, FOAF_KNOWS, BOB, G2) not in quads
    assert len(quads) == 6


def test_graph_restricts_the_match(quad_store):
    dataset = Dataset(store=quad_store)
    assert set(dataset.graph(G1).triples((None, None, None))) == {
        (ALICE, FOAF_NAME, Literal("Alice")),
        (ALICE, FOAF_KNOWS, BOB),
    }
    assert set(dataset.graph(G2).triples((ALICE, None, None))) == {
        (ALICE, FOAF_NAME, Literal("Alice"))
    }
    # A ground pattern is an existence check per graph, not over the union.
    assert list(dataset.graph(G2).triples((ALICE, FOAF_KNOWS, BOB))) == []
    assert list(dataset.graph(G1).triples((ALICE, FOAF_KNOWS, BOB))) == [(ALICE, FOAF_KNOWS, BOB)]


def test_bare_graph_is_the_union_view(quad_store):
    """rdflib names an identifier-less graph with a blank node, which names
    nothing the file holds; that is the whole-store view the README shows."""
    whole = Graph(store=quad_store)
    # Six quads, five triples: Alice's name is in two graphs, and the union,
    # a set of triples, holds it once.
    assert len(whole) == 5
    assert len(list(whole.triples((ALICE, FOAF_NAME, None)))) == 1


def test_ground_pattern_union_carries_each_graphs_context(quad_store):
    """A fully-ground pattern under the union yields the triple once, with
    the context of every graph holding it — served from the graph column
    alone, no term materialized."""
    rows = list(quad_store.triples((ALICE, FOAF_NAME, Literal("Alice"))))
    assert [triple for triple, _ in rows] == [(ALICE, FOAF_NAME, Literal("Alice"))]
    assert {ctx.identifier for ctx in rows[0][1]} == {G1, G2}
    assert list(quad_store.triples((ALICE, FOAF_NAME, Literal("nobody")))) == []


def test_union_match_in_one_graph_shares_one_context(quad_store):
    """A union match whose rows all live in one graph yields one shared
    context tuple (the single-distinct-graph loop), with that graph's name."""
    rows = list(quad_store.triples((ALICE, FOAF_KNOWS, None)))
    assert rows, "fixture holds alice-knows rows"
    contexts = {id(ctx) for _, ctx in rows}
    assert len(contexts) == 1
    assert {ctx[0].identifier for _, ctx in rows} == {G1}


def test_union_match_across_graphs_keeps_per_row_contexts(quad_store):
    """Each triple once, with the contexts of all the graphs holding it."""
    rows = list(quad_store.triples((None, FOAF_NAME, None)))
    assert len(rows) == len({triple for triple, _ in rows}) == 3
    by_graph: dict = {}
    for (s_, _, _), contexts in rows:
        for ctx in contexts:
            by_graph.setdefault(ctx.identifier, set()).add(s_)
    assert set(by_graph) >= {G1, G2}
    assert ALICE in by_graph[G1] and ALICE in by_graph[G2]


def test_default_graph_is_not_a_named_graph(quad_store):
    dataset = Dataset(store=quad_store, default_union=True)
    named = {
        str(row.g) for row in dataset.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    }
    assert named == {GRAPH_1, GRAPH_2}
    # ...but the union default graph does include it, each triple once.
    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 5


def test_dataset_without_union_sees_only_the_default_graph(quad_store):
    dataset = Dataset(store=quad_store)
    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 2


def test_union_view_is_keyed_on_default_union_not_the_class(quad_store):
    """A view whose default graph is the union of the others sees every graph,
    whatever class it is.

    rdflib's deprecated `ConjunctiveGraph` is the one such view that exists
    besides `Dataset`, and it is *always* a union — including when it carries
    an identifier of its own, which recognizing only `Dataset` would have
    read as a named graph.
    """

    class _UnionView:
        default_union = True
        identifier = URIRef("http://ex.org/g/1")

    assert quad_store._graph_n3(_UnionView()) is None

    with pytest.warns(DeprecationWarning):
        legacy = ConjunctiveGraph(store=quad_store, identifier=URIRef("http://ex.org/g/1"))
    assert quad_store._graph_n3(legacy) is None
    assert len(list(legacy.triples((None, None, None)))) == 5


def test_add_graph_is_a_no_op(quad_store):
    # A Dataset calls it whenever it hands out a graph, so it must not raise —
    # and it must not invent a graph either.
    dataset = Dataset(store=quad_store)
    dataset.graph(URIRef("http://ex.org/g/new"))
    assert {c.identifier for c in quad_store.contexts()} == {G1, G2, DATASET_DEFAULT_GRAPH_ID}
