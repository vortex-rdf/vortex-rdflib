"""The pushdown must be observationally identical to rdflib's default
evaluator — every query shape is run both ways and compared exactly, on a
file-backed and on an in-memory store."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql.sparql import QueryContext
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexStore, filters, register_sparql_pushdown, unregister_sparql_pushdown

FIXTURE_NT = """\
<http://ex.org/alice> <http://ex.org/name> "Alice" .
<http://ex.org/alice> <http://ex.org/knows> <http://ex.org/bob> .
<http://ex.org/alice> <http://ex.org/knows> <http://ex.org/carol> .
<http://ex.org/bob> <http://ex.org/name> "Bob"@en .
<http://ex.org/bob> <http://ex.org/knows> <http://ex.org/carol> .
<http://ex.org/bob> <http://ex.org/age> "42"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/carol> <http://ex.org/name> "Carol" .
<http://ex.org/carol> <http://ex.org/likes> <http://ex.org/carol> .
_:b0 <http://ex.org/name> "Anon" .
_:b0 <http://ex.org/knows> <http://ex.org/alice> .
<http://ex.org/dave> <http://ex.org/age> "042"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/dave> <http://ex.org/name> "Bob"@en-US .
<http://ex.org/dave> <http://ex.org/flag> "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
<http://ex.org/dave> <http://ex.org/knows> <http://ex.org/bob> .
<http://ex.org/erin> <http://ex.org/age> "abc"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/erin> <http://ex.org/score> "1.5"^^<http://www.w3.org/2001/XMLSchema#decimal> .
<http://ex.org/erin> <http://ex.org/code> "x"^^<http://ex.org/dt> .
<http://ex.org/erin> <http://ex.org/name> "q\\"uote" .
<http://ex.org/frank> <http://ex.org/age> "-3"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/frank> <http://ex.org/score> "1e2"^^<http://www.w3.org/2001/XMLSchema#double> .
<http://ex.org/frank> <http://ex.org/name> "" .
<http://ex.org/frank> <http://ex.org/flag> "false"^^<http://www.w3.org/2001/XMLSchema#boolean> .
<http://ex.org/gina> <http://ex.org/age> "7"^^<http://www.w3.org/2001/XMLSchema#byte> .
<http://ex.org/gina> <http://ex.org/score> "NaN"^^<http://www.w3.org/2001/XMLSchema#double> .
<http://ex.org/gina> <http://ex.org/name> "Bob"@EN .
<http://ex.org/hank> <http://ex.org/knows> <http://ex.org/alice> .
<http://ex.org/hank> <http://ex.org/note> "tab\\there" .
<http://ex.org/hank> <http://ex.org/note> "line\\nbreak" .
<http://ex.org/hank> <http://ex.org/note> "back\\\\slash" .
<http://ex.org/hank> <http://ex.org/note> "\\u0001control" .
<http://ex.org/hank> <http://ex.org/note> "\u00fcn\u00efc\u00f6d\u00e9" .
<http://ex.org/hank> <http://ex.org/note> "x"@EN-us .
<http://ex.org/hank> <http://ex.org/knows> <http://ex.org/carol> .
<http://ex.org/carol> <http://ex.org/knows> <http://ex.org/dave> .
<http://ex.org/dave> <http://ex.org/knows> <http://ex.org/hank> .
""" + (
    "<http://ex.org/dave> <http://ex.org/born> "
    '"2020-01-01T00:00:00"^^<http://www.w3.org/2001/XMLSchema#dateTime> .\n'
)

QUERIES = [
    # single patterns
    "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT ?p ?o WHERE { <http://ex.org/bob> ?p ?o }",
    "SELECT * WHERE { ?s ?p ?o }",
    # chain join (object of one TP is subject of the next)
    """SELECT ?n WHERE {
        <http://ex.org/alice> <http://ex.org/knows> ?x .
        ?x <http://ex.org/name> ?n }""",
    # star join
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n .
        ?x <http://ex.org/age> ?a }""",
    # three-pattern chain
    """SELECT ?a ?c WHERE {
        ?a <http://ex.org/knows> ?b .
        ?b <http://ex.org/knows> ?c .
        ?c <http://ex.org/name> ?n }""",
    # repeated variable inside one pattern (s == o)
    "SELECT ?x WHERE { ?x <http://ex.org/likes> ?x }",
    # ground pattern acting as a filter alongside a variable pattern
    """SELECT ?n WHERE {
        <http://ex.org/bob> <http://ex.org/knows> <http://ex.org/carol> .
        <http://ex.org/carol> <http://ex.org/name> ?n }""",
    # ground pattern that does NOT match: must kill all solutions
    """SELECT ?n WHERE {
        <http://ex.org/carol> <http://ex.org/knows> <http://ex.org/bob> .
        ?s <http://ex.org/name> ?n }""",
    # cross product (no shared variables)
    """SELECT ?a ?b WHERE {
        <http://ex.org/alice> <http://ex.org/name> ?a .
        <http://ex.org/bob> <http://ex.org/name> ?b }""",
    # bnode in the query pattern (acts as a variable)
    "SELECT ?n WHERE { [] <http://ex.org/name> ?n }",
    # OPTIONAL: inner BGP evaluated under outer bindings
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/name> ?n .
        OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    # UNION
    """SELECT ?o WHERE {
        { <http://ex.org/alice> <http://ex.org/knows> ?o }
        UNION { ?o <http://ex.org/likes> ?o } }""",
    # FILTER over joined bindings
    """SELECT ?x WHERE {
        ?x <http://ex.org/name> ?n .
        FILTER(lang(?n) = "en") }""",
    # VALUES feeding bindings into the BGP
    """SELECT ?n WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/carol> }
        ?x <http://ex.org/name> ?n }""",
    # literal join value propagating into subject position (unsatisfiable leg)
    """SELECT ?n WHERE {
        ?s <http://ex.org/name> ?n .
        ?n <http://ex.org/name> ?m }""",
    # zero-match pattern
    "SELECT ?s WHERE { ?s <http://ex.org/nothing> ?o }",
    # DISTINCT + ORDER BY on top of a join
    """SELECT DISTINCT ?b WHERE {
        ?a <http://ex.org/knows> ?b .
        ?b <http://ex.org/name> ?n } ORDER BY ?b""",
    # projection narrower than the pattern, and a projected variable the
    # pattern never binds
    "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT ?zzz WHERE { ?s <http://ex.org/name> ?o }",
    # LIMIT / OFFSET over single patterns: both evaluation paths see the
    # store's own row order, so the slices must agree exactly
    "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2",
    "SELECT * WHERE { ?s ?p ?o } LIMIT 3 OFFSET 2",
    "SELECT ?s WHERE { ?s ?p ?o } OFFSET 8",
    "SELECT ?s WHERE { ?s ?p ?o } LIMIT 0",
    # ASK: ground, variable, no match, repeated variable, join, empty
    """ASK { <http://ex.org/bob> <http://ex.org/age>
        "42"^^<http://www.w3.org/2001/XMLSchema#integer> }""",
    "ASK { ?s <http://ex.org/knows> ?o }",
    "ASK { ?s <http://ex.org/nothing> ?o }",
    "ASK { ?x <http://ex.org/likes> ?x }",
    "ASK { ?a <http://ex.org/knows> ?b . ?b <http://ex.org/age> ?n }",
    "ASK { ?a <http://ex.org/knows> ?b . ?b <http://ex.org/nothing> ?n }",
    "ASK {}",
    # --- FILTER: numeric comparisons (integer, decimal, double, NaN, ill-typed, byte)
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a < 10) }",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a >= 42) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(42 = ?a) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(?a != 42) }",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > -5 && ?a < 50) }",
    "SELECT ?x ?v WHERE { ?x <http://ex.org/score> ?v FILTER(?v > 1) }",
    "SELECT ?x ?v WHERE { ?x <http://ex.org/score> ?v FILTER(?v < 100) }",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x WHERE { ?x <http://ex.org/age> ?a
        FILTER(datatype(?a) = xsd:integer && ?a < 100) }""",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(datatype(?a) != xsd:integer) }""",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a IN (42, 7)) }",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a NOT IN (42)) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(sameTerm(?a, 42)) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(isNumeric(?a)) }",
    "SELECT ?x WHERE { ?x <http://ex.org/score> ?v FILTER(?v + 1 > 2) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(coalesce(?zzz, ?a) = 42) }",
    'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(str(?a) = "42") }',
    'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(?a = "42") }',
    # --- FILTER: strings, language tags, regex
    """SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n
        FILTER(langMatches(lang(?n), "EN")) }""",
    """SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n
        FILTER(langMatches(lang(?n), "en-US")) }""",
    'SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n FILTER(lang(?n) = "") }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(?n = "Bob"@en) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(?n = "Carol") }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(str(?n) = "Bob") }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(strstarts(?n, "A")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(strstarts(?n, "B"@en)) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(contains(?n, "o")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(strends(str(?n), "e")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(regex(?n, "^a", "i")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(regex(str(?n), "\\"")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(?n != "") }',
    "SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(?n) }",
    # --- FILTER: term kinds, IRIs, bound
    "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(isIRI(?o)) }",
    "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(isBlank(?s)) }",
    "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(isLiteral(?o) && !isBlank(?s)) }",
    "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(isIRI(?o) || isBlank(?o)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(bound(?o)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(!bound(?zzz)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(bound(?zzz)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?o = <http://ex.org/carol>) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?o != <http://ex.org/carol>) }",
    'SELECT ?s WHERE { ?s ?p ?o FILTER(?o IN (<http://ex.org/carol>, "Carol")) }',
    # --- FILTER: constants, several variables, joins, other datatypes
    "SELECT ?s WHERE { ?s ?p ?o FILTER(1 = 2) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(1 < 2) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?s != ?o) }",
    "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(sameTerm(?s, ?o)) }",
    """SELECT ?a ?b WHERE { ?x <http://ex.org/age> ?a . ?y <http://ex.org/age> ?b
        FILTER(?a < ?b) }""",
    """SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n . ?x <http://ex.org/age> ?a
        FILTER(?a > 10) }""",
    """SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n . ?x <http://ex.org/age> ?a
        FILTER(?a > 10 && lang(?n) = "en") }""",
    """SELECT ?x WHERE { ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?n
        FILTER(regex(?n, "^C")) }""",
    'SELECT ?x WHERE { ?x <http://ex.org/code> ?c FILTER(?c = "x"^^<http://ex.org/dt>) }',
    "SELECT ?x WHERE { ?x <http://ex.org/code> ?c FILTER(?c < 5) }",
    "SELECT ?x WHERE { ?x <http://ex.org/flag> ?f FILTER(?f) }",
    "SELECT ?x WHERE { ?x <http://ex.org/flag> ?f FILTER(?f = true) }",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x WHERE { ?x <http://ex.org/born> ?d
        FILTER(?d < "2021-01-01T00:00:00"^^xsd:dateTime) }""",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?x WHERE { ?x <http://ex.org/born> ?d
        FILTER(?d = "2020-01-01T00:00:00"^^xsd:dateTime) }""",
    # --- FILTER: EXISTS (generic route), OPTIONAL-bound var, two FILTERs, RAND (fallback)
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER EXISTS { ?x <http://ex.org/knows> <http://ex.org/carol> } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        OPTIONAL { ?x <http://ex.org/age> ?a } FILTER(!bound(?a)) }""",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) FILTER(?a < 50) }",
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(rand() < 2) }",
    # --- FILTER under VALUES: the filter sees a context-bound variable
    """SELECT ?x ?a WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/dave> }
        ?x <http://ex.org/age> ?a FILTER(?a > 10) }""",
    """SELECT ?x ?a WHERE { VALUES ?x { <http://ex.org/bob> }
        ?x <http://ex.org/age> ?a FILTER(?x = <http://ex.org/bob>) }""",
    "ASK { ?x <http://ex.org/age> ?a FILTER(?a > 100) }",
    "ASK { ?x <http://ex.org/age> ?a FILTER(?a > 10) }",
    # --- DISTINCT (code-level, then term-level: "042" and "42" are one integer)
    "SELECT DISTINCT ?p WHERE { ?s ?p ?o }",
    "SELECT DISTINCT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }",
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT DISTINCT ?n WHERE { ?x <http://ex.org/name> ?n }",
    "SELECT DISTINCT ?o WHERE { ?s ?p ?o } LIMIT 3",
    "SELECT DISTINCT ?o WHERE { ?s ?p ?o } OFFSET 5 LIMIT 4",
    """SELECT DISTINCT ?x WHERE {
        ?x <http://ex.org/name> ?n . ?x <http://ex.org/age> ?a }""",
    "SELECT DISTINCT ?x ?zzz WHERE { ?x <http://ex.org/name> ?n }",
    "SELECT DISTINCT * WHERE { ?s ?p ?o }",
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) }",
    "SELECT REDUCED ?p WHERE { ?s ?p ?o }",
    "SELECT DISTINCT ?p WHERE { ?s ?p ?o } ORDER BY ?p LIMIT 2",
    # --- COUNT: no grouping (count_quads fast path), bound/unbound/distinct targets
    "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
    "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/nothing> ?o }",
    "SELECT (COUNT(?o) AS ?n) WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT (COUNT(?zzz) AS ?n) WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT (COUNT(DISTINCT ?n) AS ?c) WHERE { ?x <http://ex.org/name> ?n }",
    "SELECT (COUNT(DISTINCT *) AS ?n) WHERE { ?x <http://ex.org/name> ?n }",
    "SELECT (COUNT(*) AS ?n) (COUNT(?o) AS ?m) WHERE { ?s ?p ?o }",
    "SELECT (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/likes> ?x }",
    "SELECT (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) }",
    """SELECT (COUNT(*) AS ?n) WHERE {
        ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?m }""",
    # --- GROUP BY: columns, several keys, an absent key, HAVING, joins, empty input
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p",
    "SELECT ?s (COUNT(?o) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?s",
    "SELECT ?p ?s (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p ?s",
    "SELECT ?a (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/age> ?a } GROUP BY ?a",
    "SELECT ?n (COUNT(*) AS ?c) WHERE { ?x <http://ex.org/name> ?n } GROUP BY ?n",
    "SELECT ?zzz (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?zzz",
    "SELECT ?p (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p HAVING (COUNT(*) > 3)",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER(isIRI(?o)) } GROUP BY ?p",
    """SELECT ?x (COUNT(*) AS ?n) WHERE {
        ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?m } GROUP BY ?x""",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p <http://ex.org/nothing> } GROUP BY ?p",
    "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY ?p LIMIT 2",
    # --- aggregates left to rdflib: SUM, a bare variable without GROUP BY, non-block inputs
    "SELECT (SUM(?v) AS ?n) WHERE { ?x <http://ex.org/score> ?v }",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
    """SELECT ?x (COUNT(*) AS ?n) WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/dave> }
        ?x <http://ex.org/age> ?a } GROUP BY ?x""",
    """SELECT (COUNT(?a) AS ?n) WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    # --- OPTIONAL: hash and probe paths, hoisted inner FILTER, nesting, chains, fan-out
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/knows> ?y OPTIONAL { ?y <http://ex.org/age> ?a } }""",
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a FILTER(?a > 10) } }""",
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n
        OPTIONAL { ?x <http://ex.org/age> ?a FILTER(?a > 10 && lang(?n) = "en") } }""",
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a FILTER(1 = 2) } }""",
    """SELECT ?x ?y ?z WHERE {
        ?x <http://ex.org/knows> ?y
        OPTIONAL { ?y <http://ex.org/knows> ?z OPTIONAL { ?z <http://ex.org/age> ?a } } }""",
    """SELECT ?x ?a ?s WHERE {
        ?x <http://ex.org/name> ?n
        OPTIONAL { ?x <http://ex.org/age> ?a }
        OPTIONAL { ?x <http://ex.org/score> ?s } }""",
    """SELECT ?x ?y ?n WHERE {
        ?x <http://ex.org/knows> ?y OPTIONAL { ?y <http://ex.org/name> ?n } }""",
    """SELECT ?x ?o WHERE {
        ?x <http://ex.org/age> ?a OPTIONAL { <http://ex.org/alice> <http://ex.org/name> ?o } }""",
    """SELECT ?x ?n WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/nothing> ?a } }""",
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } FILTER(bound(?a)) }""",
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } FILTER(?a > 10) }""",
    """SELECT ?x WHERE {
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a }
        FILTER(?a > 10 || !bound(?a)) }""",
    # an OPTIONAL variable joined again later: a nullable key, left to rdflib
    """SELECT ?x ?y ?n WHERE {
        ?x <http://ex.org/name> ?m OPTIONAL { ?x <http://ex.org/knows> ?y }
        ?y <http://ex.org/name> ?n }""",
    # OPTIONAL over a joined block, and inside a lazy join (a context-bound outer variable)
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?n
        OPTIONAL { ?y <http://ex.org/age> ?a } }""",
    """SELECT ?x ?a ?s WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/erin> }
        ?x <http://ex.org/age> ?a OPTIONAL { ?x <http://ex.org/score> ?s } }""",
    """SELECT ?x ?a WHERE {
        VALUES ?a { 42 7 }
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    """ASK { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a }
        FILTER(!bound(?a)) }""",
    # --- group joins: nested groups, three groups (non-lazy), cross product, a filtered group
    """SELECT ?x ?n ?a WHERE {
        { ?x <http://ex.org/name> ?n } { ?x <http://ex.org/age> ?a } }""",
    """SELECT ?x ?n ?a ?s WHERE {
        { ?x <http://ex.org/name> ?n } { ?x <http://ex.org/age> ?a }
        { ?x <http://ex.org/score> ?s } }""",
    """SELECT ?n ?m WHERE {
        { <http://ex.org/alice> <http://ex.org/name> ?n }
        { <http://ex.org/bob> <http://ex.org/name> ?m } }""",
    """SELECT ?x ?n ?a WHERE {
        { ?x <http://ex.org/name> ?n FILTER(lang(?n) = "en") } { ?x <http://ex.org/age> ?a } }""",
    """SELECT ?x ?n ?a WHERE {
        { ?x <http://ex.org/name> ?n } { ?x <http://ex.org/age> ?a FILTER(?a > 10) } }""",
    """SELECT ?x ?y WHERE {
        { ?x <http://ex.org/knows> ?y } { ?y <http://ex.org/knows> ?x } }""",
    # a filter in the second group that could see the first group's binding: left to rdflib
    """SELECT ?x ?n ?a WHERE {
        { ?x <http://ex.org/name> ?n }
        { ?x <http://ex.org/age> ?a FILTER(str(?n) != "") } }""",
    # --- MINUS: shared variables, none, empty right side, nested, under a filter
    """SELECT ?x ?n WHERE {
        ?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/age> ?a } }""",
    """SELECT ?x ?n WHERE {
        ?x <http://ex.org/name> ?n MINUS { ?y <http://ex.org/age> ?a } }""",
    """SELECT ?x ?n WHERE {
        ?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/nothing> ?a } }""",
    """SELECT ?x ?y WHERE {
        ?x <http://ex.org/knows> ?y MINUS { ?x <http://ex.org/knows> <http://ex.org/carol> } }""",
    """SELECT ?x ?y WHERE {
        ?x <http://ex.org/knows> ?y
        MINUS { ?y <http://ex.org/name> ?n FILTER(lang(?n) = "en") } }""",
    """SELECT ?x WHERE {
        ?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/age> ?a } FILTER(isIRI(?x)) }""",
    """SELECT ?x ?n WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/alice> }
        ?x <http://ex.org/name> ?n MINUS { ?y <http://ex.org/nothing> ?z } }""",
    """SELECT ?x ?n WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/alice> }
        ?x <http://ex.org/name> ?n MINUS { ?y <http://ex.org/age> ?z } }""",
    """SELECT (COUNT(*) AS ?n) WHERE {
        ?x <http://ex.org/name> ?m MINUS { ?x <http://ex.org/age> ?a } }""",
    """SELECT DISTINCT ?x WHERE {
        ?x <http://ex.org/knows> ?y OPTIONAL { ?y <http://ex.org/age> ?a } }""",
    # --- (NOT) EXISTS: semi/anti-joins on the shared variables, and the shapes left to rdflib
    "SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER EXISTS { ?x <http://ex.org/age> ?a } }",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER(!EXISTS { ?x <http://ex.org/age> ?a }) }""",
    """SELECT ?x ?y WHERE { ?x <http://ex.org/knows> ?y
        FILTER EXISTS { ?y <http://ex.org/knows> ?x } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER EXISTS { <http://ex.org/alice> <http://ex.org/knows> ?z } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?z <http://ex.org/nothing> ?w } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER(EXISTS { ?x <http://ex.org/age> ?a } || lang(?n) = "en") }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER EXISTS { ?x <http://ex.org/age> ?a FILTER(?a > 10) } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/age> ?a
        FILTER EXISTS { ?x <http://ex.org/score> ?s FILTER(?s > ?a) } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a }
        FILTER NOT EXISTS { ?y <http://ex.org/age> ?a } }""",
    """SELECT ?x ?a WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/alice> }
        ?x <http://ex.org/age> ?a FILTER EXISTS { ?x <http://ex.org/knows> ?y } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER EXISTS { ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?m } }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/knows> ?y
        OPTIONAL { ?y <http://ex.org/age> ?a } } }""",
    "SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER EXISTS { ?x <http://ex.org/age> ?x } }",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER(EXISTS { ?x <http://ex.org/age> ?a } && ?n != "") }""",
    """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER EXISTS { ?x <http://ex.org/knows> ?y
        FILTER NOT EXISTS { ?y <http://ex.org/age> ?a } } }""",
    "ASK { ?x <http://ex.org/name> ?n FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }",
    """SELECT (COUNT(*) AS ?c) WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }""",
    # --- ORDER BY (as multisets here; ORDER_QUERIES below compares the sequences)
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY DESC(?n)",
    "SELECT ?s ?p ?o WHERE { ?s ?p ?o FILTER(?o = ?o) } ORDER BY ?o",
    "SELECT ?x ?s WHERE { ?x <http://ex.org/score> ?s FILTER(?s = ?s) } ORDER BY ?s",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a LIMIT 2",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY DESC(?a) OFFSET 1 LIMIT 2",
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a",
    "SELECT DISTINCT ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY ?n LIMIT 3",
    """SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }
        ORDER BY ?a""",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY str(?n)",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY ?zzz ?n",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) } ORDER BY DESC(?a)",
    "SELECT * WHERE { ?s ?p ?o } ORDER BY ?p ?s LIMIT 5",
    """SELECT ?x ?a WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/gina> }
        ?x <http://ex.org/age> ?a } ORDER BY ?a""",
]

#: ORDER BY queries whose order is total (every tie broken by a unique variable),
#: compared as sequences: the pushdown must sort exactly as rdflib does. The
#: NaN score is kept out of sorted columns (`?o = ?o`): rdflib's comparator
#: raises decimal.InvalidOperation on a NaN double against a decimal.
ORDER_QUERIES = [
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY DESC(?a) ?x",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY ?n ?x",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY DESC(?n) DESC(?x)",
    "SELECT ?s ?p ?o WHERE { ?s ?p ?o FILTER(?o = ?o) } ORDER BY ?o ?s ?p",
    "SELECT ?s ?p ?o WHERE { ?s ?p ?o FILTER(?o = ?o) } ORDER BY DESC(?s) ?p ?o",
    "SELECT ?s ?p ?o WHERE { ?s ?p ?o FILTER(?o = ?o) } ORDER BY ?p DESC(?o) ?s",
    """SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }
        ORDER BY ?a ?x""",
    """SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }
        ORDER BY DESC(?a) ?x""",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x LIMIT 2",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x OFFSET 1 LIMIT 3",
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a",
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY DESC(?a) LIMIT 2",
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) } ORDER BY DESC(?a) ?x",
    "SELECT ?x ?s WHERE { ?x <http://ex.org/score> ?s FILTER(?s = ?s) } ORDER BY ?s ?x",
    "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY ?p",
    "SELECT ?x ?y WHERE { ?x <http://ex.org/knows> ?y } ORDER BY ?y ?x",
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n } ORDER BY ?zzz ?n ?x",
]

XSD = "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
QUERIES += [
    # --- VALUES: present and absent constants, UNDEF, spellings, joins, standalone
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/nobody> }
        ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE {
        VALUES (?x ?n) { (<http://ex.org/bob> "Bob"@EN) (<http://ex.org/alice> UNDEF) }
        ?x <http://ex.org/name> ?n }""",
    XSD
    + """SELECT ?x ?a WHERE { VALUES ?a { 42 "042"^^xsd:integer 7 "7"^^xsd:byte }
        ?x <http://ex.org/age> ?a }""",
    XSD
    + """SELECT ?x WHERE { VALUES ?n { "Carol" "Alice"^^xsd:string "Bob"@en }
        ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?n { "q\\"uote" "" } ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE {
        VALUES ?n { "tab\\there" "line\\nbreak" "back\\\\slash" "\\u0001control" }
        ?x <http://ex.org/note> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?n { "x"@en-US "x"@EN-US "x"@en }
        ?x <http://ex.org/note> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> }
        ?x <http://ex.org/name> ?n FILTER(?x = <http://ex.org/bob>) }""",
    "SELECT * WHERE { VALUES (?x ?y) { (1 2) (UNDEF 3) (<http://ex.org/nobody> UNDEF) } }",
    "SELECT ?x WHERE { VALUES ?x { <http://ex.org/nobody> } }",
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> } { ?x <http://ex.org/name> ?n } }""",
    """SELECT ?x ?n ?a WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/carol> }
        ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    """SELECT (COUNT(*) AS ?c) WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/nobody> }
        ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE { { VALUES ?x { <http://ex.org/bob> } }
        { VALUES ?x { <http://ex.org/bob> <http://ex.org/alice> } } ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/alice> }
        ?x <http://ex.org/name> ?n MINUS { VALUES ?x { <http://ex.org/bob> } } }""",
    """SELECT DISTINCT ?y WHERE { VALUES ?x { <http://ex.org/alice> <http://ex.org/bob> }
        ?x <http://ex.org/knows> ?y }""",
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/nobody> <http://ex.org/bob> }
        ?x <http://ex.org/name> ?n } ORDER BY ?x""",
]
ORDER_QUERIES += [
    """SELECT ?x ?y WHERE {
        VALUES (?x ?y) { (<http://ex.org/nobody> 3) (<http://ex.org/bob> 1) (UNDEF 2) } }
        ORDER BY ?x ?y""",
]


@pytest.fixture(scope="module")
def dict_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown")
    nt = d / "fixture.nt"
    nt.write_text(FIXTURE_NT)
    out = d / "fixture.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(dict_vortex, request):
    """The fixture store, file-backed and loaded into memory: the native
    match path differs between the two, the pushdown must not."""
    return Graph(store=VortexStore(str(dict_vortex), in_memory=request.param))


def _row_key(row):
    # rdflib's term ordering is not total (NaN literals), so sort on the
    # N-Triples spellings, which is.
    return tuple("" if term is None else term.n3() for term in row)


def run(graph, sparql):
    """A query's answer in a comparable form: the ASK boolean, or the
    solution rows as a sorted multiset."""
    result = graph.query(sparql)
    if result.type == "ASK":
        return result.askAnswer
    return sorted((tuple(row) for row in result), key=_row_key)


def run_ordered(graph, sparql):
    """The solution rows in the order the query produced them."""
    return [tuple(row) for row in graph.query(sparql)]


def both_ways(graph, sparql, runner=run):
    """The answer with the pushdown and under rdflib's default evaluator."""
    register_sparql_pushdown()
    with_pushdown = runner(graph, sparql)
    try:
        unregister_sparql_pushdown()
        without_pushdown = runner(graph, sparql)
    finally:
        register_sparql_pushdown()
    return with_pushdown, without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_pushdown_equals_default_evaluator(graph, sparql):
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("sparql", ORDER_QUERIES)
def test_order_by_sequence_equals_default_evaluator(graph, sparql):
    with_pushdown, without_pushdown = both_ways(graph, sparql, run_ordered)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_probe_join_path_equals_default_evaluator(graph, monkeypatch, sparql):
    """Same A/B matrix with the probe threshold forced to zero, so every
    shared-variable join takes the per-binding probe path instead of the
    hash join — both strategies must be observationally identical."""
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    with_probe, without_pushdown = both_ways(graph, sparql)
    assert with_probe == without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_bgp_only_mode_equals_default_evaluator(graph, monkeypatch, sparql):
    """``VORTEX_RDF_PUSHDOWN_OPS=bgp``: only basic graph patterns are solved
    here, every head is rdflib's own operator over the rows we hand back."""
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset({"BGP"}))
    bgp_only, without_pushdown = both_ways(graph, sparql)
    assert bgp_only == without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_generic_filter_route_equals_default_evaluator(graph, monkeypatch, sparql):
    """``VORTEX_RDF_FILTER_FAST=0``: every FILTER value goes through rdflib's
    own expression evaluator, once per distinct value."""
    monkeypatch.setattr(filters, "_FAST_ENABLED", False)
    generic, without_pushdown = both_ways(graph, sparql)
    assert generic == without_pushdown


def test_probe_join_triggers_on_skewed_join(tmp_path, monkeypatch):
    """A 1-row anchor joined against a 300-row predicate must take the probe
    path under the real threshold, and still produce the right rows."""
    nt = tmp_path / "skewed.nt"
    nt.write_text(
        '<http://ex.org/s0> <http://ex.org/rare> "anchor" .\n'
        + "".join(
            f"<http://ex.org/s{i}> <http://ex.org/big> <http://ex.org/o{i}> .\n" for i in range(300)
        )
    )
    out = tmp_path / "skewed.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")

    probes = []
    original = pd._probe_join
    monkeypatch.setattr(pd, "_probe_join", lambda *a: probes.append(1) or original(*a))
    graph = Graph(store=VortexStore(str(out)))
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?s ?o WHERE {
            ?s <http://ex.org/rare> "anchor" .
            ?s <http://ex.org/big> ?o }""",
    )
    assert rows == [(URIRef("http://ex.org/s0"), URIRef("http://ex.org/o0"))]
    assert probes, "the skewed join did not take the probe path"


def test_pushdown_is_actually_used(graph, monkeypatch):
    calls = []
    original = pd._solve_bgp
    monkeypatch.setattr(pd, "_solve_bgp", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert calls, "pushdown hook was not invoked"


def test_head_intercepts_the_whole_slice_project_chain(graph, monkeypatch):
    """A ``Slice(Project(BGP))`` is one interception at the Slice: rdflib
    never evaluates the Project or the BGP below it itself."""
    heads = []
    original = pd._eval_head
    monkeypatch.setattr(pd, "_eval_head", lambda *a: heads.append(a[2].name) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2
    assert heads == ["Slice"]
    heads.clear()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert heads == ["Project"]


def test_solutions_are_built_without_context_push(graph, monkeypatch):
    """Solutions are constructed directly as FrozenBindings; the per-row
    ``ctx.push()`` scope rdflib's own evalBGP opens is never needed."""
    pushes = []
    original = QueryContext.push
    monkeypatch.setattr(QueryContext, "push", lambda self: pushes.append(1) or original(self))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert not pushes


def test_limit_decodes_only_the_first_chunk(tmp_path):
    """LIMIT stops decoding: a 700-row match under ``LIMIT 10`` decodes no
    more than the first chunk of rows."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        )
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    rows = run(graph, "SELECT * WHERE { ?s ?p ?o } LIMIT 10")
    assert len(rows) == 10
    assert len(store._decode_cache) <= 3 * pd._CHUNK_START


class _CountingNative:
    """A VortexRdfStore stand-in that counts the calls the pushdown makes."""

    def __init__(self, inner):
        self._inner = inner
        self.counts = 0
        self.matches = 0

    def count_quads(self, *args, **kwargs):
        self.counts += 1
        return self._inner.count_quads(*args, **kwargs)

    def match_codes(self, *args, **kwargs):
        self.matches += 1
        return self._inner.match_codes(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_ask_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = _CountingNative(graph.store._store())
    graph.store._native = native
    assert run(graph, "ASK { ?s <http://ex.org/knows> ?o }") is True
    assert run(graph, "ASK { ?s <http://ex.org/nothing> ?o }") is False
    assert (native.counts, native.matches) == (2, 0)


def test_native_none_falls_back_at_call_time(graph):
    """A store whose code path declines (``match_codes`` -> None) makes the
    hook raise while rdflib is still listening, so the default evaluator
    answers — no lazily raised NotImplementedError reaches the caller."""
    inner = graph.store._store()

    class _NoCodes:
        def match_codes(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._native = _NoCodes()
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2


def test_pushdown_ops_env_switch(dict_vortex, monkeypatch):
    monkeypatch.setattr(pd, "_ENABLED_OPS", pd._ALL_OPS)
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "bgp")
    VortexStore(str(dict_vortex))
    assert pd._ENABLED_OPS == {"BGP"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "BGP, Project")
    VortexStore(str(dict_vortex))
    assert pd._ENABLED_OPS == {"BGP", "Project"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "Bogus")
    with pytest.raises(ValueError, match="Bogus"):
        VortexStore(str(dict_vortex))


def test_non_vortex_graphs_unaffected(dict_vortex):
    register_sparql_pushdown()
    g = Graph()
    g.add((URIRef("http://ex.org/a"), URIRef("http://ex.org/name"), Literal("A")))
    rows = run(g, "SELECT ?n WHERE { ?s <http://ex.org/name> ?n }")
    assert rows == [(Literal("A"),)]


def test_string_fallback_when_code_path_disabled(dict_vortex, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    graph = Graph(store=VortexStore(str(dict_vortex)))
    rows = run(
        graph,
        """SELECT ?n WHERE {
            <http://ex.org/alice> <http://ex.org/knows> ?x .
            ?x <http://ex.org/name> ?n }""",
    )
    assert {row[0] for row in rows} == {Literal("Bob", lang="en"), Literal("Carol")}


def test_filter_is_pushed_into_the_pattern_scan(graph, monkeypatch):
    """A single-variable conjunct restricts the pattern before any row is
    built, on the fast route: rdflib's evaluator is never consulted."""
    restrictions = []
    original = pd._restrict_pattern
    monkeypatch.setattr(pd, "_restrict_pattern", lambda *a: restrictions.append(1) or original(*a))
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


def test_values_outside_the_fast_domain_take_the_generic_route(graph, monkeypatch):
    """``str(?a)`` of a numeric literal is rdflib's normalized lexical form:
    the fast route defers those values, one call per distinct value."""
    generic_calls = []
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(b) or original_generic(self, b),
    )
    register_sparql_pushdown()
    rows = run(graph, 'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(str(?a) = "42") }')
    assert {row[0] for row in rows} == {URIRef("http://ex.org/bob"), URIRef("http://ex.org/dave")}
    assert len(generic_calls) == 5  # one per distinct age code, never per row


def test_init_bindings_are_visible_to_filters(graph):
    sparql = "SELECT ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?x = ?who && ?a > 0) }"
    init = {"who": URIRef("http://ex.org/bob")}
    register_sparql_pushdown()
    with_pushdown = sorted((tuple(r) for r in graph.query(sparql, initBindings=init)), key=_row_key)
    try:
        unregister_sparql_pushdown()
        without_pushdown = sorted(
            (tuple(r) for r in graph.query(sparql, initBindings=init)), key=_row_key
        )
    finally:
        register_sparql_pushdown()
    assert with_pushdown == without_pushdown == [(Literal(42),)]


def test_distinct_and_count_are_answered_in_code_space(graph, monkeypatch):
    distincts, aggregates = [], []
    original_distinct = pd._eval_distinct
    original_aggregate = pd._eval_aggregate
    monkeypatch.setattr(
        pd, "_eval_distinct", lambda *a: distincts.append(1) or original_distinct(*a)
    )
    monkeypatch.setattr(
        pd, "_eval_aggregate", lambda *a: aggregates.append(1) or original_aggregate(*a)
    )
    register_sparql_pushdown()
    rows = run(graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }")
    assert len(rows) == 4  # "042" and "42" are the same integer
    rows = run(graph, "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p")
    assert (URIRef("http://ex.org/name"), Literal(8)) in rows
    assert distincts and aggregates


def test_count_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = _CountingNative(graph.store._store())
    graph.store._native = native
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/name> ?o }")
    assert rows == [(Literal(8),)]
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/nothing> ?o }")
    assert rows == [(Literal(0),)]
    assert (native.counts, native.matches) == (2, 0)


def test_distinct_decodes_only_the_survivors(tmp_path):
    """``SELECT DISTINCT ?p`` over 700 rows with 7 predicates decodes 7 codes."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        )
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    rows = run(graph, "SELECT DISTINCT ?p WHERE { ?s ?p ?o }")
    assert len(rows) == 7
    assert len(store._decode_cache) == 7


def test_left_join_probe_and_hash_paths_are_ours(graph, monkeypatch):
    """An OPTIONAL is solved inside the hook — the inner pattern probed per
    outer row under a low threshold, hash-joined under a high one — never by
    rdflib re-entering the BGP hook once per outer solution."""
    left_joins, probes, bgps = [], [], []
    original_left = pd._solve_left_join
    original_probe = pd._probe_join
    original_bgp = pd._solve_bgp
    monkeypatch.setattr(
        pd, "_solve_left_join", lambda *a: left_joins.append(1) or original_left(*a)
    )
    monkeypatch.setattr(
        pd, "_probe_join", lambda *a, **k: probes.append(1) or original_probe(*a, **k)
    )
    monkeypatch.setattr(pd, "_solve_bgp", lambda *a: bgps.append(1) or original_bgp(*a))
    register_sparql_pushdown()
    sparql = (
        "SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }"
    )
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    assert len(run(graph, sparql)) == 8
    assert left_joins and probes and len(bgps) == 1
    left_joins.clear(), probes.clear(), bgps.clear()
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 10**9)
    assert len(run(graph, sparql)) == 8
    assert left_joins and not probes and len(bgps) == 1


def test_minus_is_an_anti_join_in_code_space(graph, monkeypatch):
    calls = []
    original = pd._solve_minus
    monkeypatch.setattr(pd, "_solve_minus", lambda *a: calls.append(1) or original(*a))
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
    original = pd._solve_left_join

    def spy(*args):
        try:
            return original(*args)
        except NotImplementedError:
            left_joins.append("fallback")
            raise

    monkeypatch.setattr(pd, "_solve_left_join", spy)
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
    original_apply = pd._apply_exists
    original_probe = pd._probe_exists
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(pd, "_apply_exists", lambda *a: applied.append(1) or original_apply(*a))
    monkeypatch.setattr(pd, "_probe_exists", lambda *a: probes.append(1) or original_probe(*a))
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(1) or original_generic(self, b),
    )
    register_sparql_pushdown()
    sparql = """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }"""
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    assert len(run(graph, sparql)) == 3 and applied and probes and not generic_calls
    applied.clear(), probes.clear()
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 10**9)
    assert len(run(graph, sparql)) == 3 and applied and not probes and not generic_calls
    # a body FILTER that sees the block's bindings: rdflib's evaluator, per distinct tuple
    rows = run(
        graph,
        """SELECT ?x WHERE { ?x <http://ex.org/age> ?a
            FILTER EXISTS { ?x <http://ex.org/score> ?s FILTER(?s > ?a) } }""",
    )
    assert rows == [(URIRef("http://ex.org/frank"),)] and generic_calls


def test_order_by_is_a_rank_sort_in_code_space(graph, monkeypatch):
    ordered = []
    original = pd._order_rows
    monkeypatch.setattr(pd, "_order_rows", lambda *a: ordered.append(a[4]) or original(*a))
    register_sparql_pushdown()
    rows = run_ordered(
        graph, "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x LIMIT 2"
    )
    assert [row[1].toPython() for row in rows] == [-3, 7]  # the 7 is an xsd:byte
    assert ordered == [2]  # the LIMIT bounds the sort to the top 2
    ordered.clear()
    rows = run_ordered(
        graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a LIMIT 2"
    )
    assert [row[0].toPython() for row in rows] == [-3, 7]
    assert ordered == [None]  # DISTINCT narrows after the sort: no top-k


def test_values_is_a_code_space_relation(graph, monkeypatch):
    calls = []
    original = pd._solve_values
    monkeypatch.setattr(pd, "_solve_values", lambda *a: calls.append(1) or original(*a))
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
