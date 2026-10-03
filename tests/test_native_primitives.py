"""The vortex-rdf 0.11 primitives the pushdown builds on, each pinned where
it is used: what reaches the native layer, and that the answer is still
rdflib's. The equivalence matrix in ``test_pushdown`` covers the answers of
every shape; these say *how* they are reached."""

import json

import pytest
from rdflib import Graph, Literal, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import both_ways, run
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

XSD = "http://www.w3.org/2001/XMLSchema#"


def _store_file(tmp_path, lines, name="data", indexes=()) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"{name}.nt"
    source.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / f"{name}.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary", indexes=list(indexes))
    return str(out)


def _products(tmp_path, n=200, indexes=()) -> str:
    """`n` products, each with an integer price, a label in English or
    French, and a type; product 0 is the reference the band queries use."""
    lines = []
    for i in range(n):
        s = f"<http://ex.org/p{i}>"
        lines.append(f'{s} <http://ex.org/price> "{(i * 37) % 1000}"^^<{XSD}integer> .\n')
        lines.append(f'{s} <http://ex.org/label> "product {i}"@{"en" if i % 2 else "fr"} .\n')
        lines.append(f"{s} <http://ex.org/type> <http://ex.org/Product> .\n")
    return _store_file(tmp_path, lines, "products", indexes)


def _trace(capsys) -> list[dict]:
    return [
        json.loads(line[len(pd._TRACE_PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(pd._TRACE_PREFIX)
    ]


def _native_calls(trace, operation) -> list[dict]:
    return [
        e for e in trace if e["event"] == "native_call_complete" and e["operation"] == operation
    ]


class _Spy:
    """The native store with every call recorded by name."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []
        self.args: list[tuple] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            self.args.append(args)
            return attribute(*args, **kwargs)

        return call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _spy(graph) -> _Spy:
    graph.store._graph_n3(graph)  # the store's own bookkeeping, once
    spy = _Spy(graph.store._store())
    graph.store._native = spy
    return spy


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_limit_stops_a_single_pattern_match(tmp_path, monkeypatch, capsys, in_memory):
    """A LIMIT over one pattern — with a FILTER the native match decides,
    too — fetches only the rows it reads; an OFFSET reads past them."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=in_memory))
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
        match = _native_calls(_trace(capsys), "match_codes")[0]
        assert match["limit"] == limit, query
        assert match["returned_rows"] == limit, query


def test_limit_is_not_pushed_where_rows_are_dropped_in_python(tmp_path, monkeypatch, capsys):
    """A conjunct the native layer cannot decide, a repeated variable or a
    secondary index (whose served match orders rows its own way) keep the
    match whole: the LIMIT applies to the rows as before."""
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    plain = Graph(store=VortexRdflibStore(_products(tmp_path)))
    indexed_path = _products(tmp_path / "indexed", indexes=["secondary-by-copy"])
    indexed = Graph(store=VortexRdflibStore(indexed_path))
    for graph, query in [
        (plain, 'SELECT ?s WHERE { ?s ?p ?o FILTER(regex(str(?o), "1")) } LIMIT 3'),
        (plain, "SELECT ?s WHERE { ?s ?p ?s } LIMIT 3"),
        (indexed, "SELECT ?s WHERE { ?s <http://ex.org/label> ?o } LIMIT 3"),
    ]:
        capsys.readouterr()
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown
        assert all(m["limit"] is None for m in _native_calls(_trace(capsys), "match_codes"))


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_ask_and_count_over_a_native_filter_only_count(tmp_path, in_memory):
    """ASK and COUNT(*) over one pattern whose FILTER the native layer
    decides are counts of the keep-narrowed match — capped at one row for
    the ASK — and no row is ever matched."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=in_memory))
    register_sparql_pushdown()
    spy = _spy(graph)
    count = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER(isLiteral(?o)) }"
    assert run(graph, count) == [(Literal(400),)]
    assert run(graph, "ASK { ?s ?p ?o FILTER(isIRI(?o)) }") is True
    assert run(graph, "ASK { ?s ?p ?o FILTER(isBlank(?o)) }") is False
    assert "match_codes" not in spy.names()
    counts = [kwargs for name, kwargs in spy.calls if name == "count_quads"]
    assert [kwargs.get("limit") for kwargs in counts] == [None, 1, 1]
    assert all(set(kwargs["keep"]) == {2} for kwargs in counts)


def test_count_over_a_native_code_set_only_counts(tmp_path):
    """The same for FILTERs the native layer answers with a code set — the
    datatype and value bands of a price, a language: in memory, any set is a
    keep the count takes."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    spy = _spy(graph)
    count = """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT (COUNT(*) AS ?n) WHERE {
        ?s <http://ex.org/price> ?v FILTER(?v < 300 && datatype(?v) = xsd:integer) }"""
    assert run(graph, count) == [(Literal(sum(1 for i in range(200) if (i * 37) % 1000 < 300)),)]
    assert run(graph, 'ASK { ?s ?p ?o FILTER(langMatches(lang(?o), "en")) }') is True
    assert run(graph, 'ASK { ?s ?p ?o FILTER(langMatches(lang(?o), "de")) }') is False
    assert "match_codes" not in spy.names()
    with_pushdown, without_pushdown = both_ways(graph, count)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_band_around_an_anchor_is_a_keep_on_the_scan(tmp_path, monkeypatch, capsys, in_memory):
    """BSBM Explore Q5's band: once the anchor binds `?ref`, the two-variable
    conjuncts are tests of `?v` alone and become a keep on the price scan, so
    only the products inside the band leave the native match — and, decided
    there, the FILTER above evaluates nothing."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=in_memory))
    query = """SELECT ?s ?v WHERE {
        <http://ex.org/p0> <http://ex.org/price> ?ref .
        ?s <http://ex.org/price> ?v
        FILTER(?v < ?ref + 100 && ?v > ?ref - 100) }"""
    built = []
    original = filters.tuple_predicate
    monkeypatch.setattr(
        filters, "tuple_predicate", lambda *a, **k: built.append(a[1]) or original(*a, **k)
    )
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, query)
    trace = _trace(capsys)
    integer = URIRef(XSD + "integer")
    expected = sorted(
        (URIRef(f"http://ex.org/p{i}"), Literal(str((i * 37) % 1000), datatype=integer))
        for i in range(200)
        if (i * 37) % 1000 < 100
    )
    assert sorted(rows) == expected
    specialized = [e for e in trace if e["event"] == "bgp_tuple_specialized"]
    assert sorted(e["native_predicate"] for e in specialized) == ["num_gt", "num_lt"]
    scan = next(
        m
        for m in _native_calls(trace, "match_codes")
        if m["pattern"][1] == "<http://ex.org/price>" and m["pattern"][0] is None
    )
    assert scan["returned_rows"] == len(expected)
    assert built == []
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown


def test_values_constants_are_encoded_in_one_batch(tmp_path):
    """A VALUES table's constants are looked up in one `encode_many` —
    spelling-tolerant, so no count is spent guarding the lookup — and a
    constant the store lacks joins nothing but is still yielded."""
    store = VortexRdflibStore(_products(tmp_path))
    graph = Graph(store=store)
    register_sparql_pushdown()
    spy = _spy(graph)
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
    """Probes binding no subject scan the store, so they go to the native
    layer in one batch; probes on a bound subject are point lookups, which
    plain calls answer faster than a batch can be scheduled."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    spy = _spy(graph)
    object_bound = """SELECT ?s ?o WHERE {
        VALUES ?o { "product 1"@en "product 3"@en "product 4"@fr }
        ?s <http://ex.org/label> ?o }"""
    rows = run(graph, object_bound)
    assert len(rows) == 3
    assert "match_codes_many" in spy.names()
    spy.calls.clear()
    subject_bound = """SELECT ?s ?v WHERE {
        VALUES ?s { <http://ex.org/p1> <http://ex.org/p2> <http://ex.org/p3> }
        ?s <http://ex.org/price> ?v }"""
    assert len(run(graph, subject_bound)) == 3
    assert "match_codes_many" not in spy.names()
    for query in (object_bound, subject_bound):
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
    match = _native_calls(_trace(capsys), "match_codes")[0]
    assert store._dict is not None
    assert match["keep"] == {"3": {"range": [1, len(store._dict)]}}
