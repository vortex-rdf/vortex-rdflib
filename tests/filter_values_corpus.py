"""The VALUES shapes of the FILTER scope tests (``tests/filter_scope/``),
and the answers SPARQL 1.1 gives where rdflib's differ (``SPEC_ANSWERS``)."""

import pytest

# A FILTER pushes a conjunct on one variable down to the pattern scans that
# bind it, and a VALUES table that binds it applies it too: alone, joined, in a
# group, on a MINUS's left side, in a MINUS, a GRAPH or an EXISTS body, and
# ahead of a pattern, which leaves the variable to the table. Over a UNION or
# an OPTIONAL rdflib applies the FILTER, and an UNDEF leaves it to the
# residual.
#: rdflib compares this VALUES constant as the number 300, but a byte is at
#: most 127: "300"^^xsd:byte is ill-typed, so `>` is a type error on it and
#: SPARQL 1.1 keeps no row (§17.3; docs/pushdown.md).
OUT_OF_RANGE = """SELECT * WHERE { VALUES ?n { "300"^^<http://www.w3.org/2001/XMLSchema#byte> 1 }
    FILTER (?n > 5) }"""


#: The VALUES shapes rdflib answers against the specification, and the
#: specification's answer.
SPEC_ANSWERS: dict[str, list] = {OUT_OF_RANGE: []}


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
    pytest.param(OUT_OF_RANGE, id="values-out-of-range-gt"),
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


#: VALUES rows with an UNDEF beside a term the dictionary lacks: joined with a
#: BGP, under an OPTIONAL, and filtered.
VALUES_UNDEF_SHAPES = [
    pytest.param(
        'SELECT * WHERE { VALUES (?s ?o) { (ex:nowhere UNDEF) (UNDEF "a") } ?s ex:p ?o }',
        id="values-undef-unknown-join",
    ),
    pytest.param(
        "SELECT * WHERE { VALUES (?s ?x) { (ex:nowhere 1) (UNDEF 2) } OPTIONAL { ?s ex:p ?o } }",
        id="values-undef-unknown-optional",
    ),
    pytest.param(
        """SELECT * WHERE { VALUES (?s ?o) { (ex:nowhere UNDEF) (UNDEF "a") }
        ?s ex:p ?o FILTER (?o != "b") }""",
        id="values-undef-unknown-filter",
    ),
]
