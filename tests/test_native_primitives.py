"""The vortex-rdf primitives the pushdown builds on, each pinned where it is
used: what reaches the native layer, and that the answer is rdflib's. The
equivalence matrix in ``test_pushdown`` covers the answers of every shape;
these say *how* they are reached."""

import pytest
from rdflib import Graph, Literal, URIRef
from tests.support import (
    both_ways,
    captured_trace,
    native_call_events,
    native_spy,
    products_file,
    run,
)
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown
from vortex_rdflib.settings import SETTINGS

XSD = "http://www.w3.org/2001/XMLSchema#"


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_limit_stops_a_single_pattern_match(tmp_path, monkeypatch, capsys, in_memory):
    """A LIMIT over one pattern — with a FILTER the native match decides,
    too — fetches only the rows it reads; an OFFSET reads past them."""
    graph = Graph(store=VortexRdflibStore(products_file(tmp_path), in_memory=in_memory))
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    for query, limit in [
        ("SELECT * WHERE { ?s ?p ?o } LIMIT 5", 5),
        ("SELECT * WHERE { ?s ?p ?o } LIMIT 5 OFFSET 3", 8),
        ("SELECT ?s WHERE { ?s ?p ?o FILTER(isLiteral(?o)) } LIMIT 4", 4),
    ]:
        capsys.readouterr()
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown
        match = native_call_events(captured_trace(capsys), "match_codes")[0]
        assert match["limit"] == limit, query
        assert match["returned_rows"] == limit, query


def test_limit_is_not_pushed_where_rows_are_dropped_in_python(tmp_path, monkeypatch, capsys):
    """A conjunct the native layer cannot decide, a repeated variable or a
    secondary index (whose served match orders rows its own way) keep the
    match whole: the LIMIT applies to the rows as before."""
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    plain = Graph(store=VortexRdflibStore(products_file(tmp_path)))
    indexed_path = products_file(tmp_path / "indexed", indexes=["secondary-by-copy"])
    indexed = Graph(store=VortexRdflibStore(indexed_path))
    for graph, query in [
        (plain, 'SELECT ?s WHERE { ?s ?p ?o FILTER(regex(str(?o), "1")) } LIMIT 3'),
        (plain, "SELECT ?s WHERE { ?s ?p ?s } LIMIT 3"),
        (indexed, "SELECT ?s WHERE { ?s <http://ex.org/label> ?o } LIMIT 3"),
    ]:
        capsys.readouterr()
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown
        assert all(
            m["limit"] is None for m in native_call_events(captured_trace(capsys), "match_codes")
        )


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_ask_and_count_over_a_native_filter_only_count(tmp_path, in_memory):
    """ASK and COUNT(*) over one pattern whose FILTER the native layer
    decides are counts of the keep-narrowed match — capped at one row for
    the ASK — and no row is ever matched."""
    graph = Graph(store=VortexRdflibStore(products_file(tmp_path), in_memory=in_memory))
    register_sparql_pushdown()
    spy = native_spy(graph)
    count = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER(isLiteral(?o)) }"
    assert run(graph, count) == [(Literal(400),)]
    assert run(graph, "ASK { ?s ?p ?o FILTER(isIRI(?o)) }") is True
    assert run(graph, "ASK { ?s ?p ?o FILTER(isBlank(?o)) }") is False
    assert "match_codes" not in spy.names()
    counts = [kwargs for name, kwargs in spy.calls if name == "count_quads"]
    assert [kwargs.get("limit") for kwargs in counts] == [None, 1, 1]
    assert all(set(kwargs["keep"]) == {2} for kwargs in counts)


def test_values_constants_are_encoded_in_one_batch(tmp_path):
    """A VALUES table's constants are looked up in one `encode_many` —
    spelling-tolerant, so no count is spent guarding the lookup — and a
    constant the store lacks joins nothing but is still yielded."""
    store = VortexRdflibStore(products_file(tmp_path))
    graph = Graph(store=store)
    register_sparql_pushdown()
    spy = native_spy(graph)
    encodes = []
    dictionary = store._dict
    assert dictionary is not None

    class _Dict:
        def encode_many(self, terms):
            encodes.append(list(terms))
            return dictionary.encode_many(terms)

        def __getattr__(self, name):
            return getattr(dictionary, name)

    store._dict = _Dict()  # ty: ignore[invalid-assignment]
    query = """SELECT ?s ?l WHERE {
        VALUES ?s { <http://ex.org/p1> <http://ex.org/p2> <http://ex.org/absent> }
        ?s <http://ex.org/label> ?l }"""
    rows = run(graph, query)
    assert {row[0] for row in rows} == {URIRef("http://ex.org/p1"), URIRef("http://ex.org/p2")}
    assert encodes == [["<http://ex.org/p1>", "<http://ex.org/p2>", "<http://ex.org/absent>"]]
    # No native call is spent making sure the absent constant is absent.
    assert not any("<http://ex.org/absent>" in args for args in spy.args)
    store._dict = dictionary
    yielded = "SELECT ?s WHERE { VALUES ?s { <http://ex.org/p1> <http://ex.org/absent> } }"
    for sparql in (query, yielded):
        with_pushdown, without_pushdown = both_ways(graph, sparql)
        assert with_pushdown == without_pushdown
    assert URIRef("http://ex.org/absent") in {row[0] for row in run(graph, yielded)}


def test_probes_that_scan_are_batched(tmp_path, monkeypatch):
    """Probes binding no subject scan the store. The joins above a BGP — with
    a VALUES table, an OPTIONAL — send them to the native layer in one batch;
    a step of a BGP matches its pattern once instead, narrowed to the codes
    its relation binds (a keep-scan). Probes on a bound subject are point
    lookups, which plain calls answer faster than a batch can be scheduled."""
    graph = Graph(store=VortexRdflibStore(products_file(tmp_path), in_memory=True))
    register_sparql_pushdown()
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    spy = native_spy(graph)
    values = """SELECT ?s ?o WHERE {
        VALUES ?o { "product 1"@en "product 3"@en "product 4"@fr }
        ?s <http://ex.org/label> ?o }"""
    optional = """SELECT ?o ?t WHERE {
        <http://ex.org/p1> ?p ?o
        OPTIONAL { ?t ?q ?o } }"""
    bgp = """SELECT ?o ?t WHERE {
        <http://ex.org/p1> ?p ?o .
        ?t ?q ?o }"""
    subject_bound = """SELECT ?s ?v WHERE {
        VALUES ?s { <http://ex.org/p1> <http://ex.org/p2> <http://ex.org/p3> }
        ?s <http://ex.org/price> ?v }"""
    # `?o` is p1's price, label and type: only p1 has that price and that
    # label, and every product has that type.
    for query, rows in ((values, 3), (optional, 202)):
        spy.calls.clear()
        assert len(run(graph, query)) == rows
        assert "match_codes_many" in spy.names()
    spy.calls.clear()
    assert len(run(graph, bgp)) == 202
    assert "match_codes_many" not in spy.names()
    keyed = [kwargs for name, kwargs in spy.calls if name == "match_codes" and "keep" in kwargs]
    assert [set(kwargs["keep"]) for kwargs in keyed] == [{2}]
    spy.calls.clear()
    assert len(run(graph, subject_bound)) == 3
    assert "match_codes_many" not in spy.names()
    for query in (values, optional, bgp, subject_bound):
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown


def test_graph_variable_keeps_the_named_graphs_inside_the_match(tmp_path, monkeypatch, capsys):
    """`GRAPH ?g` ranges over the named graphs: the default graph's code is
    0, so the named ones are the code range above it — a keep on the match's
    graph column, not a filter over the rows it returns."""
    source = tmp_path / "quads.nq"
    source.write_text(
        '<http://ex.org/a> <http://ex.org/p> "default" .\n'
        '<http://ex.org/a> <http://ex.org/p> "one" <http://ex.org/g1> .\n'
        '<http://ex.org/b> <http://ex.org/p> "two" <http://ex.org/g2> .\n',
        encoding="utf-8",
    )
    out = tmp_path / "quads.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary", format="nquads")
    from rdflib import Dataset

    store = VortexRdflibStore(str(out))
    dataset = Dataset(store=store, default_union=True)
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(dataset, "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g")
    assert sorted(rows) == [
        (URIRef("http://ex.org/g1"), Literal(1)),
        (URIRef("http://ex.org/g2"), Literal(1)),
    ]
    match = native_call_events(captured_trace(capsys), "match_codes")[0]
    assert store._dict is not None
    assert match["keep"] == {"3": {"range": [1, len(store._dict)]}}
