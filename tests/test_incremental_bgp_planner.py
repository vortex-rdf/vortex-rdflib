import json

import pytest
from rdflib import Graph, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import _counting_native, both_ways
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown
from vortex_rdflib.settings import SETTINGS

PREFIX = "VORTEX_RDF_QUERY_TRACE "


def events(captured):
    return [
        json.loads(line.removeprefix(PREFIX))
        for line in captured.err.splitlines()
        if line.startswith(PREFIX)
    ]


def graph_from(tmp_path, name, ntriples, in_memory=True):
    """The N-Triples as a Dictionary-layout store (in memory by default),
    behind a Graph."""
    source = tmp_path / f"{name}.nt"
    source.write_text(ntriples, encoding="utf-8")
    artifact = tmp_path / f"{name}.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=in_memory))


def graph_of(tmp_path, size=300, in_memory=True):
    """One anchor row, plus a name and a tag for each of ``size`` subjects."""
    return graph_from(
        tmp_path,
        "incremental",
        '<http://ex/s0> <http://ex/anchor> "yes" .\n'
        + "".join(f'<http://ex/s{i}> <http://ex/name> "name-{i}"@en .\n' for i in range(size))
        + "".join(f"<http://ex/s{i}> <http://ex/tag> <http://ex/t{i}> .\n" for i in range(size)),
        in_memory=in_memory,
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
    # Next to a one-row pattern, so counted at its step only up to 1 x 100 + 1.
    assert (steps[0]["estimated_rows"], steps[0]["estimate_limit"]) == (101, 101)


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_pattern_selecting_nothing_ends_the_block_before_any_match(
    tmp_path, monkeypatch, capsys, in_memory
):
    graph = graph_of(tmp_path, in_memory=in_memory)
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
    batches = [e for e in payloads if e["event"] == "native_batch_complete"]
    if in_memory:
        # Two in-memory scans overlap by less than a batch costs: plain
        # calls count the patterns, most bound first, and the anchor's zero
        # leaves the name pattern uncounted.
        assert plan["estimate_calls"] == 1
        assert batches == []
    else:
        # Both patterns are counted in one native batch, concurrently; the
        # anchor's zero then ends the block before anything is matched.
        assert plan["estimate_calls"] == 2
        assert [(e["operation"], e["probe_count"]) for e in batches] == [("count_quads_many", 2)]


@pytest.mark.parametrize("fanout", [100, 10**9])
def test_bound_variable_is_not_restricted_again(tmp_path, monkeypatch, fanout):
    """On the Python route, a per-variable filter on a variable the seed
    binds is evaluated by the seed alone; the pattern joined next — probed
    or hash-joined — inherits the codes and does not evaluate it over its
    own column."""
    graph = graph_of(tmp_path)
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    monkeypatch.setattr(SETTINGS, "native_filters", False)
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
    # A product of 4 rows is within the pair budget: nothing is counted.
    assert step["pair_count"] is None


@pytest.mark.parametrize("fanout", [100, 10**9])
def test_native_filter_narrows_every_pattern_it_reaches(tmp_path, monkeypatch, capsys, fanout):
    """A filter the native layer decides — a kind test here — is a keep on
    the native match of every pattern binding its variable, counted and
    matched (or probed) narrowed: nothing is evaluated in Python."""
    graph = graph_of(tmp_path)
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    seen = []
    original = filters.evaluate_column

    def spy(store, conjuncts, codes):
        seen.append(set(codes))
        return original(store, conjuncts, codes)

    monkeypatch.setattr(filters, "evaluate_column", spy)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" .
        ?s <http://ex/name> ?name
        FILTER(isIRI(?s))
    }""")
    )
    payloads = events(capsys.readouterr())
    assert len(rows) == 1
    assert rows[0][0] == URIRef("http://ex/s0")
    assert seen == []
    pushed = [e for e in payloads if e["event"] == "bgp_native_restriction"]
    assert sorted(e["original_pattern_index"] for e in pushed) == [0, 1]
    assert all(e["residual_predicate_count"] == 0 for e in pushed)


def indexed_graph_of(tmp_path, anchors=1, size=300, in_memory=True):
    """``graph_of``'s data behind a secondary-by-copy index, where every
    pattern with a bound predicate is a point pattern (`VortexRdflibStore._point_lookup`); the
    anchor rows are subjects s0 to s{anchors - 1}."""
    source = tmp_path / "indexed.nt"
    source.write_text(
        "".join(f'<http://ex/s{i}> <http://ex/anchor> "yes" .\n' for i in range(anchors))
        + "".join(f'<http://ex/s{i}> <http://ex/name> "name-{i}"@en .\n' for i in range(size))
        + "".join(f"<http://ex/s{i}> <http://ex/tag> <http://ex/t{i}> .\n" for i in range(size)),
        encoding="utf-8",
    )
    artifact = tmp_path / "indexed.vortex"
    serialize_rdf(str(source), str(artifact), layout="dictionary", indexes=["secondary-by-copy"])
    return Graph(store=VortexRdflibStore(str(artifact), in_memory=in_memory))


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_patterns_next_to_a_small_count_are_not_counted_when_planned(
    tmp_path, monkeypatch, capsys, in_memory
):
    """The anchor counts 1 row, at most SETTINGS.probe_fanout: name and tag share ?s
    with it, so planning leaves them uncounted. At its join step each is
    counted only as far as the decision needs — capped at 1 x 100 + 1 rows —
    and the capped answer says probe."""
    graph = indexed_graph_of(tmp_path, in_memory=in_memory)
    counting = _counting_native(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    query = """SELECT ?s ?name ?tag WHERE {
        ?s <http://ex/anchor> "yes" . ?s <http://ex/name> ?name . ?s <http://ex/tag> ?tag }"""
    rows = list(graph.query(query))
    payloads = events(capsys.readouterr())
    assert len(rows) == 1
    assert [(args[1], limit) for args, limit in counting.count_calls] == [
        ("<http://ex/anchor>", None),
        ("<http://ex/name>", 101),
        ("<http://ex/tag>", 101),
    ]
    steps = [e for e in payloads if e["event"] == "bgp_join_step_complete"]
    assert [
        (e["strategy"], e["estimated_rows"], e["estimate_limit"], e["pair_count"]) for e in steps
    ] == [
        ("probe", 101, 101, None),
        ("probe", 101, 101, None),
    ]
    assert next(e for e in payloads if e["event"] == "bgp_plan_complete")["estimate_calls"] == 3
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown


def test_a_decision_count_under_its_cap_matches_and_hash_joins(tmp_path, monkeypatch, capsys):
    """50 anchors: name is counted at its step capped at 50 x 100 + 1 rows;
    its 300 rows come back exact, under the cap, and are matched and
    hash-joined."""
    graph = indexed_graph_of(tmp_path, anchors=50)
    counting = _counting_native(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/anchor> "yes" . ?s <http://ex/name> ?name }""")
    )
    payloads = events(capsys.readouterr())
    assert len(rows) == 50
    assert [(args[1], limit) for args, limit in counting.count_calls] == [
        ("<http://ex/anchor>", None),
        ("<http://ex/name>", 5001),
    ]
    [step] = [e for e in payloads if e["event"] == "bgp_join_step_complete"]
    assert step["strategy"] in {"hash_columns", "hash_rows"}
    assert (step["estimated_rows"], step["estimate_limit"], step["pair_count"]) == (300, 5001, None)


def test_a_scan_next_to_a_small_point_pattern_is_not_counted_when_planned(tmp_path):
    """Without an index, the bound-subject name pattern is the one point
    pattern and counts 1 row. The scan sharing ?n with it leaves the scan
    batch: it is counted at its step, capped at 1 x 100 + 1 rows, and the
    other scan is counted on its own, not as a batch of one."""
    graph = graph_of(tmp_path, in_memory=False)
    counting = _counting_native(graph)
    register_sparql_pushdown()
    query = """SELECT ?x ?n ?t WHERE {
        <http://ex/s5> <http://ex/name> ?n . ?x <http://ex/name> ?n . ?x <http://ex/tag> ?t }"""
    rows = list(graph.query(query))
    assert len(rows) == 1
    assert [(args[0], args[1], limit) for args, limit in counting.count_calls] == [
        ("<http://ex/s5>", "<http://ex/name>", None),
        (None, "<http://ex/tag>", None),
        (None, "<http://ex/name>", 101),
    ]
    assert counting.batches == 0
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown


def test_a_zero_point_count_still_ends_the_block_first(tmp_path):
    graph = indexed_graph_of(tmp_path)
    counting = _counting_native(graph)
    register_sparql_pushdown()
    rows = list(
        graph.query("""SELECT ?s ?name WHERE {
        ?s <http://ex/name> ?name . ?s <http://ex/anchor> "no" }""")
    )
    assert rows == []
    assert [args[1] for args, _ in counting.count_calls] == ["<http://ex/anchor>"]


def test_the_mid_join_prune_weighs_only_the_patterns_that_have_a_count(tmp_path):
    """The conjunct over ?s and ?name is ready once name is joined, while tag
    remains to join. Tag is next to the one-row anchor, so it is not counted
    when planned and has no estimate to weigh."""
    graph = indexed_graph_of(tmp_path)
    register_sparql_pushdown()
    query = """SELECT ?s ?name ?tag WHERE {
        ?s <http://ex/anchor> "yes" . ?s <http://ex/name> ?name . ?s <http://ex/tag> ?tag
        FILTER(?s != ?name) }"""
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert len(with_pushdown) == 1
    assert with_pushdown == without_pushdown


def test_a_capped_count_is_not_remembered_as_exact(tmp_path):
    """A capped answer is a lower bound: it is never shared with a pattern
    that needs the count itself."""
    graph = indexed_graph_of(tmp_path)
    pat = {"n3": [None, "<http://ex/name>", None, None], "varpos": {}, "unsatisfiable": False}
    counts: dict = {}
    assert pd._count_pattern(graph.store, pat, counts, limit=11) == 11
    assert counts == {}
    assert pd._count_pattern(graph.store, pat, counts) == 300
    assert pd._count_pattern(graph.store, pat, counts, limit=11) == 300
