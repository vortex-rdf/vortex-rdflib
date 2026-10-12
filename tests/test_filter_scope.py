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
body leaves unbound is the row's."""

import pytest
from rdflib import Dataset
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
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER EXISTS { ?s ex:q ?x MINUS { ?s ex:r ?y } } }",
        id="exists-minus-shared-outer-var",
    ),
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o FILTER NOT EXISTS { ?s ex:q ?x MINUS { ?x ex:r ?y } } }",
        id="not-exists-minus",
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


@pytest.mark.parametrize("sparql", GROUP_SHAPES)
def test_graph_minus_and_exists_answer_as_rdflib_does(dataset, route, sparql):
    with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
    assert with_pushdown == without_pushdown


_BY_ID = {param.id: param for param in SHAPES + GROUP_SHAPES}

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
@pytest.mark.parametrize("sparql", [shape for shape in NATIVE if "exists" in str(shape.id)])
def test_an_exists_the_pushdown_answers_whole_is_a_semi_join(
    quad_files, monkeypatch, in_memory, sparql
):
    """rdflib's evaluator decides none of its rows, as it does those of an
    EXISTS whose key the body may leave unbound."""
    dataset = Dataset(
        store=VortexRdflibStore(str(quad_files["copy"]), in_memory=in_memory), default_union=True
    )
    register_sparql_pushdown()
    calls = []
    generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct, "generic", lambda self, bound: calls.append(bound) or generic(self, bound)
    )
    run(dataset, PREFIX + sparql)
    assert calls == []
    run(dataset, PREFIX + str(_BY_ID["exists-values-undef"].values[0]))
    assert calls
