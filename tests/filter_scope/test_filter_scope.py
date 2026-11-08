"""A group's solutions carry the bindings of its own patterns only, as in
rdflib's evaluator.

A FILTER sees the bindings of its own group: a variable the group does not
bind is unbound in the expression, even where an enclosing ``GRAPH ?g``, join
or OPTIONAL binds it, and a MINUS's right side binds nothing in the group. A
FILTER directly inside an OPTIONAL is that LeftJoin's condition and sees the
left side's bindings as well. Where rdflib keeps an outer binding the
expression names outside a comparison (the Filter node's ``_vars``), the
pushdown gives rdflib's answer too.

``GRAPH ?g`` solves its group once per named graph and joins ``?g`` in after
it. rdflib solves the right side of a lazy join or an OPTIONAL, and an EXISTS
body, once per row with that row's bindings: a MINUS there reads them on its
right side and always shares them between its sides, and an EXISTS key the
body leaves unbound is the row's. A FILTER rdflib evaluates itself once per
row forgets the row's bindings outside its node's ``_vars``, an EXISTS in it
included."""

import pytest
from rdflib import Dataset
from tests.filter_corpus import BY_ID, GROUP_SHAPES, PREFIX, REENTRY_SHAPES, SHAPES
from tests.support import both_ways, captured_trace, run

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown


@pytest.mark.parametrize("sparql", SHAPES)
def test_a_filter_sees_only_its_own_groups_bindings(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_a_filter_on_a_variable_the_graph_group_binds_stays_native(
    quad_files, monkeypatch, capsys, in_memory
):
    """Under GRAPH ?g, a conjunct on a variable the group's patterns bind is
    evaluated by vortex-rdf over the codes of the group's match (the two
    named graphs' values), beside a conjunct that reads ?g unbound."""
    dataset = Dataset(
        store=VortexRdflibStore(str(quad_files["copy"]), in_memory=in_memory), default_union=True
    )
    register_sparql_pushdown()
    for condition in ('strstarts(str(?o), "y")', 'strstarts(str(?o), "y") && !bound(?g)'):
        query = f"{PREFIX}SELECT ?s ?g WHERE {{ GRAPH ?g {{ ?s ex:r ?o FILTER({condition}) }} }}"
        monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
        capsys.readouterr()
        rows = run(dataset, query)
        native = [e for e in captured_trace(capsys) if e["event"] == "native_filter_complete"]
        monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
        assert [(e["kind"], e["candidate_codes"], e["passed_codes"]) for e in native] == [
            ("str_prefix", 2, 2)
        ]
        assert rows == both_ways(dataset, query)[1]
        assert len(rows) == 2


@pytest.mark.parametrize("sparql", GROUP_SHAPES)
def test_graph_minus_and_exists_answer_as_rdflib_does(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("union", [True, False], ids=["union", "default-graph"])
@pytest.mark.parametrize("sparql", REENTRY_SHAPES)
def test_an_exists_re_entered_with_a_row_sees_only_its_filters_bindings(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown


# One shape per family: a FILTER under GRAPH ?g, under a constant GRAPH, in a
# nested group, as an OPTIONAL's condition and in a group in an OPTIONAL; a
# MINUS; an EXISTS semi-join and an EXISTS rdflib's evaluator decides; a
# re-entered EXISTS, over both datasets; VALUES ahead of a pattern and in a
# group a row re-enters.
SMOKE = [
    pytest.param(str(BY_ID[shape].values[0]), union, id=shape + suffix)
    for shape, union, suffix in (
        ("graph-eq", True, ""),
        ("named-graph-eq", True, ""),
        ("nested-shared-eq", True, ""),
        ("optional-condition-eq", True, ""),
        ("optional-nested-eq", True, ""),
        ("nested-minus", True, ""),
        ("named-graph-exists", True, ""),
        ("or-exists", True, ""),
        ("reentry-exists-filter-coalesce", True, "-union"),
        ("reentry-exists-filter-coalesce", False, "-default-graph"),
        ("values-first-pattern-eq", True, ""),
        ("row-values-coalesce-eq", True, ""),
    )
]


@pytest.mark.parametrize(("sparql", "union"), SMOKE)
def test_each_family_answers_as_rdflib_does_in_every_configuration(any_dataset, route, sparql):
    with_pushdown, without_pushdown = both_ways(any_dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown
