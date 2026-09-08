import json

import pytest
from rdflib import Graph
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


@pytest.fixture
def traced_graph(tmp_path):
    source = tmp_path / "trace.nt"
    source.write_text(
        "".join(
            f'<http://ex.org/s{i}> <http://ex.org/date> "{i}"^^<http://www.w3.org/2001/XMLSchema#integer> .\n'  # noqa: E501
            f'<http://ex.org/s{i}> <http://ex.org/name> "n{i}" .\n'
            for i in range(40)
        ),
        encoding="utf-8",
    )
    output = tmp_path / "trace.vortex"
    serialize_rdf(str(source), str(output), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(output), in_memory=True))


def events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def test_trace_disabled_is_silent_and_preserves_results(traced_graph, capsys, monkeypatch):
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY", raising=False)
    register_sparql_pushdown()
    rows = list(traced_graph.query("SELECT ?s WHERE { ?s <http://ex.org/name> ?n } LIMIT 3"))
    assert len(rows) == 3
    assert events(capsys.readouterr()) == []


def test_trace_reports_bounded_order_and_decoding(traced_graph, capsys, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "q8-test")
    register_sparql_pushdown()
    query = """SELECT ?s ?d WHERE {
        ?s <http://ex.org/date> ?d .
        ?s <http://ex.org/name> ?n
    } ORDER BY DESC(?d) LIMIT 5"""
    traced = list(traced_graph.query(query))
    payloads = events(capsys.readouterr())
    assert len(traced) == 5
    assert payloads
    assert all(item["schema"] == "vortex-rdf-query-trace-v1" for item in payloads)
    assert all(item["query_id"] == "q8-test" for item in payloads)
    assert [item["sequence"] for item in payloads] == sorted(item["sequence"] for item in payloads)
    planned = next(item for item in payloads if item["event"] == "head_planned")
    assert planned["limit"] == 5
    ordered = next(item for item in payloads if item["event"] == "order_complete")
    assert ordered["algorithm"] == "bounded_heap_nsmallest"
    assert ordered["input_rows"] == 40
    assert ordered["output_rows"] == 5
    ranked = next(item for item in payloads if item["event"] == "rank_codes_complete")
    assert ranked["unique_codes"] == 40
    yielded = next(item for item in payloads if item["event"] == "yield_rows_complete")
    assert yielded["rows_yielded"] == 5
    assert yielded["chunks"] == 1

    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "0")
    untraced = list(traced_graph.query(query))
    assert traced == untraced


def test_trace_rejects_invalid_switch(monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "yes")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        pd._trace_enabled()
