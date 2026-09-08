import json

from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def _events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def _graph(tmp_path):
    source = tmp_path / "probe-filter.nt"
    source.write_text(
        '<http://ex/s0> <http://ex/anchor> "yes" .\n'
        + "".join(
            f'<http://ex/s{i}> <http://ex/name> "name-{i}"@{("en" if i % 2 == 0 else "fr")} .\n'
            for i in range(300)
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "probe-filter.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=True))


def test_probe_filter_evaluates_only_reached_codes(tmp_path, capsys, monkeypatch):
    graph = _graph(tmp_path)
    query = """SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name
        FILTER(langMatches(lang(?name), "EN"))
    }"""
    seen = []
    original = filters.evaluate_column

    def spy(store, conjuncts, codes):
        seen.append(set(codes))
        return original(store, conjuncts, codes)

    monkeypatch.setattr(filters, "evaluate_column", spy)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "probe-filter")
    register_sparql_pushdown()
    rows = list(graph.query(query))
    payloads = _events(capsys.readouterr())

    assert rows == [(URIRef("http://ex/s0"), rows[0][1])]
    assert len(seen) == 1
    assert len(seen[0]) == 1
    record = next(e for e in payloads if e["event"] == "bgp_restriction_complete")
    assert record["mode"] == "probe"
    assert record["input_rows"] == 1
    assert record["output_rows"] == 1
    assert record["distinct_binding_count"] == 1
    step = next(
        e
        for e in payloads
        if e["event"] == "bgp_join_step_complete" and e["original_pattern_index"] == 1
    )
    assert step["strategy"] == "probe"
    assert step["pattern_match_rows"] == 300
    assert step["pattern_restricted_rows"] == 1


def test_probe_filter_generic_fallback_is_once_per_reached_code(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    calls = []
    original = filters.Conjunct.generic

    def generic(self, bound):
        calls.append(bound)
        return original(self, bound)

    monkeypatch.setattr(filters.Conjunct, "generic", generic)
    monkeypatch.setattr(filters, "_FAST_ENABLED", False)
    register_sparql_pushdown()
    rows = list(
        graph.query(
            """SELECT ?s ?name WHERE {
                ?s <http://ex/anchor> "yes" .
                ?s <http://ex/name> ?name
                FILTER(langMatches(lang(?name), "EN"))
            }"""
        )
    )
    assert len(rows) == 1
    assert len(calls) == 1


def test_lazy_join_probe_evaluates_only_reached_codes(tmp_path, monkeypatch):
    """The same filter over nested groups — a lazy join rather than one
    BGP — reaches the probe with the restriction deferred, so it is
    evaluated over the one name the probe returns, not the 300 of the scan."""
    graph = _graph(tmp_path)
    seen = []
    original = filters.evaluate_column

    def spy(store, conjuncts, codes):
        seen.append(set(codes))
        return original(store, conjuncts, codes)

    monkeypatch.setattr(filters, "evaluate_column", spy)
    register_sparql_pushdown()
    rows = list(
        graph.query(
            """SELECT ?s ?name WHERE {
                { ?s <http://ex/anchor> "yes" }
                { ?s <http://ex/name> ?name }
                FILTER(langMatches(lang(?name), "EN"))
            }"""
        )
    )
    assert rows == [(URIRef("http://ex/s0"), rows[0][1])]
    assert len(seen) == 1
    assert len(seen[0]) == 1


def test_non_probe_filter_keeps_eager_route(tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 10**9)
    seen = []
    original = filters.evaluate_column

    def spy(store, conjuncts, codes):
        seen.append(set(codes))
        return original(store, conjuncts, codes)

    monkeypatch.setattr(filters, "evaluate_column", spy)
    register_sparql_pushdown()
    rows = list(
        graph.query(
            """SELECT ?s ?name WHERE {
                ?s <http://ex/anchor> "yes" .
                ?s <http://ex/name> ?name
                FILTER(langMatches(lang(?name), "EN"))
            }"""
        )
    )
    assert len(rows) == 1
    assert len(seen) == 1
    assert len(seen[0]) == 300
