"""The pair budget (``_PAIR_BUDGET``): a join step of a BGP whose two sides'
product passes the budget counts its pairs natively before it is built. A
last step over the budget that shares one variable streams (tier 1): the
smaller side's key column is grouped and the other side is fed through it a
piece at a time, whatever consumes the rows. Any other step over the budget
is not built: another pattern that provably fits joins first (tier 2), and
when none does, rdflib finishes the remaining patterns per row of the
relation so far (tier 3) where the relation goes straight to the emitter —
otherwise the block goes back to rdflib. Either way the answer is rdflib's.

A step that would probe its pattern per row with probes that each scan the
store matches it once instead, narrowed to the relation's codes (a
keep-scan), and joins that match as a hash step does, under the same
budget."""

import os
import subprocess
import sys
import weakref

import pytest
from rdflib import Dataset, Graph, URIRef, Variable
from rdflib.plugins.sparql.processor import SPARQLProcessor
from vortex_rdf import U64Column, serialize_rdf

import vortex_rdflib.pushdown as pd
from test_native_primitives import _spy
from test_pushdown import (
    _EX,
    PROBE_MULTIPLICITY,
    PROBE_QUADS,
    _trace_events,
    both_ways,
    run,
    run_ordered,
)
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
#: 120 `ta` rows seed the relation. Both `tl` patterns count all 245 `tl`
#: rows, and the `?s` one comes first: its step makes 240 pairs, two `tl`
#: rows per seed subject. The `?x` one reads the match that step left in the
#: memo, and its step makes 5, the five seed objects that are `tl` subjects.
#: Ten answers.
_TIER_BODY = "?s :ta ?x . ?s :tl ?c . ?x :tl ?d"
REORDER = _PREFIX + f"SELECT ?s ?x ?c ?d WHERE {{ {_TIER_BODY} }}"
#: 120 `sa` rows bind `?k` to one literal, so the FILTER is a prefix test of
#: `?c` alone for the pattern that binds it. `?c :sl ?s` (pattern 0) comes
#: first: its 120 `spc` rows that pass make 120 pairs. `?s :sl ?c` (pattern
#: 1) makes 2 of the seed rows, `sp0`'s two `sl` rows, and one of them fails
#: the test.
SPECIALIZED = (
    _PREFIX + "SELECT ?s ?k ?c WHERE { ?s :sa ?k . ?s :sl ?c . ?c :sl ?s "
    "FILTER(STRSTARTS(STR(?c), ?k)) }"
)
#: `c0`'s nine keys against every statement that has one as its object: their
#: nine `has` and nine `ref` statements. Counted at its step up to the nine
#: rows x 100 + 1, `?f ?p ?k` reaches that cap (the store holds 1,348
#: statements), so the step joins it per key — with probes that would each
#: scan the store, binding no subject on a store with no index.
SCANNED = _PREFIX + "SELECT ?k ?f ?p WHERE { :c0 :has ?k . ?f ?p ?k }"
#: The anchor against the 170 `owner` statements, 152 of which name it.
OWNED = _PREFIX + 'SELECT ?n WHERE { ?s :anchor "yes" . ?n :owner ?s }'
#: The chain's anchor probed into its two `r` keys, then every statement that
#: has one as its object: `?x ?p ?k` counts the store's 1,348 when planned,
#: past the two rows x 100, and is joined per key.
ANCHORED_SCAN = _PREFIX + 'SELECT ?s ?k ?x ?p WHERE { ?s :anchor "yes" . ?s :r ?k . ?x ?p ?k }'
#: The four `ra` rows hold their two keys twice each. Counted at its step up
#: to four rows x 100 + 1, `?x ?p ?k` reaches that cap; each key is the object
#: of its two `ra` statements and of its own `loop`, so the keep-scan reads
#: six rows, which make twelve pairs with the four.
KEYED_TWICE = _PREFIX + "SELECT ?r ?k ?x ?p WHERE { ?r :ra ?k . ?x ?p ?k }"
#: The same keys against their own loops: `?k :loop ?k` repeats `?k`.
LOOPED = _PREFIX + "SELECT ?r ?k WHERE { ?r :ra ?k . ?k :loop ?k }"

#: Quads for Review Focus 2's shapes beyond `PROBE_MULTIPLICITY`'s, added to
#: `PROBE_QUADS`, whose cases they leave alone: `?s :q ?v` binds `?v` to `x`
#: four times and to `y` once, and the `r` statements repeat a code across
#: their subject and object — `x`'s in two graphs, `y`'s in one, and `z`'s,
#: which the relation does not bind — or do not (`x :r :y`, `y :r :x`).
KEEP_SCAN_QUADS = """\
<http://ex/s5> <http://ex/q> <http://ex/y> <http://ex/g1> .
<http://ex/x> <http://ex/r> <http://ex/x> <http://ex/g1> .
<http://ex/x> <http://ex/r> <http://ex/x> <http://ex/g2> .
<http://ex/x> <http://ex/r> <http://ex/y> <http://ex/g1> .
<http://ex/y> <http://ex/r> <http://ex/x> <http://ex/g2> .
<http://ex/y> <http://ex/r> <http://ex/y> <http://ex/g3> .
<http://ex/z> <http://ex/r> <http://ex/z> <http://ex/g1> .
"""

#: Per shape, as `PROBE_MULTIPLICITY` has it: whether the default graph is the
#: union of the graphs, the query and the rows rdflib answers.
KEEP_SCAN_MULTIPLICITY = {
    # The shared `?v` sits at both positions of `?v :r ?v`, the pattern a
    # step keep-scans: of the five rows its keep selects, the three that
    # repeat a code join, `x`'s four rows with two quads, `y`'s one with one.
    "shared-twice": (True, _EX + "SELECT ?s ?v WHERE { ?s :q ?v . ?v :r ?v }", 9),
    # The FILTER's keep on `?s` is on the pattern before the keep-scan
    # narrows it to the relation's codes: `s1` joins 2 `q` quads with 3 `p`.
    "keep-before": (
        True,
        _EX + "SELECT ?s WHERE { ?s :q :x . ?s :p :o1 FILTER(?s IN (:s1, :s2, :s3)) }",
        6,
    ),
}

#: The shapes whose `p` or `r` pattern is a step of the BGP: forced to probe
#: with probes that scan, it keep-scans. The others join above a BGP, or
#: test an OPTIONAL or EXISTS, which keep their probes.
_KEEP_SCANNED = {
    "bgp-join",
    "graph-variable",
    "named-graph",
    "default-graph",
    "shared-twice",
    "keep-before",
}

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
    lines += _tier_statements(120, 5)
    lines += [f'<{EX}sp{i}> <{EX}sa> "{EX}spc" .' for i in range(120)]
    lines += [f"<{EX}spc{i}-0> <{EX}sl> <{EX}sp{i}> ." for i in range(120)]
    lines += [f"<{EX}spd0> <{EX}sl> <{EX}sp0> ."]
    lines += [f"<{EX}sp0> <{EX}sl> <{EX}{o}> ." for o in ("spc0-0", "spd0")]
    return "\n".join(lines) + "\n"


def _tier_statements(seeds: int, linked: int) -> list[str]:
    """``_TIER_BODY``'s data: ``seeds`` `ta` rows `ts{i} :ta tx{i}`, two `tl`
    rows per `ts{i}`, one `tl` row for each of the first ``linked`` `tx{i}`,
    and a `tw` row for each of the first three `ts{i}`."""
    lines = [f"<{EX}ts{i}> <{EX}ta> <{EX}tx{i}> ." for i in range(seeds)]
    lines += [f"<{EX}ts{i}> <{EX}tl> <{EX}tc{i}-{j}> ." for i in range(seeds) for j in range(2)]
    lines += [f"<{EX}tx{i}> <{EX}tl> <{EX}td{i}> ." for i in range(linked)]
    lines += [f"<{EX}ts{i}> <{EX}tw> <{EX}tw{i}> ." for i in range(3)]
    return lines


@pytest.fixture(scope="module")
def joins_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("join-budget")
    nt = d / "joins.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "joins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(scope="module")
def tier_quads_vortex(tmp_path_factory):
    """``_TIER_BODY``'s data in two named graphs: all of it in `g1`; in `g2`,
    that of the first six seeds, two of their objects linked."""
    d = tmp_path_factory.mktemp("join-budget-quads")
    nq = d / "tiers.nq"
    quads = [
        line[: -len(" .")] + f" <{EX}{name}> ."
        for name, seeds, linked in (("g1", 120, 5), ("g2", 6, 2))
        for line in _tier_statements(seeds, linked)
    ]
    nq.write_text("\n".join(quads) + "\n", encoding="utf-8")
    out = d / "tiers.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


@pytest.fixture(scope="module")
def indexed_joins_vortex(tmp_path_factory):
    """This module's statements with a secondary index, which answers a probe
    binding a predicate and an object as a point lookup."""
    d = tmp_path_factory.mktemp("join-budget-indexed")
    nt = d / "joins.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "joins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary", indexes=["secondary-by-copy"])
    return out


@pytest.fixture(scope="module")
def multiplicity_vortex(tmp_path_factory):
    """`PROBE_QUADS` and `KEEP_SCAN_QUADS`."""
    d = tmp_path_factory.mktemp("join-budget-multiplicity")
    nq = d / "probe.nq"
    nq.write_text(PROBE_QUADS + KEEP_SCAN_QUADS, encoding="utf-8")
    out = d / "probe.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(joins_vortex, request):
    return Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=request.param))


@pytest.fixture
def file_graph(joins_vortex):
    """This module's store, file-backed only: where point lookups batch."""
    return Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=False))


def _traced_both_ways(graph, monkeypatch, capsys, sparql, runner=run):
    """Both answers to ``sparql`` (see ``both_ways``) and the trace events of
    the pushdown's run."""
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    answers = both_ways(graph, sparql, runner)
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


def _plans(events) -> list:
    """The patterns rdflib was left to finish (tier 3), per completed BGP
    plan: ``None`` where it was left none."""
    return [plan["rest_pattern_indices"] for plan in _fields(events, "bgp_plan_complete")]


@pytest.mark.parametrize(
    ("sparql", "budget", "over", "steps"),
    [
        (TWIN + " ORDER BY ?s ?x", 19, (1, 1, "hash_rows", 20, 10, 20), []),
        (LOOP + " ORDER BY ?s ?k", 10, (2, 1, "hash_rows", 20, 2, 170), [("probe", 2, None)]),
    ],
    ids=["column", "row"],
)
def test_tier3_needs_the_emitter(graph, monkeypatch, capsys, sparql, budget, over, steps):
    """A last step sharing two variables, which no tier streams, over the
    budget with no other pattern left to join first: under ORDER BY the
    relation does not go straight to the emitter, so the head goes back to
    rdflib, which offers the BGP alone, and there rdflib finishes that
    pattern per row (tier 3). Each attempt traces the step with the count of
    its first shared variable: the star's seed columns against the 20 `pq`
    rows, or the chain's two probed rows (2 of the 10 // 4 the probe step may
    make) against the 170 `owner` rows, their first variable's codes copied
    into native columns."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", budget)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql, run_ordered
    )
    assert with_pushdown == without_pushdown
    names = (
        "execution_index",
        "original_pattern_index",
        "strategy",
        "pair_count",
        "input_rows",
        "estimated_rows",
    )
    assert _fields(events, "bgp_over_budget") == [dict(zip(names, over, strict=True))] * 3
    fallbacks = [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert fallbacks == ["Project", "OrderBy"]
    assert _plans(events) == [[1]]
    completed = _fields(events, "bgp_join_step_complete")
    assert [(s["strategy"], s["output_rows"], s["pair_count"]) for s in completed] == steps * 3


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
    with its last probe never pulled and the rows it made are dropped. It is
    planned again as a keep-scan, whose rows are the answer, none twice."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 16)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_PROBE_BATCH", 3)
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: True)
    monkeypatch.setattr(pd, "_CHEAP_BATCH_MIN", 1)
    watched = _WatchedBatches(file_graph.store._store())
    file_graph.store._native = watched
    if traced:
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(file_graph, FAN)
    events = _trace_events(capsys.readouterr().err)
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
            9,
            9,
        )


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


def test_a_batch_past_its_probes_raises(graph, monkeypatch):
    """A batch that answers more probes than it was given raises at its last
    probe rather than handing that probe an answer the batch may have
    misplaced."""
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: False)
    monkeypatch.setattr(pd, "_SCAN_BATCH_MIN", 1)
    monkeypatch.setattr(pd, "_MEMORY_SCAN_BATCH_MIN", 1)
    graph.store._native = _LongBatches(graph.store._store())
    probes = [((None, f"<{EX}p>", None, None), {}), ((None, f"<{EX}q>", None, None), {})]
    answers = iter(pd._run_probes(graph.store, probes, count=False))
    assert len(next(answers)[0]) == 10
    with pytest.raises(ValueError, match="more than its 2 probes"):
        next(answers)


def _streams(events) -> list[dict]:
    """The join steps that streamed (tier 1)."""
    return [
        step for step in _fields(events, "bgp_join_step_complete") if step["strategy"] == "stream"
    ]


def test_an_over_budget_last_step_streams(graph, monkeypatch, capsys):
    """The star's `q` pattern counts 20 rows, more than a budget of 19 and
    than the ten seed rows, so its last step streams without reading it
    whole: the ten seed rows are grouped and the 20 `q` rows fed four at a
    time, a piece each."""
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


def test_a_probed_relation_streams_as_columns(graph, monkeypatch, capsys):
    """The chain's anchor is probed into its two `r` keys, which leaves the
    relation as rows. The last step's `t` pattern counts 150 rows, more than a
    budget of 19 and than those two rows, so it streams from chunks: the two
    rows are copied into native columns to be grouped."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 19)
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


def test_tier2_joins_a_fitting_pattern_first(graph, monkeypatch, capsys):
    """The `?s :tl ?c` step makes 240 pairs of the 120 seed rows, over a
    budget of 239 with a pattern still to join: it goes back, and
    `?x :tl ?d`, whose match the memo holds and whose 5 pairs fit, joins
    first, recording the pattern it was reordered around. It narrows the
    relation to five rows; the `?s` pattern, now the last step and still
    counting more rows than the budget, streams them from the memo's match
    (tier 1)."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 239)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, REORDER
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 10
    [over] = _fields(events, "bgp_over_budget")
    assert (over["original_pattern_index"], over["strategy"], over["pair_count"]) == (
        1,
        "hash_columns",
        240,
    )
    [plan] = _fields(events, "bgp_plan_complete")
    assert (plan["execution_order"], plan["rest_pattern_indices"]) == ([0, 2, 1], None)
    assert plan["native_initial_match_count"] == 2
    names = ("original_pattern_index", "strategy", "reordered_around", "input_rows", "output_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [(2, "hash_columns", [1], 120, 5), (1, "stream", None, 5, None)]
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["output_rows"] == 10
    assert _fields(events, "eval_part_fallback") == []


def test_tier2_does_not_trust_a_capped_estimate(graph, monkeypatch):
    """`?x :tl ?d` is next to the one-row `ts0 :ta ?x`, so it is counted at
    its step only up to 1 x 100 + 1 rows: its 245 rows come back as 101, a
    lower bound, and the pattern is marked capped; the seed, counted when
    planned, is not. A capped count proves no fit: against a two-row
    relation 101 rows make a product of 202, within a budget of 202, yet the
    pattern fits only once its count is known whole."""
    x, d = Variable("x"), Variable("d")
    seed = {
        "n3": [f"<{EX}ts0>", f"<{EX}ta>", None, None],
        "varpos": {x: [2]},
        "unsatisfiable": False,
        "index": 0,
    }
    linked = {
        "n3": [None, f"<{EX}tl>", None, None],
        "varpos": {x: [0], d: [2]},
        "unsatisfiable": False,
        "index": 1,
    }
    pd._join_patterns(graph.store, [seed, linked])
    assert (seed["estimate"], seed.get("estimate_capped", False)) == (1, False)
    assert (linked["estimate"], linked["estimate_capped"]) == (101, True)
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 202)
    rows = [(1,), (2,)]
    assert not pd._fits(2, (x,), None, rows, linked, {}, graph.store)
    linked["estimate_capped"] = False
    assert pd._fits(2, (x,), None, rows, linked, {}, graph.store)


def test_tier2_counts_the_pairs_of_a_memoised_match(graph, monkeypatch):
    """`?x :tl ?d` counts 245 rows, far past what its product with the 120
    seed rows needs to fit, but with its match in the memo it fits by the 5
    pairs it makes on `?x` — whether the relation is columns or rows — and
    not past a budget of 4. A pattern the step would probe fits only by the
    product, memo or not."""
    s, x, d = (Variable(name) for name in "sxd")
    native = graph.store._store()
    seed = native.match_codes(None, f"<{EX}ta>", None, None)
    n3 = [None, f"<{EX}tl>", None, None]
    pat = {"n3": n3, "varpos": {x: [0], d: [2]}, "unsatisfiable": False, "estimate": 245}
    memo = {tuple(n3): native.match_codes(*n3)}
    cols = (memoryview(seed[0]).cast("Q"), memoryview(seed[2]).cast("Q"))
    bodies = [(cols, None), (None, pd._cols_to_rows(cols))]
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 5)
    assert [pd._fits(120, (s, x), *body, pat, memo, graph.store) for body in bodies] == [True] * 2
    assert not pd._fits(120, (s, x), cols, None, pat, {}, graph.store)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 2)
    assert not pd._fits(120, (s, x), cols, None, pat, memo, graph.store)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 100)
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 4)
    assert [pd._fits(120, (s, x), *body, pat, memo, graph.store) for body in bodies] == [False] * 2


def test_tier2_undoes_what_a_put_back_step_specialized(graph, monkeypatch, capsys):
    """The FILTER is specialized onto pattern 0 as a prefix test of `?c`, and
    that step is one pair over a budget of 119. Going back, the step takes
    the specialization with it: the test is pending again and is specialized
    onto pattern 1, which joins first and binds `?c`, so the `spd0` row that
    fails it never joins, and pattern 0 then joins the one row left."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 119)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, SPECIALIZED
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 1
    [over] = _fields(events, "bgp_over_budget")
    assert (over["original_pattern_index"], over["pair_count"]) == (0, 120)
    specialized = [
        event["original_pattern_index"] for event in _fields(events, "bgp_tuple_specialized")
    ]
    assert specialized == [0, 1]
    names = ("original_pattern_index", "reordered_around", "output_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [(1, [0], 1), (0, None, 1)]


def test_tier2_restricts_a_put_back_pattern_again(graph, monkeypatch, capsys):
    """A FILTER over `?c` alone restricts `?s :tl ?c` to its 120 `-1` rows as
    its step reads it, which still make 120 pairs, over a budget of 119: the
    pattern goes back with its restriction, and once `?x :tl ?d` has joined,
    its step reads it again from the memo and restricts it again, joining it
    whole rather than streaming its unrestricted match."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 119)
    sparql = _PREFIX + f'SELECT ?s ?x ?c ?d WHERE {{ {_TIER_BODY} FILTER(STRENDS(STR(?c), "-1")) }}'
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 5
    [plan] = _fields(events, "bgp_plan_complete")
    assert plan["execution_order"] == [0, 2, 1]
    names = ("original_pattern_index", "strategy", "reordered_around", "output_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [(2, "hash_columns", [1], 5), (1, "hash_columns", None, 5)]


def test_tier3_lets_rdflib_finish_the_rest(graph, monkeypatch, capsys):
    """At a budget of 0 the `?s :tl ?c` step is over it and the 5 pairs of
    `?x :tl ?d` do not fit either: nothing joins next, so the relation is the
    120 seed rows, and rdflib's ``evalBGP`` finishes both patterns per row,
    reading them through the store's ``triples()`` with the row's bindings
    in place — once per seed row, then once per `?c` that row finds."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    unregister_sparql_pushdown()
    try:
        expected = run(graph, REORDER)
    finally:
        register_sparql_pushdown()
    read = []
    triples = VortexRdflibStore.triples

    def watched(store, pattern, context=None):
        read.append(pattern)
        return triples(store, pattern, context)

    monkeypatch.setattr(VortexRdflibStore, "triples", watched)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, REORDER)
    events = _trace_events(capsys.readouterr().err)
    assert rows == expected
    assert len(rows) == 10
    [plan] = _fields(events, "bgp_plan_complete")
    assert (plan["execution_order"], plan["rest_pattern_indices"]) == ([0, 1, 2], [1, 2])
    assert _fields(events, "bgp_join_step_complete") == []
    assert _fields(events, "eval_part_fallback") == []
    assert {predicate for _, predicate, _ in read} == {URIRef(f"{EX}tl")}
    assert all(s is not None and o is None for s, _, o in read)
    assert len(read) == 120 + 240


def test_tier3_projects_and_windows(graph, monkeypatch, capsys):
    """rdflib finishes the rows before the head projects and windows them:
    of the 120 seed rows, the five linked ones finish into two solutions
    each and the others into none, and `LIMIT 3 OFFSET 2` gives the third to
    fifth solution, each bound to `?c` alone."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    register_sparql_pushdown()
    select = _PREFIX + f"SELECT ?c WHERE {{ {_TIER_BODY} }}"
    every = list(SPARQLProcessor(graph).query(select)["bindings"])
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    window = list(SPARQLProcessor(graph).query(select + " LIMIT 3 OFFSET 2")["bindings"])
    events = _trace_events(capsys.readouterr().err)
    assert _plans(events) == [[1, 2]]
    assert len(every) == 10
    assert [dict(solution) for solution in window] == [dict(solution) for solution in every[2:5]]
    assert [set(solution) for solution in window] == [{Variable("c")}] * 3


#: Nodes above a BGP whose last patterns rdflib finishes (tier 3), by id: the
#: query, the node it hands back, how its answer is compared, and whether it
#: runs over the named graphs. Each keeps the relation from going straight
#: to the emitter, or the remaining patterns from being rdflib's alone to
#: evaluate: a conjunct pushed into them or over a variable only they bind,
#: a GRAPH scope other than the active graph, an OPTIONAL, an ORDER BY.
TIER3_ABOVE = {
    "filter-one-var": (
        f'SELECT ?s ?c ?d WHERE {{ {_TIER_BODY} FILTER(STRENDS(STR(?c), "-1")) }}',
        "Filter",
        run,
        False,
    ),
    "filter-two-var": (
        f"SELECT ?s ?c ?d WHERE {{ {_TIER_BODY} "
        f'FILTER(CONCAT(STR(?x), STR(?d)) != "{EX}tx0{EX}td0") }}',
        "Filter",
        run,
        False,
    ),
    "graph-iri": (f"SELECT ?s ?c ?d WHERE {{ GRAPH :g1 {{ {_TIER_BODY} }} }}", "Graph", run, True),
    "graph-var": (
        f"SELECT ?g ?s ?c ?d WHERE {{ GRAPH ?g {{ {_TIER_BODY} }} }}",
        "Graph",
        run,
        True,
    ),
    "optional-above": (
        f"SELECT ?s ?c ?d ?w WHERE {{ {_TIER_BODY} OPTIONAL {{ ?s :tw ?w }} }}",
        "LeftJoin",
        run,
        False,
    ),
    "order-above": (
        f"SELECT ?s ?c ?d WHERE {{ {_TIER_BODY} }} ORDER BY ?c",
        "OrderBy",
        run_ordered,
        False,
    ),
}


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("shape", list(TIER3_ABOVE))
def test_tier3_keeps_the_answer(
    joins_vortex, tier_quads_vortex, monkeypatch, capsys, shape, in_memory
):
    """Where rdflib's evaluation of the remaining patterns would not be the
    block's, the node above the BGP goes back to rdflib, which offers the BGP
    alone in the scope it sets, and there rdflib finishes the rest per row.
    The answer is rdflib's."""
    sparql, node, runner, named = TIER3_ABOVE[shape]
    if named:
        store = VortexRdflibStore(str(tier_quads_vortex), in_memory=in_memory)
        view = Dataset(store=store, default_union=True)
    else:
        view = Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=in_memory))
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        view, monkeypatch, capsys, _PREFIX + sparql, runner
    )
    assert with_pushdown == without_pushdown
    assert with_pushdown
    assert node in [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert [1, 2] in _plans(events)


def test_a_filter_over_the_native_rows_keeps_tier3_below_it(graph, monkeypatch, capsys):
    """A FILTER that pushes nothing into the BGP leaves tier 3 in place under
    it: its conjunct, over two variables the native rows bind and with no
    native or fast route, tests those rows before rdflib finishes them, and
    nothing is handed back."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 0)
    sparql = (
        _PREFIX + f"SELECT ?s ?c ?d WHERE {{ {_TIER_BODY} "
        f'FILTER(CONCAT(STR(?s), STR(?x)) != "{EX}ts0{EX}tx0") }}'
    )
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 8
    assert _fields(events, "eval_part_fallback") == []
    assert _plans(events) == [[1, 2]]


def test_a_relation_rdflib_finishes_reaches_only_the_emitter():
    """The rows of a relation rdflib still finishes are not its answers: a
    consumer that would hold or pull them fails loudly."""
    x = Variable("x")
    rest = ((x, URIRef(f"{EX}p"), Variable("y")),)
    rel = pd.Relation((x,), None, [(1,)], 1, rest=rest)
    with pytest.raises(AssertionError):
        pd._rows_of(rel)
    with pytest.raises(AssertionError):
        pd._code_rows(rel)


def _keep_scans(events) -> list[dict]:
    """The join steps that keep-scanned their pattern."""
    return [
        step
        for step in _fields(events, "bgp_join_step_complete")
        if step["strategy"] == "keep_scan"
    ]


def _keyed_matches(spy) -> list[dict]:
    """The keywords of the ``match_codes`` calls ``spy`` saw narrowed by a
    keep."""
    return [kwargs for name, kwargs in spy.calls if name == "match_codes" and "keep" in kwargs]


def _codes(column) -> list[int]:
    """The codes of a native column, in order."""
    return memoryview(column).cast("Q").tolist()


def test_scan_probes_become_one_keep_scan(file_graph, monkeypatch, capsys):
    """The nine keys' probes would each scan the store: they bind no subject,
    and the file-backed store has no index. The step matches `?f ?p ?k` once
    instead, narrowed at its object to the nine keys, ascending, and
    hash-joins that match; no probe is made. The pattern's count stopped at
    its cap, a lower bound, so the match stops one row past the budget."""
    register_sparql_pushdown()
    spy = _spy(file_graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(file_graph, SCANNED)
    events = _trace_events(capsys.readouterr().err)
    assert "match_codes_many" not in spy.names()
    [keyed] = _keyed_matches(spy)
    assert set(keyed["keep"]) == {2}
    keys = _codes(keyed["keep"][2])
    assert len(keys) == 9 and keys == sorted(set(keys))
    assert keyed["limit"] == pd._PAIR_BUDGET + 1
    [step] = _fields(events, "bgp_join_step_complete")
    assert (step["strategy"], step["cardinality_source"], step["output_rows"]) == (
        "keep_scan",
        "matched",
        18,
    )
    assert step["native_probe_call_count"] == 0
    unregister_sparql_pushdown()
    try:
        assert rows == run(file_graph, SCANNED)
    finally:
        register_sparql_pushdown()


def test_an_over_budget_keep_scan_streams_its_chunks(graph, monkeypatch, capsys):
    """At a budget of 10 the keep-scan reads its match only up to 11 rows,
    and it comes back full. The step is the last and shares one variable, so
    it streams: the pattern is fed from ``match_code_chunks`` narrowed by the
    same keep, never matched whole."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 10)
    register_sparql_pushdown()
    spy = _spy(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, SCANNED)
    events = _trace_events(capsys.readouterr().err)
    [keyed] = _keyed_matches(spy)
    assert keyed["limit"] == 11
    chunked = [kwargs for name, kwargs in spy.calls if name == "match_code_chunks"]
    assert [kwargs["keep"] for kwargs in chunked] == [keyed["keep"]]
    [over] = _fields(events, "bgp_over_budget")
    assert (over["strategy"], over["pair_count"]) == ("keep_scan", None)
    [step] = _streams(events)
    assert (step["feed"], step["cardinality_source"]) == ("chunks", "chunked")
    [stream] = _fields(events, "bgp_stream_complete")
    assert stream["output_rows"] == 18
    unregister_sparql_pushdown()
    try:
        assert rows == run(graph, SCANNED)
    finally:
        register_sparql_pushdown()


def test_a_probe_step_over_its_tally_replans_as_a_keep_scan(
    indexed_joins_vortex, monkeypatch, capsys
):
    """With a secondary index an `owner` probe binding the object is a point
    lookup, so the anchor's step probes. At a budget of 200 its tally is 50
    rows, which the one probe's 152 rows pass: the step is traced over budget
    and planned again, in the same pass, as a keep-scan — the `owner` match
    narrowed to the anchor, its 152 rows within the budget, hash-joined."""
    graph = Graph(store=VortexRdflibStore(str(indexed_joins_vortex), in_memory=False))
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 200)
    (with_pushdown, without_pushdown), events = _traced_both_ways(graph, monkeypatch, capsys, OWNED)
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 152
    names = ("strategy", "pair_count", "input_rows")
    overs = [tuple(over[n] for n in names) for over in _fields(events, "bgp_over_budget")]
    assert overs == [("probe", None, 1)]
    names = ("strategy", "cardinality_source", "native_probe_call_count", "output_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [("keep_scan", "matched", 1, 152)]
    assert _fields(events, "eval_part_fallback") == []


def test_a_keep_scan_reads_a_relation_of_rows(graph, monkeypatch, capsys):
    """The anchor's probes leave the relation as rows, of two keys: the next
    step keep-scans `?x ?p ?k` by the keys those rows hold."""
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, ANCHORED_SCAN
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 2
    names = ("strategy", "input_rows", "output_rows")
    steps = [tuple(step[n] for n in names) for step in _fields(events, "bgp_join_step_complete")]
    assert steps == [("probe", 1, 2), ("keep_scan", 2, 2)]


def test_a_keep_scan_over_budget_by_its_pairs_streams_its_match(graph, monkeypatch, capsys):
    """The keep-scan's six rows are within a budget of 11, but its key is held
    twice by the relation, so they make twelve pairs. The last step streams
    the match it read, fed in slices."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 11)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, KEYED_TWICE
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 12
    [over] = _fields(events, "bgp_over_budget")
    assert (over["strategy"], over["pair_count"]) == ("keep_scan", 12)
    [step] = _streams(events)
    assert (step["feed"], step["cardinality_source"], step["pattern_match_rows"]) == (
        "slices",
        "matched",
        6,
    )


def test_an_over_budget_keep_scan_short_of_the_last_step_goes_back(graph, monkeypatch, capsys):
    """Forced to keep-scan, the `?s :tl ?c` step reads its match up to 240
    rows, one past a budget of 239: it comes back full with a pattern still
    to join, and goes back as planned. `?x :tl ?d`, which the step would join
    per row too, can fit only by its product, which does not, so rdflib
    finishes both patterns per seed row (tier 3)."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 239)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: False)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, REORDER
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 10
    [over] = _fields(events, "bgp_over_budget")
    assert (over["original_pattern_index"], over["strategy"], over["pair_count"]) == (
        1,
        "keep_scan",
        None,
    )
    assert _plans(events) == [[1, 2]]
    assert _fields(events, "bgp_join_step_complete") == []


def test_an_over_budget_keep_scan_needing_whole_columns_goes_back(graph, monkeypatch, capsys):
    """Forced to keep-scan, `?k :loop ?k` (pattern 0, as rdflib orders the
    block) comes back full at a budget of 1 on the last step, which shares
    one variable; but its repeated `?k` asks for a test across two of a
    row's columns, which chunks do not make. It goes back, and rdflib
    finishes it per row (tier 3)."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 1)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: False)
    spy = _spy(graph)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, LOOPED
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 4
    [over] = _fields(events, "bgp_over_budget")
    assert (over["original_pattern_index"], over["strategy"], over["pair_count"]) == (
        0,
        "keep_scan",
        None,
    )
    assert _plans(events) == [[0]]
    assert "match_code_chunks" not in spy.names()


def test_an_over_budget_keep_scan_with_a_deferred_restriction_goes_back(graph, monkeypatch, capsys):
    """A FILTER over `?f` alone restricts the keep-scanned pattern over its
    whole match, which chunks do not carry: come back full at a budget of
    10, the last step goes back, and with the conjunct pushed into the block
    the FILTER goes back to rdflib too. rdflib offers the block alone, whose
    keep-scan, full again, streams from chunks."""
    monkeypatch.setattr(pd, "_PAIR_BUDGET", 10)
    sparql = (
        _PREFIX + "SELECT ?k ?f ?p WHERE { :c0 :has ?k . ?f ?p ?k "
        f'FILTER(STRSTARTS(STR(?f), "{EX}f")) }}'
    )
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 9
    assert {over["strategy"] for over in _fields(events, "bgp_over_budget")} == {"keep_scan"}
    assert "Filter" in [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert [step["feed"] for step in _streams(events)] == ["chunks"]


@pytest.mark.parametrize("planned", [None, {0: (0, 1 << 40)}], ids=["no-keep", "keep"])
def test_a_put_back_keep_scan_drops_its_keep(graph, planned):
    """A keep-scan narrows its pattern's keep to the relation's codes and
    memoizes its match under that keep. Put back, the pattern has the keep it
    was planned with again, which the keep-scan left whole, and the match
    leaves the memo with the narrower keep."""
    k, f, p = (Variable(name) for name in "kfp")
    keys = graph.store._store().match_codes(f"<{EX}c0>", f"<{EX}has>", None, None)[2]
    pat: dict = {
        "n3": [None, None, None, None],
        "varpos": {f: [0], p: [1], k: [2]},
        "unsatisfiable": False,
        "estimate": 1348,
    }
    if planned is not None:
        pat["nkeep"] = planned
    whole = None if planned is None else dict(planned)
    memo: dict = {}
    pd._keep_scan(graph.store, pat, (k,), (memoryview(keys).cast("Q"),), None, memo)
    assert pat["nrows"] == 18
    assert set(pat["nkeep"]) == {2} | set(planned or ())
    assert list(memo) == [pd._pattern_key(pat)]
    pd._put_back(pat, None, (), None, (), planned, memo)
    assert pat.get("nkeep") is planned
    assert planned == whole
    assert "cols" not in pat and memo == {}


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("case", [*PROBE_MULTIPLICITY, *KEEP_SCAN_MULTIPLICITY])
def test_probe_multiplicity_under_keep_scan(
    multiplicity_vortex, monkeypatch, capsys, case, in_memory
):
    """Forced to probe with probes that scan, each step of a BGP keep-scans
    and joins as many rows as the probes would: one per quad — in the union
    default graph one per graph holding the triple, in a fixed graph at most
    one — a shared variable at two positions only where the row repeats its
    code, and a keep already on the pattern narrowing it further. The joins
    above a BGP, OPTIONAL and EXISTS still probe."""
    if case in PROBE_MULTIPLICITY:
        union, sparql, rows, _, _ = PROBE_MULTIPLICITY[case]
    else:
        union, sparql, rows = KEEP_SCAN_MULTIPLICITY[case]
    store = VortexRdflibStore(str(multiplicity_vortex), in_memory=in_memory)
    dataset = Dataset(store=store, default_union=union)
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_cheap_probe", lambda store, n3: False)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        dataset, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == rows
    assert bool(_keep_scans(events)) == (case in _KEEP_SCANNED)


def test_probe_batches_name_their_lookahead(file_graph, monkeypatch):
    """Each batch of probes names its lookahead. Point lookups run as a whole
    batch: the star's ten `q` matches and the twin's ten existence counts,
    each binding its subject, batched on a file-backed store. Probes that
    scan run a scan per core, up to eight: the nine `ref` probes of an EXISTS
    and of an OPTIONAL, each binding only its object. The planning counts
    keep the binding's default."""
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    monkeypatch.setattr(pd, "_CHEAP_BATCH_MIN", 1)
    register_sparql_pushdown()
    spy = _spy(file_graph)
    scans = min(os.cpu_count() or 1, 8)
    exists = _PREFIX + "SELECT ?k WHERE { :c0 :has ?k FILTER EXISTS { ?f :ref ?k } }"
    optional = _PREFIX + "SELECT ?k ?f WHERE { :c0 :has ?k OPTIONAL { ?f :ref ?k } }"
    cases = {
        STAR: [("count_quads_many", None, 2), ("match_codes_many", 10, 10)],
        TWIN: [("count_quads_many", None, 2), ("count_quads_many", 10, 10)],
        exists: [("count_quads_many", scans, 9)],
        optional: [("match_codes_many", scans, 9)],
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
