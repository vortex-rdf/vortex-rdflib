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
included.

A conjunct on a variable a VALUES table binds holds on the table's rows."""

import pytest
from rdflib import Dataset, Variable
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_native_primitives import _trace
from test_pushdown import both_ways, run
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

# Two named graphs and the default graph: a first hop (p), a second and third
# hop for the nested groups, OPTIONAL and MINUS (q, r), a subject with a first
# hop in one named graph and a second in the other (s3), a graph named in its
# own subject position (g1), and graph names a pattern reaches (in).
QUADS = """\
<http://ex/s1> <http://ex/p> "a" <http://ex/g1> .
<http://ex/s2> <http://ex/p> "b" <http://ex/g2> .
<http://ex/s3> <http://ex/p> <http://ex/o3> <http://ex/g2> .
<http://ex/s4> <http://ex/p> "c" .
<http://ex/s1> <http://ex/q> <http://ex/x1> <http://ex/g1> .
<http://ex/s2> <http://ex/q> <http://ex/x2> <http://ex/g2> .
<http://ex/s3> <http://ex/q> <http://ex/x3> <http://ex/g1> .
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
    # ... where the group's own pattern, VALUES table or GRAPH ?g binds ?g, or
    # the FILTER follows the group, or reads only what the group binds: the
    # FILTER sees it.
    pytest.param(
        "SELECT ?g ?o WHERE { GRAPH ?g { ?g ?p ?o FILTER(isIRI(?g)) } }", id="graph-pattern-binds"
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { VALUES ?g { ex:g2 } ?s ex:p ?o FILTER(?g = ex:g2) } }",
        id="graph-values-binds",
    ),
    pytest.param(
        """SELECT ?s ?g WHERE { GRAPH ?g {
            VALUES ?g { ex:g1 ex:g2 } ?s ex:p ?o FILTER(isIRI(?g)) } }""",
        id="graph-values-binds-isiri",
    ),
    pytest.param(
        """SELECT ?s ?g WHERE { GRAPH ?g {
            ?s ex:p ?o GRAPH ?g { ?s ex:q ?x } FILTER(?g = ex:g2) } }""",
        id="graph-same-var-binds",
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
    # A FILTER in a group nested inside an OPTIONAL under GRAPH ?g.
    pytest.param(
        """SELECT ?s ?g ?x ?y WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x { ?x ex:r ?y FILTER(?g = ex:g2) } } } }""",
        id="graph-optional-nested-eq",
    ),
    pytest.param(
        """SELECT ?s ?g ?x ?y WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x { ?x ex:r ?y FILTER(isIRI(?g)) } } } }""",
        id="graph-optional-nested-isiri",
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

GROUP_SHAPES = [
    # A MINUS under GRAPH ?g: the two sides share the graph only where both
    # name ?g, and only rows that share a binding (an enclosing row's
    # included) are removed.
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?x ex:q ?y } } }",
        id="graph-minus-disjoint",
    ),
    pytest.param(
        "SELECT ?o ?g WHERE { GRAPH ?g { ?g ex:p ?o MINUS { ?x ex:q ?y } } }",
        id="graph-minus-disjoint-left-names-g",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?g ex:q ?y } } }",
        id="graph-minus-disjoint-right-names-g",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?s ex:q ?g } } }",
        id="graph-minus-right-names-g",
    ),
    pytest.param(
        "SELECT ?o ?g WHERE { GRAPH ?g { ?g ex:p ?o MINUS { ?g ex:q ?y } } }",
        id="graph-minus-both-name-g",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { ?s ex:q ?x } } }",
        id="graph-minus-shared",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o MINUS { GRAPH ?g { ?s ex:q ?x } } } }",
        id="graph-minus-graph-same-var",
    ),
    pytest.param(
        """SELECT ?z ?g ?s WHERE { ?z ex:in ?g .
            GRAPH ?g { ?s ex:p ?o MINUS { ?x ex:q ?y } } }""",
        id="graph-joined-minus-disjoint",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g {
            ?s ex:p ?o { ?s ex:q ?x MINUS { ?a ex:r ?b } } } }""",
        id="graph-nested-minus-disjoint",
    ),
    # An OPTIONAL under GRAPH ?g whose right side alone names ?g: rdflib
    # matches it with any ?g, and the graph join drops a row whose ?g is not
    # the graph instead of padding it.
    pytest.param(
        "SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o OPTIONAL { ?g ex:p ?x } } }",
        id="graph-optional-names-g",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?g ex:p ?x } FILTER(bound(?g)) } }""",
        id="graph-optional-names-g-bound",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o OPTIONAL { ?s ex:q ?g } } }",
        id="graph-optional-names-g-shared",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?y OPTIONAL { ?g ex:p ?x } } } }""",
        id="graph-optional-nested-names-g",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x MINUS { ?g ex:p ?y } } } }""",
        id="graph-optional-minus-names-g",
    ),
    pytest.param(
        """SELECT ?s ?g ?x WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { GRAPH ?g { ?s ex:q ?x } } } }""",
        id="graph-optional-graph-same-var",
    ),
    pytest.param(
        "SELECT ?g ?o ?x WHERE { GRAPH ?g { ?g ex:p ?o OPTIONAL { ?g ex:q ?x } } }",
        id="graph-optional-both-name-g",
    ),
    # A part of a GRAPH ?g group matched in another graph, or in none: rdflib
    # repeats it once per named graph.
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { GRAPH ex:g1 { ?s ex:p ?o } } }", id="graph-in-graph"
    ),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { GRAPH ex:g1 { ?s ex:p ?o } FILTER(bound(?g)) } }",
        id="graph-in-graph-bound",
    ),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { GRAPH ex:g1 { ?s ex:p ?o } FILTER(!bound(?g)) } }",
        id="graph-in-graph-not-bound",
    ),
    pytest.param(
        "SELECT ?g ?o WHERE { GRAPH ?g { GRAPH ex:g1 { ?g ex:p ?o } } }",
        id="graph-in-graph-names-g",
    ),
    pytest.param(
        """SELECT ?g ?s ?x WHERE { GRAPH ?g {
            GRAPH ex:g1 { ?s ex:p ?o } OPTIONAL { ?s ex:q ?x } } }""",
        id="graph-in-graph-optional",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g {
            GRAPH ex:g1 { ?s ex:p ?o } MINUS { ?s ex:q ?x } } }""",
        id="graph-in-graph-minus",
    ),
    pytest.param(
        "SELECT ?g ?s ?x WHERE { GRAPH ?g { ?s ex:q ?x GRAPH ex:g1 { ?s ex:p ?o } } }",
        id="graph-in-graph-joined",
    ),
    pytest.param(
        """SELECT ?g ?s ?x WHERE { GRAPH ?g { ?z ex:p ?w {
            GRAPH ex:g1 { ?s ex:p ?o } OPTIONAL { ?s ex:q ?x } } } }""",
        id="graph-in-graph-optional-joined",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g { ?z ex:p ?w {
            GRAPH ex:g1 { ?s ex:p ?o } MINUS { ?s ex:q ?x } } } }""",
        id="graph-in-graph-minus-joined",
    ),
    pytest.param(
        "SELECT ?g ?h ?s WHERE { GRAPH ?g { GRAPH ?h { ?s ex:p ?o } } }", id="graph-var-in-graph"
    ),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { GRAPH ?g { ?s ex:p ?o } } }", id="graph-same-var-in-graph"
    ),
    pytest.param("SELECT ?g ?x WHERE { GRAPH ?g { VALUES ?x { ex:a } } }", id="graph-values"),
    pytest.param("SELECT ?g WHERE { GRAPH ?g { VALUES ?g { ex:g1 ex:zz } } }", id="graph-values-g"),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g {
            VALUES ?g { ex:g1 ex:zz } OPTIONAL { ?s ex:p ?o } } }""",
        id="graph-values-g-optional",
    ),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { VALUES ?g { UNDEF ex:g1 } ?s ex:p ?o } }",
        id="graph-values-g-undef-joined",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g { ?s ex:p ?o
            OPTIONAL { VALUES ?g { UNDEF ex:g1 } } } }""",
        id="graph-values-g-undef-optional",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g { ?s ex:p ?o
            MINUS { VALUES (?s ?g) { (ex:s1 UNDEF) (ex:s2 ex:g1) } } } }""",
        id="graph-values-g-undef-minus",
    ),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { VALUES ?s { ex:s1 ex:s2 } ?s ex:p ?o } }",
        id="graph-values-joined",
    ),
    pytest.param("SELECT ?g WHERE { GRAPH ?g { } }", id="graph-empty"),
    pytest.param("SELECT ?g WHERE { GRAPH ?g { } FILTER(bound(?g)) }", id="graph-empty-bound"),
    pytest.param("SELECT DISTINCT ?g WHERE { GRAPH ?g { } }", id="graph-empty-distinct"),
    pytest.param("SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { } }", id="graph-empty-count"),
    pytest.param("ASK { GRAPH ?g { } }", id="graph-empty-ask"),
    pytest.param(
        "SELECT ?g ?s WHERE { GRAPH ?g { OPTIONAL { ?s ex:in ?x } } }", id="graph-empty-optional"
    ),
    # A MINUS rdflib solves once per row of a lazy join, an OPTIONAL or a
    # VALUES table to its left: its right side reads the row's bindings, and
    # its two sides always share them.
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x MINUS { ?x ex:r ?o } } }",
        id="nested-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x MINUS { ?a ex:r ?b } } }",
        id="nested-minus-disjoint",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x MINUS { ?x ex:r ?o } } }",
        id="optional-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x MINUS { ?a ex:r ?b } } }",
        id="optional-minus-disjoint",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "y1" } { ?x ex:r ?y MINUS { ?x ex:r ?o } } }',
        id="values-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x MINUS { ?s ex:r ?y } } }",
        id="nested-minus-shared-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x MINUS { ?x ex:r ?y } } }", id="nested-minus"
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x MINUS { ?x ex:r ?y } } }",
        id="optional-minus",
    ),
    pytest.param("SELECT * WHERE { ?s ex:p ?o MINUS { ?a ex:q ?x } }", id="minus-disjoint"),
    # ... unless the row binds nothing: a padded OPTIONAL, or under GRAPH ?g
    # only the graph, which rdflib does not bind.
    pytest.param(
        """SELECT * WHERE { { OPTIONAL { ?a ex:zz ?b } }
            { ?s ex:q ?x MINUS { ?c ex:r ?d } } }""",
        id="optional-row-minus-disjoint",
    ),
    pytest.param(
        """SELECT ?g ?s WHERE { GRAPH ?g { ex:s1 ex:p "a"
            { ?s ex:q ?x MINUS { ?c ex:r ?d } } } }""",
        id="graph-ground-row-minus-disjoint",
    ),
    # FILTER (NOT) EXISTS: rdflib solves the body once per row, with the row's
    # bindings; a key the body's solutions may leave unbound is the row's, and
    # a MINUS in the body shares the row's bindings where the EXISTS names
    # none of them.
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?o } } }",
        id="exists-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?o } } }",
        id="not-exists-minus-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER EXISTS { ?s ex:q ?x { ?x ex:r ?y MINUS { ?y ex:p ?o } } } }""",
        id="exists-nested-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x MINUS { ?a ex:r ?b } } }",
        id="exists-minus-disjoint",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:in ?h FILTER EXISTS { GRAPH ?h { GRAPH ex:g1 { ?s ex:p ?o } } } }",
        id="exists-graph-in-graph",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:in ?h FILTER EXISTS { GRAPH ?h { } } }", id="exists-graph-empty"
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x VALUES ?o { UNDEF } } }",
        id="exists-values-undef",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { VALUES (?s ?o) { (ex:s1 UNDEF) } } }",
        id="exists-values-undef-pair",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x OPTIONAL { ?x ex:r ?o } } }",
        id="exists-optional-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?s ex:q ?x OPTIONAL { ?x ex:r ?o } } }",
        id="not-exists-optional-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } } }",
        id="exists-uncorrelated-minus-disjoint",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } } }",
        id="not-exists-uncorrelated-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER EXISTS {
            { ?a ex:q ?x MINUS { ?c ex:r ?b } } UNION { ?a ex:zz ?b } } }""",
        id="exists-union-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS {
            { ?a ex:q ?x MINUS { ?c ex:r ?b } } UNION { ?a ex:zz ?b } } }""",
        id="not-exists-union-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:zz ?k }
            FILTER EXISTS { ?a ex:q ?k MINUS { ?c ex:r ?b } } }""",
        id="exists-unbound-key-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:zz ?k }
            FILTER NOT EXISTS { ?a ex:q ?k MINUS { ?c ex:r ?b } } }""",
        id="not-exists-unbound-key-minus-disjoint",
    ),
    # ... and an EXISTS under another operator.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER (EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } } || false) }""",
        id="or-exists-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER (NOT EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } } || false) }""",
        id="or-not-exists-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER (COALESCE(EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } }, false)) }""",
        id="coalesce-exists-minus-disjoint",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER (EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } } = true) }",
        id="eq-exists-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER (IF(EXISTS { ?a ex:q ?x MINUS { ?c ex:r ?b } }, true, false)) }""",
        id="if-exists-minus-disjoint",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:zz ?k }
            FILTER (EXISTS { ?a ex:q ?k MINUS { ?c ex:r ?b } } || bound(?k)) }""",
        id="or-exists-unbound-key-minus-disjoint",
    ),
    # A body whose relation lacks a key: a MINUS with an empty left side,
    # whose right side names it only in its own MINUS.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER EXISTS { ?s ex:zz ?x MINUS { ?x ex:r ?y MINUS { ?y ex:p ?o } } } }""",
        id="exists-empty-minus-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER NOT EXISTS { ?s ex:zz ?x MINUS { ?x ex:r ?y MINUS { ?y ex:p ?o } } } }""",
        id="not-exists-empty-minus-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x MINUS { ?s ex:r ?y } } }",
        id="exists-minus-shared-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?y } } }",
        id="not-exists-minus",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o FILTER (?o = "a" || EXISTS { ?s ex:q ?x }) }',
        id="or-exists",
    ),
    # ... and a FILTER in a group of the body forgets the row's bindings, even
    # of a variable its own pattern binds.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER EXISTS { { ?x ex:p ?o FILTER (?o != "zz") } } }""",
        id="exists-group-filter-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?x ex:q ?y
            { ?t ex:p ?o FILTER (COALESCE(?o = "zz", true)) } } }""",
        id="exists-nested-group-filter-outer-var",
    ),
    # A FILTER (NOT) EXISTS in a group on a lazy join's or an OPTIONAL's right
    # side: its body sees the enclosing row's binding of a variable the group
    # does not bind.
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "y1" }
            { ?x ex:r ?y FILTER EXISTS { ?x ex:r ?o } } }""",
        id="values-group-exists-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "y1" }
            { ?x ex:r ?y FILTER NOT EXISTS { ?x ex:r ?o } } }""",
        id="values-group-not-exists-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { ?s ex:q ?x2 FILTER EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?o } } } }""",
        id="nested-group-exists-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { ?s ex:q ?x2 FILTER NOT EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?o } } } }""",
        id="nested-group-not-exists-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            OPTIONAL { { ?s ex:q ?x FILTER EXISTS { ?x ex:r ?o } } } }""",
        id="optional-group-exists-outer-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            OPTIONAL { { ?s ex:q ?x FILTER NOT EXISTS { ?x ex:r ?o } } } }""",
        id="optional-group-not-exists-outer-var",
    ),
    # Under GRAPH, an EXISTS is matched in that graph, also where rdflib's
    # evaluator decides it: under another operator, in an OPTIONAL's
    # condition, or on the generic route.
    pytest.param(
        "SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x } } }",
        id="named-graph-exists",
    ),
    pytest.param(
        "SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o FILTER NOT EXISTS { ?s ex:q ?x } } }",
        id="named-graph-not-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER (EXISTS { ?a ex:q ex:x3 } || false) } }""",
        id="named-graph-or-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER (NOT EXISTS { ?a ex:q ex:x3 } || false) } }""",
        id="named-graph-or-not-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER (false || EXISTS { ?a ex:q ex:x3 }) } }""",
        id="named-graph-or-second-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER (false || NOT EXISTS { ?a ex:q ex:x3 }) } }""",
        id="named-graph-or-second-not-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER (COALESCE(EXISTS { ?a ex:q ex:x3 }, false)) } }""",
        id="named-graph-coalesce-exists",
    ),
    pytest.param(
        "SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o FILTER (EXISTS { ?s ex:q ?x } || false) } }",
        id="graph-or-exists",
    ),
    pytest.param(
        """SELECT ?s ?g WHERE { GRAPH ?g { ?s ex:p ?o
            FILTER (NOT EXISTS { ?s ex:q ?x } || false) } }""",
        id="graph-or-not-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            OPTIONAL { ?s ex:q ?x FILTER (EXISTS { ?a ex:q ex:x3 } || false) } } }""",
        id="named-graph-optional-or-exists",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER EXISTS { { ?a ex:q ex:x3 } UNION { ?a ex:zz ?b } } } }""",
        id="named-graph-exists-generic",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g2 { ?s ex:p ?o
            FILTER NOT EXISTS { { ?a ex:q ex:x3 } UNION { ?a ex:zz ?b } } } }""",
        id="named-graph-not-exists-generic",
    ),
]

# A FILTER rdflib evaluates itself, once per row of an enclosing join it was
# handed, sees only the row's bindings its node's _vars keep, and an
# OPTIONAL's FILTER none. The _vars leave out a variable that only a FILTER in
# an EXISTS body names, or only a comparison: an EXISTS in the expression does
# not see it, whether the EXISTS is the conjunct or sits under another
# operator.
REENTRY_SHAPES = [
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b
            FILTER (EXISTS { FILTER (COALESCE(EXISTS { ?s ex:q ?x }, false)) }) } }""",
        id="reentry-exists-filter-coalesce",
    ),
    pytest.param(
        """SELECT * WHERE { ?x ex:r ?y GRAPH ex:g2 { ?h ex:p ?o
            FILTER (EXISTS { FILTER ((EXISTS { ?x ex:r ?y } || bound(?s))) }) } }""",
        id="reentry-exists-filter-or-bound",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h { ?a ex:q ?b
            FILTER (EXISTS { FILTER (EXISTS { ?s ex:q ?x }) }) MINUS { ?c ex:zz ?d } } }""",
        id="reentry-exists-filter-minus",
    ),
    pytest.param(
        """SELECT * WHERE { FILTER ((EXISTS { MINUS { ?h ex:p ?o } }
            || EXISTS { { FILTER (EXISTS { ?s ex:q ?o }) . ?s ex:in ?h } })) . ?s ex:p ?o }""",
        id="reentry-or-exists-minus",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            FILTER (false || EXISTS { { FILTER (EXISTS { ?s ex:q ?o }) . ?s ex:in ?h } }) }""",
        id="reentry-or-exists-group-filter",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b
            FILTER (EXISTS { ?s ex:q ?x } = true) } }""",
        id="reentry-exists-eq",
    ),
    pytest.param(
        """SELECT * WHERE { ?x ex:r ?y GRAPH ex:g2 { ?h ex:p ?o
            FILTER ((EXISTS { ?x ex:r ?y } = true) || bound(?s)) } }""",
        id="reentry-exists-eq-or-bound",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b OPTIONAL { ?a ex:zz ?k }
            FILTER ((EXISTS { ?s ex:q ?x } = true) || bound(?k)) } }""",
        id="reentry-exists-eq-or-unbound",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h { ?a ex:q ?b
            FILTER (EXISTS { ?s ex:q ?x } = true) MINUS { ?c ex:zz ?d } } }""",
        id="reentry-exists-eq-minus",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b
            OPTIONAL { ?a ex:q ?c FILTER (COALESCE(EXISTS { ?s ex:q ?x }, false)) } } }""",
        id="reentry-optional-coalesce-exists",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b
            OPTIONAL { ?a ex:q ?c FILTER (EXISTS { ?s ex:q ?x } = true) } } }""",
        id="reentry-optional-exists-eq",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:in ?h GRAPH ex:g1 { ?a ex:q ?b FILTER EXISTS { ?s ex:q ?x } } }",
        id="reentry-exists-kept",
    ),
]

# A FILTER pushes a conjunct on one variable down to the pattern scans that
# bind it, and a VALUES table that binds it applies it too: alone, joined, in a
# group, on a MINUS's left side, in a MINUS, a GRAPH or an EXISTS body, and
# ahead of a pattern, which leaves the variable to the table. Over a UNION or
# an OPTIONAL rdflib applies the FILTER, and an UNDEF leaves it to the
# residual.
VALUES_SHAPES = [
    pytest.param('SELECT * WHERE { VALUES ?o { "y1" "a" } FILTER (?o = "a") }', id="values-eq"),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 VALUES ?o { "y1" "a" } FILTER (?o = "a") }',
        id="values-joined-eq",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 { VALUES ?o { "y1" "a" } } FILTER (?o = "a") }',
        id="values-group-eq",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES (?s ?o) { (ex:s1 "a") (ex:s2 "zz") }
            ?s ex:p ?o2 FILTER (?o = "a") }""",
        id="values-pair-eq",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 VALUES ?o { "y1" "a" } FILTER (strstarts(?o, "y")) }',
        id="values-strstarts",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 VALUES ?o { "y1" "a" } FILTER (isIRI(?o)) }',
        id="values-isiri",
    ),
    pytest.param('SELECT ?o WHERE { VALUES ?o { "y1" "a" } FILTER (?o != "a") }', id="values-ne"),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 VALUES ?o { "y1" "a" } FILTER (!bound(?o)) }',
        id="values-not-bound",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "y1" "a" }
            FILTER (COALESCE(EXISTS { ?t ex:p ?o }, false)) }""",
        id="values-coalesce-exists",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "y1" "a" } MINUS { ?c ex:zz ?d } FILTER (?o = "a") }',
        id="values-minus-left",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "y1" "a" } MINUS { ?s ex:p ?o } FILTER (?o = "a") }',
        id="values-minus-shared",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o MINUS { VALUES ?o { "a" "b" } FILTER (?o = "a") } }',
        id="values-in-minus",
    ),
    pytest.param(
        """SELECT * WHERE { { VALUES ?o { "y1" "a" } } UNION { ?s ex:p ?o }
            FILTER (?o = "a") }""",
        id="values-union",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "y1" "a" } OPTIONAL { ?s ex:p ?o }
            FILTER (?o = "a") }""",
        id="values-optional",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ?g { ?s ex:p ?o2 VALUES ?o { "y1" "a" }
            FILTER (?o = "a") } }""",
        id="graph-var-values-joined-eq",
    ),
    pytest.param(
        'SELECT * WHERE { GRAPH ?g { VALUES ?o { "y1" "a" } FILTER (?o = "a") } }',
        id="graph-var-values-eq",
    ),
    pytest.param(
        """SELECT * WHERE { GRAPH ex:g1 { ?s ex:p ?o2 VALUES ?o { "y1" "a" }
            FILTER (?o = "a") } }""",
        id="named-graph-values-joined-eq",
    ),
    pytest.param(
        'SELECT * WHERE { GRAPH ex:g1 { VALUES ?o { "y1" "a" } FILTER (?o = "a") } }',
        id="named-graph-values-eq",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o2 VALUES ?o { UNDEF "a" } FILTER (?o = "a") }',
        id="values-undef-eq",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o VALUES ?o { "a" "b" } FILTER (?o = "a") }',
        id="values-pattern-eq",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "a" "b" } ?s ex:p ?o FILTER (?o = "a") }',
        id="values-first-pattern-eq",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "a" ex:o3 } ?s ex:p ?o FILTER (isLiteral(?o)) }',
        id="values-first-pattern-isliteral",
    ),
    pytest.param(
        """SELECT ?s ?g WHERE { GRAPH ?g {
            VALUES ?g { ex:g1 ex:g2 } ?s ex:p ?o FILTER (?g = ex:g2) } }""",
        id="graph-values-first-eq",
    ),
    pytest.param(
        'SELECT * WHERE { VALUES ?o { "zz" "a" } FILTER (?o != "a") }', id="values-foreign-ne"
    ),
    pytest.param("SELECT * WHERE { VALUES ?n { 1 5 } FILTER (?n < 3) }", id="values-number-lt"),
    # rdflib compares the query's "300"^^xsd:byte by its value, a data
    # literal out of its type's range not at all.
    pytest.param(
        """SELECT * WHERE { VALUES ?n { "300"^^<http://www.w3.org/2001/XMLSchema#byte> 1 }
            FILTER (?n > 5) }""",
        id="values-out-of-range-gt",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o2 { VALUES ?o { "y1" "a" } FILTER (?o != "zz") }
            FILTER (?o = "a") }""",
        id="values-nested-filter-eq",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o2
            FILTER EXISTS { VALUES ?o { "y1" "a" } FILTER (?o = "zz") } }""",
        id="exists-values-eq",
    ),
    # On a lazy join's or an OPTIONAL's right side, or in an EXISTS body,
    # rdflib's FILTER forgets the row's binding of a variable the group's
    # VALUES table binds too, outside the node's _vars: a comparison sees it
    # unbound.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } }""",
        id="row-values-coalesce-eq",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (COALESCE(?o, "x") = "x") VALUES ?o { "a" } } }""",
        id="row-values-coalesce-var",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (bound(?o) = false) VALUES ?o { "a" } } }""",
        id="row-values-bound-eq-false",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" "b" } } }""",
        id="row-values-coalesce-two",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (COALESCE(?o = "zz", true)) VALUES (?o ?k) { ("a" 1) ("b" 2) } } }""",
        id="row-values-pair-coalesce",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            { FILTER (COALESCE(?o = "zz", true)) VALUES (?o ?s) { ("a" ex:s1) } } }""",
        id="row-values-pair-shared",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o
            GRAPH ex:g1 { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } }""",
        id="row-values-named-graph",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?x ex:q ?y
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="row-values-exists-group",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER EXISTS {
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="row-values-exists-subgroup",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?x ex:q ?y
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="row-values-not-exists-group",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="row-values-optional-group",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "a" }
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } }""",
        id="row-values-outer-values",
    ),
    pytest.param(
        'SELECT * WHERE { ?s ex:p ?o { FILTER (?o != "zz") VALUES ?o { "a" } } }',
        id="row-values-ne",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES ?o { "y1" "a" } GRAPH ex:g2 {
            FILTER ((?o = ex:s1 || ?o != "a"))
            VALUES (?s ?o) { (ex:s4 1) (ex:s4 "y1") (ex:zz "a") } } }""",
        id="row-values-named-graph-pair-or",
    ),
    # ... where no row binds it first (a MINUS's right side, or a join of a
    # join, which rdflib does not evaluate row by row), the FILTER sees the
    # table's binding.
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o MINUS { ?s ex:q ?x
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="values-in-minus-group",
    ),
    pytest.param(
        """SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x
            { FILTER (COALESCE(?o = "zz", true)) VALUES ?o { "a" } } } }""",
        id="values-in-nested-group",
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


@pytest.fixture
def union():
    """Whether the query's default graph is the union of the graphs; a test
    parametrizes it to query the default graph alone too."""
    return True


def _open(quad_files, layout, in_memory, union):
    store = VortexRdflibStore(str(quad_files[layout]), in_memory=in_memory)
    return Dataset(store=store, default_union=union)


@pytest.fixture
def dataset(quad_files, union):
    """The quads file-backed, with the copy layout's index: each shape runs
    once, on the default route, and one shape per family runs in every
    storage configuration and route (``SMOKE``)."""
    return _open(quad_files, "copy", False, union)


@pytest.fixture(
    params=[("copy", True), ("copy", False), ("reference", True), ("reference", False)],
    ids=["copy-mem", "copy-file", "reference-mem", "reference-file"],
)
def any_dataset(quad_files, request, union):
    """The quads in every storage configuration: either index layout, in
    memory or file-backed."""
    layout, in_memory = request.param
    return _open(quad_files, layout, in_memory, union)


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
        native = [e for e in _trace(capsys) if e["event"] == "native_filter_complete"]
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


@pytest.mark.parametrize("sparql", VALUES_SHAPES)
def test_a_filter_on_a_values_variable_answers_as_rdflib_does(dataset, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown


_BY_ID = {param.id: param for param in SHAPES + GROUP_SHAPES + REENTRY_SHAPES + VALUES_SHAPES}

# One shape per family: a FILTER under GRAPH ?g, under a constant GRAPH, in a
# nested group, as an OPTIONAL's condition and in a group in an OPTIONAL; a
# MINUS; an EXISTS semi-join and an EXISTS rdflib's evaluator decides; a
# re-entered EXISTS, over both datasets; VALUES ahead of a pattern and in a
# group a row re-enters.
SMOKE = [
    pytest.param(str(_BY_ID[shape].values[0]), union, id=shape + suffix)
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


# Shapes the pushdown answers whole, with no block node handed to rdflib.
NATIVE = [
    _BY_ID[shape]
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
    events = _trace(capsys)
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
    restrict, filter_relation = pd._restrict_pattern, pd._filter_relation
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        pd,
        "_restrict_pattern",
        lambda store, pat, preds: restricted.extend(preds) or restrict(store, pat, preds),
    )
    monkeypatch.setattr(
        pd, "_filter_relation", lambda *a: filtered.append(a) or filter_relation(*a)
    )
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, bound: generic.append(bound) or original_generic(self, bound),
    )
    query = PREFIX + str(_BY_ID["values-pattern-eq"].values[0])
    rows = run(dataset, query)
    assert restricted == [Variable("o")] and not filtered and not generic
    assert rows == both_ways(dataset, query)[1] and len(rows) == 1


# The EXISTS shapes among those: a semi- or anti-join never asks rdflib's
# evaluator; the generic route, for a body that reads the row's values or an
# EXISTS under another operator, asks it once per distinct tuple, without
# handing the FILTER to rdflib.
EXISTS_ROUTES = [
    pytest.param(str(_BY_ID[shape].values[0]), generic, id=shape)
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
