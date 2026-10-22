"""The pair budget (``_PAIR_BUDGET``): a join step of a BGP whose two sides'
product passes the budget counts its pairs natively before it is built. A
last step over the budget that shares one variable streams (tier 1): the
smaller side's key column is grouped and the other side is fed through it a
piece at a time, whatever consumes the rows. Any other step over the budget
is not built — the block goes back to rdflib. Either way the answer is
rdflib's."""

import os
import subprocess
import sys
import weakref

import pytest
from rdflib import Graph, Variable
from rdflib.plugins.sparql.processor import SPARQLProcessor
from vortex_rdf import U64Column, serialize_rdf

import vortex_rdflib.pushdown as pd
from test_native_primitives import _spy
from test_pushdown import _trace_events, both_ways, run, run_ordered
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown, unregister_sparql_pushdown

EX = "http://ex/"
_PREFIX = f"PREFIX : <{EX}> "

#: Ten subjects with one `p` and two `q` each: the `?s` step joins 10 rows
#: with 20, a product of 200 that makes 20 pairs.
STAR = _PREFIX + "SELECT ?s ?x ?y WHERE { ?s :p ?x . ?s :q ?y }"
#: The one anchor is probed into its two `r` keys (of 122 `r` rows); the `?k`
#: step then hash-joins those 2 rows with the 150 `t` rows, a product of 300
#: that makes 20 pairs.
CHAIN = _PREFIX + 'SELECT ?s ?k ?z WHERE { ?s :anchor "yes" . ?s :r ?k . ?k :t ?z }'
#: `c0` has nine keys, each referenced by one subject.
FAN = _PREFIX + "SELECT ?k ?f WHERE { :c0 :has ?k . ?f :ref ?k }"
#: The star's seed joined on `?s` and `?x` at once: its ten rows against the
#: 20 `pq` rows, counted on `?s`, the first shared variable, as 20 pairs.
TWIN = _PREFIX + "SELECT ?s ?x WHERE { ?s :p ?x . ?s :pq ?x }"
#: The chain's anchor probed into its two keys, then `?k :owner ?s` (170
#: rows, more than `r`'s 122, so joined after it) on both shared variables:
#: ten `owner` rows per key, counted on `?k` as 20 pairs.
LOOP = _PREFIX + 'SELECT ?s ?k WHERE { ?s :anchor "yes" . ?s :r ?k . ?k :owner ?s }'
#: Ten `ga` rows, two per key, against the 20 `gb` rows, four per key: 40
#: pairs, the relation the smaller side.
WIDE = _PREFIX + "SELECT ?g ?k ?z WHERE { ?g :ga ?k . ?k :gb ?z }"
#: Five keys fanned out into 50 rows over ten `pn` keys, then the 20 `fc`
#: rows of those keys: 100 pairs, the pattern the smaller side.
FANNED = _PREFIX + "SELECT ?m ?n ?z WHERE { :fa0 :fa ?m . ?m :fb ?n . ?n :fc ?z }"
#: Three `u` rows on one key against its four `w` rows.
ONE_KEY = _PREFIX + "SELECT ?h ?z WHERE { ?h :u ?k . ?k :w ?z }"
#: Four `ra` rows on two keys against `?k :loop ?k`: of the 110 `loop` rows,
#: the ten that loop a key onto itself pass, and 4 pairs are made.
SELF_LOOP = _PREFIX + "SELECT ?s ?k WHERE { ?s :ra ?k . ?k :loop ?k }"

#: The fields every trace event carries; the rest are the event's own.
_ENVELOPE = ("schema", "event", "sequence", "query_id")


def _statements() -> str:
    lines = []
    for i in range(10):
        lines.append(f"<{EX}s{i}> <{EX}p> <{EX}x{i}> .")
        lines += [f"<{EX}s{i}> <{EX}q> <{EX}y{i}-{j}> ." for j in range(2)]
    lines.append(f'<{EX}a0> <{EX}anchor> "yes" .')
    lines += [f"<{EX}a0> <{EX}r> <{EX}k{i}> ." for i in range(2)]
    lines += [f"<{EX}b{i}> <{EX}r> <{EX}k{i + 2}> ." for i in range(120)]
    lines += [f"<{EX}k{i}> <{EX}t> <{EX}z{i}-{j}> ." for i in range(2) for j in range(10)]
    lines += [f"<{EX}k{i + 2}> <{EX}t> <{EX}w{i}> ." for i in range(130)]
    lines += [f"<{EX}c0> <{EX}has> <{EX}m{i}> ." for i in range(9)]
    lines += [f"<{EX}f{i}> <{EX}ref> <{EX}m{i}> ." for i in range(9)]
    lines += [f"<{EX}s{i}> <{EX}pq> <{EX}{o}{i}> ." for i in range(10) for o in ("x", "v")]
    owners = ["a0", *(f"o{j}" for j in range(9))]
    lines += [f"<{EX}k{i}> <{EX}owner> <{EX}{o}> ." for i in range(2) for o in owners]
    lines += [f"<{EX}n{i}> <{EX}owner> <{EX}a0> ." for i in range(150)]
    lines += [f"<{EX}g{i}> <{EX}ga> <{EX}gk{i // 2}> ." for i in range(10)]
    lines += [f"<{EX}gk{k}> <{EX}gb> <{EX}gz{k}-{t}> ." for k in range(5) for t in range(4)]
    lines += [f"<{EX}fa0> <{EX}fa> <{EX}pm{i}> ." for i in range(5)]
    lines += [f"<{EX}pm{i}> <{EX}fb> <{EX}pn{j}> ." for i in range(5) for j in range(10)]
    lines += [f"<{EX}pn{j}> <{EX}fc> <{EX}pz{j}-{t}> ." for j in range(10) for t in range(2)]
    lines += [f"<{EX}h{i}> <{EX}u> <{EX}hk> ." for i in range(3)]
    lines += [f"<{EX}hk> <{EX}w> <{EX}hz{i}> ." for i in range(4)]
    lines += [f"<{EX}rs{i}> <{EX}ra> <{EX}lk{i % 2}> ." for i in range(4)]
    lines += [f"<{EX}lk{j}> <{EX}loop> <{EX}lk{j}> ." for j in range(10)]
    lines += [f"<{EX}lk{j}> <{EX}loop> <{EX}lz{j}-{t}> ." for j in range(10) for t in range(10)]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def joins_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("join-budget")
    nt = d / "joins.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "joins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(joins_vortex, request):
    return Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=request.param))


def _traced_both_ways(graph, monkeypatch, capsys, sparql):
    """Both answers to ``sparql`` (see ``both_ways``) and the trace events of
    the pushdown's run."""
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    answers = both_ways(graph, sparql)
    return answers, _trace_events(capsys.readouterr().err)


def _fields(events, name) -> list[dict]:
    """The own fields of each ``name`` event."""
    return [
        {key: value for key, value in event.items() if key not in _ENVELOPE}
        for event in events
        if event["event"] == name
    ]


def _budget_in_a_fresh_interpreter(value: str | None) -> subprocess.CompletedProcess:
    """``_PAIR_BUDGET`` as a new interpreter imports it, with
    ``VORTEX_RDF_PAIR_BUDGET`` set to ``value`` (unset for ``None``)."""
    env = {key: item for key, item in os.environ.items() if key != "VORTEX_RDF_PAIR_BUDGET"}
    if value is not None:
        env["VORTEX_RDF_PAIR_BUDGET"] = value
    return subprocess.run(
        [sys.executable, "-c", "import vortex_rdflib.pushdown as p; print(p._PAIR_BUDGET)"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_budget_reads_its_environment_variable():
    """The budget is read once, at import: an integer of at least 0, or
    ``1 << 24`` when the variable is unset; anything else is refused by
    name."""
    done = _budget_in_a_fresh_interpreter("123")
    assert done.stdout == "123\n", done.stderr
    done = _budget_in_a_fresh_interpreter(None)
    assert done.stdout == f"{1 << 24}\n", done.stderr
    for value in ("abc", "-1"):
        done = _budget_in_a_fresh_interpreter(value)
        assert done.returncode != 0
        assert "ValueError: VORTEX_RDF_PAIR_BUDGET" in done.stderr, done.stderr


def test_the_pair_count_skips_a_product_within_the_budget(monkeypatch):
    """Keys 1, 1, 2 against 1, 2, 2, 3: a product of 12 that makes 4 pairs,
    counted only once the product passes the budget."""
    left, right = U64Column([1, 1, 2]), U64Column([1, 2, 2, 3])
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 12)
    assert pd._pair_count(left, right) is None
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 11)
    assert pd._pair_count(left, right) == 4


def test_a_row_step_counts_its_first_shared_variable(monkeypatch):
    """Past the product bound a row step counts the pairs of its first
    shared variable's codes — with a second shared variable, an upper bound
    of the rows the join makes — and, sharing none, is the product."""
    k, x, y, z = (Variable(name) for name in "kxyz")
    rows_a = [(1, 5), (1, 6), (2, 7)]
    rows_b = [(1, 5, 0), (2, 9, 0), (3, 7, 0)]
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    assert pd._row_step_pairs((k, x), rows_a, (k, x, y), rows_b) == 3
    assert len(pd._join((k, x), rows_a, (k, x, y), rows_b)[1]) == 1
    assert pd._row_step_pairs((z,), [(1,), (2,)], (k, x, y), rows_b) == 6
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 6)
    assert pd._row_step_pairs((z,), [(1,), (2,)], (k, x, y), rows_b) is None


def test_small_joins_make_no_count(graph, monkeypatch, capsys):
    """Every join step of this module's queries has a product far within the
    default budget: none counts its pairs, whatever its strategy."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 1 << 24)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    capsys.readouterr()
    for sparql in (STAR, CHAIN, FAN):
        run(graph, sparql)
    steps = _fields(_trace_events(capsys.readouterr().err), "bgp_join_step_complete")
    assert {step["strategy"] for step in steps} == {"hash_columns", "hash_rows", "probe"}
    assert [step["pair_count"] for step in steps] == [None] * len(steps)


@pytest.mark.parametrize("budget", [20, 199])
def test_a_join_past_the_product_bound_is_counted_then_built(graph, monkeypatch, capsys, budget):
    """The star's step has a product of 200 and makes 20 pairs: a budget from
    the pairs to just under the product counts the step, which fits and is
    built."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", budget)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, STAR)
    assert with_pushdown == without_pushdown
    [step] = _fields(events, "bgp_join_step_complete")
    assert (step["strategy"], step["input_rows"], step["pair_count"], step["output_rows"]) == (
        "hash_columns",
        10,
        20,
        20,
    )
    assert _fields(events, "bgp_over_budget") == []
    assert _fields(events, "eval_part_fallback") == []


def test_an_over_budget_column_join_hands_the_node_back(graph, monkeypatch, capsys):
    """The seed's columns meet a last step sharing two variables, which no
    tier streams: one pair over the budget it is not built, it is traced with
    the count of its first shared variable, and rdflib answers the block."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 19)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, TWIN)
    assert with_pushdown == without_pushdown
    over = _fields(events, "bgp_over_budget")
    assert over
    assert over == [
        {
            "execution_index": 1,
            "original_pattern_index": 1,
            "strategy": "hash_rows",
            "pair_count": 20,
            "input_rows": 10,
            "estimated_rows": 20,
        }
    ] * len(over)
    assert _fields(events, "eval_part_fallback")
    assert _fields(events, "bgp_join_step_complete") == []


def test_an_over_budget_row_join_hands_the_node_back(graph, monkeypatch, capsys):
    """The chain's anchor is probed first, which leaves a rows body; its last
    step shares two variables, which no tier streams, and is counted over the
    first one's codes copied into native columns — 20 pairs, over a budget of
    10 — and rdflib answers the block. The probe step keeps to its tally: 2
    rows of the 10 // 4 it may make."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 10)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, LOOP)
    assert with_pushdown == without_pushdown
    over = _fields(events, "bgp_over_budget")
    assert over
    assert over == [
        {
            "execution_index": 2,
            "original_pattern_index": 1,
            "strategy": "hash_rows",
            "pair_count": 20,
            "input_rows": 2,
            "estimated_rows": 170,
        }
    ] * len(over)
    steps = _fields(events, "bgp_join_step_complete")
    assert [(s["strategy"], s["output_rows"], s["pair_count"]) for s in steps] == [
        ("probe", 2, None)
    ] * len(over)
    assert _fields(events, "eval_part_fallback")


class _Batch:
    """The answers of one ``match_codes_many`` call, counting in
    ``pulls[slot]`` the answers pulled."""

    def __init__(self, inner, pulls: list[int]):
        self._inner = inner
        self._pulls = pulls
        self._slot = len(pulls)
        pulls.append(0)

    def __iter__(self):
        return self

    def __next__(self):
        answer = next(self._inner)
        self._pulls[self._slot] += 1
        return answer

    def __len__(self):
        return len(self._inner)


class _WatchedBatches:
    """A VortexRdfStore stand-in whose ``match_codes_many`` answers count
    their pulls and are watched by weak references, so a test sees whether
    they were released."""

    def __init__(self, inner):
        self._inner = inner
        self.pulls: list[int] = []
        self.batches: list[weakref.ref] = []

    def match_codes_many(self, probes, **options):
        batch = _Batch(self._inner.match_codes_many(probes, **options), self.pulls)
        self.batches.append(weakref.ref(batch))
        return batch

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("traced", [False, True], ids=["untraced", "traced"])
def test_a_probe_step_over_budget_discards_its_rows(graph, monkeypatch, capsys, traced):
    """The fan's nine keys are probed three to a batch, one row per probe. A
    budget of 16 lets the probe step make 16 // 4 = 4 rows, which the fifth
    probe passes, mid-way through the second batch. The step stops there: its
    batch is released with its last probe never pulled, the rows it made are
    dropped, and rdflib answers the block with no row of them."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 16)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_PROBE_BATCH", 3)
    watched = _WatchedBatches(graph.store._store())
    graph.store._native = watched
    if traced:
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(graph, FAN)
    events = _trace_events(capsys.readouterr().err)
    assert with_pushdown == without_pushdown
    assert len(set(with_pushdown)) == len(with_pushdown) == 9
    attempts = len(watched.pulls) // 2
    assert attempts
    assert watched.pulls == [3, 2] * attempts
    assert [batch() for batch in watched.batches] == [None] * len(watched.batches)
    if traced:
        sequence = [
            (event["event"], event.get("returned_rows"))
            for event in events
            if event["event"] == "bgp_over_budget"
            or event["event"] == "native_batch_complete"
            and event["operation"] == "match_codes_many"
        ]
        # Per attempt: the first batch pulled whole, the second released
        # after two answers, before the step is traced over budget.
        attempt = [
            ("native_batch_complete", 3),
            ("native_batch_complete", 2),
            ("bgp_over_budget", None),
        ]
        assert sequence == attempt * attempts
        over = {
            "execution_index": 1,
            "original_pattern_index": 1,
            "strategy": "probe",
            "pair_count": None,
            "input_rows": 9,
            "estimated_rows": 9,
        }
        assert _fields(events, "bgp_over_budget") == [over] * attempts


class _ShortBatches:
    """A VortexRdfStore stand-in whose ``match_codes_many`` answers one probe
    fewer than it is given."""

    def __init__(self, inner):
        self._inner = inner

    def match_codes_many(self, probes, **options):
        return iter(list(self._inner.match_codes_many(probes, **options))[:-1])

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_batch_short_of_answers_raises(graph, monkeypatch):
    """The answers of a batch are pulled one probe at a time: a batch that
    answers fewer probes than it was given raises at the first probe left
    without an answer rather than handing another probe's answer on."""
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: False)
    monkeypatch.setattr(pd, "_SCAN_BATCH_MIN", 1)
    monkeypatch.setattr(pd, "_MEMORY_SCAN_BATCH_MIN", 1)
    graph.store._native = _ShortBatches(graph.store._store())
    probes = [((None, f"<{EX}p>", None, None), {}), ((None, f"<{EX}q>", None, None), {})]
    answers = iter(pd._run_probes(graph.store, probes, count=False))
    assert len(next(answers)[0]) == 10
    with pytest.raises(ValueError, match="fewer than its 2 probes"):
        next(answers)


def _streams(events) -> list[dict]:
    """The join steps that streamed (tier 1)."""
    return [
        step for step in _fields(events, "bgp_join_step_complete") if step["strategy"] == "stream"
    ]


def test_an_over_budget_last_step_streams(graph, monkeypatch, capsys):
    """One pair over the budget, the star's last step streams: the ten seed
    rows are grouped and the 20 `q` rows fed four at a time, a piece each."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 19)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 4)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", budget)
    grouped = []
    gather_pieces = pd._gather_pieces

    def watched(groups, fed, key, columns, order):
        grouped.append((len(groups), {len(column) for column in columns}))
        return gather_pieces(groups, fed, key, columns, order)

    monkeypatch.setattr(pd, "_gather_pieces", watched)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 15)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 8)
    register_sparql_pushdown()
    spy = _spy(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, STAR)
    events = _trace_events(capsys.readouterr().err)
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


def test_a_fed_pattern_with_a_repeated_variable_is_sliced(graph, monkeypatch, capsys):
    """`?k :loop ?k` counts 110 rows, more than a budget of 3 and than the
    four seed rows, but its repeated variable is a test across two of a
    row's columns, which chunks do not carry: the pattern is matched whole,
    its ten passing rows are fed in slices, and its answers are rdflib's."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 3)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 1)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 19)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 4)
    unregister_sparql_pushdown()
    try:
        every = run(graph, STAR)
    finally:
        register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, STAR + " LIMIT 2")
    events = _trace_events(capsys.readouterr().err)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 2)
    monkeypatch.setattr(pd, "_CHUNK_START", 1)
    if traced:
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    spy = _spy(graph)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 4)
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
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 2)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(graph, _PREFIX + sparql, runner)
    events = _trace_events(capsys.readouterr().err)
    assert with_pushdown == without_pushdown
    assert [step["strategy"] for step in _fields(events, "bgp_join_step_complete")] == ["stream"]
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["pieces"] > 1
    fallbacks = [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert fallbacks == kept_by_rdflib


def test_an_ask_gathers_one_piece(graph, monkeypatch, capsys):
    """ASK pulls one row of the streamed star, every fed row of which
    matches: one piece is gathered, and the stream is closed after it."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    monkeypatch.setattr(pd, "_PIECE_ROWS", 2)
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
    rel = pd.Relation((x, y), None, None, 0, pieces=pieces)
    rows = pd._rows_of(rel)
    assert rows == [(1, 3), (2, 4), (5, 6)]
    assert pd._rows_of(rel) is rows
    assert (rel.rows, rel.cols, rel.pieces, rel.nrows) == (rows, None, None, 3)


def test_a_memoized_match_is_sliced(graph):
    """A last step whose unread pattern another pattern of the block already
    matched feeds that match in slices rather than reading it again in
    chunks; its rows are the one-shot join's."""
    s, x, y = (Variable(name) for name in "sxy")
    native = graph.store._store()
    seed = native.match_codes(None, f"<{EX}p>", None, None)
    n3 = [None, f"<{EX}q>", None, None]
    match = native.match_codes(*n3)
    pat = {"n3": n3, "varpos": {s: [0], y: [2]}, "unsatisfiable": False, "estimate": 20}
    spy = _spy(graph)
    columns = (seed[0], seed[2])
    schema, pieces = pd._streamed_step(graph.store, (s, x), columns, pat, {tuple(n3): match})
    rows = pd._rows_of(pd.Relation(schema, None, None, 0, pieces=pieces))
    assert spy.names() == []
    views = tuple(memoryview(column).cast("Q") for column in columns)
    joined_schema, joined = pd._join_columns(
        (s, x), views, (s, y), (memoryview(match[0]).cast("Q"), memoryview(match[2]).cast("Q"))
    )
    assert schema == joined_schema == (s, x, y)
    assert sorted(rows) == sorted(pd._cols_to_rows(joined))
