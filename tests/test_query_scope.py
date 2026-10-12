"""What a query learns about terms lives as long as the query: decoded
terms, literal alias classes, the codes of constants outside the dictionary
and blank-node graph names are kept in the query's scope, which covers the
lazy iteration of its results and is dropped with them; the store itself
keeps fixed-size structure only."""

import gc
import weakref

import pytest
from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import _counting_term_dict, both_ways, is_graph_name_check, run
from vortex_rdflib import VortexRdflibStore, query_scope, register_sparql_pushdown

XSD = "http://www.w3.org/2001/XMLSchema#"


def _store(tmp_path, in_memory=False) -> VortexRdflibStore:
    nt = tmp_path / "scope.nt"
    nt.write_text(
        "".join(
            f'<http://ex.org/s{i}> <http://ex.org/name> "name {i}"@en .\n'
            f'<http://ex.org/s{i}> <http://ex.org/age> "0{i % 7}"^^<{XSD}integer> .\n'
            f"<http://ex.org/s{i}> <http://ex.org/knows> <http://ex.org/s{(i + 1) % 50}> .\n"
            for i in range(50)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "scope.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return VortexRdflibStore(str(out), in_memory=in_memory)


def _footprint(store) -> dict:
    """The size of every container the store holds."""
    return {
        name: len(value)
        for name, value in vars(store).items()
        if isinstance(value, (dict, set, list, tuple))
    }


def _queries(i: int) -> list[str]:
    """Fresh constants through every per-query table: decodes, a VALUES
    constant the dictionary lacks, typed literals classified for aliasing,
    a regex constant, an ORDER BY rank."""
    return [
        f'SELECT ?s ?n WHERE {{ ?s <http://ex.org/name> ?n FILTER(regex(?n, "{i}$")) }}',
        f"""SELECT ?s WHERE {{ VALUES ?s {{ <http://ex.org/absent{i}> <http://ex.org/s{i}> }}
            ?s <http://ex.org/knows> ?o }}""",
        "SELECT DISTINCT ?a WHERE { ?s <http://ex.org/age> ?a }",
        f"SELECT ?s WHERE {{ ?s <http://ex.org/knows> ?o }} ORDER BY ?o LIMIT {i % 5 + 1}",
    ]


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_the_store_does_not_grow_with_the_queries_it_answers(tmp_path, in_memory):
    store = _store(tmp_path, in_memory)
    graph = Graph(store=store)
    register_sparql_pushdown()
    for query in _queries(0):
        run(graph, query)
    before = _footprint(store)
    for i in range(1, 40):
        for query in _queries(i):
            run(graph, query)
    assert _footprint(store) == before
    assert query_scope.current() is None


def test_a_query_scope_is_released_with_its_results(tmp_path, monkeypatch):
    """The scope is current only while a step of the query runs, and is
    cleared — then collected — once the results are consumed, whether read
    to the end or dropped half-way."""
    graph = Graph(store=_store(tmp_path))
    opened = []

    class _Tracked(query_scope.QueryScope):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            opened.append(weakref.ref(self))

    monkeypatch.setattr(query_scope, "QueryScope", _Tracked)
    register_sparql_pushdown()
    assert len(run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }")) == 50
    result = graph.query("SELECT ?s ?n WHERE { ?s <http://ex.org/name> ?n }")
    rows = iter(result)
    next(rows)
    assert query_scope.current() is None  # not current between steps
    del rows, result
    gc.collect()
    assert len(opened) == 2
    assert all(ref() is None for ref in opened)


def test_each_query_has_a_scope_of_its_own(tmp_path, monkeypatch):
    """A second query run while the first is half read gets its own scope,
    and both answer in full."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    seen = []
    original = store._prime_decode_cache

    def spy(*columns):
        seen.append(query_scope.current())
        return original(*columns)

    monkeypatch.setattr(store, "_prime_decode_cache", spy)
    register_sparql_pushdown()
    first = iter(graph.query("SELECT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }"))
    head = [next(first)]
    second = run(graph, "SELECT ?s ?n WHERE { ?s <http://ex.org/name> ?n }")
    head.extend(first)
    assert len(head) == 50 and len(second) == 50
    assert None not in seen and len({id(scope) for scope in seen}) == 2


def test_triples_outside_a_query_keep_their_decodes_for_the_call(tmp_path):
    graph = Graph(store=_store(tmp_path))
    list(graph.store.contexts())  # one context per graph: store state, decoded once
    counting = _counting_term_dict(graph)
    first = sorted(graph.triples((None, None, None)))
    once = counting.decoded
    second = sorted(graph.triples((None, None, None)))
    assert first == second and len(first) == 150
    assert counting.decoded == 2 * once  # nothing carried over between calls
    assert query_scope.current() is None


def test_triples_inside_a_query_share_the_query_decodes(tmp_path, monkeypatch):
    """With no node pushed down, rdflib's own evaluator calls triples() once
    per pattern and binding; inside the query they share its decodes: the 50
    subjects and the predicate are decoded once each."""
    graph = Graph(store=_store(tmp_path))
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset())
    register_sparql_pushdown()
    list(graph.store.contexts())  # one context per graph: store state, decoded once
    counting = _counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?a ?c WHERE {
        ?a <http://ex.org/knows> ?b . ?b <http://ex.org/knows> ?c }""",
    )
    assert len(rows) == 50
    assert counting.decoded == 51


def test_constants_outside_the_dictionary_do_not_outlive_their_query(tmp_path):
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    for i in range(3):
        query = f"SELECT ?x WHERE {{ VALUES ?x {{ <http://ex.org/absent{i}> }} }}"
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown == [(URIRef(f"http://ex.org/absent{i}"),)]
    assert not hasattr(store, "_foreign") and not hasattr(store, "_decode_cache")


def test_a_bare_graphs_label_is_resolved_once_per_query(tmp_path):
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    checks = []
    inner = store._store()

    class _Counting:
        def count_quads(self, *args, **kwargs):
            if is_graph_name_check(args):
                checks.append(args[3])
            return inner.count_quads(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    store._native = _Counting()  # ty: ignore[invalid-assignment]
    for _ in range(3):
        run(graph, "SELECT ?s WHERE { ?s <http://ex.org/knows> ?o . ?o <http://ex.org/knows> ?x }")
    assert len(checks) == 3


def test_graph_names_the_file_lacks_leave_no_state(tmp_path):
    """A view of a graph the file does not hold matches nothing and leaves
    nothing behind: the store keeps one context per graph of the file."""
    store = _store(tmp_path)
    before = _footprint(store)
    for i in range(20):
        view = Graph(store=store, identifier=URIRef(f"http://ex.org/absent{i}"))
        assert list(view.triples((None, None, None))) == []
    assert _footprint(store) == before
