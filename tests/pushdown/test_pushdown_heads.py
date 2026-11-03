"""The pushdown's heads: the slice and projection chain it takes whole,
LIMIT's decoding, ASK and COUNT over one pattern answered by a count,
DISTINCT and COUNT in code space, term aliases, and ORDER BY's rank sort."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql.sparql import QueryContext
from tests.support import (
    both_ways,
    counting_native,
    counting_term_dict,
    row_key,
    run,
    run_ordered,
)
from vortex_rdf import CancelToken, serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def test_pushdown_is_actually_used(graph, monkeypatch):
    calls = []
    original = pd.bgp._solve_bgp
    monkeypatch.setattr(pd.bgp, "_solve_bgp", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert calls, "pushdown hook was not invoked"


def test_head_intercepts_the_whole_slice_project_chain(graph, monkeypatch):
    """A ``Slice(Project(BGP))`` is one interception at the Slice: rdflib
    never evaluates the Project or the BGP below it itself."""
    heads = []
    original = pd.head._eval_head
    for name in ("Project", "Slice", "Distinct", "OrderBy"):
        monkeypatch.setitem(
            pd.hook._HANDLERS, name, lambda *a: heads.append(a[2].name) or original(*a)
        )
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2
    assert heads == ["Slice"]
    heads.clear()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert heads == ["Project"]


def test_solutions_are_built_without_context_push(graph, monkeypatch):
    """Solutions are constructed directly as FrozenBindings; the per-row
    ``ctx.push()`` scope rdflib's own evalBGP opens is never needed."""
    pushes = []
    original = QueryContext.push
    monkeypatch.setattr(QueryContext, "push", lambda self: pushes.append(1) or original(self))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert not pushes


def test_limit_decodes_only_the_first_chunk(tmp_path):
    """LIMIT stops decoding: a 700-row match under ``LIMIT 10`` decodes no
    more than the first chunk of rows."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(graph, "SELECT * WHERE { ?s ?p ?o } LIMIT 10")
    assert len(rows) == 10
    assert counting.decoded <= 3 * SETTINGS.chunk_start


def test_batch_stand_ins_accept_keywords(graph):
    """The counting stand-in passes the native batch keywords on, as the real
    store takes them."""
    native = counting_native(graph)
    token = CancelToken()
    probes = [
        (None, "<http://ex.org/name>", None, None),
        ("<http://ex.org/alice>", None, None, None),
    ]
    counts = native.count_quads_many(probes, lookahead=2, cancel=token)
    answers = native.match_codes_many(probes, lookahead=2, cancel=token)
    assert counts == [len(answer[0]) for answer in answers]
    assert (native.counts, native.matches, native.batches) == (2, 2, 2)


def test_ask_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = counting_native(graph)
    assert run(graph, "ASK { ?s <http://ex.org/knows> ?o }") is True
    assert run(graph, "ASK { ?s <http://ex.org/nothing> ?o }") is False
    assert (native.counts, native.matches) == (2, 0)


def test_native_none_falls_back_at_call_time(graph):
    """A store whose code path declines (``match_codes`` -> None) makes the
    hook raise while rdflib is still listening, so the default evaluator
    answers — no lazily raised NotImplementedError reaches the caller."""
    inner = graph.store._opened().native

    class _NoCodes:
        def match_codes(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    graph.store._opened().native = _NoCodes()
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2


def test_distinct_and_count_are_answered_in_code_space(graph, monkeypatch):
    distincts, aggregates = [], []
    original_distinct = pd.head._eval_distinct
    original_aggregate = pd.aggregate._eval_aggregate
    monkeypatch.setattr(
        pd.head, "_eval_distinct", lambda *a: distincts.append(1) or original_distinct(*a)
    )
    monkeypatch.setitem(
        pd.hook._HANDLERS,
        "AggregateJoin",
        lambda *a: aggregates.append(1) or original_aggregate(*a),
    )
    register_sparql_pushdown()
    rows = run(graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }")
    assert len(rows) == 4  # "042" and "42" are the same integer
    rows = run(graph, "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p")
    assert (URIRef("http://ex.org/name"), Literal(8)) in rows
    assert distincts and aggregates


_ALIAS_QUERIES = [
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT ?a (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/age> ?a } GROUP BY ?a",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT DISTINCT ?a WHERE {
        { VALUES ?a { "0042"^^xsd:integer "43"^^xsd:integer } }
        UNION { ?x <http://ex.org/age> ?a } }""",
    "SELECT DISTINCT ?o WHERE { ?s ?p ?o }",
]


@pytest.mark.parametrize("order", [1, -1], ids=["forward", "reverse"])
def test_term_aliases_are_classified_within_each_query(graph, order):
    """Which codes alias ("042" and "42" are one integer, and so is a query's
    "0042" the dictionary lacks) is classified per query: whatever query runs
    first, each answers as rdflib does, and a query run again decodes exactly
    what it did the first time — nothing carried over from earlier queries."""
    for sparql in _ALIAS_QUERIES[::order]:
        with_pushdown, without_pushdown = both_ways(graph, sparql)
        assert with_pushdown == without_pushdown, sparql
    counting = counting_term_dict(graph)
    decoded = []
    for _ in range(2):
        before = counting.decoded
        run(graph, _ALIAS_QUERIES[0])
        decoded.append(counting.decoded - before)
    assert decoded[0] == decoded[1] > 0


def test_count_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = counting_native(graph)
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/name> ?o }")
    assert rows == [(Literal(8),)]
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/nothing> ?o }")
    assert rows == [(Literal(0),)]
    assert (native.counts, native.matches) == (2, 0)


#: COUNT(DISTINCT ?c) of a ?c bound everywhere: ``(query, initBindings,
#: the counts it answers)`` — by the context, in the columnar route (no
#: group key, one key) and the row route (two keys), and over no row; by a
#: VALUES table of the block.
_BOUND_COUNTS = {
    "context": (
        "SELECT (COUNT(DISTINCT ?c) AS ?n) WHERE { ?s <http://ex.org/name> ?o }",
        {"c": Literal(1)},
        [1],
    ),
    "context-one-key": (
        "SELECT ?s (COUNT(DISTINCT ?c) AS ?n) WHERE { ?s <http://ex.org/name> ?o } GROUP BY ?s",
        {"c": Literal(1)},
        [1] * 8,
    ),
    "context-two-keys": (
        """SELECT ?s ?o (COUNT(DISTINCT ?c) AS ?n)
        WHERE { ?s <http://ex.org/name> ?o } GROUP BY ?s ?o""",
        {"c": Literal(1)},
        [1] * 8,
    ),
    "context-no-row": (
        "SELECT (COUNT(DISTINCT ?c) AS ?n) WHERE { ?s <http://ex.org/nothing> ?o }",
        {"c": Literal(1)},
        [0],
    ),
    "values": (
        "SELECT (COUNT(DISTINCT ?c) AS ?n) WHERE { VALUES ?c { 1 } ?s <http://ex.org/name> ?o }",
        {},
        [1],
    ),
}


@pytest.mark.parametrize("case", list(_BOUND_COUNTS))
def test_count_distinct_of_a_bound_constant(graph, case):
    """A variable bound in every row has one distinct value per group: rdflib
    counts 1 per group, and 0 over no row."""
    sparql, init, counts = _BOUND_COUNTS[case]
    register_sparql_pushdown()
    with_pushdown = sorted((tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key)
    try:
        unregister_sparql_pushdown()
        without_pushdown = sorted(
            (tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key
        )
    finally:
        register_sparql_pushdown()
    assert with_pushdown == without_pushdown
    assert [row[-1] for row in with_pushdown] == [Literal(n) for n in counts]


def test_distinct_decodes_only_the_survivors(tmp_path):
    """``SELECT DISTINCT ?p`` over 700 rows with 7 predicates decodes 7 codes."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(graph, "SELECT DISTINCT ?p WHERE { ?s ?p ?o }")
    assert len(rows) == 7
    assert counting.decoded == 7


def test_order_by_is_a_rank_sort_in_code_space(graph, monkeypatch):
    ordered = []
    original = pd.head._order_rows
    monkeypatch.setattr(pd.head, "_order_rows", lambda *a: ordered.append(a[4]) or original(*a))
    register_sparql_pushdown()
    rows = run_ordered(
        graph, "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x LIMIT 2"
    )
    assert [row[1].toPython() for row in rows] == [-3, 7]  # the 7 is an xsd:byte
    assert ordered == [2]  # the LIMIT bounds the sort to the top 2
    ordered.clear()
    rows = run_ordered(
        graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a LIMIT 2"
    )
    assert [row[0].toPython() for row in rows] == [-3, 7]
    assert ordered == [None]  # DISTINCT narrows after the sort: no top-k
