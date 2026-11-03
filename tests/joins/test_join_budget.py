"""The pair budget (``SETTINGS.pair_budget``): a join step of a BGP whose two
sides' product passes the budget counts its pairs natively before it is
built, and a probe step stops once its rows pass a quarter of it. A batch
of probes is pulled one answer at a time and checked."""

import contextlib
import os
import subprocess
import sys
import weakref

import pytest
from rdflib import Variable
from tests.join_corpus import (
    _PREFIX,
    CHAIN,
    EX,
    FAN,
    STAR,
    TWIN,
    _fields,
    _traced_both_ways,
)
from tests.support import both_ways, native_spy, run, trace_events
from vortex_rdf import CancelToken

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    cancel_scope,
    register_sparql_pushdown,
)
from vortex_rdflib.open_file import OpenFile
from vortex_rdflib.settings import SETTINGS


def _budget_in_a_fresh_interpreter(value: str | None) -> subprocess.CompletedProcess:
    """``SETTINGS.pair_budget`` as a new interpreter reads its switches
    (``apply_switches``, which every store construction calls), with
    ``VORTEX_RDF_PAIR_BUDGET`` set to ``value`` (unset for ``None``)."""
    env = {key: item for key, item in os.environ.items() if key != "VORTEX_RDF_PAIR_BUDGET"}
    if value is not None:
        env["VORTEX_RDF_PAIR_BUDGET"] = value
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from vortex_rdflib import settings; settings.apply_switches(); "
            "print(settings.SETTINGS.pair_budget)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_budget_reads_its_environment_variable():
    """The budget is read with the other switches: an integer of at least 0,
    or ``1 << 24`` when the variable is unset; anything else is refused by
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
    counted only once the product passes the budget, and refused past it."""
    k = Variable("k")
    left = pd.relation.Relation.from_rows((k,), [(1,), (1,), (2,)])
    right = pd.relation.Relation.from_rows((k,), [(1,), (2,), (2,), (3,)])
    monkeypatch.setattr(SETTINGS, "pair_budget", 12)
    joined, pairs = pd.relation._hash_join(left, right)
    assert (pairs, joined.nrows) == (None, 4)
    monkeypatch.setattr(SETTINGS, "pair_budget", 11)
    assert pd.relation._hash_join(left, right)[1] == 4
    monkeypatch.setattr(SETTINGS, "pair_budget", 3)
    with pytest.raises(pd.relation.OverBudget) as over:
        pd.relation._hash_join(left, right)
    assert over.value.pairs == 4


def test_a_row_step_counts_its_first_shared_variable(monkeypatch):
    """Past the product bound a join of rows counts the pairs of its first
    shared variable's codes — with a second shared variable, an upper bound
    of the rows the join makes — and, sharing none, is the product."""
    k, x, y, z = (Variable(name) for name in "kxyz")
    left = pd.relation.Relation.from_rows((k, x), [(1, 5), (1, 6), (2, 7)])
    right = pd.relation.Relation.from_rows((k, x, y), [(1, 5, 0), (2, 9, 0), (3, 7, 0)])
    lone = pd.relation.Relation.from_rows((z,), [(1,), (2,)])
    assert pd.relation._hash_kind(left, right) == "hash_rows"
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    for side, pairs in ((left, 3), (lone, 6)):
        with pytest.raises(pd.relation.OverBudget) as over:
            pd.relation._hash_join(side, right)
        assert over.value.pairs == pairs
    monkeypatch.setattr(SETTINGS, "pair_budget", 3)
    joined, pairs = pd.relation._hash_join(left, right)
    assert (pairs, joined.rows) == (3, [(1, 5, 0)])
    monkeypatch.setattr(SETTINGS, "pair_budget", 6)
    assert pd.relation._hash_join(lone, right)[1] is None


def test_small_joins_make_no_count(graph, monkeypatch, capsys):
    """Every join step of this module's queries has a product far within the
    default budget: none counts its pairs, whatever its strategy."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 1 << 24)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    capsys.readouterr()
    for sparql in (STAR, CHAIN, FAN):
        run(graph, sparql)
    steps = _fields(trace_events(capsys.readouterr().err), "bgp_join_step_complete")
    assert {step["strategy"] for step in steps} == {"hash_columns", "hash_rows", "probe"}
    assert [step["pair_count"] for step in steps] == [None] * len(steps)


@pytest.mark.parametrize("budget", [20, 199])
def test_a_join_past_the_product_bound_is_counted_then_built(graph, monkeypatch, capsys, budget):
    """The star's step has a product of 200 and makes 20 pairs: a budget from
    the pairs to just under the product counts the step, which fits and is
    built."""
    monkeypatch.setattr(SETTINGS, "pair_budget", budget)
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
def test_a_probe_step_over_budget_discards_its_rows(file_graph, monkeypatch, capsys, traced):
    """The fan's nine keys are probed three to a batch, one row per probe:
    point lookups, which a file-backed store batches. A budget of 16 lets the
    probe step make 16 // 4 = 4 rows, which the fifth probe passes, mid-way
    through the second batch. The step stops there: its batch is released
    with its last probe never pulled, the third batch is never sent, and the
    rows it made are dropped. It is planned again as a keep-scan, whose rows
    are the answer, none twice; its statistics count the six probes sent."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 16)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(SETTINGS, "probe_batch", 3)
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: True)
    monkeypatch.setattr(SETTINGS, "cheap_batch_min", 1)
    watched = _WatchedBatches(file_graph.store._opened().native)
    file_graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    file_graph.store._opened().native = watched
    if traced:
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(file_graph, FAN)
    events = trace_events(capsys.readouterr().err)
    assert with_pushdown == without_pushdown
    assert len(set(with_pushdown)) == len(with_pushdown) == 9
    assert watched.pulls == [3, 2]
    assert [batch() for batch in watched.batches] == [None, None]
    if traced:
        sequence = [
            (event["event"], event.get("returned_rows"))
            for event in events
            if event["event"] == "bgp_over_budget"
            or event["event"] == "native_batch_complete"
            and event["operation"] == "match_codes_many"
        ]
        # The first batch pulled whole, the second released after two
        # answers, before the step is traced over budget.
        assert sequence == [
            ("native_batch_complete", 3),
            ("native_batch_complete", 2),
            ("bgp_over_budget", None),
        ]
        over = {
            "execution_index": 1,
            "original_pattern_index": 1,
            "strategy": "probe",
            "pair_count": None,
            "input_rows": 9,
            "estimated_rows": 9,
        }
        assert _fields(events, "bgp_over_budget") == [over]
        [step] = _fields(events, "bgp_join_step_complete")
        assert (step["strategy"], step["native_probe_call_count"], step["output_rows"]) == (
            "keep_scan",
            6,
            9,
        )
        [done] = _fields(events, "bgp_complete")
        assert done["native_probe_call_count"] == 6


class _ShortBatches:
    """A VortexRdfStore stand-in whose ``match_codes_many`` answers one probe
    fewer than it is given."""

    def __init__(self, inner):
        self._inner = inner

    def match_codes_many(self, probes, **options):
        return iter(list(self._inner.match_codes_many(probes, **options))[:-1])

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _scope(tokened: bool):
    """A ``cancel_scope`` with a fresh token, or no scope at all."""
    return cancel_scope(CancelToken()) if tokened else contextlib.nullcontext()


@pytest.mark.parametrize("tokened", [False, True], ids=["untokened", "tokened"])
def test_a_batch_short_of_answers_raises(graph, monkeypatch, tokened):
    """The answers of a batch are pulled one probe at a time, under a cancel
    token or without one: a batch that answers fewer probes than it was
    given raises at the first probe left without an answer rather than
    handing another probe's answer on."""
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: False)
    monkeypatch.setattr(SETTINGS, "scan_batch_min", 1)
    monkeypatch.setattr(SETTINGS, "memory_scan_batch_min", 1)
    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._opened().native = _ShortBatches(graph.store._opened().native)
    probes = [((None, f"<{EX}p>", None, None), {}), ((None, f"<{EX}q>", None, None), {})]
    with _scope(tokened):
        answers = iter(pd.probes._run_probes(graph.store, probes, count=False))
        assert len(next(answers)[0]) == 10
        with pytest.raises(ValueError, match="fewer than its 2 probes"):
            next(answers)


class _LongBatches:
    """A VortexRdfStore stand-in whose ``match_codes_many`` answers one probe
    more than it is given."""

    def __init__(self, inner):
        self._inner = inner

    def match_codes_many(self, probes, **options):
        answers = list(self._inner.match_codes_many(probes, **options))
        return iter(answers + answers[-1:])

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("tokened", [False, True], ids=["untokened", "tokened"])
def test_a_batch_past_its_probes_raises(graph, monkeypatch, tokened):
    """A batch that answers more probes than it was given, under a cancel
    token or without one, raises at its last probe rather than handing that
    probe an answer the batch may have misplaced."""
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: False)
    monkeypatch.setattr(SETTINGS, "scan_batch_min", 1)
    monkeypatch.setattr(SETTINGS, "memory_scan_batch_min", 1)
    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._opened().native = _LongBatches(graph.store._opened().native)
    probes = [((None, f"<{EX}p>", None, None), {}), ((None, f"<{EX}q>", None, None), {})]
    with _scope(tokened):
        answers = iter(pd.probes._run_probes(graph.store, probes, count=False))
        assert len(next(answers)[0]) == 10
        with pytest.raises(ValueError, match="more than its 2 probes"):
            next(answers)


def test_probe_batches_name_their_lookahead(file_graph, monkeypatch):
    """A batch of point lookups names no lookahead, so the binding's default
    applies: the star's ten `q` matches and the twin's ten existence counts,
    each binding its subject, batched on a file-backed store. Probes that
    scan run a scan per core, up to eight: the nine `ref` existence counts
    of an EXISTS, each binding only its object (a join keep-scans instead).
    The planning counts name none either."""
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(SETTINGS, "cheap_batch_min", 1)
    register_sparql_pushdown()
    spy = native_spy(file_graph)
    scans = min(os.cpu_count() or 1, 8)
    exists = _PREFIX + "SELECT ?k WHERE { :c0 :has ?k FILTER EXISTS { ?f :ref ?k } }"
    cases = {
        STAR: [("count_quads_many", None, 2), ("match_codes_many", None, 10)],
        TWIN: [("count_quads_many", None, 2), ("count_quads_many", None, 10)],
        exists: [("count_quads_many", scans, 9)],
    }
    for sparql, expected in cases.items():
        spy.calls.clear()
        spy.args.clear()
        with_pushdown, without_pushdown = both_ways(file_graph, sparql)
        assert with_pushdown == without_pushdown
        batches = [
            (name, kwargs.get("lookahead"), len(args[0]))
            for (name, kwargs), args in zip(spy.calls, spy.args, strict=True)
            if name.endswith("_many")
        ]
        assert batches == expected, sparql
