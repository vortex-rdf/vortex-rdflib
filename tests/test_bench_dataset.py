"""Invariants of the benchmark generator and query set.

`bench/` is not part of the published package, and `uv run pytest` does not
collect it — but `bench/test_codspeed.py` builds the query set *at import*,
so a generator that cannot derive a constant at some size breaks collection
of the CodSpeed job rather than failing a test. These are the cheap checks
that catch that here, where every CI run sees them.

The sizes span the ones that matter: the CodSpeed default (32,768), the
dashboard scale (250,000), and the neighbours where the nudged moduli land on
different values — `n_pred` is 32 at some sizes and 33 or 37 at others, and
it is the interaction between `n_pred` and `n_graph` that the graph
partition depends on.
"""

import math

import pytest
from bench.dataset import (
    DatasetConfig,
    config_from_env,
    graph_of_subject,
    moduli,
    quad_nq,
    triple_nt,
)
from bench.queries import build_queries

#: Small enough to walk in a test, plus the two sizes the suites actually use.
SIZES = [4_000, 8_000, 16_384, 32_768, 50_000, 65_536]
DEFAULTS = config_from_env()


def _config(n: int) -> DatasetConfig:
    return DatasetConfig(
        n=n,
        subject_ratio=DEFAULTS.subject_ratio,
        predicates=DEFAULTS.predicates,
        object_ratio=DEFAULTS.object_ratio,
        literal_frac=DEFAULTS.literal_frac,
        graphs=DEFAULTS.graphs,
    )


@pytest.mark.parametrize("n", SIZES)
def test_every_query_constant_can_be_derived(n):
    """Every query's constants come from the residue arithmetic, so the set
    must build at any size — this is what the CodSpeed suite does at import."""
    cfg = _config(n)
    queries = build_queries(cfg, moduli(cfg))
    assert {q.group for q in queries} == {"lookups", "joins", "features", "graphs"}
    assert all(q.sparql.strip() for q in queries)


@pytest.mark.parametrize("n", SIZES)
def test_graph_count_is_coprime_with_the_predicate_count(n):
    """The subjects carrying one predicate step by `n_pred`, and a subject's
    graph is its index mod `n_graph`. A shared factor pins the graph to the
    predicate across a whole block of `n_subj` rows, which makes any query
    reading one block see a single graph."""
    m = moduli(_config(n))
    assert math.gcd(m.n_pred, m.n_graph) == 1


@pytest.mark.parametrize("n", SIZES)
def test_the_named_graphs_carry_every_predicate(n):
    """Not just the union: each *named* graph has to hold rows for every
    predicate, or a graph-scoped query is empty for reasons no constant
    derivation can see."""
    cfg = _config(n)
    m = moduli(cfg)
    per_graph: dict[int, set[int]] = {}
    for i in range(cfg.n):
        per_graph.setdefault(graph_of_subject(i % m.n_subj, m), set()).add(i % m.n_pred)
    assert set(per_graph) == set(range(m.n_graph)), "some graph holds nothing"
    for graph, predicates in per_graph.items():
        assert len(predicates) == m.n_pred, f"graph {graph} is missing predicates"


@pytest.mark.parametrize("n", [4_000, 32_768])
def test_the_quads_are_the_triples_one_graph_each(n):
    """The union of the graphs is the triple set: `write_ntriples` is a
    flattening of `write_nquads`, which is what keeps the stores without named
    graphs comparable on every query that does not name one."""
    cfg = _config(n)
    m = moduli(cfg)
    triples = [triple_nt(i, cfg, m) for i in range(cfg.n)]
    assert len(set(triples)) == cfg.n  # distinct with no dedupe set
    # Each quad is its triple plus a graph, so dropping the graph recovers it.
    for i in (0, 1, cfg.n // 2, cfg.n - 1):
        graph = quad_nq(i, cfg, m).removesuffix(" .").removeprefix(triples[i].removesuffix(" ."))
        assert graph.strip() in (
            "",
            f"<http://data.example.org/graph/{i % m.n_subj % m.n_graph:04d}>",
        )
