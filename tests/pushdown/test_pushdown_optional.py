"""An OPTIONAL answers as SPARQL 1.1 does (#24). The pushdown answers the
shapes it used to hand back to rdflib only to copy rdflib's re-check of an
unmatched row — a VALUES table on the left, a condition reading a variable
its left side binds, an enclosing join's variable on the right — and checks
each against Oxigraph. It still hands back a ``GRAPH ?g`` whose OPTIONAL
alone binds ``?g``, where rdflib's answer is the specification's (§18.6:
the inner pattern is evaluated with ``?g`` free, then joined with the graph's
name) and Oxigraph's departs, and an OPTIONAL under bindings rdflib
substituted itself."""

import pytest
import rdflib.plugins.sparql.evaluate as rdflib_evaluate
from rdflib import Dataset, URIRef, Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.sparql import QueryContext
from tests import oracle
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown
from vortex_rdflib import pushdown as pd

DATA = """
<http://ex/s0> <http://ex/optional> <http://ex/o0> .
<http://ex/s1> <http://ex/optional> <http://ex/o1> .
<http://ex/x1> <http://ex/name> "one" .
<http://ex/x2> <http://ex/name> "two" .
<http://ex/y> <http://ex/knows> <http://ex/z1> .
<http://ex/y> <http://ex/knows> <http://ex/z2> .
<http://ex/z1> <http://ex/mbox> <http://ex/x1> .
<http://ex/x1> <http://ex/knows2> <http://ex/z1> .
<http://ex/x2> <http://ex/knows2> <http://ex/z1> .
<http://ex/a> <http://ex/p> <http://ex/b> <http://ex/g1> .
<http://ex/b> <http://ex/q> <http://ex/g2> <http://ex/g1> .
<http://ex/c> <http://ex/p> <http://ex/d> <http://ex/g1> .
"""

SPEC_SHAPES = {
    "values-left": """SELECT ?o ?s WHERE { VALUES ?o { <http://ex/o0> <http://ex/unknown> }
        OPTIONAL { ?s <http://ex/optional> ?o } }""",
    "values-left-undef": """SELECT ?o ?s WHERE {
        VALUES ?o { UNDEF <http://ex/o0> <http://ex/unknown> }
        OPTIONAL { ?s <http://ex/optional> ?o } }""",
    "values-left-condition": """SELECT ?o ?s WHERE {
        VALUES ?o { <http://ex/o0> <http://ex/o1> <http://ex/unknown> }
        OPTIONAL { ?s <http://ex/optional> ?o FILTER(?s != <http://ex/s1>) } }""",
    "values-left-nested": """SELECT ?o ?s ?n WHERE {
        VALUES ?o { <http://ex/o0> <http://ex/unknown> }
        OPTIONAL { ?s <http://ex/optional> ?o OPTIONAL { ?s <http://ex/name> ?n } } }""",
    "condition-reads-joined-var": """SELECT ?x ?z ?m WHERE { ?x <http://ex/name> ?n .
        { ?x <http://ex/knows2> ?z OPTIONAL { ?z <http://ex/mbox> ?m FILTER(?m = ?x) } } }""",
}


@pytest.fixture(scope="module")
def quads_file(tmp_path_factory):
    directory = tmp_path_factory.mktemp("optional")
    source = directory / "data.nq"
    source.write_text(DATA, encoding="utf-8")
    path = directory / "data.vortex"
    serialize_rdf(str(source), str(path), layout="dictionary")
    return str(path)


@pytest.fixture
def rdflib_left_joins(monkeypatch) -> list:
    """The OPTIONALs rdflib's own evaluator solved."""
    calls: list = []
    original = rdflib_evaluate.evalLeftJoin
    monkeypatch.setattr(
        rdflib_evaluate,
        "evalLeftJoin",
        lambda ctx, join: calls.append(join) or original(ctx, join),
    )
    return calls


def _dataset(path: str, in_memory: bool = False) -> Dataset:
    register_sparql_pushdown()
    return Dataset(store=VortexRdflibStore(path, in_memory=in_memory), default_union=False)


def _rows(dataset: Dataset, sparql: str) -> list[tuple]:
    return sorted(
        tuple("" if term is None else term.n3() for term in row) for row in dataset.query(sparql)
    )


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("sparql", SPEC_SHAPES.values(), ids=SPEC_SHAPES.keys())
def test_an_optional_answers_as_sparql_does(quads_file, rdflib_left_joins, in_memory, sparql):
    dataset = _dataset(quads_file, in_memory)
    assert _rows(dataset, sparql) == oracle.query_rows(DATA, sparql)
    assert rdflib_left_joins == []


def test_a_graph_variable_only_the_right_side_binds_keeps_the_specs_answer(quads_file):
    """§18.6: the inner pattern is evaluated with ?g free, so <b> <q> <g2>
    extends the <a> row with ?g = <g2>, which the join with the graph's name
    <g1> then drops. Oxigraph binds ?g first and pads the row instead."""
    sparql = """SELECT ?g ?s ?o WHERE { GRAPH ?g { ?s <http://ex/p> ?o
        OPTIONAL { ?o <http://ex/q> ?g } } }"""
    assert _rows(_dataset(quads_file), sparql) == [
        ("<http://ex/g1>", "<http://ex/c>", "<http://ex/d>")
    ]
    assert len(oracle.query_rows(DATA, sparql)) == 2  # Oxigraph's departure


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_values_on_the_left_inside_a_graph_keeps_its_unmatched_rows(
    quads_file, rdflib_left_joins, in_memory
):
    """rdflib evaluates ``GRAPH ?g`` graph by graph and hands the OPTIONAL to
    the pushdown, which keeps the unmatched VALUES row; every row then joins
    the graph's name (§18.6). rdflib alone drops that row, and Oxigraph
    leaves its ?g unbound."""
    sparql = """SELECT ?g ?o ?x WHERE { GRAPH ?g {
        VALUES ?o { <http://ex/b> <http://ex/unknown> } OPTIONAL { ?o <http://ex/q> ?x } } }"""
    assert _rows(_dataset(quads_file, in_memory), sparql) == [
        ("<http://ex/g1>", "<http://ex/b>", "<http://ex/g2>"),
        ("<http://ex/g1>", "<http://ex/unknown>", ""),
    ]
    assert rdflib_left_joins == []
    assert ("", "<http://ex/unknown>", "") in oracle.query_rows(DATA, sparql)  # Oxigraph's


def test_an_enclosing_joins_variable_on_the_right_keeps_the_specs_answer(quads_file):
    """The enclosing join keys on ?x, which the OPTIONAL may leave unbound,
    so rdflib joins it and hands the OPTIONAL over with ?x substituted, and
    rdflib's own re-check of an unmatched row gives the specification's
    answer here (Oxigraph's too)."""
    sparql = """SELECT ?x ?n ?z WHERE { ?x <http://ex/name> ?n .
        { <http://ex/y> <http://ex/knows> ?z OPTIONAL { ?z <http://ex/mbox> ?x } } }"""
    assert _rows(_dataset(quads_file), sparql) == oracle.query_rows(DATA, sparql)


def _first(node, name: str):
    """The first algebra node named ``name`` under ``node``, depth first."""
    if getattr(node, "name", None) == name:
        return node
    for value in getattr(node, "values", lambda: ())():
        found = _first(value, name)
        if found is not None:
            return found
    return None


def test_an_optional_under_a_variable_rdflib_substituted_is_handed_back(
    quads_file, rdflib_left_joins
):
    """A binding rdflib substituted from outside (here ?x, as in an EXISTS
    body it evaluates) that reaches the right side but not the left: the
    enclosing evaluation is rdflib's, so rdflib solves the OPTIONAL."""
    dataset = _dataset(quads_file)
    query = prepareQuery(
        "SELECT * WHERE { VALUES ?o { <http://ex/o0> } OPTIONAL { ?s <http://ex/mbox> ?x } }"
    )
    node = _first(query.algebra, "LeftJoin")
    assert node is not None
    ctx = QueryContext(graph=dataset, initBindings={}).push()
    ctx[Variable("x")] = URIRef("http://ex/x1")
    list(pd.hook._eval_part(ctx, node))
    assert rdflib_left_joins == [node]


def test_a_left_row_with_an_unbound_key_joins_every_right_row_agreeing_on_the_rest():
    """SPARQL's compatibility: an unbound shared variable on the left matches
    every right row on it, and the joined row takes the right row's value;
    a left row whose bound key no right row has is padded."""
    rows = pd.relation._left_join_rows(
        ("o", "t"),
        [(None, "a"), (1, "b"), (3, "c")],
        ("s", "o"),
        [(10, 1), (20, 2)],
        (),
        nullable_left=True,
    )
    assert rows == [(1, "a", 10), (2, "a", 20), (1, "b", 10), (3, "c", None)]
