"""A conjunct on a variable a VALUES table binds holds on the table's rows —
alone, joined, in a group, on a MINUS's left side, in a MINUS, a GRAPH or
an EXISTS body — and an UNDEF beside a term the dictionary lacks leaves
the answer rdflib's."""

import pytest
from tests.filter_corpus import PREFIX
from tests.filter_values_corpus import SPEC_ANSWERS, VALUES_SHAPES, VALUES_UNDEF_SHAPES
from tests.support import both_ways


@pytest.mark.parametrize("sparql", VALUES_SHAPES)
def test_a_filter_on_a_values_variable_answers_as_rdflib_does(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == SPEC_ANSWERS.get(sparql, without_pushdown)


@pytest.mark.parametrize("sparql", VALUES_UNDEF_SHAPES)
def test_values_undef_and_unknown_terms_through_joins(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown
