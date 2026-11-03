"""Joins over the pair budget that do not stream: another pattern that
provably fits joins first (tier 2), and when none does, rdflib finishes the
remaining patterns per row of the relation so far (tier 3) where the
relation goes straight to the emitter — otherwise the block goes back to
rdflib. Either way the answer is rdflib's."""

import pytest
from rdflib import Dataset, Graph, URIRef, Variable
from rdflib.plugins.sparql.processor import SPARQLProcessor
from tests.join_corpus import (
    _PREFIX,
    _TIER_BODY,
    ANCHORED_SCAN,
    CHAIN,
    EX,
    FAN,
    FANNED,
    KEYED_TWICE,
    LOOP,
    LOOPED,
    ONE_KEY,
    OWNED,
    REORDER,
    SCANNED,
    SELF_LOOP,
    SPECIALIZED,
    STAR,
    TWIN,
    WIDE,
    _fields,
    _plans,
    _traced_both_ways,
)
from tests.support import run, run_ordered, trace_events

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


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
    monkeypatch.setattr(SETTINGS, "pair_budget", budget)
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


def test_tier2_joins_a_fitting_pattern_first(graph, monkeypatch, capsys):
    """The `?s :tl ?c` step makes 240 pairs of the 120 seed rows, over a
    budget of 239 with a pattern still to join: it goes back, and
    `?x :tl ?d`, whose match the memo holds and whose 5 pairs fit, joins
    first, recording the pattern it was reordered around. It narrows the
    relation to five rows; the `?s` pattern, now the last step and still
    counting more rows than the budget, streams them from the memo's match
    (tier 1)."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 239)
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
    seed = pd.matching.Pattern((f"<{EX}ts0>", f"<{EX}ta>", None, None), {x: [2]}, index=0)
    linked = pd.matching.Pattern((None, f"<{EX}tl>", None, None), {x: [0], d: [2]}, index=1)
    pd.planner._join_incremental_bgp(graph.store, [seed, linked], pd.tracing._BgpStats())
    assert (seed.estimate, seed.estimate_capped) == (1, False)
    assert (linked.estimate, linked.estimate_capped) == (101, True)
    monkeypatch.setattr(SETTINGS, "pair_budget", 202)
    relation = pd.relation.Relation.from_rows((x,), [(1,), (2,)])
    assert not pd.planner._fits(relation, linked, {})
    linked.estimate_capped = False
    assert pd.planner._fits(relation, linked, {})


def test_tier2_counts_the_pairs_of_a_memoised_match(graph, monkeypatch):
    """`?x :tl ?d` counts 245 rows, far past what its product with the 120
    seed rows needs to fit, but with its match in the memo it fits by the 5
    pairs it makes on `?x` — whether the relation is columns or rows — and
    not past a budget of 4. A pattern the step would probe fits only by the
    product, memo or not."""
    s, x, d = (Variable(name) for name in "sxd")
    native = graph.store._opened().native
    seed = native.match_codes(None, f"<{EX}ta>", None, None)
    n3 = (None, f"<{EX}tl>", None, None)
    pat = pd.matching.Pattern(n3, {x: [0], d: [2]}, estimate=245)
    memo = {n3: native.match_codes(*n3)}
    cols = (memoryview(seed[0]).cast("Q"), memoryview(seed[2]).cast("Q"))
    columnar = pd.relation.Relation.from_cols((s, x), cols)
    assert columnar.nrows == 120
    bodies = [columnar, pd.relation.Relation.from_rows((s, x), pd.relation._cols_to_rows(cols))]
    monkeypatch.setattr(SETTINGS, "pair_budget", 5)
    assert [pd.planner._fits(body, pat, memo) for body in bodies] == [True] * 2
    assert not pd.planner._fits(columnar, pat, {})
    monkeypatch.setattr(SETTINGS, "probe_fanout", 2)
    assert not pd.planner._fits(columnar, pat, memo)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 100)
    monkeypatch.setattr(SETTINGS, "pair_budget", 4)
    assert [pd.planner._fits(body, pat, memo) for body in bodies] == [False] * 2


@pytest.mark.parametrize("budget", [119, 60])
def test_tier2_undoes_what_a_put_back_step_specialized(graph, monkeypatch, capsys, budget):
    """The FILTER is specialized onto pattern 0 as a prefix test of `?c`, and
    that step's 120 pairs are over the budget: by one pair at 119, by sixty at
    60, and the count the trace carries is the whole one either way. Going
    back, the step takes the specialization with it: the test is pending
    again and is specialized onto pattern 1, which joins first and binds `?c`,
    so the `spd0` row that fails it never joins, and pattern 0 then joins the
    one row left."""
    monkeypatch.setattr(SETTINGS, "pair_budget", budget)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 119)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
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
    events = trace_events(capsys.readouterr().err)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    register_sparql_pushdown()
    select = _PREFIX + f"SELECT ?c WHERE {{ {_TIER_BODY} }}"
    every = list(SPARQLProcessor(graph).query(select)["bindings"])
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    window = list(SPARQLProcessor(graph).query(select + " LIMIT 3 OFFSET 2")["bindings"])
    events = trace_events(capsys.readouterr().err)
    assert _plans(events) == [[1, 2]]
    assert len(every) == 10
    assert [dict(solution) for solution in window] == [dict(solution) for solution in every[2:5]]
    assert [set(solution) for solution in window] == [{Variable("c")}] * 3


#: Nodes above a BGP whose last patterns rdflib finishes (tier 3), by id: the
#: query, the node it hands back (a FILTER's pattern, whose rows the hook
#: still filters), how its answer is compared, and whether it runs over the
#: named graphs. Each keeps the relation from going straight
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
    alone in the scope it sets, and there rdflib finishes the rest per row —
    under a FILTER, the hook hands rdflib the FILTER's pattern and filters the
    rows itself. The answer is rdflib's."""
    sparql, node, runner, named = TIER3_ABOVE[shape]
    if named:
        store = VortexRdflibStore(str(tier_quads_vortex), in_memory=in_memory)
        view = Dataset(store=store, default_union=True)
    else:
        view = Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=in_memory))
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        view, monkeypatch, capsys, _PREFIX + sparql, runner
    )
    assert with_pushdown == without_pushdown
    assert with_pushdown
    if node == "Filter":
        assert _fields(events, "filter_over_rdflib_rows")
    else:
        assert node in [event["algebra_node"] for event in _fields(events, "eval_part_fallback")]
    assert [1, 2] in _plans(events)


def test_a_filter_over_the_native_rows_keeps_tier3_below_it(graph, monkeypatch, capsys):
    """A FILTER that pushes nothing into the BGP leaves tier 3 in place under
    it: its conjunct, over two variables the native rows bind and with no
    native or fast route, tests those rows before rdflib finishes them, and
    nothing is handed back."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
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
    rel = pd.relation.Relation((x,), None, [(1,)], 1, rest=rest)
    with pytest.raises(AssertionError):
        pd.relation._rows_of(rel)
    with pytest.raises(AssertionError):
        pd.relation._code_rows(rel)


#: The module's join queries, swept by
#: ``test_a_step_s_source_follows_its_strategy_and_feed``.
_SWEPT = (
    STAR, CHAIN, FAN, TWIN, LOOP, WIDE, FANNED, ONE_KEY, SELF_LOOP,
    REORDER, SCANNED, OWNED, ANCHORED_SCAN, KEYED_TWICE, LOOPED,
)  # fmt: skip


def test_a_step_s_source_follows_its_strategy_and_feed(graph, monkeypatch, capsys):
    """Every traced join step says where its rows came from by its strategy
    and feed alone — its probes for a probe step, chunks of its pattern for
    a stream fed by them, its pattern's match otherwise — and made an
    initial match exactly when it did not probe; over budgets that reach
    every kind of step."""
    seen = set()
    for budget in (SETTINGS.pair_budget, 40, 0):
        monkeypatch.setattr(SETTINGS, "pair_budget", budget)
        for sparql in _SWEPT:
            _, events = _traced_both_ways(graph, monkeypatch, capsys, sparql)
            for step in _fields(events, "bgp_join_step_complete"):
                if step["strategy"] == "probe":
                    source = "probed"
                else:
                    source = "chunked" if step["feed"] == "chunks" else "matched"
                assert step["cardinality_source"] == source, (budget, sparql, step)
                assert step["native_initial_match_count"] == int(source != "probed")
                seen.add(source)
    assert seen == {"probed", "chunked", "matched"}
