import pytest
from rdflib import Graph, URIRef
from tests.support import (
    counting_native,
    is_graph_name_check,
    native_spy,
    run,
    store_file,
    trace_events,
)
from vortex_rdf import serialize_rdf

from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def _graph(tmp_path, size=300):
    source = tmp_path / "leftjoin.nt"
    source.write_text(
        '<http://ex/s0> <http://ex/anchor> "yes" .\n'
        + "".join(
            f"<http://ex/s{i}> <http://ex/optional> <http://ex/o{i}> .\n" for i in range(size)
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "leftjoin.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=True))


def test_probe_optional_counts_before_match_and_skips_complete_match(tmp_path, monkeypatch, capsys):
    graph = _graph(tmp_path)
    native = graph.store._store()
    matches = []
    counts = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return native.match_codes(*args, **kwargs)

        def count_quads(self, *args, **kwargs):
            if not is_graph_name_check(args):
                counts.append((args, kwargs))
            return native.count_quads(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(native, name)

    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._native = Counting()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?o WHERE {
        ?s <http://ex/anchor> "yes"
        OPTIONAL { ?s <http://ex/optional> ?o }
    }""")
    )
    payloads = trace_events(capsys.readouterr().err)
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    # One full match for the left BGP and one selective probe. There is no
    # full match for the 300-row OPTIONAL predicate.
    assert len(matches) == 2
    assert [kwargs.get("limit") for _, kwargs in counts] == [101]
    event = next(item for item in payloads if item["event"] == "left_join_plan_complete")
    assert event["strategy"] == "probe"
    assert event["input_rows"] == 1
    assert event["estimated_right_rows"] == 101
    assert event["native_initial_match_count"] == 0
    assert event["native_probe_call_count"] == 1
    assert event["matched_left_rows"] == 1
    assert event["unmatched_left_rows"] == 0
    assert event["output_rows"] == 1


def test_lazy_join_counts_before_match_and_probes(tmp_path, monkeypatch, capsys):
    """A nested group's lazy join takes the same count-first route as an
    OPTIONAL: the 300-row right pattern is counted, never matched whole."""
    graph = _graph(tmp_path)
    native = graph.store._store()
    matches = []
    counts = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return native.match_codes(*args, **kwargs)

        def count_quads(self, *args, **kwargs):
            if not is_graph_name_check(args):
                counts.append((args, kwargs))
            return native.count_quads(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(native, name)

    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._native = Counting()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?o WHERE {
        { ?s <http://ex/anchor> "yes" }
        { ?s <http://ex/optional> ?o }
    }""")
    )
    payloads = trace_events(capsys.readouterr().err)
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    assert len(matches) == 2
    assert [kwargs.get("limit") for _, kwargs in counts] == [101]
    event = next(item for item in payloads if item["event"] == "join_plan_complete")
    assert event["strategy"] == "probe"
    assert event["estimated_right_rows"] == 101
    assert event["native_probe_call_count"] == 1


def test_exists_counts_its_pattern_only_as_far_as_the_probe_decision_needs(tmp_path):
    """A one-row block and an EXISTS over the 300-row predicate: the pattern
    is counted up to 1 x 100 + 1 rows, the capped answer says probe, and the
    row is probed with a count capped at one row."""
    graph = _graph(tmp_path)
    counting = counting_native(graph)
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s WHERE {
        ?s <http://ex/anchor> "yes"
        FILTER EXISTS { ?s <http://ex/optional> ?o }
    }""")
    )
    assert rows == [(URIRef("http://ex/s0"),)]
    assert [(args[1], limit) for args, limit in counting.count_calls] == [
        ("<http://ex/optional>", 101),
        ("<http://ex/optional>", 1),
    ]


def test_probe_optional_preserves_unmatched_padding(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?o WHERE {
        ?s <http://ex/anchor> "yes"
        OPTIONAL { ?s <http://ex/missing> ?o }
    }""")
    )
    assert rows == [(URIRef("http://ex/s0"), None)]


def test_hash_optional_still_materializes_right_pattern(tmp_path, monkeypatch, capsys):
    graph = _graph(tmp_path, size=3)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 10**9)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?o WHERE {
        ?s <http://ex/anchor> "yes"
        OPTIONAL { ?s <http://ex/optional> ?o }
    }""")
    )
    payloads = trace_events(capsys.readouterr().err)
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    event = next(item for item in payloads if item["event"] == "left_join_plan_complete")
    assert event["strategy"] == "hash"
    assert event["estimated_right_rows"] == 3
    assert event["native_initial_match_count"] == 1
    assert event["native_probe_call_count"] == 0


def _pushdown_then_rdflib(graph, sparql) -> list:
    """The pushdown's answer to ``sparql``, checked against rdflib's own
    evaluator, which runs after it."""
    register_sparql_pushdown()
    rows = run(graph, sparql)
    unregister_sparql_pushdown()
    try:
        assert rows == run(graph, sparql)
    finally:
        register_sparql_pushdown()
    return rows


#: An OPTIONAL whose left side binds only the right pattern's object.
OBJECT_BOUND = """SELECT ?s ?o WHERE {
    ?o <http://ex/anchor> "yes"
    OPTIONAL { ?s <http://ex/optional> ?o }
}"""


def test_optional_keep_scans_when_probes_would_scan(tmp_path, monkeypatch, capsys):
    """The left side binds only the right pattern's object, and the
    file-backed store has no index: a probe per left row would scan the
    store. The OPTIONAL matches its pattern once instead, narrowed at the
    object to the left side's codes, and hash-joins it, as a BGP's join step
    does (a keep-scan): no probe is made."""
    lines = ['<http://ex/o0> <http://ex/anchor> "yes" .\n'] + [
        f"<http://ex/s{i}> <http://ex/optional> <http://ex/o{i}> .\n" for i in range(300)
    ]
    graph = Graph(store=VortexRdflibStore(store_file(tmp_path, lines), in_memory=False))
    spy = native_spy(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = _pushdown_then_rdflib(graph, OBJECT_BOUND)
    payloads = trace_events(capsys.readouterr().err)
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    [keyed] = [kwargs for name, kwargs in spy.calls if name == "match_codes" and "keep" in kwargs]
    assert set(keyed["keep"]) == {2}
    assert "match_codes_many" not in spy.names()
    event = next(item for item in payloads if item["event"] == "left_join_plan_complete")
    assert event["strategy"] == "keep_scan"
    assert event["native_probe_call_count"] == 0


#: An OPTIONAL whose condition is false whatever the rows. (Not
#: ``FILTER(false)``: rdflib's translator drops a FILTER whose expression is a
#: falsy literal, so its algebra holds no condition at all.)
CONSTANT_FALSE = """SELECT ?s ?o WHERE {
    ?s <http://ex/anchor> "yes"
    OPTIONAL { ?s <http://ex/optional> ?o FILTER(1 = 0) }
}"""


def test_a_constant_false_optional_matches_nothing(tmp_path):
    """An OPTIONAL whose condition is false whatever the rows pads every
    left row: its pattern is neither counted nor matched."""
    graph = _graph(tmp_path, size=3)
    spy = native_spy(graph)
    register_sparql_pushdown()
    rows = run(graph, CONSTANT_FALSE)
    assert rows == [(URIRef("http://ex/s0"), None)]
    assert not [args for args in spy.args if "<http://ex/optional>" in args]
    assert _pushdown_then_rdflib(graph, CONSTANT_FALSE) == rows


def _scan_graph(tmp_path):
    """The 300 `optional` rows of `s{i}` to `o{i}`, and `s0`'s `self` row
    naming itself, in a file-backed store with no index: a probe that binds
    no subject scans it."""
    lines = [f"<http://ex/s{i}> <http://ex/optional> <http://ex/o{i}> .\n" for i in range(300)]
    lines.append("<http://ex/s0> <http://ex/self> <http://ex/self> .\n")
    return Graph(store=VortexRdflibStore(store_file(tmp_path, lines), in_memory=False))


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ("<http://ex/o0> <http://ex/unknown>", [(URIRef("http://ex/o0"), URIRef("http://ex/s0"))]),
        ("<http://ex/unknown>", []),
    ],
    ids=["one-outside", "all-outside"],
)
def test_a_keep_scan_leaves_out_terms_outside_the_dictionary(
    tmp_path, monkeypatch, values, expected
):
    """A VALUES term the dictionary lacks has a code of its own, outside the
    dictionary: a keep-scan narrowed to the left rows' codes leaves it out,
    as it joins nothing, and a left side with no other code makes no
    match."""
    graph = _scan_graph(tmp_path)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    spy = native_spy(graph)
    sparql = f"SELECT ?o ?s WHERE {{ VALUES ?o {{ {values} }} ?s <http://ex/optional> ?o }}"
    assert _pushdown_then_rdflib(graph, sparql) == expected
    keyed = [kwargs for name, kwargs in spy.calls if name == "match_codes" and "keep" in kwargs]
    assert [len(kwargs["keep"][2]) for kwargs in keyed] == ([1] if expected else [])
    assert "match_codes_many" not in spy.names()


def test_an_exists_keep_scans_a_pattern_a_count_cannot_test(tmp_path, monkeypatch):
    """An EXISTS whose pattern repeats a variable the block does not bind
    cannot be probed with counts, which do not test it: it is keep-scanned,
    matched once narrowed to the block's codes, and the repeat tested on the
    rows."""
    graph = _scan_graph(tmp_path)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    spy = native_spy(graph)
    sparql = """SELECT ?s WHERE {
        ?s <http://ex/optional> ?o
        FILTER EXISTS { ?s ?x ?x }
    }"""
    assert _pushdown_then_rdflib(graph, sparql) == [(URIRef("http://ex/s0"),)]
    keyed = [kwargs for name, kwargs in spy.calls if name == "match_codes" and "keep" in kwargs]
    assert [set(kwargs["keep"]) for kwargs in keyed] == [{0}]
    assert "count_quads_many" not in spy.names()
