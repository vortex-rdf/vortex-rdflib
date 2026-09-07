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


def test_restriction_trace_reports_distinct_bindings_and_rows(tmp_path, capsys, monkeypatch):
    source = tmp_path / "restriction.nt"
    source.write_text(
        '<http://ex/s1> <http://ex/anchor> "yes" .\n'
        '<http://ex/s1> <http://ex/name> "one"@en .\n'
        '<http://ex/s2> <http://ex/anchor> "yes" .\n'
        '<http://ex/s2> <http://ex/name> "deux"@fr .\n'
        '<http://ex/s3> <http://ex/name> "three"@en .\n',
        encoding="utf-8",
    )
    artifact = tmp_path / "restriction.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(artifact), in_memory=True))
    query = """SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name
        FILTER(langMatches(lang(?name), "EN"))
    }"""
    register_sparql_pushdown()

    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "0")
    expected = list(graph.query(query))
    assert _events(capsys.readouterr()) == []

    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "restriction-test")
    actual = list(graph.query(query))
    payloads = _events(capsys.readouterr())

    assert actual == expected
    records = [e for e in payloads if e["event"] == "bgp_restriction_complete"]
    assert len(records) == 1
    record = records[0]
    assert record["original_pattern_index"] == 1
    assert record["input_rows"] == 3
    assert record["output_rows"] == 2
    assert record["predicate_count"] == 1
    assert record["fast_predicate_count"] == 1
    assert record["generic_only_predicate_count"] == 0
    assert record["kind_only_predicate_count"] == 0
    assert record["distinct_binding_count"] == 3
    assert record["allowed_binding_count"] == 2
    assert record["elapsed_ns"] > 0
