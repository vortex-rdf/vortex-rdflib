import json

from rdflib import Graph
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def _events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def test_restriction_trace_reports_distinct_bindings_and_rows(tmp_path, capsys, monkeypatch):
    # The Python restriction route: langMatches would otherwise be a keep
    # on the native match (see test_native_restriction_trace).
    monkeypatch.setattr(filters, "_NATIVE_ENABLED", False)
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


def test_native_restriction_trace(tmp_path, capsys, monkeypatch):
    """A conjunct the native layer decides is traced where it is pushed —
    the pattern, the variable and the keep it became — and the narrowed
    match it rides is a `native_call_complete` carrying that keep."""
    source = tmp_path / "native.nt"
    source.write_text(
        "".join(
            f'<http://ex/s{i}> <http://ex/name> "n{i}"@{"en" if i % 3 else "fr"} .\n'
            for i in range(30)
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / "native.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(artifact), in_memory=True))
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    rows = list(
        graph.query(
            'SELECT ?s WHERE { ?s <http://ex/name> ?n FILTER(langMatches(lang(?n), "FR")) }'
        )
    )
    payloads = _events(capsys.readouterr())
    assert len(rows) == 10
    pushed = next(e for e in payloads if e["event"] == "bgp_native_restriction")
    assert pushed["variable"] == "n"
    assert pushed["pushed_predicate_count"] == 1
    assert pushed["keep"] == {"2": {"codes": 10}}
    match = next(
        e
        for e in payloads
        if e["event"] == "native_call_complete" and e["operation"] == "match_codes"
    )
    assert match["keep"] == {"2": {"codes": 10}}
    assert match["returned_rows"] == 10
