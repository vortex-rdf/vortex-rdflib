import json

from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def graph_of(tmp_path, size=300):
    source = tmp_path / "incremental.nt"
    source.write_text(
        '<http://ex/s0> <http://ex/anchor> "yes" .\n'
        + "".join(f'<http://ex/s{i}> <http://ex/name> "name-{i}"@en .\n' for i in range(size))
        + "".join(f"<http://ex/s{i}> <http://ex/tag> <http://ex/t{i}> .\n" for i in range(size)),
        encoding="utf-8",
    )
    artifact = tmp_path / "incremental.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=True))


def test_connected_patterns_are_probed_without_complete_matches(tmp_path, monkeypatch, capsys):
    graph = graph_of(tmp_path)
    native = graph.store._store()
    calls = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            calls.append(args)
            return native.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(native, name)

    graph.store._native = Counting()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name ?tag WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name .
        ?s <http://ex/tag> ?tag
    }""")
    )
    payloads = events(capsys.readouterr())
    assert rows == [(URIRef("http://ex/s0"), rows[0][1], URIRef("http://ex/t0"))]
    done = next(e for e in payloads if e["event"] == "bgp_complete")
    assert done["native_initial_match_count"] == 1
    assert done["native_probe_call_count"] == 2
    assert done["native_call_count"] == 3
    assert len(calls) == 3
    plan = next(e for e in payloads if e["event"] == "bgp_plan_complete")
    assert plan["seed_pattern_index"] == 0
    assert plan["execution_order"] == [0, 1, 2]
    steps = [e for e in payloads if e["event"] == "bgp_join_step_complete"]
    assert [e["strategy"] for e in steps] == ["probe", "probe"]
    assert all(e["cardinality_source"] == "probed" for e in steps)
    assert (
        done["elapsed_ns"]
        == done["native_match_ns"] + done["restriction_ns"] + done["join_ns"] + done["other_ns"]
    )


def test_incremental_probe_preserves_deferred_filter(tmp_path):
    graph = graph_of(tmp_path)
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name
        FILTER(langMatches(lang(?name), "EN"))
    }""")
    )
    assert len(rows) == 1
    assert rows[0][0] == URIRef("http://ex/s0")


def test_disconnected_pattern_uses_complete_match(tmp_path, monkeypatch, capsys):
    graph = graph_of(tmp_path, 4)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?x <http://ex/name> ?name
    }""")
    )
    payloads = events(capsys.readouterr())
    assert len(rows) == 4
    step = next(e for e in payloads if e["event"] == "bgp_join_step_complete")
    assert step["strategy"] in {"hash_columns", "hash_rows"}
    assert step["cardinality_source"] == "matched"
