"""The pushdown's joins: probe joins and the multiplicity rdflib reads, hash
joins that keep a columnar body, a BGP pruned mid-join, the join kernels
against a nested loop, and a repeated pattern matched once."""

import pytest
from rdflib import Dataset, Graph, URIRef, Variable
from tests.corpus import (
    PROBE_MULTIPLICITY,
    PROBE_QUADS,
)
from tests.support import (
    both_ways,
    counting_native,
    counting_term_dict,
    file_codes,
    run,
    three_predicate_graph,
)
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def test_probe_join_triggers_on_skewed_join(tmp_path, monkeypatch):
    """A 1-row anchor joined against a 300-row predicate must take the probe
    path under the real threshold, and still produce the right rows."""
    nt = tmp_path / "skewed.nt"
    nt.write_text(
        '<http://ex.org/s0> <http://ex.org/rare> "anchor" .\n'
        + "".join(
            f"<http://ex.org/s{i}> <http://ex.org/big> <http://ex.org/o{i}> .\n" for i in range(300)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "skewed.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")

    probes = []
    original = pd.probes._probe_join
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original(*a, **k)
    )
    graph = Graph(store=VortexRdflibStore(str(out)))
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?s ?o WHERE {
            ?s <http://ex.org/rare> "anchor" .
            ?s <http://ex.org/big> ?o }""",
    )
    assert rows == [(URIRef("http://ex.org/s0"), URIRef("http://ex.org/o0"))]
    assert probes, "the skewed join did not take the probe path"


def test_probe_join_pads_rows_when_the_pattern_shares_no_variable(graph):
    """A left-join probe keeps each relation row that finds no continuation,
    padded with None — also when the pattern shares no variable with the
    relation, so nothing is substituted per row. No caller probes such a
    pattern today, but `_probe_join` must not rely on that."""
    pattern = pd.matching.Pattern(
        (None, "<http://ex.org/nothing>", None, None), {Variable("x"): [0], Variable("a"): [2]}
    )
    schema, rows = pd.probes._probe_join(
        graph.store, (Variable("z"),), [(0,), (1,)], pattern, keep_unmatched=True
    )
    assert schema == (Variable("z"), Variable("x"), Variable("a"))
    assert rows == [(0, None, None), (1, None, None)]


@pytest.fixture(scope="module")
def probe_quads(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown-probe-quads")
    nq = d / "probe.nq"
    nq.write_text(PROBE_QUADS, encoding="utf-8")
    out = d / "probe.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


def _recording(calls, name):
    """`pd.probes.<name>`, appending its name to `calls` as it is called."""
    original = getattr(pd.probes, name)

    def record(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    return record


def _probe_dataset(path, union: bool) -> Dataset:
    """The probe quads, file-backed, with the union default graph or the default
    graph alone."""
    store = VortexRdflibStore(str(path), in_memory=False)
    assert store._opened().codes is not None and file_codes(store).term_dict.file_backed
    return Dataset(store=store, default_union=union)


@pytest.mark.parametrize("fanout", [0, 100])
@pytest.mark.parametrize("case", PROBE_MULTIPLICITY)
def test_a_probe_join_keeps_the_multiplicity_rdflib_reads(probe_quads, monkeypatch, case, fanout):
    """A pattern with subject, predicate and object bound is one solution
    when its graph holds the triple, probed per row (fanout 0) or hash-joined
    (fanout 100): a fixed graph holds a triple once, and so does the union
    default graph, however many graphs hold it; an EXISTS test adds none."""
    union, sparql, rows, probed, _ = PROBE_MULTIPLICITY[case]
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    probes = []
    for name in ("_probe_join", "_probe_exists"):
        monkeypatch.setattr(pd.probes, name, _recording(probes, name))
    with_pushdown, without_pushdown = both_ways(_probe_dataset(probe_quads, union), sparql)
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == rows
    assert (probed in probes) == (fanout == 0)


@pytest.mark.parametrize("case", PROBE_MULTIPLICITY)
def test_an_existence_probe_asks_for_one_row(probe_quads, monkeypatch, case):
    """A fully bound probe asks for one row in any graph: a fixed graph holds
    a triple at most once, the union default graph is a set too, and an
    EXISTS test is boolean."""
    union, sparql, _, _, expected = PROBE_MULTIPLICITY[case]
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    dataset = _probe_dataset(probe_quads, union)
    native = counting_native(dataset)
    register_sparql_pushdown()
    run(dataset, sparql)
    asked = [limit for args, limit in native.count_calls if None not in args[:3]]
    assert asked and set(asked) == {expected}


def test_hash_joined_bgp_keeps_a_columnar_body(graph, monkeypatch):
    """A hash-joined BGP's relation is columns, not row tuples — tuples
    appear only as rows are consumed."""
    rels = []
    original = pd.planner._join_incremental_bgp
    monkeypatch.setattr(
        pd.planner, "_join_incremental_bgp", lambda *a: rels.append(original(*a)) or rels[-1]
    )
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?a ?b ?c WHERE {
            ?a <http://ex.org/knows> ?b .
            ?c <http://ex.org/knows> ?b }""",
    )
    assert rows
    assert rels and rels[-1].cols is not None


@pytest.mark.parametrize(
    ("extra_pattern", "expected_rows"),
    [("", 25), ("?s <http://ex.org/r> ?c .", 50)],
    ids=["ready-at-last-step", "ready-mid-join"],
)
def test_tuple_filtered_bgp_keeps_a_columnar_body(
    tmp_path, monkeypatch, extra_pattern, expected_rows
):
    """A FILTER over two variables of a hash-joined BGP leaves its body
    columnar when pruning cannot pay: at the last join step there is nothing
    left to prune for, and mid-join `r`'s 100 rows are too few for any
    pruned relation to be probed into (that takes fewer than 100 / 100).

    `?a < ?b` holds for the even subjects only, and each subject has two `r`
    values, so 25 rows over two patterns and 50 over three."""
    graph = three_predicate_graph(tmp_path, "s", lambda i: i + 1000 if i % 2 == 0 else i - 1000)
    rels = []
    original = pd.planner._join_incremental_bgp
    monkeypatch.setattr(
        pd.planner, "_join_incremental_bgp", lambda *a: rels.append(original(*a)) or rels[-1]
    )
    register_sparql_pushdown()
    rows = run(
        graph,
        f"""SELECT ?s WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            {extra_pattern}
            FILTER(?a < ?b) }}""",
    )
    assert len(rows) == expected_rows
    assert {str(row[0]) for row in rows} == {f"http://ex.org/s{i}" for i in range(0, 50, 2)}
    assert rels and rels[-1].cols is not None, "the tuple FILTER materialized the BGP as rows"


@pytest.mark.parametrize(
    "extra_pattern", ["", "?s <http://ex.org/r> ?c ."], ids=["two-patterns", "three-patterns"]
)
def test_limit_streams_a_tuple_filter_over_a_hash_join(tmp_path, extra_pattern):
    """Under a LIMIT, a FILTER over two variables of a hash-joined BGP runs
    as the LIMIT pulls rows, not over the whole join first. All 1,000 `p`/`q`
    rows pass and 5 are asked for: streaming decodes the few terms those rows
    need, where evaluating the join first decodes its 2,000 distinct `?a` and
    `?b` codes. With `r` still to join, the FILTER is evaluable mid-join, but
    `r`'s 2,000 rows could only be probed from fewer than 20 survivors, so
    pruning stops as soon as 20 pass and leaves the rest to stream."""
    graph = three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=1000)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        f"""SELECT ?s WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            {extra_pattern}
            FILTER(?a < ?b) }} LIMIT 5""",
    )
    assert len(rows) == 5
    assert counting.decoded < 1000, f"decoded {counting.decoded} codes for a LIMIT 5"


@pytest.mark.parametrize("in_graph", [False, True], ids=["bgp", "graph-block"])
def test_tuple_filter_prunes_a_bgp_so_a_remaining_pattern_is_probed(
    tmp_path, monkeypatch, in_graph
):
    """When pruning lets a remaining pattern be probed instead of matched
    whole, the FILTER prunes mid-join, as in BSBM Explore Q5. `?a < ?b`
    holds for `s0` only, so one row survives the `p`/`q` join, and `r`'s 400
    rows are probed from it (1 × 100 < 400) rather than matched and
    hash-joined, as the unpruned 50 rows would be (50 × 100 ≥ 400). A GRAPH
    block is no barrier: the conjunct reaches every BGP the FILTER's
    single-variable conjuncts do."""
    graph = three_predicate_graph(
        tmp_path,
        "s",
        lambda i: 1000 if i == 0 else i - 1000,
        r_per_subject=8,
        graph_name="http://ex.org/g" if in_graph else None,
    )
    probes = []
    original = pd.probes._probe_join
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original(*a, **k)
    )
    register_sparql_pushdown()
    patterns = "?s <http://ex.org/p> ?a . ?s <http://ex.org/q> ?b . ?s <http://ex.org/r> ?c"
    body = f"GRAPH <http://ex.org/g> {{ {patterns} }}" if in_graph else patterns
    rows = run(graph, f"SELECT ?c WHERE {{ {body} FILTER(?a < ?b) }}")
    assert sorted(str(row[0]) for row in rows) == sorted(f"r{j}" for j in range(0, 400, 50))
    assert probes, "the FILTER did not prune the relation down to a probe of r"


@pytest.mark.parametrize(
    ("q_subject", "q_value", "condition", "r_per_subject"),
    [("t", lambda i: i, "", 2), ("s", lambda i: i + 1000, "FILTER(?a > ?b)", 8)],
    ids=["hash-join-runs-empty", "tuple-filter-rejects-all"],
)
def test_bgp_that_runs_empty_before_its_last_pattern(
    tmp_path, q_subject, q_value, condition, r_per_subject
):
    """When the relation runs empty with a pattern still to join, the BGP
    still answers over every variable its patterns name: a DISTINCT over the
    variable only the unjoined `r` binds is empty, as rdflib says, rather
    than an IndexError from a schema with more variables than columns.

    In the first case `p` and `q` share no subject; in the second `?a > ?b`
    holds for none of them, and `r`'s 400 rows make pruning worth trying, so
    the FILTER empties the relation mid-join."""
    graph = three_predicate_graph(tmp_path, q_subject, q_value, r_per_subject=r_per_subject)
    with_pushdown, without_pushdown = both_ways(
        graph,
        f"""SELECT DISTINCT ?c WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            ?s <http://ex.org/r> ?c
            {condition} }}""",
    )
    assert with_pushdown == without_pushdown == []


def test_columnar_join_matches_row_join():
    """The columnar kernel produces the row join's exact rows, in its exact
    order, and declines the shapes the row join handles."""
    import random
    from array import array

    rnd = random.Random(7)
    ka = [rnd.randrange(40) for _ in range(300)]
    va = [rnd.randrange(1000) for _ in range(300)]
    kb = [rnd.randrange(40) for _ in range(200)]
    vb = [rnd.randrange(1000) for _ in range(200)]
    schema_a, schema_b = ("k", "x"), ("k", "y")
    expected = pd.relation._join(
        schema_a, list(zip(ka, va, strict=True)), schema_b, list(zip(kb, vb, strict=True))
    )[1]
    cols_a = (array("Q", ka), array("Q", va))
    cols_b = (array("Q", kb), array("Q", vb))
    joined = pd.relation._join_columns(schema_a, cols_a, schema_b, cols_b)
    assert joined is not None
    schema, cols, pairs = joined
    assert schema == ("k", "x", "y")
    assert pd.relation._cols_to_rows(cols) == expected
    assert pairs is None  # within the product bound: built uncounted
    # Several shared variables decline: the row join handles them.
    assert pd.relation._join_columns(("k", "x"), cols_a, ("k", "x"), cols_a) is None


@pytest.mark.parametrize("tail_width", [0, 1, 2], ids=["no-tail", "one-tail", "two-tails"])
def test_hash_join_kernels_join_like_a_nested_loop(tail_width):
    """Both kernels join exactly as a nested loop does, in its order: the
    row kernel buckets the build side — a key seen once holds its bare
    value, a key seen again a list, inlined per tail shape for speed — and
    the columnar one is the native `join_indices`, which pairs rows in
    nested-loop order. These keys hit every case of every copy: key 5
    twice, first at row index 0 with an empty tail (both falsy, which must
    not read as absent), 7 twice, 9 three times, 3 once, 11 only on the
    build side, and 5 probed twice."""
    from array import array

    keys_a = [5, 7, 9, 3, 5]
    keys_b = [5, 7, 7, 9, 9, 9, 3, 11, 5]
    rows_a = [(k, 100 + i) for i, k in enumerate(keys_a)]
    rows_b = [(k, 200 + j, 300 + j)[: 1 + tail_width] for j, k in enumerate(keys_b)]
    schema_a, schema_b = ("k", "x"), ("k", "y", "z")[: 1 + tail_width]
    expected = [ra + rb[1:] for ra in rows_a for rb in rows_b if ra[0] == rb[0]]

    assert pd.relation._join(schema_a, rows_a, schema_b, rows_b) == (
        schema_a + schema_b[1:],
        expected,
    )
    cols_a = tuple(array("Q", column) for column in zip(*rows_a, strict=True))
    cols_b = tuple(array("Q", column) for column in zip(*rows_b, strict=True))
    joined = pd.relation._join_columns(schema_a, cols_a, schema_b, cols_b)
    assert joined is not None
    assert pd.relation._cols_to_rows(joined[1]) == expected


def test_repeated_pattern_is_matched_once(graph):
    """Two triples that differ only in their variable names resolve to the
    same quad — variables are `None` in a resolved pattern — so a self-chain
    costs one native match, not one per hop."""
    matches = []
    inner = graph.store._opened().native

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._opened().native = _Counting()
    register_sparql_pushdown()
    try:
        rows = run(
            graph,
            """SELECT ?a ?c WHERE {
                ?a <http://ex.org/knows> ?b .
                ?b <http://ex.org/knows> ?c }""",
        )
    finally:
        graph.store._opened().native = inner
    assert rows, "the self-chain has solutions to find"
    assert len(matches) == 1, matches


def test_distinct_patterns_are_matched_separately(graph):
    """The memo is keyed by the resolved quad, so patterns that differ in a
    bound position still get a match each."""
    matches = []
    inner = graph.store._opened().native

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._opened().native = _Counting()
    register_sparql_pushdown()
    try:
        run(
            graph,
            """SELECT ?s WHERE {
                ?s <http://ex.org/knows> ?o .
                ?s <http://ex.org/name> ?n }""",
        )
    finally:
        graph.store._opened().native = inner
    assert len(matches) == 2, matches
