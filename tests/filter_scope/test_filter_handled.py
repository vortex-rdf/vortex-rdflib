"""The shapes the pushdown answers whole, handing no block node to rdflib,
and the routes its EXISTS shapes keep: a semi- or anti-join never asks
rdflib's evaluator; the generic route asks it once per distinct tuple."""

import pytest
from rdflib import Dataset, Variable
from tests.filter_corpus import BY_ID, PREFIX
from tests.support import both_ways, captured_trace, run

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

# Shapes the pushdown answers whole, with no block node handed to rdflib.
NATIVE = [
    BY_ID[shape]
    for shape in (
        "graph-strstarts",
        "graph-eq",
        "graph-isiri",
        "graph-bound",
        "graph-not-bound",
        "graph-tuple",
        "graph-or",
        "graph-and",
        "graph-one-pattern",
        "graph-ask-eq",
        "graph-ask-not-bound",
        "graph-count-eq",
        "graph-count-not-bound",
        "graph-limit",
        "graph-nested-eq",
        "graph-nested-outer-pattern-binds",
        "graph-optional-eq",
        "graph-optional-bound",
        "graph-optional-not-bound",
        "graph-optional-tuple",
        "graph-optional-two-patterns",
        "graph-minus-eq",
        "graph-minus-bound",
        "graph-joined-eq",
        "graph-values-eq",
        "nested-pattern-var-ne",
        "optional-nested-pattern-var-ne",
        "minus-var-bound",
        "minus-var-isiri",
        "minus-var-tuple",
        "minus-var-or",
        "minus-var-ask",
        "nested-minus-var",
        "graph-minus-var",
        "optional-minus-var",
        "graph-optional-condition-minus-var",
        "optional-over-minus-isiri",
        "optional-over-minus-bound",
        "graph-minus-disjoint",
        "graph-minus-disjoint-left-names-g",
        "graph-minus-disjoint-right-names-g",
        "graph-minus-both-name-g",
        "graph-minus-shared",
        "graph-optional-both-name-g",
        "graph-in-graph-joined",
        "graph-same-var-in-graph",
        "graph-values-joined",
        "nested-minus-shared-outer-var",
        "nested-minus",
        "optional-minus",
        "minus-disjoint",
        "exists-minus-shared-outer-var",
        "not-exists-minus",
        "exists-minus-outer-var",
        "not-exists-minus-outer-var",
        "or-exists",
        "named-graph-exists",
        "named-graph-not-exists",
        "values-eq",
        "values-joined-eq",
        "values-pair-eq",
        "values-minus-shared",
        "values-in-minus",
        "named-graph-values-eq",
        "values-pattern-eq",
        "values-first-pattern-eq",
        "graph-values-first-eq",
        "values-foreign-ne",
        "exists-values-eq",
    )
]


_BLOCK_NODES = frozenset({"BGP", "Filter", "Join", "LeftJoin", "Minus", "ToMultiSet", "Graph"})


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
@pytest.mark.parametrize("sparql", NATIVE)
def test_the_pushdown_answers_these_shapes_whole(
    quad_files, monkeypatch, capsys, in_memory, sparql
):
    dataset = Dataset(
        store=VortexRdflibStore(str(quad_files["copy"]), in_memory=in_memory), default_union=True
    )
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    run(dataset, PREFIX + sparql)
    events = captured_trace(capsys)
    assert any(e["event"] == "eval_part_complete" for e in events)
    assert {e["algebra_node"] for e in events if e["event"] == "eval_part_fallback"}.isdisjoint(
        _BLOCK_NODES
    )


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_a_values_variable_a_pattern_binds_keeps_its_route(quad_files, monkeypatch, in_memory):
    """Where a pattern binds the VALUES variable too, its scan tests the
    conjunct over its codes, as for any pushed conjunct, and the FILTER
    filters no row itself; the fast route decides every value, the table's
    own terms included."""
    dataset = Dataset(
        store=VortexRdflibStore(str(quad_files["copy"]), in_memory=in_memory), default_union=True
    )
    register_sparql_pushdown()
    restricted, filtered, generic = [], [], []
    restrict, filter_relation = pd.matching._restrict_pattern, pd.relation._filter_relation
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        pd.matching,
        "_restrict_pattern",
        lambda store, pat, preds: restricted.extend(preds) or restrict(store, pat, preds),
    )
    monkeypatch.setattr(
        pd.relation, "_filter_relation", lambda *a: filtered.append(a) or filter_relation(*a)
    )
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, bound: generic.append(bound) or original_generic(self, bound),
    )
    query = PREFIX + str(BY_ID["values-pattern-eq"].values[0])
    rows = run(dataset, query)
    assert restricted == [Variable("o")] and not filtered and not generic
    assert rows == both_ways(dataset, query)[1] and len(rows) == 1


# The EXISTS shapes among those: a semi- or anti-join never asks rdflib's
# evaluator; the generic route, for a body that reads the row's values or an
# EXISTS under another operator, asks it once per distinct tuple, without
# handing the FILTER to rdflib.
EXISTS_ROUTES = [
    pytest.param(str(BY_ID[shape].values[0]), generic, id=shape)
    for shape, generic in (
        ("exists-minus-shared-outer-var", False),
        ("not-exists-minus", False),
        ("named-graph-exists", False),
        ("named-graph-not-exists", False),
        ("exists-values-eq", False),
        ("exists-minus-outer-var", True),
        ("not-exists-minus-outer-var", True),
        ("or-exists", True),
    )
]


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
@pytest.mark.parametrize(("sparql", "generic"), EXISTS_ROUTES)
def test_an_exists_the_pushdown_answers_whole_keeps_its_route(
    quad_files, monkeypatch, in_memory, sparql, generic
):
    dataset = Dataset(
        store=VortexRdflibStore(str(quad_files["copy"]), in_memory=in_memory), default_union=True
    )
    register_sparql_pushdown()
    calls = []
    original = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, bound: calls.append(bound) or original(self, bound),
    )
    run(dataset, PREFIX + sparql)
    assert bool(calls) is generic
