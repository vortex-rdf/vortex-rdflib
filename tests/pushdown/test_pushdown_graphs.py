"""The pushdown over named graphs: a GRAPH's scope reaches the native
match, the graph column binds a graph variable, and a graph name or a term
the parser rejects matches nothing."""

import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from tests.corpus import (
    FIXTURE_NQ,
    FIXTURE_NT,
)
from tests.support import (
    run,
)

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def test_solutions_do_not_share_a_context_under_a_pushed_graph(dataset, monkeypatch):
    """rdflib's ``evalGraph`` writes ``solution.ctx.graph`` back to the graph
    it replaced as it yields. Here it evaluates the ``GRAPH`` and the
    ``OPTIONAL`` itself over rows this module produced, so one context shared
    across those rows would carry that write forward and send the OPTIONAL's
    right side to the union instead of the row's graph."""
    monkeypatch.setattr(SETTINGS, "enabled_ops", frozenset({"BGP"}))
    active = []
    original = pd.algebra._active_scope
    monkeypatch.setattr(
        pd.algebra,
        "_active_scope",
        lambda ctx, store: active.append(ctx.graph) or original(ctx, store),
    )
    register_sparql_pushdown()
    rows = run(
        dataset,
        """SELECT ?g ?s ?n ?a WHERE { GRAPH ?g {
            ?s <http://ex.org/name> ?n OPTIONAL { ?s <http://ex.org/age> ?a } } }""",
    )
    assert rows
    assert active, "the hook was never reached"
    # Every match belongs to the graph rdflib pushed, never to the dataset it
    # was pushed over.
    assert all(graph is not dataset for graph in active)


def test_graph_variable_is_bound_from_the_column(dataset):
    """`GRAPH ?g` is one match with the graph as a schema variable, not one
    match per graph: the store's five graphs cost a single `match_codes`."""
    matches = []
    inner = dataset.store._opened().native

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    dataset.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    dataset.store._opened().native = _Counting()
    register_sparql_pushdown()
    try:
        rows = run(dataset, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    finally:
        dataset.store._opened().native = inner
    # Four named graphs, the default graph excluded, from one wildcard match.
    assert len(rows) == 4
    assert len(matches) == 1
    assert matches[0][3] is None


def test_graph_scope_reaches_the_native_match(dataset, monkeypatch):
    """The graph is the pattern's fourth position, not a filter over the
    union: an absent graph matches nothing and never materializes a row."""
    patterns = []
    original = pd.matching._pattern_terms

    def spy(*args, **kwargs):
        pattern = original(*args, **kwargs)
        patterns.append(pattern.n3[3])
        return pattern

    monkeypatch.setattr(pd.matching, "_pattern_terms", spy)
    register_sparql_pushdown()
    assert run(dataset, "SELECT * WHERE { GRAPH <http://ex.org/g/1> { ?s ?p ?o } }")
    assert run(dataset, "SELECT * WHERE { GRAPH <http://ex.org/absent> { ?s ?p ?o } }") == []
    assert run(dataset, "SELECT * WHERE { ?s ?p ?o }")
    assert patterns == ["<http://ex.org/g/1>", "<http://ex.org/absent>", None]


#: Graph names vortex-rdf's parser rejects, each next to a graph the file
#: holds: a literal ``?g`` is bound to (rdflib's ``GRAPH`` asks for the
#: context ``URIRef("zz")``), and a relative IRI, bound or written (no BASE).
REJECTED_GRAPH_NAMES = {
    "literal": """SELECT ?s WHERE {
        VALUES ?g { "zz" <http://ex.org/g/1> } GRAPH ?g { ?s <http://ex.org/name> ?o } }""",
    "relative-iri": """SELECT ?s WHERE {
        VALUES ?g { <zz> <http://ex.org/g/1> } GRAPH ?g { ?s <http://ex.org/name> ?o } }""",
    "relative-iri-constant": """SELECT ?s WHERE {
        { GRAPH <zz> { ?s <http://ex.org/name> ?o } }
        UNION { GRAPH <http://ex.org/g/1> { ?s <http://ex.org/name> ?o } } }""",
}


#: Terms vortex-rdf's parser rejects in subject, predicate and object
#: position: a relative IRI (no BASE), written or bound through VALUES. Where
#: its VALUES row survives (an OPTIONAL with nothing to match), rdflib yields
#: the IRI.
REJECTED_TERMS = {
    "predicate": "SELECT ?s ?o WHERE { ?s <zz> ?o }",
    "subject": "SELECT ?p ?o WHERE { <zz> ?p ?o }",
    "object": "SELECT ?s ?p WHERE { ?s ?p <zz> }",
    "values": "SELECT ?s ?p ?o WHERE { VALUES ?o { <zz> <http://ex.org/carol> } ?s ?p ?o }",
    "values-optional": """SELECT ?o ?s WHERE {
        VALUES ?o { <zz> <http://ex.org/carol> } OPTIONAL { ?s <http://ex.org/likes> ?o } }""",
}


def _answer_in_mode(monkeypatch, mode, path, view, sparql):
    """``sparql`` over ``view`` of the file-backed store at ``path``, with the
    pushdown on, limited to BGPs (``VORTEX_RDF_PUSHDOWN_OPS=bgp``) or
    unregistered."""
    monkeypatch.setattr(SETTINGS, "enabled_ops", SETTINGS.enabled_ops)
    if mode == "bgp":
        monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "bgp")
    # Building the store registers the pushdown, so it is built first.
    store = VortexRdflibStore(str(path), in_memory=False)
    if mode == "unregistered":
        unregister_sparql_pushdown()
    try:
        return run(view(store), sparql)
    finally:
        register_sparql_pushdown()


@pytest.mark.parametrize("mode", ["pushdown", "bgp", "unregistered"])
@pytest.mark.parametrize(
    "sparql", list(REJECTED_GRAPH_NAMES.values()), ids=list(REJECTED_GRAPH_NAMES)
)
def test_a_graph_name_the_parser_rejects_matches_nothing(
    dict_vortex_quads, monkeypatch, sparql, mode
):
    """A graph name vortex-rdf's parser rejects names no graph of the file,
    so it matches nothing, as in rdflib's own store; the pushdown leaves its
    block to rdflib, and rdflib's evaluation (BGP-only pushdown, or none)
    asks the store for that context."""
    memory = Dataset(default_union=True)
    memory.parse(data=FIXTURE_NQ, format="nquads")
    expected = run(memory, sparql)
    assert expected
    union = _answer_in_mode(
        monkeypatch, mode, dict_vortex_quads, lambda s: Dataset(store=s, default_union=True), sparql
    )
    assert union == expected


@pytest.mark.parametrize("mode", ["pushdown", "bgp", "unregistered"])
@pytest.mark.parametrize("sparql", list(REJECTED_TERMS.values()), ids=list(REJECTED_TERMS))
def test_a_term_the_parser_rejects_matches_nothing(dict_vortex, monkeypatch, sparql, mode):
    """A term vortex-rdf's parser rejects is no term of the file, so a
    pattern holding it matches nothing, as in rdflib's own store: the
    pushdown's pattern is unsatisfiable, a VALUES constant gets a code that
    joins nothing and is still yielded, and ``triples()`` yields nothing."""
    memory = Graph()
    memory.parse(data=FIXTURE_NT, format="nt")
    expected = run(memory, sparql)
    answer = _answer_in_mode(monkeypatch, mode, dict_vortex, lambda s: Graph(store=s), sparql)
    assert answer == expected


def test_a_context_naming_no_graph_is_empty(dict_vortex_quads):
    """A literal, or an IRI the native parser rejects, names no graph of the
    file: a context it identifies has no triples and no length."""
    store = VortexRdflibStore(str(dict_vortex_quads), in_memory=False)
    name = URIRef("http://ex.org/name")
    ground = (URIRef("http://ex.org/alice"), name, Literal("Alice"))
    for context in (Literal("zz"), Graph(store=store, identifier=URIRef("zz"))):
        for pattern in ((None, None, None), (None, name, None), ground):
            assert list(store.triples(pattern, context)) == []
        assert store.__len__(context) == 0


def test_a_term_the_parser_rejects_has_no_triples_and_no_graphs(dict_vortex_quads):
    """An IRI the native parser rejects, in any position, is no term of the
    file: ``triples()`` yields nothing and ``contexts()`` names no graph."""
    store = VortexRdflibStore(str(dict_vortex_quads), in_memory=False)
    relative = URIRef("zz")
    for pattern in ((relative, None, None), (None, relative, None), (None, None, relative)):
        assert list(store.triples(pattern, None)) == []
        assert list(store.contexts(pattern)) == []
