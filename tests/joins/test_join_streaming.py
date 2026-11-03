"""Tier 1: a last join step over the pair budget that shares one variable
streams — the smaller side's key column is grouped and the other side fed
through it a piece at a time, whatever consumes the rows."""

import pytest
from rdflib import Variable
from rdflib.plugins.sparql.processor import SPARQLProcessor
from tests.join_corpus import (
    _PREFIX,
    CHAIN,
    EX,
    FANNED,
    ONE_KEY,
    SELF_LOOP,
    STAR,
    WIDE,
    _fields,
    _streams,
    _traced_both_ways,
)
from tests.support import both_ways, native_spy, run, run_ordered, trace_events
from vortex_rdf import U64Column

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def test_an_over_budget_last_step_streams(graph, monkeypatch, capsys):
    """The star's `q` pattern counts 20 rows, more than a budget of 19 and
    than the ten seed rows, so its last step streams without reading it
    whole: the ten seed rows are grouped and the 20 `q` rows fed four at a
    time, a piece each."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 19)
    monkeypatch.setattr(SETTINGS, "piece_rows", 4)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, STAR)
    assert with_pushdown == without_pushdown
    [step] = _fields(events, "bgp_join_step_complete")
    assert (step["strategy"], step["input_rows"], step["output_rows"]) == ("stream", 10, None)
    [stream] = _fields(events, "bgp_stream_complete")
    assert (stream["pieces"], stream["output_rows"]) == (5, 20)
    assert _fields(events, "eval_part_fallback") == []


@pytest.mark.parametrize(
    ("sparql", "budget", "relation_rows", "grouped_rows"),
    [(WIDE, 30, 10, 10), (FANNED, 60, 50, 20)],
    ids=["relation-smaller", "pattern-smaller"],
)
def test_groups_go_on_the_smaller_side(
    graph, monkeypatch, capsys, sparql, budget, relation_rows, grouped_rows
):
    """The side with fewer rows is grouped, all its columns gathered at the
    grouped rows, and the other side is fed in slices: the ten-row relation
    against a pattern of 20, or a pattern of 20 rows against the 50 rows the
    fan-out made of the relation."""
    monkeypatch.setattr(SETTINGS, "pair_budget", budget)
    grouped = []
    gather_pieces = pd.streaming._gather_pieces

    def watched(groups, fed, key, columns, order):
        grouped.append((len(groups), {len(column) for column in columns}))
        return gather_pieces(groups, fed, key, columns, order)

    monkeypatch.setattr(pd.streaming, "_gather_pieces", watched)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    [step] = _streams(events)
    assert (step["feed"], step["input_rows"]) == ("slices", relation_rows)
    assert grouped == [(grouped_rows, {grouped_rows})]


def test_a_large_unread_pattern_is_fed_in_chunks(graph, monkeypatch, capsys):
    """The star's `q` pattern counts 20 rows, more than a budget of 15 and
    than the ten seed rows: the last step reads it through
    ``match_code_chunks``, a piece at a time, and never matches it whole."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 15)
    monkeypatch.setattr(SETTINGS, "piece_rows", 8)
    register_sparql_pushdown()
    spy = native_spy(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, STAR)
    events = trace_events(capsys.readouterr().err)
    reads = [
        (name, kwargs)
        for (name, kwargs), args in zip(spy.calls, spy.args, strict=True)
        if name.startswith("match") and args[1] == f"<{EX}q>"
    ]
    assert reads == [("match_code_chunks", {"columns": ["s", "o"], "batch_rows": 8})]
    assert spy.pulls == [3]
    [step] = _streams(events)
    assert (step["feed"], step["pair_count"], step["estimated_rows"]) == ("chunks", None, 20)
    assert _fields(events, "bgp_over_budget") == []
    unregister_sparql_pushdown()
    try:
        assert rows == run(graph, STAR)
    finally:
        register_sparql_pushdown()


def test_a_probed_relation_streams_as_columns(graph, monkeypatch, capsys):
    """The chain's anchor is probed into its two `r` keys, which leaves the
    relation as rows. The last step's `t` pattern counts 150 rows, more than a
    budget of 19 and than those two rows, so it streams from chunks: the two
    rows are copied into native columns to be grouped."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 19)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, CHAIN)
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 20
    names = ("strategy", "feed", "input_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [("probe", None, 1), ("stream", "chunks", 2)]
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["output_rows"] == 20


def test_a_fed_pattern_with_a_repeated_variable_is_sliced(graph, monkeypatch, capsys):
    """`?k :loop ?k` counts 110 rows, more than a budget of 3 and than the
    four seed rows, but its repeated variable is a test across two of a
    row's columns, which chunks do not carry: the pattern is matched whole,
    its ten passing rows are fed in slices, and its answers are rdflib's."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 3)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, SELF_LOOP
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 4
    [step] = _streams(events)
    assert (step["feed"], step["pair_count"], step["estimated_rows"]) == ("slices", 4, 110)
    assert [event for event in events if event["event"] == "native_chunks_complete"] == []


def test_a_row_over_budget_alone_is_gathered_whole(graph, monkeypatch, capsys):
    """At a budget of 1 each fed `w` row matches the three grouped `u` rows of
    its key: halving stops at the single row, which is gathered whole, so the
    step ends with a piece per fed row."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 1)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, ONE_KEY
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 12
    [stream] = _fields(events, "bgp_stream_complete")
    assert (stream["pieces"], stream["output_rows"]) == (4, 12)


def test_limit_stops_a_streamed_join(graph, monkeypatch, capsys):
    """A LIMIT stops pulling pieces: its two rows come from the first piece
    of four, and no other piece is gathered."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 19)
    monkeypatch.setattr(SETTINGS, "piece_rows", 4)
    unregister_sparql_pushdown()
    try:
        every = run(graph, STAR)
    finally:
        register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, STAR + " LIMIT 2")
    events = trace_events(capsys.readouterr().err)
    assert len(rows) == 2
    assert set(rows) <= set(every)
    [step] = _streams(events)
    assert step["feed"] == "chunks"
    [stream] = _fields(events, "bgp_stream_complete")
    assert (stream["pieces"], stream["output_rows"]) == (1, 4)


@pytest.mark.parametrize("traced", [False, True], ids=["untraced", "traced"])
def test_an_early_stop_releases_the_feed(graph, monkeypatch, traced):
    """A result closed after its first row drops the chunk iterator its
    streamed step reads, with nine of the `q` pattern's ten chunks never
    pulled."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setattr(SETTINGS, "piece_rows", 2)
    monkeypatch.setattr(SETTINGS, "chunk_start", 1)
    if traced:
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    spy = native_spy(graph)
    rows = SPARQLProcessor(graph).query(STAR)["bindings"]
    assert next(rows)
    assert len(spy.feeds) == 1
    assert spy.feeds[0]() is not None
    rows.close()
    assert spy.feeds[0]() is None
    assert spy.pulls == [1]


def test_a_filter_residual_rides_the_pieces(graph, monkeypatch, capsys):
    """A FILTER over two of a streamed step's variables is a predicate of the
    streamed rows: the Filter's relation is still the stream, and its rows
    are tested as they are pulled."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setattr(SETTINGS, "piece_rows", 4)
    sparql = (
        _PREFIX
        + """SELECT ?s ?x ?y WHERE { ?s :p ?x . ?s :q ?y
        FILTER(strends(str(?y), "-0") || strends(str(?x), "1")) }"""
    )
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 11
    assert len(_streams(events)) == 1
    filtered = [
        event["rows"]
        for event in _fields(events, "block_operator_complete")
        if event["operator"] == "Filter"
    ]
    assert filtered == [None]
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["output_rows"] == 20


_STAR_BODY = "?s :p ?x . ?s :q ?y"

#: Row consumers above a streamed star, by id: the query, how its answer is
#: compared, and the nodes left to rdflib (a head over an aggregate's
#: ``Extend`` is rdflib's).
DRAINED = {
    "order": (f"SELECT ?s ?y WHERE {{ {_STAR_BODY} }} ORDER BY ?y", run_ordered, []),
    "distinct": (f"SELECT DISTINCT ?s WHERE {{ {_STAR_BODY} }}", run, []),
    "count": (f"SELECT (COUNT(*) AS ?n) WHERE {{ {_STAR_BODY} }}", run, ["Project"]),
    "optional": (f"SELECT ?s ?y ?w WHERE {{ {_STAR_BODY} OPTIONAL {{ ?s :pq ?w }} }}", run, []),
    "minus": (f"SELECT ?s ?y WHERE {{ {_STAR_BODY} MINUS {{ ?s :q :y3-0 }} }}", run, []),
    "exists": (
        f"SELECT ?s ?y WHERE {{ {_STAR_BODY} FILTER NOT EXISTS {{ ?s :q :y3-0 }} }}",
        run,
        [],
    ),
}


@pytest.mark.parametrize("consumer", list(DRAINED))
def test_over_budget_under_a_row_consumer_drains_the_pieces(graph, monkeypatch, capsys, consumer):
    """Under ORDER BY, DISTINCT, COUNT, OPTIONAL, MINUS and EXISTS the star's
    over-budget last step still streams: the consumer takes its rows piece by
    piece, and nothing of the block is handed back."""
    sparql, runner, kept_by_rdflib = DRAINED[consumer]
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setattr(SETTINGS, "piece_rows", 2)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(graph, _PREFIX + sparql, runner)
    events = trace_events(capsys.readouterr().err)
    assert with_pushdown == without_pushdown
    assert [step["strategy"] for step in _fields(events, "bgp_join_step_complete")] == ["stream"]
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["pieces"] > 1
    fallbacks = [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert fallbacks == kept_by_rdflib


def test_an_ask_gathers_one_piece(graph, monkeypatch, capsys):
    """ASK pulls one row of the streamed star, every fed row of which
    matches: one piece is gathered, and the stream is closed after it."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setattr(SETTINGS, "piece_rows", 2)
    sparql = _PREFIX + f"ASK {{ {_STAR_BODY} }}"
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown is without_pushdown is True
    assert len(_streams(events)) == 1
    [stream] = _fields(events, "bgp_stream_complete")
    assert (stream["pieces"], stream["output_rows"]) == (1, 1)


def test_rows_of_drains_once():
    """A streamed body drained for a consumer that holds rows keeps them in
    its place: a second call returns the same list, the pieces long gone."""
    x, y = Variable("x"), Variable("y")

    def piece(*columns):
        return tuple(memoryview(U64Column(column)).cast("Q") for column in columns)

    pieces = iter([piece([1, 2], [3, 4]), piece([5], [6])])
    rel = pd.relation.Relation((x, y), None, None, 0, pieces=pieces)
    rows = pd.relation._rows_of(rel)
    assert rows == [(1, 3), (2, 4), (5, 6)]
    assert pd.relation._rows_of(rel) is rows
    assert (rel.rows, rel.cols, rel.pieces, rel.nrows) == (rows, None, None, 3)


def test_a_memoized_match_is_sliced(graph):
    """A last step whose unread pattern another pattern of the block already
    matched feeds that match in slices rather than reading it again in
    chunks; its rows are the one-shot join's."""
    s, x, y = (Variable(name) for name in "sxy")
    native = graph.store._opened().native
    seed = native.match_codes(None, f"<{EX}p>", None, None)
    n3 = (None, f"<{EX}q>", None, None)
    match = native.match_codes(*n3)
    pat = pd.matching.Pattern(n3, {s: [0], y: [2]}, estimate=20)
    spy = native_spy(graph)
    columns = (seed[0], seed[2])
    relation = pd.relation.Relation.from_cols((s, x), columns)
    schema, pieces = pd.streaming._streamed_step(graph.store, relation, pat, {tuple(n3): match})
    rows = pd.relation._rows_of(pd.relation.Relation(schema, None, None, 0, pieces=pieces))
    assert spy.names() == []
    views = tuple(memoryview(column).cast("Q") for column in columns)
    joined_schema, joined, _ = pd.relation._join_columns(
        (s, x), views, (s, y), (memoryview(match[0]).cast("Q"), memoryview(match[2]).cast("Q"))
    )
    assert schema == joined_schema == (s, x, y)
    assert sorted(rows) == sorted(pd.relation._cols_to_rows(joined))
