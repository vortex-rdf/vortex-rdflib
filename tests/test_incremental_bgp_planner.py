import json

import pytest
from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def graph_from(tmp_path, name, ntriples):
    """The N-Triples as an in-memory Dictionary-layout store, behind a Graph."""
    source = tmp_path / f"{name}.nt"
    source.write_text(ntriples, encoding="utf-8")
    artifact = tmp_path / f"{name}.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=True))


def graph_of(tmp_path, size=300):
    """One anchor row, plus a name and a tag for each of ``size`` subjects."""
    return graph_from(
        tmp_path,
        "incremental",
        '<http://ex/s0> <http://ex/anchor> "yes" .\n'
        + "".join(f'<http://ex/s{i}> <http://ex/name> "name-{i}"@en .\n' for i in range(size))
        + "".join(f"<http://ex/s{i}> <http://ex/tag> <http://ex/t{i}> .\n" for i in range(size)),
    )


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


def test_seed_is_the_smallest_count_not_the_most_selective_shape(tmp_path, monkeypatch, capsys):
    """An object-bound pattern selecting one row seeds the plan ahead of a
    predicate scan, whatever the shapes suggest: the scan is probed once,
    never matched whole."""
    graph = graph_from(
        tmp_path,
        "seed",
        "<http://ex/s0> <http://ex/rare> <http://ex/target> .\n"
        + "".join(f"<http://ex/s{i}> <http://ex/big> <http://ex/o{i}> .\n" for i in range(300)),
    )
    native = graph.store._store()
    matched_rows = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            cols = native.match_codes(*args, **kwargs)
            matched_rows.append(len(cols[0]))
            return cols

        def __getattr__(self, name):
            return getattr(native, name)

    graph.store._native = Counting()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?p ?z WHERE {
        ?s <http://ex/big> ?z .
        ?s ?p <http://ex/target>
    }""")
    )
    payloads = events(capsys.readouterr())
    assert rows == [(URIRef("http://ex/s0"), URIRef("http://ex/rare"), URIRef("http://ex/o0"))]
    assert matched_rows == [1, 1], "the 300-row scan was matched whole"
    plan = next(e for e in payloads if e["event"] == "bgp_plan_complete")
    assert plan["seed_selection"] == "smallest_count"
    assert plan["estimate_calls"] == 2
    steps = [e for e in payloads if e["event"] == "bgp_join_step_complete"]
    assert [e["strategy"] for e in steps] == ["probe"]
    assert steps[0]["estimated_rows"] == 300


def test_pattern_selecting_nothing_ends_the_block_before_any_match(tmp_path, monkeypatch, capsys):
    graph = graph_of(tmp_path)
    native = graph.store._store()
    matches = []

    class Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return native.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(native, name)

    graph.store._native = Counting()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/name> ?name .
        ?s <http://ex/anchor> "no"
    }""")
    )
    payloads = events(capsys.readouterr())
    assert rows == []
    assert matches == []
    plan = next(e for e in payloads if e["event"] == "bgp_plan_complete")
    assert plan["seed_selection"] == "empty_count"
    # The anchor is counted first, by shape, so the scan is never counted.
    assert plan["estimate_calls"] == 1


@pytest.mark.parametrize("fanout", [100, 10**9])
def test_bound_variable_is_not_restricted_again(tmp_path, monkeypatch, fanout):
    """A per-variable filter on a variable the seed binds is evaluated by
    the seed alone; the pattern joined next — probed or hash-joined —
    inherits the codes and does not evaluate it over its own column."""
    graph = graph_of(tmp_path)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", fanout)
    seen = []
    original = filters.evaluate_column

    def spy(store, conjuncts, codes):
        seen.append(set(codes))
        return original(store, conjuncts, codes)

    monkeypatch.setattr(filters, "evaluate_column", spy)
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name
        FILTER(isIRI(?s))
    }""")
    )
    assert len(rows) == 1
    assert rows[0][0] == URIRef("http://ex/s0")
    assert len(seen) == 1
    assert len(seen[0]) == 1


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
