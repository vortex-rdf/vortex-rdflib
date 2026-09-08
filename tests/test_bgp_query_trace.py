import json

from rdflib import Graph
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def _events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def test_bgp_pattern_trace_reconciles_and_preserves_results(tmp_path, capsys, monkeypatch):
    source = tmp_path / "bgp.nt"
    source.write_text(
        "<http://ex/s1> <http://ex/p> <http://ex/o1> .\n"
        '<http://ex/s1> <http://ex/q> "one" .\n'
        "<http://ex/s2> <http://ex/p> <http://ex/o2> .\n"
        '<http://ex/s2> <http://ex/q> "two" .\n',
        encoding="utf-8",
    )
    artifact = tmp_path / "bgp.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(artifact), in_memory=True))
    query = "SELECT ?s ?o ?v WHERE { ?s <http://ex/p> ?o . ?s <http://ex/q> ?v }"
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "0")
    expected = list(graph.query(query))
    assert _events(capsys.readouterr()) == []
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "bgp-test")
    actual = list(graph.query(query))
    payloads = _events(capsys.readouterr())
    assert actual == expected
    assert sum(e["event"] == "bgp_start" for e in payloads) == 1
    assert sum(e["event"] == "bgp_pattern_start" for e in payloads) == 2
    completes = [e for e in payloads if e["event"] == "bgp_pattern_complete"]
    assert len(completes) == 2
    assert all(e["timing_reconciled"] for e in completes)
    assert all(e["elapsed_ns"] == e["native_match_ns"] for e in completes)
    plan = next(e for e in payloads if e["event"] == "bgp_plan_complete")
    assert sorted(plan["execution_order"]) == [0, 1]
    assert plan["seed_pattern_index"] == plan["execution_order"][0]
    done = next(e for e in payloads if e["event"] == "bgp_complete")
    assert done["timing_reconciled"] is True
    assert done["elapsed_ns"] == (
        done["native_match_ns"] + done["restriction_ns"] + done["join_ns"] + done["other_ns"]
    )
    assert done["native_call_count"] == 2
    assert done["output_rows"] == 2
