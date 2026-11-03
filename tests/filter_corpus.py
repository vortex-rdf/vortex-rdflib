"""The FILTER scope tests' corpus (``tests/filter_scope/``): the quads they
ask over, and the query shapes by family — a FILTER's own group
(``SHAPES``); GRAPH, MINUS and EXISTS groups (``GROUP_SHAPES``); a FILTER
rdflib evaluates again with a row (``REENTRY_SHAPES``) — every shape, the
VALUES ones (:mod:`tests.filter_values_corpus`) included, by its id
(``BY_ID``)."""

import pytest
from tests.filter_values_corpus import VALUES_SHAPES

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
    # The group's own pattern binds ?s too: the FILTER sees that binding.
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o { ?s ex:q ?x FILTER(?s != ex:s2) } }",
        id="nested-pattern-var-ne",
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
    # The nested group's own pattern binds ?s too: the FILTER sees that binding.
    pytest.param(
        "SELECT * WHERE { ?s ex:p ?o OPTIONAL { ?s ex:q ?x { ?s ex:q ?x2 FILTER(?s != ex:s2) } } }",
        id="optional-nested-pattern-var-ne",
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


BY_ID = {param.id: param for param in SHAPES + GROUP_SHAPES + REENTRY_SHAPES + VALUES_SHAPES}
