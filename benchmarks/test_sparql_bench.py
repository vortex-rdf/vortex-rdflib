"""SPARQL benchmarks over the synthetic query set.

The query set is ``bench/queries.py`` — lookups, joins and rdflib operators
layered on a BGP — so these numbers line up with the panels of the comparative
dashboard. Three angles are measured:

- every query against the fast configuration (dictionary layout, in memory,
  BGP pushdown on);
- the join queries with the pushdown unregistered, which is the A/B that
  isolates whole-BGP evaluation from rdflib's per-binding nested loop;
- a lookup and a star join against a file-backed store, where the per-call
  native match floor dominates.
"""

import pytest
from bench.dataset import DatasetConfig, moduli
from bench.queries import Query, build_queries
from rdflib import Graph

from vortex_rdflib import VortexStore, register_sparql_pushdown, unregister_sparql_pushdown

# Query *names* do not depend on the dataset scale, so the parametrization is
# generated from the query set itself with a throwaway config: adding a query
# to bench/queries.py adds a CodSpeed benchmark, with no list to keep in sync.
_NAMING_CONFIG = DatasetConfig(
    n=4_000, subject_ratio=0.1, predicates=32, object_ratio=0.5, literal_frac=0.4
)
QUERY_NAMES = tuple(q.name for q in build_queries(_NAMING_CONFIG, moduli(_NAMING_CONFIG)))

# Queries whose cost is dominated by the BGP join strategy.
JOIN_QUERY_NAMES = ("star-2", "star-3", "chain-2", "optional")

# Cheap enough to also run against the file-backed store.
FILE_BACKED_QUERY_NAMES = ("po-lookup", "star-2")


@pytest.fixture(scope="session")
def memory_graph(dict_memory_store: VortexStore) -> Graph:
    return Graph(store=dict_memory_store)


@pytest.fixture(scope="session")
def file_graph(dict_file_store: VortexStore) -> Graph:
    return Graph(store=dict_file_store)


@pytest.fixture
def without_pushdown():
    """Drop the CUSTOM_EVALS hook for one benchmark, then put it back."""
    unregister_sparql_pushdown()
    yield
    register_sparql_pushdown()


def consume(graph: Graph, query: Query) -> int:
    """Execute and fully consume a query; ASK answers count as one row."""
    result = graph.query(query.sparql)
    if query.is_ask:
        return 1 if result.askAnswer else 0
    return sum(1 for _ in result)


@pytest.mark.parametrize("query_name", QUERY_NAMES)
def test_query(benchmark, memory_graph: Graph, queries: dict, query_name: str):
    query = queries[query_name]
    assert consume(memory_graph, query) > 0
    benchmark(consume, memory_graph, query)


@pytest.mark.parametrize("query_name", JOIN_QUERY_NAMES)
def test_query_without_pushdown(
    benchmark, without_pushdown, memory_graph: Graph, queries: dict, query_name: str
):
    query = queries[query_name]
    assert consume(memory_graph, query) > 0
    benchmark(consume, memory_graph, query)


@pytest.mark.parametrize("query_name", FILE_BACKED_QUERY_NAMES)
def test_query_file_backed(benchmark, file_graph: Graph, queries: dict, query_name: str):
    query = queries[query_name]
    assert consume(file_graph, query) > 0
    benchmark(consume, file_graph, query)
