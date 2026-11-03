"""Keep-scans: a step that would probe its pattern per row with probes that
each scan the store matches it once instead, narrowed to the relation's
codes, and joins that match as a hash step does, under the same budget."""

import builtins

import pytest
from rdflib import Dataset, Graph, Variable
from tests.corpus import PROBE_MULTIPLICITY
from tests.join_corpus import (
    _KEEP_SCANNED,
    _PREFIX,
    ANCHORED_SCAN,
    EX,
    KEEP_SCAN_MULTIPLICITY,
    KEYED_TWICE,
    LOOPED,
    OWNED,
    REORDER,
    SCANNED,
    _fields,
    _plans,
    _streams,
    _traced_both_ways,
)
from tests.support import both_ways, native_spy, run, trace_events
from vortex_rdf import U64Column

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.open_file import OpenFile
from vortex_rdflib.settings import SETTINGS


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
    spy = native_spy(file_graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(file_graph, SCANNED)
    events = trace_events(capsys.readouterr().err)
    assert "match_codes_many" not in spy.names()
    [keyed] = _keyed_matches(spy)
    assert set(keyed["keep"]) == {2}
    keys = _codes(keyed["keep"][2])
    assert len(keys) == 9 and keys == sorted(set(keys))
    assert keyed["limit"] == SETTINGS.pair_budget + 1
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 10)
    register_sparql_pushdown()
    spy = native_spy(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    rows = run(graph, SCANNED)
    events = trace_events(capsys.readouterr().err)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 200)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 11)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 239)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: False)
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
    monkeypatch.setattr(SETTINGS, "pair_budget", 1)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: False)
    spy = native_spy(graph)
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
    the FILTER's pattern goes back to rdflib, the hook filtering its rows.
    rdflib offers the block alone, whose keep-scan, full again, streams from
    chunks."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 10)
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
    assert _fields(events, "filter_over_rdflib_rows")
    assert [step["feed"] for step in _streams(events)] == ["chunks"]


def test_keep_scan_does_not_memoize_its_match(graph, monkeypatch, capsys):
    """A keep-scan's match is narrowed by the relation's codes, a keep no
    other step reads: it is not memoized, so it is let go once its step has
    joined it rather than held until the plan ends."""
    memos: list[dict] = []
    match = pd.matching._match_resolved_pattern

    def spy(store, pat, memo=None, limit=None):
        if memo is not None and all(seen is not memo for seen in memos):
            memos.append(memo)
        return match(store, pat, memo, limit)

    monkeypatch.setattr(pd.matching, "_match_resolved_pattern", spy)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        graph, monkeypatch, capsys, ANCHORED_SCAN
    )
    assert with_pushdown == without_pushdown
    steps = [step["strategy"] for step in _fields(events, "bgp_join_step_complete")]
    assert steps == ["probe", "keep_scan"]
    assert memos and [key for memo in memos for key in memo if isinstance(key[-1], tuple)] == []


@pytest.mark.parametrize("planned", [{}, {0: (0, 1 << 40)}], ids=["no-keep", "keep"])
def test_a_keep_scan_narrows_its_copy_only(graph, planned):
    """A keep-scan narrows its step's copy of the pattern to the relation's
    codes: the pattern keeps the keep it was planned with, whole, for a step
    that puts it back."""
    k, f, p = (Variable(name) for name in "kfp")
    keys = graph.store._opened().native.match_codes(f"<{EX}c0>", f"<{EX}has>", None, None)[2]
    pat = pd.matching.Pattern((None, None, None, None), {f: [0], p: [1], k: [2]}, estimate=1348)
    pat.nkeep = planned
    whole = dict(planned)
    relation = pd.relation.Relation.from_cols((k,), (memoryview(keys).cast("Q"),))
    work = pat.copy()
    pd.planner._keep_scan(graph.store, work, relation)
    assert work.nrows == 18
    assert set(work.nkeep) == {2} | set(planned)
    assert pat.nkeep is planned and planned == whole
    assert pat.cols is None


def test_a_memo_key_holds_its_keep_column():
    """A memo key names a code-column keep by the column itself, compared by
    identity: a column of the same codes is another key, and the key holds
    its column, so no later column can take its place while the memo keeps
    the entry."""
    n3 = (None, f"<{EX}q>", None, None)
    keep = U64Column([1, 2])
    key = pd.matching._pattern_key(pd.matching.Pattern(n3, {}, nkeep={2: keep}))
    assert key == pd.matching._pattern_key(pd.matching.Pattern(n3, {}, nkeep={2: keep}))
    assert key != pd.matching._pattern_key(
        pd.matching.Pattern(n3, {}, nkeep={2: U64Column([1, 2])})
    )
    [(position, (kind, held))] = key[-1]
    assert (position, kind) == (2, "codes")
    assert held is keep


def test_a_keep_column_at_a_reused_address_is_no_memo_hit(graph, monkeypatch):
    """Two patterns of one quad pattern, narrowed at their object by
    different FILTER keeps, are matched apart even when every code column
    has one address: the module's ``id`` patched to the worst reuse a freed
    column's address could see. The memo does not name a keep by where it
    lives."""

    def one_address(obj):
        return 0 if isinstance(obj, U64Column) else builtins.id(obj)

    # The modules that key the memo: matching builds the keys, bgp keeps it.
    monkeypatch.setattr(pd.matching, "id", one_address, raising=False)
    monkeypatch.setattr(pd.bgp, "id", one_address, raising=False)
    sparql = (
        _PREFIX + "SELECT ?a ?b ?c ?d WHERE { ?a :q ?b . ?c :q ?d "
        "FILTER(?b IN (:y0-0, :y1-0) && ?d IN (:y2-1)) }"
    )
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 2


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("case", [*PROBE_MULTIPLICITY, *KEEP_SCAN_MULTIPLICITY])
def test_probe_multiplicity_under_keep_scan(
    multiplicity_vortex, monkeypatch, capsys, case, in_memory
):
    """Forced to probe with probes that scan, each step of a BGP keep-scans
    and joins as many rows as the probes would: one per triple of its graph,
    the union default graph a set like any other — a shared variable at two
    positions only where the row repeats its code, and a keep already on the
    pattern narrowing it further. The joins
    above a BGP, OPTIONAL and EXISTS still probe."""
    if case in PROBE_MULTIPLICITY:
        union, sparql, rows, _, _ = PROBE_MULTIPLICITY[case]
    else:
        union, sparql, rows = KEEP_SCAN_MULTIPLICITY[case]
    store = VortexRdflibStore(str(multiplicity_vortex), in_memory=in_memory)
    dataset = Dataset(store=store, default_union=union)
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: False)
    (with_pushdown, without_pushdown), events = _traced_both_ways(
        dataset, monkeypatch, capsys, sparql
    )
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == rows
    assert bool(_keep_scans(events)) == (case in _KEEP_SCANNED)
