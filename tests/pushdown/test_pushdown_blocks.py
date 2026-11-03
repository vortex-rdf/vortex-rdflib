"""The pushdown's blocks in code space: OPTIONAL's probe and hash paths,
MINUS as an anti-join, EXISTS as a semi-join, VALUES as a relation, and a
scope it cannot keep handed back to rdflib."""

from rdflib import Literal, URIRef
from tests.support import (
    both_ways,
    run,
)

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    filters,
    register_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


def test_left_join_probe_and_hash_paths_are_ours(graph, monkeypatch):
    """An OPTIONAL is solved inside the hook — the inner pattern probed per
    outer row under a low threshold, hash-joined under a high one — never by
    rdflib re-entering the BGP hook once per outer solution."""
    left_joins, probes, bgps = [], [], []
    original_left = pd.blocks._solve_left_join
    original_probe = pd.probes._probe_join
    original_bgp = pd.bgp._solve_bgp
    monkeypatch.setitem(
        pd.blocks._SOLVERS,
        pd.algebra.LeftJoin,
        lambda *a: left_joins.append(1) or original_left(*a),
    )
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original_probe(*a, **k)
    )
    monkeypatch.setattr(pd.bgp, "_solve_bgp", lambda *a: bgps.append(1) or original_bgp(*a))
    register_sparql_pushdown()
    sparql = (
        "SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }"
    )
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    assert len(run(graph, sparql)) == 8
    assert left_joins and probes and len(bgps) == 1
    left_joins.clear(), probes.clear(), bgps.clear()
    monkeypatch.setattr(SETTINGS, "probe_fanout", 10**9)
    assert len(run(graph, sparql)) == 8
    assert left_joins and not probes and len(bgps) == 1


def test_minus_is_an_anti_join_in_code_space(graph, monkeypatch):
    calls = []
    original = pd.blocks._solve_minus
    monkeypatch.setitem(
        pd.blocks._SOLVERS, pd.algebra.Minus, lambda *a: calls.append(1) or original(*a)
    )
    register_sparql_pushdown()
    rows = run(
        graph, "SELECT ?x WHERE { ?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/age> ?a } }"
    )
    subjects = {row[0] for row in rows}
    assert len(rows) == 3  # alice, carol and the blank node have a name but no age
    assert {URIRef("http://ex.org/alice"), URIRef("http://ex.org/carol")} <= subjects
    assert calls


def test_cheated_scope_falls_back_to_rdflib(graph, monkeypatch):
    """An OPTIONAL whose inner pattern uses a variable bound outside its own
    left side is re-checked by rdflib with that binding dropped; that shape
    is handed back, and still answered correctly."""
    left_joins = []
    original = pd.blocks._solve_left_join

    def spy(*args):
        try:
            return original(*args)
        except NotImplementedError:
            left_joins.append("fallback")
            raise

    monkeypatch.setitem(pd.blocks._SOLVERS, pd.algebra.LeftJoin, spy)
    sparql = """SELECT ?x ?a ?y WHERE {
        { VALUES ?a { 42 } }
        { ?x <http://ex.org/name> ?n OPTIONAL { ?y <http://ex.org/age> ?a } } }"""
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown
    assert "fallback" in left_joins


def test_exists_is_a_semi_join_in_code_space(graph, monkeypatch):
    """A (NOT) EXISTS conjunct is applied as a set membership on the shared
    variables — probed per row for a small block, hash-joined otherwise —
    without rdflib's evaluator running per row."""
    applied, probes, generic_calls = [], [], []
    original_apply = pd.blocks._apply_exists
    original_probe = pd.probes._probe_exists
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        pd.blocks, "_apply_exists", lambda *a: applied.append(1) or original_apply(*a)
    )
    monkeypatch.setattr(
        pd.probes, "_probe_exists", lambda *a: probes.append(1) or original_probe(*a)
    )
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(1) or original_generic(self, b),
    )
    register_sparql_pushdown()
    sparql = """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }"""
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    assert len(run(graph, sparql)) == 3 and applied and probes and not generic_calls
    applied.clear(), probes.clear()
    monkeypatch.setattr(SETTINGS, "probe_fanout", 10**9)
    assert len(run(graph, sparql)) == 3 and applied and not probes and not generic_calls
    # a body FILTER that sees the block's bindings: rdflib's evaluator, per distinct tuple
    rows = run(
        graph,
        """SELECT ?x WHERE { ?x <http://ex.org/age> ?a
            FILTER EXISTS { ?x <http://ex.org/score> ?s FILTER(?s > ?a) } }""",
    )
    assert rows == [(URIRef("http://ex.org/frank"),)] and generic_calls


def test_values_is_a_code_space_relation(graph, monkeypatch):
    calls = []
    original = pd.blocks._solve_values
    monkeypatch.setitem(
        pd.blocks._SOLVERS, pd.algebra.Values, lambda *a: calls.append(1) or original(*a)
    )
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/nobody> }
            ?x <http://ex.org/name> ?n }""",
    )
    assert rows == [(URIRef("http://ex.org/bob"), Literal("Bob", lang="en"))]
    # a constant outside the dictionary is still yielded verbatim
    rows = run(graph, "SELECT ?x WHERE { VALUES ?x { <http://ex.org/nobody> } }")
    assert rows == [(URIRef("http://ex.org/nobody"),)]
    assert calls
