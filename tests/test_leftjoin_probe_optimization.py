from rdflib import Graph, URIRef
from tests.support import counting_native, is_graph_name_check, trace_events
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown
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
