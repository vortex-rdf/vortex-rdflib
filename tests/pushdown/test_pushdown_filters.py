"""The pushdown's FILTERs: conjuncts pushed into a pattern scan, the fast
route, a term decoded once, the query's initBindings, and the IRI keep
paths against rdflib's answers."""

import pytest
from rdflib import Literal, URIRef
from tests.support import (
    both_ways,
    counting_term_dict,
    row_key,
    run,
    three_predicate_graph,
    trace_events,
)

import vortex_rdflib.pushdown as pd
from vortex_rdflib import filters, register_sparql_pushdown, unregister_sparql_pushdown
from vortex_rdflib.settings import SETTINGS


def test_filter_decodes_a_generic_only_conjunct_once(tmp_path):
    """A FILTER conjunct with no fast route is answered by rdflib's evaluator
    over the store's decoded terms, so the batch decode that readies the fast
    route's parsed terms must skip it: each of the 20 `?a` and 20 `?b` codes
    of this 400-row cross product is decoded once."""
    graph = three_predicate_graph(tmp_path, "t", lambda i: i + 1000, n=20)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?a WHERE {
            ?s <http://ex.org/p> ?a .
            ?t <http://ex.org/q> ?b
            FILTER(CONCAT(STR(?a), STR(?b)) = "x") }""",
    )
    assert rows == []
    assert counting.decoded == 40


def test_filter_reuses_the_terms_its_early_pass_decoded(tmp_path):
    """Terms a mid-join FILTER attempt decoded before deferring are not
    decoded again by the FILTER above. Pruning stops once 20 of the 1,000
    `p`/`q` rows pass; the FILTER then reads all 2,000 joined rows, so each
    of the 1,000 `?a` and 1,000 `?b` codes is decoded once, plus the 1,000
    subjects the answer names."""
    graph = three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=1000)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?s WHERE {
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            ?s <http://ex.org/r> ?c
            FILTER(?a < ?b) }""",
    )
    assert len(rows) == 2000
    assert counting.decoded == 3000


def test_filter_is_pushed_into_the_pattern_scan(graph, monkeypatch):
    """A single-variable conjunct restricts the pattern before any row is
    built, on the fast route: rdflib's evaluator is never consulted."""
    restrictions = []
    original = pd.matching._restrict_pattern
    monkeypatch.setattr(
        pd.matching, "_restrict_pattern", lambda *a: restrictions.append(1) or original(*a)
    )
    generic_calls = []
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(1) or original_generic(self, b),
    )
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?x ?v WHERE { ?x <http://ex.org/score> ?v FILTER(?v > 1) }")
    assert {row[0] for row in rows} == {URIRef("http://ex.org/erin"), URIRef("http://ex.org/frank")}
    assert restrictions and not generic_calls


def test_str_of_a_number_is_answered_by_the_fast_route(graph, monkeypatch):
    """``str(?a)`` of a numeric literal is its lexical form as written: the
    fast route answers every value itself, so dave's "042" is no "42", and no
    value goes to rdflib's evaluator."""
    generic_calls = []
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(b) or original_generic(self, b),
    )
    register_sparql_pushdown()
    rows = run(graph, 'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(str(?a) = "42") }')
    assert {row[0] for row in rows} == {URIRef("http://ex.org/bob")}
    assert generic_calls == []


def test_a_filter_over_a_block_rdflib_solves_answers_as_the_pushdown(graph, monkeypatch, capsys):
    """A join over the pair budget sends its block back to rdflib, which
    solves the pattern; the FILTER above it is still the pushdown's, over
    rdflib's terms, so the answer does not depend on how large the join got:
    erin's ill-typed age orders against nothing (§17.3)."""
    sparql = (
        "SELECT ?a ?b WHERE { ?x <http://ex.org/age> ?a . ?y <http://ex.org/age> ?b "
        "FILTER(?a < ?b) }"
    )
    register_sparql_pushdown()
    expected = run(graph, sparql)
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    assert run(graph, sparql) == expected
    events = [e["event"] for e in trace_events(capsys.readouterr().err)]
    assert "bgp_over_budget" in events and "filter_over_rdflib_rows" in events
    integer = URIRef("http://www.w3.org/2001/XMLSchema#integer")
    assert not any(Literal("abc", datatype=integer) in row for row in expected)


def test_init_bindings_are_visible_to_filters(graph):
    sparql = "SELECT ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?x = ?who && ?a > 0) }"
    init = {"who": URIRef("http://ex.org/bob")}
    register_sparql_pushdown()
    with_pushdown = sorted((tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key)
    try:
        unregister_sparql_pushdown()
        without_pushdown = sorted(
            (tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key
        )
    finally:
        register_sparql_pushdown()
    assert with_pushdown == without_pushdown == [(Literal(42),)]


#: Per path of an IRI keep, a query whose answer changes if the path breaks:
#: ``(query, IRIs bound through initBindings, rows rdflib answers)``.
IRI_KEEP_PATHS = {
    "column-tested-on-probed-rows": (
        """SELECT * WHERE {
            <http://ex.org/alice> <http://ex.org/knows> ?v . ?v <http://ex.org/knows> ?w
            FILTER(?w IN (<http://ex.org/bob>, <http://ex.org/carol>)) }""",
        None,
        1,
    ),
    "columns-matched-apart": (
        """SELECT * WHERE { ?a ?p ?x . ?b ?q ?y
            FILTER(?x IN (<http://ex.org/bob>)) FILTER(?y IN (<http://ex.org/carol>)) }""",
        None,
        8,
    ),
    "columns-intersect": (
        """SELECT ?s ?o WHERE { ?s ?p ?o
            FILTER(?o IN (<http://ex.org/bob>, <http://ex.org/carol>)
                && ?o IN (<http://ex.org/carol>, <http://ex.org/dave>)) }""",
        None,
        4,
    ),
    "columns-intersect-to-nothing": (
        """SELECT ?s ?o WHERE { ?s ?p ?o
            FILTER(?o IN (<http://ex.org/bob>) && ?o IN (<http://ex.org/carol>)) }""",
        None,
        0,
    ),
    "iri-that-is-no-iriref": (
        "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(?o = ?y) }",
        {"y": "http://ex.org/bob>"},
        0,
    ),
}


@pytest.mark.parametrize("fanout", [100, 0])
@pytest.mark.parametrize(
    ("sparql", "init", "expected"), list(IRI_KEEP_PATHS.values()), ids=list(IRI_KEEP_PATHS)
)
def test_iri_keep_paths_agree_with_rdflib(file_graph, monkeypatch, fanout, sparql, init, expected):
    """The paths an IRI keep takes, each with a query whose answer changes if
    the path breaks, hash-joined (``SETTINGS.probe_fanout`` 100) and probed (0):

    - a probed pattern's free variable tests the keep's codes on the rows each
      probe returns (``keep_member``);
    - two patterns narrowed by different code sets at one position are two
      matches, not one (``_pattern_key``);
    - code sets on one variable intersect (``intersect_keeps``);
    - an IRI that is no N-Triples IRIREF makes no keep (``_SPELLABLE_IRI``):
      a bound variable's here, which ``encode_many`` would read as another
      IRI."""
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    bindings = None if init is None else {name: URIRef(iri) for name, iri in init.items()}

    def answer(graph, sparql):
        rows = graph.query(sparql, initBindings=bindings)
        return sorted((tuple(row) for row in rows), key=row_key)

    with_pushdown, without_pushdown = both_ways(file_graph, sparql, answer)
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == expected
