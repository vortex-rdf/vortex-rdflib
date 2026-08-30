import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
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
def graph(vortex_files, request):
    return Graph(store=VortexRdflibStore(str(vortex_files[request.param])))


@pytest.fixture(params=LAYOUTS)
def quad_store(vortex_quad_files, request):
    return VortexRdflibStore(str(vortex_quad_files[request.param]))


def test_len(graph):
    assert len(graph) == 5


def test_triples_pattern(graph):
    names = list(graph.triples((None, FOAF_NAME, None)))
    assert len(names) == 3
    assert (ALICE, FOAF_NAME, Literal("Alice")) in names

    bob = set(graph.triples((BOB, None, None)))
    assert bob == {
        (BOB, FOAF_NAME, Literal("Bob", lang="en")),
        (BOB, URIRef("http://ex.org/age"), Literal(42)),
    }


def test_join_position_guard(graph):
    # A literal propagated into subject position is unsatisfiable, not an error.
    assert list(graph.triples((Literal("Alice"), None, None))) == []
    assert list(graph.triples((None, Literal("Alice"), None))) == []


def test_sparql_single_pattern(graph):
    query = "SELECT ?n WHERE { <http://ex.org/alice> <http://xmlns.com/foaf/0.1/name> ?n }"
    res = list(graph.query(query))
    assert res == [(Literal("Alice"),)]


def test_sparql_join(graph):
    res = list(
        graph.query(
            """SELECT ?n WHERE {
                <http://ex.org/alice> <http://xmlns.com/foaf/0.1/knows> ?x .
                ?x <http://xmlns.com/foaf/0.1/name> ?n
            }"""
        )
    )
    assert res == [(Literal("Bob", lang="en"),)]


def test_sparql_typed_literal(graph):
    res = list(graph.query("SELECT ?a WHERE { ?s <http://ex.org/age> ?a }"))
    assert len(res) == 1
    assert res[0][0].toPython() == 42


def test_literal_object_patterns(graph):
    # Bound-object lookups with language tags and datatypes go through the
    # native pattern parser and must not degrade to simple literals.
    assert list(graph.triples((None, None, Literal("Bob", lang="en")))) == [
        (BOB, FOAF_NAME, Literal("Bob", lang="en"))
    ]
    assert list(graph.triples((None, None, Literal("Bob")))) == []
    assert list(graph.triples((None, None, Literal(42)))) == [
        (BOB, URIRef("http://ex.org/age"), Literal(42))
    ]


def test_blank_node_subject(graph):
    rows = list(graph.triples((None, None, Literal("Anon"))))
    assert len(rows) == 1
    assert isinstance(rows[0][0], BNode)


def test_read_only(graph):
    with pytest.raises(NotImplementedError):
        graph.store.add((ALICE, FOAF_NAME, Literal("x")), None)
    with pytest.raises(NotImplementedError):
        graph.store.remove((None, None, None))
    with pytest.raises(NotImplementedError):
        graph.store.remove_graph(graph)


# ─── named graphs ───────────────────────────────────────────────────────────


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
    assert len(quad_store) == 6  # the union counts quads, not distinct triples


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
    assert len(whole) == 6
    # Alice's name is in two graphs, so the union yields it twice.
    assert len(list(whole.triples((ALICE, FOAF_NAME, None)))) == 2


def test_default_graph_is_not_a_named_graph(quad_store):
    dataset = Dataset(store=quad_store, default_union=True)
    named = {
        str(row.g) for row in dataset.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    }
    assert named == {GRAPH_1, GRAPH_2}
    # ...but the union default graph does include it.
    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 6


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
    assert len(list(legacy.triples((None, None, None)))) == 6


def test_add_graph_is_a_no_op(quad_store):
    # A Dataset calls it whenever it hands out a graph, so it must not raise —
    # and it must not invent a graph either.
    dataset = Dataset(store=quad_store)
    dataset.graph(URIRef("http://ex.org/g/new"))
    assert {c.identifier for c in quad_store.contexts()} == {G1, G2, DATASET_DEFAULT_GRAPH_ID}


def test_dictionary_graph_uses_code_path(vortex_files, monkeypatch):
    store = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert store._dict is not None  # code path active

    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    disabled = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert disabled._dict is None  # string fallback forced

    # Both paths yield identical triples.
    got_codes = sorted(Graph(store=store).triples((None, None, None)))
    got_strings = sorted(Graph(store=disabled).triples((None, None, None)))
    assert got_codes == got_strings and len(got_codes) == 5


def test_in_memory_graph_equality(vortex_files):
    on_file = Graph(store=VortexRdflibStore(str(vortex_files["dictionary"])))
    in_mem = Graph(store=VortexRdflibStore(str(vortex_files["dictionary"]), in_memory=True))
    assert len(in_mem) == len(on_file) == 5
    assert sorted(in_mem.triples((None, None, None))) == sorted(on_file.triples((None, None, None)))


def test_layout_alias_and_detection(vortex_files):
    # Branch-era labels are accepted; the detected layout wins.
    store = VortexRdflibStore(str(vortex_files["dictionary"]), layout="cottas-native-ids")
    assert store.layout == "dictionary"
    store = VortexRdflibStore(str(vortex_files["default"]), layout="cottas-native-strings")
    assert store.layout == "default"


def test_from_n3_safe_dbpedia_escaped_apostrophe():
    # Language-tagged literal with a non-canonical \' escape (DBpedia quirk).
    term = VortexRdflibStore._from_n3_safe('"L\\\'agent"@fr')
    assert term == Literal("L'agent", lang="fr")


def test_no_path_store():
    store = VortexRdflibStore()
    assert len(store) == 0
    assert list(store.triples((None, None, None))) == []
