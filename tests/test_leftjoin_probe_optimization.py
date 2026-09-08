import json

from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


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


def _events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def test_probe_optional_counts_before_match_and_skips_complete_match(tmp_path, monkeypatch, capsys):
    graph = _graph(tmp_path)
    # A bare Graph has a blank-node identifier. Resolve and cache its graph
    # scope before counting query-planner calls. That lookup uses count_quads
    # but is not part of OPTIONAL planning.
    graph.store._graph_n3(graph)
    native = graph.store._store()
    matches = []
    counts = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return native.match_codes(*args, **kwargs)

        def count_quads(self, *args, **kwargs):
            counts.append(args)
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
    payloads = _events(capsys.readouterr())
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    # One full match for the left BGP and one selective probe. There is no
    # full match for the 300-row OPTIONAL predicate.
    assert len(matches) == 2
    assert len(counts) == 1
    event = next(item for item in payloads if item["event"] == "left_join_plan_complete")
    assert event["strategy"] == "probe"
    assert event["input_rows"] == 1
    assert event["estimated_right_rows"] == 300
    assert event["native_initial_match_count"] == 0
    assert event["native_probe_call_count"] == 1
    assert event["matched_left_rows"] == 1
    assert event["unmatched_left_rows"] == 0
    assert event["output_rows"] == 1


def test_lazy_join_counts_before_match_and_probes(tmp_path, monkeypatch, capsys):
    """A nested group's lazy join takes the same count-first route as an
    OPTIONAL: the 300-row right pattern is counted, never matched whole."""
    graph = _graph(tmp_path)
    graph.store._graph_n3(graph)
    native = graph.store._store()
    matches = []
    counts = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return native.match_codes(*args, **kwargs)

        def count_quads(self, *args, **kwargs):
            counts.append(args)
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
    payloads = _events(capsys.readouterr())
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    assert len(matches) == 2
    assert len(counts) == 1
    event = next(item for item in payloads if item["event"] == "join_plan_complete")
    assert event["strategy"] == "probe"
    assert event["estimated_right_rows"] == 300
    assert event["native_probe_call_count"] == 1


def test_probe_optional_preserves_unmatched_padding(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
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
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 10**9)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?o WHERE {
        ?s <http://ex/anchor> "yes"
        OPTIONAL { ?s <http://ex/optional> ?o }
    }""")
    )
    payloads = _events(capsys.readouterr())
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/o0"))]
    event = next(item for item in payloads if item["event"] == "left_join_plan_complete")
    assert event["strategy"] == "hash"
    assert event["estimated_right_rows"] == 3
    assert event["native_initial_match_count"] == 1
    assert event["native_probe_call_count"] == 0
