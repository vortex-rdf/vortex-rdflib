"""The pair budget (``_PAIR_BUDGET``): a join step of a BGP whose two sides'
product passes the budget counts its pairs natively before it is built, and
a step over the budget is not built — the block goes back to rdflib, whose
answer the pushdown's must equal."""

import os
import subprocess
import sys
import weakref

import pytest
from rdflib import Graph, Variable
from vortex_rdf import U64Column, serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import _trace_events, both_ways, run
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

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
    """One pair over the budget, the star's column step is not built: it is
    traced with its count, and rdflib answers the block."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 19)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, STAR)
    assert with_pushdown == without_pushdown
    over = _fields(events, "bgp_over_budget")
    assert over
    assert over == [
        {
            "execution_index": 1,
            "original_pattern_index": 1,
            "strategy": "hash_columns",
            "pair_count": 20,
            "input_rows": 10,
            "estimated_rows": 20,
        }
    ] * len(over)
    assert _fields(events, "eval_part_fallback")
    assert _fields(events, "bgp_join_step_complete") == []


def test_an_over_budget_row_join_hands_the_node_back(graph, monkeypatch, capsys):
    """The chain's anchor is probed first, which leaves a rows body; its hash
    step is then counted over the key codes copied into native columns — 20
    pairs, over a budget of 10 — and rdflib answers the block. The probe step
    keeps to its tally: 2 rows of the 10 // 4 it may make."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 10)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, CHAIN)
    assert with_pushdown == without_pushdown
    over = _fields(events, "bgp_over_budget")
    assert over
    assert over == [
        {
            "execution_index": 2,
            "original_pattern_index": 2,
            "strategy": "hash_rows",
            "pair_count": 20,
            "input_rows": 2,
            "estimated_rows": 150,
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
