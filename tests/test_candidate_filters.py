"""FILTER conjuncts with a native shape are evaluated by vortex-rdf over the
codes the query produced: the distinct codes of a matched column, of the join
step that binds the variable, or of the rows the probes reached — never over
the whole dictionary, and nothing is remembered between calls. The codes it
leaves undecided go on to the Python routes, and the match is compacted
natively to the rows that pass."""

import pytest
from rdflib import Graph, Literal, URIRef, Variable
from vortex_rdf import U64Column, serialize_rdf

import vortex_rdflib.pushdown as pd
from test_filters import CTX, V, W, filter_expr, typed
from test_native_filters import _Recording
from test_native_primitives import _native_calls, _products, _spy, _store_file, _trace
from test_pushdown import FIXTURE_NT, both_ways, run
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown
from vortex_rdflib.terms import parse_spelling

XSD = "http://www.w3.org/2001/XMLSchema#"
PRICE = "<http://ex.org/price>"
#: BSBM Explore Q5's band: the anchor p0 costs 0, so the products under 100.
BAND = f"""SELECT ?s ?v WHERE {{
    <http://ex.org/p0> {PRICE} ?ref .
    ?s {PRICE} ?v
    FILTER(?v < ?ref + 100 && ?v > ?ref - 100) }}"""
S, X, Y, G = (Variable(name) for name in "sxyg")


def _spy_filters(graph) -> _Recording:
    """Record every ``filter_codes`` call the store's dictionary receives."""
    spy = _Recording(graph.store._dict)
    graph.store._dict = spy
    return spy


def _price_code(store, i: int) -> int:
    return store._dict.encode(f'"{(i * 37) % 1000}"^^<{XSD}integer>')


def _price_match(store):
    """The four code columns of every price quad."""
    return store._store().match_codes(None, PRICE, None, None)


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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (f"SELECT ?s WHERE {{ ?s {PRICE} ?v FILTER(?v < 300) }}", _under(300)),
        (BAND, _under(100)),
    ],
    ids=["single-pattern", "band"],
)
def test_the_native_filters_switch_sends_no_candidates(
    tmp_path, monkeypatch, capsys, query, expected
):
    """VORTEX_RDF_NATIVE_FILTERS=0 is the A/B switch: nothing reaches
    filter_codes — neither a pattern's own conjunct nor a band specialized
    from a tuple conjunct — and the answer is rdflib's."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    monkeypatch.setattr(filters, "_NATIVE_ENABLED", False)
    register_sparql_pushdown()
    spy = _spy_filters(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, query)
    events = {e["event"] for e in _trace(capsys)}
    assert len(rows) == expected
    assert not events & {"bgp_tuple_specialized", "native_filter_complete"}
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert rows == with_pushdown == without_pushdown
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
    built = []
    original = filters.tuple_predicate
    monkeypatch.setattr(
        filters, "tuple_predicate", lambda *a, **k: built.append(a[1]) or original(*a, **k)
    )
    register_sparql_pushdown()
    spy = _spy_filters(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, BAND)
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
    with_pushdown, without_pushdown = both_ways(graph, BAND)
    assert with_pushdown == without_pushdown


def test_iri_equality_between_two_variables_is_evaluated_over_the_codes_it_reaches(
    tmp_path, monkeypatch, capsys
):
    """Once an anchor binds ?y to one IRI, ``?y = ?w`` is a test of ?w alone:
    the conjunct is specialized to the ``iri_in`` shape of that IRI, which
    vortex-rdf decides over the codes the pattern binding ?w brings — the
    literal "Carol" is no IRI — and the FILTER above evaluates nothing."""
    graph = Graph(store=VortexRdflibStore(_store_file(tmp_path, [FIXTURE_NT]), in_memory=True))
    built = []
    original = filters.tuple_predicate
    monkeypatch.setattr(
        filters, "tuple_predicate", lambda *a, **k: built.append(a[1]) or original(*a, **k)
    )
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    sparql = """SELECT ?x ?w WHERE {
        <http://ex.org/carol> <http://ex.org/likes> ?y . ?x ?p ?w FILTER(?y = ?w) }"""
    rows = run(graph, sparql)
    trace = _trace(capsys)
    assert len(rows) == 4 and {row[1] for row in rows} == {URIRef("http://ex.org/carol")}
    specialized = [e for e in trace if e["event"] == "bgp_tuple_specialized"]
    assert [(e["variable"], e["native_predicate"]) for e in specialized] == [("w", "iri_in")]
    restriction = next(
        e for e in trace if e["event"] == "bgp_restriction_complete" and e["variable"] == "w"
    )
    assert restriction["output_rows"] == 4 < restriction["input_rows"]
    assert built == []
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert rows == with_pushdown == without_pushdown


def test_both_restriction_modes_trace_the_same_fields(tmp_path, monkeypatch, capsys):
    """A restriction is traced with the same fields whether its pattern was
    matched or probed, the native predicate count included."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    query = f"""SELECT ?s ?v WHERE {{
        ?s <http://ex.org/type> <http://ex.org/Product> .
        ?s {PRICE} ?v FILTER(?v < 300) }}"""
    records = {}
    for mode, fanout in (("match", 10**9), ("probe", 0)):
        monkeypatch.setattr(pd, "_PROBE_FANOUT", fanout)
        capsys.readouterr()
        run(graph, query)
        [record] = [e for e in _trace(capsys) if e["event"] == "bgp_restriction_complete"]
        assert record["mode"] == mode
        assert (record["predicate_count"], record["native_predicate_count"]) == (1, 1)
        records[mode] = record
    assert records["match"].keys() == records["probe"].keys()


def test_a_specialized_conjunct_compiles_as_analyze_expr_does():
    """``specialize`` fixes the other variables to terms and compiles the
    conjunct as ``analyze_expr`` does when the context binds them: the same
    shape and routes, the same answers."""
    expr = filter_expr("?v < ?w + 10")
    five = Literal(5)
    [band] = filters.analyze_expr(expr, [V, W], CTX, {}).tuples
    proxy = filters.specialize(band, V, {W: five})
    [analyzed] = filters.analyze_expr(expr, [V], CTX, {W: five}).per_var[V]
    assert proxy is not None and proxy.fast is not None and analyzed.fast is not None
    assert proxy.vars == analyzed.vars == (V,)
    assert (proxy.native, proxy.kind_only, proxy.consts) == (
        analyzed.native,
        analyzed.kind_only,
        analyzed.consts,
    )
    for spelling in (
        typed("14", "integer"),
        typed("15", "integer"),
        typed("16", "integer"),
        typed("abc", "integer"),
        '"x"',
        "<http://ex.org/x>",
    ):
        view = parse_spelling(spelling)
        assert proxy.fast((view,)) == analyzed.fast((view,)), spelling


#: A match of eight rows over the positions (s, p, o, g): positions 0 and 2
#: agree on some rows only, as a repeated variable's do.
MATCH = [
    [5, 6, 5, 7, 8, 5, 6, 9],
    [1, 2, 1, 2, 1, 2, 1, 2],
    [5, 3, 9, 7, 8, 5, 4, 9],
    [3, 4, 3, 4, 4, 3, 3, 4],
]

#: id -> (variables and their positions, allowed codes per variable, the rows
#: that remain). A position no variable takes holds a constant.
COMPACTIONS = {
    "one variable": ({X: [0]}, {X: {5, 7}}, [0, 2, 3, 5]),
    "two variables": ({X: [0], Y: [2]}, {X: {5, 6, 8}, Y: {5, 9, 4}}, [0, 2, 5, 6]),
    "the other order": ({X: [0], Y: [2]}, {Y: {5, 9, 4}, X: {5, 6, 8}}, [0, 2, 5, 6]),
    "the graph position": ({X: [0], G: [3]}, {X: {5, 6}, G: {3}}, [0, 2, 5, 6]),
    "a repeated variable is tested at its first position": (
        {X: [0, 2]},
        {X: {5, 6}},
        [0, 1, 2, 5, 6],
    ),
    "constants in the other positions": ({Y: [2]}, {Y: {9, 4, 8}}, [2, 4, 6, 7]),
    "an empty set": ({X: [0], Y: [2]}, {X: set()}, []),
    "an empty set after a take": ({X: [0], Y: [2]}, {X: {5}, Y: set()}, []),
    "every code": ({X: [0], Y: [2]}, {X: set(MATCH[0]), Y: set(MATCH[2])}, list(range(8))),
    "a set larger than the column": ({X: [0]}, {X: set(range(6, 1000))}, [1, 3, 4, 6, 7]),
    "a set larger than the column holding every code": (
        {X: [0]},
        {X: set(range(1000))},
        list(range(8)),
    ),
}


@pytest.mark.parametrize(
    ("varpos", "keep", "remaining"), list(COMPACTIONS.values()), ids=list(COMPACTIONS)
)
def test_compacting_a_match_keeps_the_allowed_rows_in_match_order(varpos, keep, remaining):
    """Every column a variable reads holds exactly the rows whose code at each
    kept variable's first position is allowed, in match order; the columns of
    constant positions are dropped, and the match's own columns are untouched."""
    columns = tuple(U64Column(column) for column in MATCH)
    pat: dict = {"varpos": varpos, "cols": columns, "nrows": len(MATCH[0])}
    kept = [
        i
        for i in range(len(MATCH[0]))
        if all(MATCH[varpos[var][0]][i] in allowed for var, allowed in keep.items())
    ]
    assert kept == remaining
    pd._compact_pattern(pat, keep)
    assert pat["nrows"] == len(kept)
    read = {idx for positions in varpos.values() for idx in positions}
    for idx in range(4):
        if idx in read:
            assert memoryview(pat["cols"][idx]).cast("Q").tolist() == [MATCH[idx][i] for i in kept]
        else:
            assert pat["cols"][idx] is None
    assert [memoryview(column).cast("Q").tolist() for column in columns] == MATCH


@pytest.mark.parametrize(
    ("limit", "compacted"), [(300, True), (5000, False)], ids=["narrowed", "every-row-passes"]
)
def test_restricting_a_match_compacts_it_to_the_rows_that_pass(tmp_path, limit, compacted):
    """The restriction compacts the match once, in match order, to the rows
    whose code passes the conjunct, dropping the columns of constant
    positions; a match whose rows all pass keeps its own columns."""
    store = VortexRdflibStore(_products(tmp_path), in_memory=True)
    columns = _price_match(store)
    subjects, prices = (memoryview(columns[i]).cast("Q").tolist() for i in (0, 2))
    passing = {_price_code(store, i) for i in range(200) if (i * 37) % 1000 < limit}
    pat: dict = {"varpos": {S: [0], V: [2]}, "cols": columns, "nrows": len(subjects), "index": 0}
    [conjunct] = filters.analyze_expr(filter_expr(f"?v < {limit}"), [V], CTX, {}).per_var[V]
    pd._restrict_pattern(store, pat, {V: [conjunct]})
    rows = [i for i, price in enumerate(prices) if price in passing]
    assert 0 < len(rows) and (len(rows) < len(subjects)) == compacted
    assert pat["nrows"] == len(rows)
    assert memoryview(pat["cols"][0]).cast("Q").tolist() == [subjects[i] for i in rows]
    assert memoryview(pat["cols"][2]).cast("Q").tolist() == [prices[i] for i in rows]
    if compacted:
        assert pat["cols"][1] is None and pat["cols"][3] is None
    else:
        assert pat["cols"] is columns


def test_iri_equality_is_a_keep_on_the_match(tmp_path, monkeypatch, capsys):
    """?v IN (<iri>, ...) is the IRIs' codes — one encode_many — riding the
    native match as a keep: no candidate is evaluated and only the matching
    rows leave vortex-rdf."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    spy = _spy_filters(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(
        graph,
        """SELECT ?s WHERE { ?s <http://ex.org/type> ?t
        FILTER(?s IN (<http://ex.org/p1>, <http://ex.org/p2>, <http://ex.org/nobody>)) }""",
    )
    trace = _trace(capsys)
    assert rows == [(URIRef("http://ex.org/p1"),), (URIRef("http://ex.org/p2"),)]
    assert spy.calls == []
    pushed = next(e for e in trace if e["event"] == "bgp_native_restriction")
    assert pushed["keep"] == {"0": {"codes": 2}}
    match = _native_calls(trace, "match_codes")[0]
    assert match["keep"] == {"0": {"codes": 2}} and match["returned_rows"] == 2


def test_iri_constants_the_dictionary_lacks_match_nothing(tmp_path):
    """An IRI equality whose constants are all absent keeps no code: the
    pattern matches nothing, so nothing is counted or matched."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    register_sparql_pushdown()
    native = _spy(graph)
    assert run(graph, "SELECT ?s WHERE { ?s ?p ?o FILTER(?o = <http://ex.org/nobody>) }") == []
    assert run(graph, "ASK { ?s ?p ?o FILTER(?o IN ()) }") is False
    count = """SELECT (COUNT(*) AS ?n) WHERE {
        ?s ?p ?o FILTER(sameTerm(?o, <http://ex.org/nobody>)) }"""
    assert run(graph, count) == [(Literal(0),)]
    assert [name for name in native.names() if name in ("match_codes", "count_quads")] == []


def test_in_the_empty_list_is_false_even_for_rdf_nil(tmp_path, monkeypatch):
    """rdflib spells `IN ()` as rdf:nil and reads it back as the empty list:
    nothing is in it, rdf:nil included — as a keep and on the fast route."""
    nil = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"
    nt = tmp_path / "list.nt"
    nt.write_text(
        f"<http://ex.org/l> <http://www.w3.org/1999/02/22-rdf-syntax-ns#rest> <{nil}> .\n",
        encoding="utf-8",
    )
    out = tmp_path / "list.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(out)))
    cases = [
        ("SELECT ?o WHERE { ?s ?p ?o FILTER(?o IN ()) }", []),
        (f"SELECT ?o WHERE {{ ?s ?p ?o FILTER(?o IN (<{nil}>)) }}", [(URIRef(nil),)]),
        ("SELECT ?o WHERE { ?s ?p ?o FILTER(?o NOT IN ()) }", [(URIRef(nil),)]),
    ]
    for native in (True, False):
        monkeypatch.setattr(filters, "_NATIVE_ENABLED", native)
        for query, expected in cases:
            with_pushdown, without_pushdown = both_ways(graph, query)
            assert with_pushdown == without_pushdown == expected, (native, query)


def test_values_only_rdflib_answers_are_decoded_in_one_batch(tmp_path, monkeypatch):
    """A conjunct rdflib's evaluator answers decodes the values it reads in
    one decode_many, not one decode per value."""
    graph = Graph(store=VortexRdflibStore(_products(tmp_path), in_memory=True))
    monkeypatch.setattr(filters, "_FAST_ENABLED", False)
    register_sparql_pushdown()
    inner = graph.store._dict  # ty: ignore[unresolved-attribute]
    calls: list[str] = []

    class _Calls:
        def decode(self, code):
            calls.append("decode")
            return inner.decode(code)

        def decode_many(self, codes):
            calls.append("decode_many")
            return inner.decode_many(codes)

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._dict = _Calls()  # ty: ignore[unresolved-attribute]
    query = "SELECT ?s WHERE { ?s <http://ex.org/label> ?l FILTER(strlen(?l) > 10) }"
    assert len(run(graph, query)) == sum(1 for i in range(200) if len(f"product {i}") > 10)
    assert "decode" not in calls
