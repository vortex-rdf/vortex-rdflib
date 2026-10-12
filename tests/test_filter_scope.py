"""A FILTER sees the bindings of its own group only, as rdflib's evaluator
does: a variable the group does not bind is unbound in the expression, even
where an enclosing ``GRAPH ?g``, join or OPTIONAL binds it, and a MINUS's
right side binds nothing in the group. A FILTER directly inside an OPTIONAL is
that LeftJoin's condition and sees the left side's bindings as well. Where
rdflib keeps an outer binding the expression names outside a comparison (the
Filter node's ``_vars``), the pushdown gives rdflib's answer too."""

import pytest
from rdflib import Dataset
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_native_primitives import _trace
from test_pushdown import both_ways, run
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

# Two named graphs and the default graph: a first hop (p), a second and third
# hop for the nested groups, OPTIONAL and MINUS (q, r), a graph named in its
# own subject position (g1), and graph names a pattern reaches (in).
QUADS = """\
<http://ex/s1> <http://ex/p> "a" <http://ex/g1> .
<http://ex/s2> <http://ex/p> "b" <http://ex/g2> .
<http://ex/s3> <http://ex/p> <http://ex/o3> <http://ex/g2> .
<http://ex/s4> <http://ex/p> "c" .
<http://ex/s1> <http://ex/q> <http://ex/x1> <http://ex/g1> .
<http://ex/s2> <http://ex/q> <http://ex/x2> <http://ex/g2> .
<http://ex/s4> <http://ex/q> <http://ex/x4> .
<http://ex/x1> <http://ex/r> "y1" <http://ex/g1> .
<http://ex/x2> <http://ex/r> "y2" <http://ex/g2> .
<http://ex/x4> <http://ex/r> "y4" .
<http://ex/g1> <http://ex/p> "gx" <http://ex/g1> .
<http://ex/s1> <http://ex/in> <http://ex/g1> .
<http://ex/s2> <http://ex/in> <http://ex/g2> .
"""

PREFIX = "PREFIX ex: <http://ex/> "

SHAPES = [
    # A FILTER directly inside GRAPH ?g: rdflib joins ?g in after the group.
    pytest.param(
        'SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(strstarts(str(?g), "http://ex/g2")) } }',
        id="graph-strstarts",
    ),
    pytest.param("SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(?g = ex:g2) } }", id="graph-eq"),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(isIRI(?g)) } }", id="graph-isiri"
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(bound(?g)) } }", id="graph-bound"
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(!bound(?g)) } }", id="graph-not-bound"
    ),
    pytest.param("SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(?o != ?g) } }", id="graph-tuple"),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(isLiteral(?o) || bound(?g)) } }",
        id="graph-or",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(isLiteral(?o) && !bound(?g)) } }",
        id="graph-and",
    ),
    pytest.param(
        "SELECT ?s WHERE { GRAPH ?g { ?s ex:p ?o FILTER(?g = ex:g2) } }", id="graph-one-pattern"
    ),
    pytest.param("ASK { GRAPH ?g { ?s ?p ?o FILTER(?g = ex:g2) } }", id="graph-ask-eq"),
    pytest.param("ASK { GRAPH ?g { ?s ?p ?o FILTER(!bound(?g)) } }", id="graph-ask-not-bound"),
    pytest.param(
        "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o FILTER(?g = ex:g2) } }",
        id="graph-count-eq",
    ),
    pytest.param(
        "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o FILTER(!bound(?g)) } }",
        id="graph-count-not-bound",
    ),
    pytest.param(
        "SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o FILTER(!bound(?g)) } } ORDER BY ?s LIMIT 2",
        id="graph-limit",
    ),
    # ... where the group's own pattern binds ?g, or the FILTER follows the
    # group, or reads only what the group binds: the FILTER sees it.
    pytest.param(
        "SELECT ?g ?o WHERE { GRAPH ?g { ?g ?p ?o FILTER(isIRI(?g)) } }", id="graph-pattern-binds"
    ),
    pytest.param(
        'SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o } FILTER(strstarts(str(?g), "http://ex/g2")) }',
        id="graph-filter-after",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ?p ?o FILTER(isLiteral(?o)) } }", id="graph-own-var"
    ),
    # A FILTER in a group nested inside GRAPH ?g.
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o { ?s ex:q ?x FILTER(?g = ex:g2) } } }",
        id="graph-nested-eq",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o { ?s ex:q ?x FILTER(isIRI(?g)) } } }",
        id="graph-nested-isiri",
    ),
    pytest.param(
        "SELECT ?g ?o WHERE { GRAPH ?g { ?g ex:p ?o { ?s ex:q ?x FILTER(?g = ex:g1) } } }",
        id="graph-nested-outer-pattern-binds",
    ),
    # An OPTIONAL's condition inside GRAPH ?g: it sees both sides, not ?g.
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(?g = ex:g2) } } }""",
        id="graph-optional-eq",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(bound(?g)) } } }""",
        id="graph-optional-bound",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(!bound(?g)) } } }""",
        id="graph-optional-not-bound",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(?x != ?g) } } }""",
        id="graph-optional-tuple",
    ),
    pytest.param(
        """SELECT ?s ?g ?x ?y WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x . ?x ex:r ?y FILTER(?g = ex:g2) } } }""",
        id="graph-optional-two-patterns",
    ),
    pytest.param(
        """SELECT ?g ?o ?x WHERE { GRAPH ?g { ?g ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(?g = ex:g1) } } }""",
        id="graph-optional-left-pattern-binds",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER(?o = "a") } } }""",
        id="graph-optional-own-var",
    ),
    # A MINUS's right side and the UNION branches inside GRAPH ?g.
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?s ex:q ?x FILTER(?g = ex:g2) } } }",
        id="graph-minus-eq",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?s ex:q ?x FILTER(bound(?g)) } } }",
        id="graph-minus-bound",
    ),
    pytest.param(
        """SELECT ?s ?g WHERE { GRAPH ?g {
            { ?s ex:p ?o FILTER(?g = ex:g2) } UNION { ?s ex:q ?x FILTER(!bound(?g)) } } }""",
        id="graph-union",
    ),
    # GRAPH ?g with ?g bound row by row by an enclosing join: rdflib forgets
    # the binding inside the FILTER, unless the expression names ?g outside a
    # comparison.
    pytest.param(
        "SELECT ?x ?g ?s WHERE { ?x ex:in ?g . GRAPH ?g { ?s ex:p ?o FILTER(?g = ex:g2) } }",
        id="graph-joined-eq",
    ),
    pytest.param(
        "SELECT ?x ?g ?s WHERE { ?x ex:in ?g . GRAPH ?g { ?s ex:p ?o FILTER(isIRI(?g)) } }",
        id="graph-joined-isiri",
    ),
    pytest.param(
        "SELECT ?x ?g ?s WHERE { ?x ex:in ?g . GRAPH ?g { ?s ex:p ?o FILTER(!bound(?g)) } }",
        id="graph-joined-not-bound",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE {
            VALUES ?g { ex:g1 ex:g2 } GRAPH ?g { ?s ex:p ?o FILTER(?g = ex:g2) } }""",
        id="graph-values-eq",
    ),
    # A nested group.
    pytest.param('SELECT * WHERE { ?s ex:p ?o { ?s2 ex:p ?o2 FILTER(?o = "a") } }', id="nested-eq"),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s2 ex:p ?o2 FILTER(isLiteral(?o)) } }",
        id="nested-isliteral",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x FILTER(?o = "a") } }', id="nested-shared-eq"
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x FILTER(!bound(?o)) } }",
        id="nested-shared-not-bound",
    ),
    # OPTIONAL: its own FILTER is the condition and sees the left side; a
    # FILTER in a group nested inside it does not.
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x FILTER(?o = "a") } }',
        id="optional-condition-eq",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x FILTER(bound(?o)) } }",
        id="optional-condition-bound",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x { ?x ex:r ?y FILTER(?o = "a") } } }',
        id="optional-nested-eq",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x { ?x ex:r ?y FILTER(strstarts(?o, "a")) } } }""",
        id="optional-nested-strstarts",
    ),
    # UNION branches.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { ?s ex:q ?x FILTER(?o = "a") } UNION { ?s ex:q ?x FILTER(?o != "a") } }""",
        id="union-eq",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x FILTER(bound(?o)) } UNION { ?s ex:r ?y } }",
        id="union-bound",
    ),
    pytest.param(
        "SELECT * WHERE { { ?s ex:p ?o } UNION { ?s ex:q ?x FILTER(!bound(?o)) } }",
        id="union-other-branch",
    ),
    # MINUS.
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x FILTER(?o = "a") } }', id="minus-eq"
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x FILTER(isLiteral(?o)) } }",
        id="minus-isliteral",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x FILTER(!bound(?o)) } }",
        id="minus-not-bound",
    ),
    # A MINUS's right side binds nothing in the group's solutions...
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(bound(?x)) }",
        id="minus-var-bound",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(!bound(?x)) }",
        id="minus-var-not-bound",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(isIRI(?x)) }",
        id="minus-var-isiri",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(?o != ?x) }",
        id="minus-var-tuple",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o MINUS { ?x ex:q ?y } FILTER(bound(?x) || isLiteral(?o)) }",
        id="minus-var-or",
    ),
    pytest.param("ASK { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(bound(?x)) }", id="minus-var-ask"),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x MINUS { ?s ex:r ?y } } FILTER(bound(?y)) }",
        id="nested-minus-var",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?s ex:q ?x } FILTER(bound(?x)) } }",
        id="graph-minus-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x MINUS { ?x ex:r ?y } } FILTER(bound(?y)) }""",
        id="optional-minus-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x MINUS { ?x ex:r ?y } FILTER(bound(?y)) } }""",
        id="optional-condition-minus-var",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { { ?s ex:p ?o MINUS { ?g ex:q ?x } }
            OPTIONAL { ?s ex:q ?x FILTER(bound(?g)) } } }""",
        id="graph-optional-condition-minus-var",
    ),
    # ... so an OPTIONAL's variable its left side names only there stays
    # unbound where the OPTIONAL does not match.
    pytest.param(
        """SELECT * WHERE { { ?s ex:p ?o MINUS { ?s ex:r ?y } }
            OPTIONAL { ?s ex:q ?y } FILTER(isIRI(?y)) }""",
        id="optional-over-minus-isiri",
    ),
    pytest.param(
        """SELECT * WHERE { { ?s ex:p ?o MINUS { ?s ex:r ?y } }
            OPTIONAL { ?s ex:q ?y } FILTER(bound(?y)) }""",
        id="optional-over-minus-bound",
    ),
    # GRAPH <iri>.
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o GRAPH ex:g1 { ?s ex:q ?x FILTER(?o = "a") } }',
        id="named-graph-eq",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o GRAPH ex:g1 { ?s ex:q ?x FILTER(isLiteral(?o)) } }",
        id="named-graph-isliteral",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o GRAPH ex:g1 { ?s ex:q ?x FILTER(!bound(?o)) } }",
        id="named-graph-not-bound",
    ),
]


@pytest.fixture(scope="module")
def quad_files(tmp_path_factory):
    """The quads, once per secondary index layout."""
    directory = tmp_path_factory.mktemp("filter-scope")
    nq = directory / "quads.nq"
    nq.write_text(QUADS, encoding="utf-8")
    files = {}
    for layout in ("copy", "reference"):
        out = directory / f"quads-{layout}.vortex"
        index = f"secondary-by-{layout}"
        serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads", indexes=[index])
        files[layout] = out
    return files


@pytest.fixture(
    params=[("copy", True), ("copy", False), ("reference", True), ("reference", False)],
    ids=["copy-mem", "copy-file", "reference-mem", "reference-file"],
)
def dataset(quad_files, request):
    layout, in_memory = request.param
    store = VortexRdflibStore(str(quad_files[layout]), in_memory=in_memory)
    return Dataset(store=store, default_union=True)


@pytest.fixture(params=["default", "probe", "generic"])
def route(request, monkeypatch):
    """The default planning; every one-pattern side probed per row; every
    FILTER value through rdflib's evaluator."""
    if request.param == "probe":
        monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    elif request.param == "generic":
        monkeypatch.setattr(filters, "_FAST_ENABLED", False)
    return request.param


@pytest.mark.parametrize("sparql", SHAPES)
def test_a_filter_sees_only_its_own_groups_bindings(dataset, route, sparql):
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
        native = [e for e in _trace(capsys) if e["event"] == "native_filter_complete"]
        monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY")
        assert [(e["kind"], e["candidate_codes"], e["passed_codes"]) for e in native] == [
            ("str_prefix", 2, 2)
        ]
        assert rows == both_ways(dataset, query)[1]
        assert len(rows) == 2
