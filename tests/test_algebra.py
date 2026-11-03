"""The typed blocks the solver takes (``pushdown.algebra.block``): what each
kind mentions, binds and may leave unbound, whether its rows carry a
``GRAPH ?g``'s column, and what conversion refuses — the capability check."""

import pytest
from rdflib.plugins.sparql import prepareQuery
from rdflib.term import Variable

import vortex_rdflib.pushdown as pd
from vortex_rdflib import settings

A, B, C, D, G = (Variable(name) for name in "abcdg")


def _where(sparql: str):
    """The block under a SELECT's projection, converted."""
    return pd.algebra.block(prepareQuery(sparql).algebra.p.p)


def test_an_optional_may_leave_its_right_side_unbound():
    node = _where("SELECT * { ?a <p> ?b OPTIONAL { ?b <q> ?c } }")
    assert isinstance(node, pd.algebra.LeftJoin)
    assert node.vars == (A, B, C)
    assert node.bound_vars == (A, B, C)
    assert node.nullable == {C}


def test_a_minus_right_side_binds_nothing():
    node = _where("SELECT * { ?a <p> ?b MINUS { ?b <q> ?c } }")
    assert isinstance(node, pd.algebra.Minus)
    assert node.vars == (A, B, C)
    assert node.bound_vars == (A, B)
    assert node.nullable == frozenset()


def test_a_values_table_may_leave_an_undef_unbound():
    node = _where("SELECT * { VALUES (?a ?b) { (1 2) (3 UNDEF) } }")
    assert isinstance(node, pd.algebra.Values)
    assert set(node.vars) == {A, B}
    assert node.nullable == {B}


def test_a_graph_variable_binds_after_its_block():
    node = _where("SELECT * { GRAPH ?g { ?a <p> ?b } }")
    assert isinstance(node, pd.algebra.Graph)
    assert node.vars == (A, B, G)
    assert node.bound_vars == (A, B, G)
    assert node.carries_graph(G)


@pytest.mark.parametrize(
    ("sparql", "carries"),
    [
        ("SELECT * { GRAPH ?g { VALUES ?a { 1 } } }", False),
        ("SELECT * { GRAPH ?g { ?a <p> ?b MINUS { ?b <q> ?c } } }", True),
        ("SELECT * { GRAPH ?g { { VALUES ?a { 1 } } { ?a <p> ?b } } }", True),
    ],
    ids=["values-only", "minus-left", "join-either"],
)
def test_a_block_carries_the_graph_column_where_a_pattern_is_matched(sparql, carries):
    node = _where(sparql)
    assert isinstance(node, pd.algebra.Graph)
    assert node.inner.carries_graph(G) is carries


def test_an_optional_whose_right_side_alone_carries_the_graph_is_refused():
    node = _where("SELECT * { GRAPH ?g { { VALUES ?a { 1 } } OPTIONAL { ?a <p> ?b } } }")
    with pytest.raises(NotImplementedError):
        node.inner.carries_graph(G)


@pytest.mark.parametrize(
    "sparql",
    [
        "SELECT * { ?a <p> ?b BIND(1 AS ?c) }",
        "SELECT * { { ?a <p> ?b } UNION { ?a <q> ?b } }",
    ],
    ids=["bind", "union"],
)
def test_conversion_refuses_what_the_solver_does_not_take(sparql):
    """Conversion checks the shape; a pattern's own terms (a property path)
    are checked when it is resolved."""
    with pytest.raises(NotImplementedError):
        _where(sparql)


def test_every_operation_has_a_handler_function():
    assert pd.hook._HANDLERS.keys() == settings.ALL_OPS
    assert all(callable(handler) for handler in pd.hook._HANDLERS.values())


def test_every_kind_of_block_has_a_solver():
    kinds = {
        pd.algebra.Bgp,
        pd.algebra.Filter,
        pd.algebra.Join,
        pd.algebra.LeftJoin,
        pd.algebra.Minus,
        pd.algebra.Values,
        pd.algebra.Graph,
    }
    assert pd.blocks._SOLVERS.keys() == kinds


def test_the_package_exports_the_registration_functions():
    assert pd.__all__ == ["register_sparql_pushdown", "unregister_sparql_pushdown"]
