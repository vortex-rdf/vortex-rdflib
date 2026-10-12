"""FILTER conjuncts with a native shape are evaluated by vortex-rdf over the
codes the query produced: the distinct codes of a matched column, of the join
step that binds the variable, or of the rows the probes reached — never over
the whole dictionary, and nothing is remembered between calls. The codes it
leaves undecided go on to the Python routes, and the match is compacted
natively to the rows that pass."""

import pytest
from rdflib import Graph, Literal, URIRef
from vortex_rdf import U64Column

import vortex_rdflib.pushdown as pd
from test_native_filters import _Recording
from test_native_primitives import _products, _spy, _trace
from test_pushdown import both_ways, run
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

XSD = "http://www.w3.org/2001/XMLSchema#"
PRICE = "<http://ex.org/price>"


def _spy_filters(graph) -> _Recording:
    """Record every ``filter_codes`` call the store's dictionary receives."""
    spy = _Recording(graph.store._dict)
    graph.store._dict = spy
    return spy


def _price_code(store, i: int) -> int:
    return store._dict.encode(f'"{(i * 37) % 1000}"^^<{XSD}integer>')


def _under(limit: int) -> int:
    return sum(1 for i in range(200) if (i * 37) % 1000 < limit)


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_a_single_pattern_filter_is_evaluated_over_its_matched_codes(tmp_path, in_memory):
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=in_memory))
    register_sparql_pushdown()
    expected = sorted(_price_code(graph.store, i) for i in range(200))
    spy = _spy_filters(graph)
    query = f"SELECT ?s ?v WHERE {{ ?s {PRICE} ?v FILTER(?v < 300) }}"
    assert len(run(graph, query)) == _under(300)
    [(kind, arg, options, codes)] = spy.calls
    assert (kind, arg, options) == ("num_lt", f'"300"^^<{XSD}integer>', {})
    assert codes == expected  # the 200 distinct prices, nothing dictionary-wide
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("fanout", [0, 10**9], ids=["probe", "hash"])
def test_a_join_step_evaluates_the_codes_it_binds(tmp_path, monkeypatch, fanout):
    """In a BGP of several patterns the conjunct waits for the step that binds
    its variable: a hash-joined pattern's matched column, or the rows a probed
    pattern's probes reached — here every product, so 200 prices either way."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    monkeypatch.setattr(pd, "_PROBE_FANOUT", fanout)
    register_sparql_pushdown()
    expected = sorted(_price_code(graph.store, i) for i in range(200))
    spy = _spy_filters(graph)
    query = f"""SELECT ?s ?v WHERE {{
        ?s <http://ex.org/type> <http://ex.org/Product> .
        ?s {PRICE} ?v FILTER(?v < 300) }}"""
    assert len(run(graph, query)) == _under(300)
    [(kind, _, _, codes)] = spy.calls
    assert kind == "num_lt" and codes == expected


def test_a_probe_join_evaluates_only_the_codes_its_probes_reached(tmp_path, monkeypatch):
    store = VortexRdflibStore(_products(tmp_path), in_memory=True)
    graph = Graph(store=store)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 1)
    register_sparql_pushdown()
    spy = _spy_filters(graph)
    query = f"""SELECT ?s ?v WHERE {{
        VALUES ?s {{ <http://ex.org/p1> <http://ex.org/p2> <http://ex.org/p3> }}
        ?s {PRICE} ?v FILTER(?v < 300) }}"""
    rows = run(graph, query)
    [(_, _, _, codes)] = spy.calls
    assert codes == sorted(_price_code(store, i) for i in (1, 2, 3))
    store._dict = spy._inner
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert rows == with_pushdown == without_pushdown


def test_undecided_codes_take_the_python_routes(tmp_path):
    """Whatever vortex-rdf leaves undecided, the fast route or rdflib answers,
    so the result stands."""
    store = VortexRdflibStore(_products(tmp_path), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()

    class _Undecided(_Recording):
        def filter_codes(self, kind, arg, codes, **options):
            super().filter_codes(kind, arg, codes, **options)
            return U64Column([]), codes

    spy = _Undecided(store._dict)
    store._dict = spy  # ty: ignore[invalid-assignment]
    for query in (
        f"SELECT ?s WHERE {{ ?s {PRICE} ?v FILTER(?v < 300) }}",
        'SELECT ?s WHERE { ?s <http://ex.org/label> ?l FILTER(langMatches(lang(?l), "en")) }',
    ):
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown
    assert spy.calls


def test_the_native_filters_switch_sends_no_candidates(tmp_path, monkeypatch):
    """VORTEX_RDF_NATIVE_FILTERS=0 is the A/B switch: nothing reaches
    filter_codes, and the answer stands."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    monkeypatch.setattr(filters, "_NATIVE_ENABLED", False)
    register_sparql_pushdown()
    spy = _spy_filters(graph)
    assert len(run(graph, f"SELECT ?s WHERE {{ ?s {PRICE} ?v FILTER(?v < 300) }}")) == _under(300)
    assert spy.calls == []


def test_a_restricted_match_stays_columnar(tmp_path, monkeypatch):
    """The rows that fail are dropped by the native join_indices/take, so the
    single pattern's relation is still the match's columns, compacted, with no
    row predicate left to stream."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    rels = []
    original = pd._rel_from_pattern
    monkeypatch.setattr(pd, "_rel_from_pattern", lambda pat: rels.append(original(pat)) or rels[-1])
    register_sparql_pushdown()
    assert len(run(graph, f"SELECT ?s WHERE {{ ?s {PRICE} ?v FILTER(?v < 300) }}")) == _under(300)
    [rel] = rels
    assert rel.cols is not None and rel.preds == () and rel.nrows == _under(300)


def test_count_over_a_candidate_filter_matches_once(tmp_path):
    """COUNT(*) over a FILTER vortex-rdf decides by candidates: one match of
    the pattern, each conjunct over the codes still in play — the second over
    the first one's survivors — and the count of the rows that pass."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    native = _spy(graph)
    filtered = _spy_filters(graph)
    count = f"""PREFIX xsd: <{XSD}>
        SELECT (COUNT(*) AS ?n) WHERE {{
        ?s {PRICE} ?v FILTER(?v < 300 && datatype(?v) = xsd:integer) }}"""
    assert run(graph, count) == [(Literal(_under(300)),)]
    assert native.names().count("match_codes") == 1
    first, second = filtered.calls
    assert (first[0], second[0]) == ("num_lt", "datatype")
    assert len(first[3]) == 200 and len(second[3]) == _under(300)


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_band_around_an_anchor_is_evaluated_over_the_price_codes(
    tmp_path, monkeypatch, capsys, in_memory
):
    """BSBM Explore Q5's band: once the anchor binds ?ref, the two-variable
    conjuncts are tests of ?v alone, evaluated by vortex-rdf over the price
    codes the scan brings — decided there, the FILTER above evaluates nothing."""
    store = VortexRdflibStore(_products(tmp_path), in_memory=in_memory)
    graph = Graph(store=store)
    query = f"""SELECT ?s ?v WHERE {{
        <http://ex.org/p0> {PRICE} ?ref .
        ?s {PRICE} ?v
        FILTER(?v < ?ref + 100 && ?v > ?ref - 100) }}"""
    built = []
    original = filters.tuple_predicate
    monkeypatch.setattr(
        filters, "tuple_predicate", lambda *a, **k: built.append(a[1]) or original(*a, **k)
    )
    register_sparql_pushdown()
    spy = _spy_filters(graph)
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
    assert sorted(call[0] for call in spy.calls) == ["num_gt", "num_lt"]
    assert all(len(call[3]) <= 200 for call in spy.calls)
    restriction = next(
        e for e in trace if e["event"] == "bgp_restriction_complete" and e["variable"] == "v"
    )
    assert (restriction["input_rows"], restriction["output_rows"]) == (200, len(expected))
    assert built == []
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
    store._dict = spy._inner
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown
