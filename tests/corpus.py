"""The data the test modules share: the fixture statements, as triples and
as quads, and the query shapes run both ways over them; the quads the
probe joins' multiplicity is checked over; the literal spellings the
FILTER routes are checked over."""

from tests.support import typed

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
<http://ex.org/ivan> <http://ex.org/rating> "10"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/ivan> <http://ex.org/weight> "3"^^<http://www.w3.org/2001/XMLSchema#byte> .
<http://ex.org/jane> <http://ex.org/rating> "150"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/jane> <http://ex.org/weight> "-4"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/kim> <http://ex.org/rating> "42"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/kim> <http://ex.org/weight> "1.5"^^<http://www.w3.org/2001/XMLSchema#decimal> .
""" + (
    "<http://ex.org/dave> <http://ex.org/born> "
    '"2020-01-01T00:00:00"^^<http://www.w3.org/2001/XMLSchema#dateTime> .\n'
)

#: Graph names the quad fixture spreads the same statements over; the empty
#: one is the default graph, which `GRAPH ?g` must never bind. `_:g4` is a
#: blank-node graph name — legal in N-Quads, and the case a bare `Graph`'s own
#: blank-node label has to be told apart from.
FIXTURE_GRAPHS = (
    "",
    "<http://ex.org/g/1>",
    "<http://ex.org/g/2>",
    "<http://ex.org/g/3>",
    "_:g4",
)


def as_quads(ntriples: str) -> str:
    """The triple fixture as N-Quads: every statement round-robin into one of
    ``FIXTURE_GRAPHS``, then the first few repeated into the last graph — so
    the union view holds some triples more than once, joins have to span
    graphs, and each graph is a proper subset of the whole."""

    def quad(line: str, graph: str) -> str:
        statement = line.rstrip().removesuffix(".").rstrip()
        return f"{statement} {graph} ." if graph else f"{statement} ."

    lines = [line for line in ntriples.splitlines() if line]
    quads = [quad(line, FIXTURE_GRAPHS[i % len(FIXTURE_GRAPHS)]) for i, line in enumerate(lines)]
    quads += [quad(line, FIXTURE_GRAPHS[-1]) for line in lines[:4]]
    return "\n".join(quads) + "\n"


FIXTURE_NQ = as_quads(FIXTURE_NT)

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
    # --- FILTER: integer + and - (compiled), and an operand that defers.
    # `rating` and `weight` are the only well-formed integer columns: `age`
    # holds an ill-typed "abc", which rdflib's own arithmetic raises on.
    "SELECT ?x ?r WHERE { ?x <http://ex.org/rating> ?r FILTER(?r - 8 > 0) }",
    "SELECT ?x ?r WHERE { ?x <http://ex.org/rating> ?r FILTER(?r + 1 - 3 < 40) }",
    "SELECT ?x ?w WHERE { ?x <http://ex.org/weight> ?w FILTER(?w + 1 > 0) }",
    # BSBM Explore Q5's similarity band: two block variables in one
    # arithmetic conjunct, so it is memoized per distinct code pair.
    """SELECT ?x ?r WHERE {
        <http://ex.org/kim> <http://ex.org/rating> ?ref .
        ?x <http://ex.org/rating> ?r
        FILTER(?r < ?ref + 100 && ?r > ?ref - 100) }""",
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

XSD_PREFIX = "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
QUERIES += [
    # --- VALUES: present and absent constants, UNDEF, spellings, joins, standalone
    """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/nobody> }
        ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE {
        VALUES (?x ?n) { (<http://ex.org/bob> "Bob"@EN) (<http://ex.org/alice> UNDEF) }
        ?x <http://ex.org/name> ?n }""",
    XSD_PREFIX
    + """SELECT ?x ?a WHERE { VALUES ?a { 42 "042"^^xsd:integer 7 "7"^^xsd:byte }
        ?x <http://ex.org/age> ?a }""",
    XSD_PREFIX
    + """SELECT ?x WHERE { VALUES ?n { "Carol" "Alice"^^xsd:string "Bob"@en }
        ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?n { "q\\"uote" "" } ?x <http://ex.org/name> ?n }""",
    """SELECT ?x ?n WHERE {
        VALUES ?n { "tab\\there" "line\\nbreak" "back\\\\slash" "\\u0001control" }
        ?x <http://ex.org/note> ?n }""",
    """SELECT ?x ?n WHERE { VALUES ?n { "x"@en-US "x"@EN-US "x"@en }
        ?x <http://ex.org/note> ?n }""",
    # an absent literal with a newline, substituted into a probe
    'SELECT ?s WHERE { VALUES ?n { "zz\\nyy" "Alice" } ?s <http://ex.org/name> ?n }',
    """SELECT ?n WHERE { VALUES ?n { "zz\\nyy" "Alice" }
        FILTER EXISTS { ?s <http://ex.org/name> ?n } }""",
    # a relative IRI (no BASE), which no probe can match
    "SELECT ?s ?o WHERE { VALUES ?o { <zz> <http://ex.org/carol> } ?s <http://ex.org/knows> ?o }",
    """SELECT ?o ?s WHERE { VALUES ?o { <zz> <http://ex.org/carol> }
        OPTIONAL { ?s <http://ex.org/likes> ?o } }""",
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

#: Two-variable FILTERs over composite blocks. Their fast conjuncts reach
#: every BGP the single-variable ones do, as a mid-join pre-filter (always
#: applied under the probe-forcing runs, `SETTINGS.probe_fanout = 0`): a group
#: join's sides and an OPTIONAL's or a MINUS's left side, never their right
#: side, where dropping a row would change the answer.
QUERIES += [
    """SELECT ?x ?a ?s WHERE {
        { ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . ?x <http://ex.org/name> ?n }
        { ?x <http://ex.org/name> ?m }
        FILTER(?a < ?s) }""",
    """SELECT ?x ?a ?w WHERE {
        ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . ?x <http://ex.org/name> ?n
        OPTIONAL { ?x <http://ex.org/weight> ?w }
        FILTER(?a < ?s) }""",
    """SELECT ?x ?a ?s WHERE {
        ?x <http://ex.org/age> ?a . ?x <http://ex.org/name> ?n
        OPTIONAL { ?x <http://ex.org/score> ?s }
        FILTER(!bound(?s) || ?a < ?s) }""",
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . ?x <http://ex.org/name> ?n
        MINUS { ?x <http://ex.org/weight> ?w }
        FILTER(?a < ?s) }""",
]

#: The string shapes vortex-rdf evaluates over candidate codes:
#: lowered and upper-cased texts, STR() over IRIs, blank nodes and typed
#: literals, tagged constants (an upper-case tag never matches a stored one),
#: and REGEX over texts with newlines.
QUERIES += [
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(regex(lcase(?n), "^bo")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(contains(ucase(str(?n)), "OB")) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(strstarts(?n, "B"@en-us)) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(contains(?n, "o"@EN)) }',
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(strends(lcase(?n), "b")) }',
    'SELECT ?x ?n WHERE { ?x <http://ex.org/note> ?n FILTER(regex(?n, "k$")) }',
    'SELECT ?x ?n WHERE { ?x <http://ex.org/note> ?n FILTER(regex(?n, "^line$", "m")) }',
    'SELECT ?x ?n WHERE { ?x <http://ex.org/note> ?n FILTER(regex(str(?n), "\\\\w+", "i")) }',
    'SELECT ?x ?n WHERE { ?x <http://ex.org/note> ?n FILTER(contains(lcase(?n), "ün")) }',
    'SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(strstarts(str(?a), "4")) }',
    'SELECT ?s WHERE { ?s ?p ?o FILTER(regex(str(?s), "^b")) }',
    'SELECT ?s ?o WHERE { ?s ?p ?o FILTER(strstarts(str(?o), "http://ex.org/c")) }',
]

#: IRI equality as a keep: =, sameTerm and IN over IRIs only, constants the
#: dictionary lacks, the empty list, the predicate position.
QUERIES += [
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?o IN (<http://ex.org/carol>, <http://ex.org/bob>)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(sameTerm(?o, <http://ex.org/carol>)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(<http://ex.org/carol> = ?o) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?o IN (<http://ex.org/nobody>)) }",
    "SELECT ?s WHERE { ?s ?p ?o FILTER(?o IN ()) }",
    "SELECT ?s ?p WHERE { ?s ?p ?o FILTER(?p IN (<http://ex.org/knows>, <http://ex.org/likes>)) }",
    "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER(?o = <http://ex.org/carol>) }",
    "ASK { ?s ?p ?o FILTER(?o = <http://ex.org/nobody>) }",
    """SELECT ?x ?n WHERE { ?x <http://ex.org/knows> ?y . ?y <http://ex.org/name> ?n
        FILTER(?y IN (<http://ex.org/bob>, <http://ex.org/carol>)) }""",
]

#: Shapes that name a graph. rdflib evaluates the `Graph` node itself — a
#: bound name scopes the block below, an unbound one fans out over the
#: store's graphs — so what is under test here is that every pushed-down
#: block sees exactly the graph rdflib made active, and no other.
GRAPH_QUERIES = [
    # a bound graph, an absent one, and the default graph a `GRAPH` never names
    "SELECT ?s ?o WHERE { GRAPH <http://ex.org/g/1> { ?s <http://ex.org/name> ?o } }",
    "SELECT * WHERE { GRAPH <http://ex.org/g/1> { ?s ?p ?o } }",
    "SELECT * WHERE { GRAPH <http://ex.org/absent> { ?s ?p ?o } }",
    "ASK { GRAPH <http://ex.org/g/2> { ?s <http://ex.org/name> ?o } }",
    "ASK { GRAPH <http://ex.org/absent> { ?s ?p ?o } }",
    'ASK { GRAPH <http://ex.org/g/1> { <http://ex.org/alice> <http://ex.org/name> "Alice" } }',
    # a graph variable: one interception per graph, the default graph excluded
    "SELECT ?g ?s ?o WHERE { GRAPH ?g { ?s <http://ex.org/name> ?o } }",
    "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
    "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g",
    "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <http://ex.org/g/2> { ?s ?p ?o } }",
    "SELECT ?g ?s WHERE { GRAPH ?g { ?s <http://ex.org/name> ?o } } ORDER BY ?g ?s LIMIT 5",
    # joins, OPTIONAL, MINUS and FILTER inside one graph
    """SELECT ?s ?n ?k WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n . ?s <http://ex.org/knows> ?k } }""",
    """SELECT ?s ?n ?a WHERE { GRAPH <http://ex.org/g/3> {
        ?s <http://ex.org/name> ?n OPTIONAL { ?s <http://ex.org/age> ?a } } }""",
    """SELECT ?s WHERE { GRAPH <http://ex.org/g/3> {
        ?s <http://ex.org/name> ?n MINUS { ?s <http://ex.org/knows> ?k } } }""",
    """SELECT ?s ?n WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n FILTER(isIRI(?s)) } }""",
    """SELECT ?s ?a WHERE { GRAPH <http://ex.org/g/1> {
        ?s <http://ex.org/age> ?a
        FILTER(datatype(?a) = <http://www.w3.org/2001/XMLSchema#integer>) } }""",
    """SELECT ?s ?n WHERE { GRAPH <http://ex.org/g/3> {
        VALUES ?s { <http://ex.org/alice> <http://ex.org/bob> } ?s <http://ex.org/name> ?n } }""",
    # a block spanning the union default graph and a named one
    """SELECT ?s ?n ?k WHERE {
        ?s <http://ex.org/name> ?n .
        GRAPH <http://ex.org/g/1> { ?s <http://ex.org/knows> ?k } }""",
    """SELECT ?s ?n WHERE {
        ?s <http://ex.org/name> ?n
        FILTER NOT EXISTS { GRAPH <http://ex.org/g/1> { ?s <http://ex.org/knows> ?k } } }""",
    # two graphs joined through the same variable
    """SELECT ?s ?n ?k WHERE {
        GRAPH <http://ex.org/g/1> { ?s <http://ex.org/name> ?n }
        GRAPH <http://ex.org/g/2> { ?s <http://ex.org/knows> ?k } }""",
    # --- the graph as a column: shapes where ?g is an ordinary variable ---
    # a join inside one graph variable: both patterns must land in the *same*
    # graph, which is the join on ?g
    """SELECT ?g ?s ?n ?k WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n . ?s <http://ex.org/knows> ?k } }""",
    # two graph variables, joined on the subject
    """SELECT ?g ?h ?s WHERE {
        GRAPH ?g { ?s <http://ex.org/name> ?n }
        GRAPH ?h { ?s <http://ex.org/knows> ?k } }""",
    # the union default graph joined to a graph variable
    """SELECT ?g ?s ?k WHERE {
        ?s <http://ex.org/name> ?n .
        GRAPH ?g { ?s <http://ex.org/knows> ?k } }""",
    # OPTIONAL and MINUS under a graph variable
    """SELECT ?g ?s ?n ?a WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n OPTIONAL { ?s <http://ex.org/age> ?a } } }""",
    """SELECT ?g ?s ?n WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n MINUS { ?s <http://ex.org/knows> ?k } } }""",
    # an OPTIONAL whose *whole* right side is a graph: ?g may be unbound
    """SELECT ?s ?g ?k WHERE {
        ?s <http://ex.org/name> ?n
        OPTIONAL { GRAPH ?g { ?s <http://ex.org/knows> ?k } } }""",
    # FILTER on the graph variable itself
    "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } FILTER(isIRI(?g)) }",
    "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } FILTER(?g = <http://ex.org/g/2>) }",
    "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } FILTER(isBLANK(?g)) }",
    # the graph name repeated in another position of the same pattern
    "SELECT ?g ?p ?o WHERE { GRAPH ?g { ?g ?p ?o } }",
    # VALUES over graph names, including one the file does not hold
    """SELECT ?g ?s WHERE {
        VALUES ?g { <http://ex.org/g/1> <http://ex.org/absent> }
        GRAPH ?g { ?s <http://ex.org/name> ?o } }""",
    # heads over the graph column
    "SELECT ?g ?s WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g ?s LIMIT 7",
    "SELECT (COUNT(DISTINCT ?g) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }",
    "SELECT ?g (COUNT(DISTINCT ?s) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g",
    "ASK { GRAPH ?g { ?s <http://ex.org/knows> ?o } }",
    # nested GRAPH: the inner one wins
    """SELECT ?g ?h ?s WHERE {
        GRAPH ?g { GRAPH <http://ex.org/g/2> { ?s <http://ex.org/name> ?n } }
        GRAPH ?h { ?s <http://ex.org/knows> ?k } }""",
    # (NOT) EXISTS under a graph variable — the body would be answered in the
    # union, so the block goes back to rdflib rather than being pushed down
    """SELECT ?g ?s WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n
        FILTER EXISTS { ?s <http://ex.org/knows> ?k } } }""",
    """SELECT ?g ?s WHERE { GRAPH ?g {
        ?s <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?s <http://ex.org/knows> ?k } } }""",
    # an EXISTS body that names a graph of its own
    """SELECT ?s WHERE {
        ?s <http://ex.org/name> ?n
        FILTER EXISTS { GRAPH ?g { ?s <http://ex.org/knows> ?k } } }""",
    # a FILTER EXISTS hoisted into an OPTIONAL's condition
    """SELECT ?s ?n ?k WHERE {
        ?s <http://ex.org/name> ?n
        OPTIONAL { ?s <http://ex.org/knows> ?k
                   FILTER EXISTS { ?s <http://ex.org/age> ?a } } }""",
    # a two-variable FILTER over a graph block: its fast conjunct reaches the
    # BGP inside, as the single-variable ones do
    """SELECT ?x ?y WHERE {
        GRAPH ?g { ?x <http://ex.org/knows> ?y . ?x <http://ex.org/name> ?n .
                   ?y <http://ex.org/name> ?m }
        FILTER(?n != ?m) }""",
]


#: Quads under `http://ex/` for the probe joins' multiplicity. `s1` holds `q`
#: in g1 and g2 and `p` in g1, g2 and g3, so the union default graph has two
#: quads of its `q` triple and three of its `p` triple; `s2` holds only `p`,
#: `s3` only `q`; `s4` holds both in the default graph and `p` in g1 too.
PROBE_QUADS = """\
<http://ex/s1> <http://ex/q> <http://ex/x> <http://ex/g1> .
<http://ex/s1> <http://ex/p> <http://ex/o1> <http://ex/g1> .
<http://ex/s1> <http://ex/p> <http://ex/o1> <http://ex/g2> .
<http://ex/s1> <http://ex/p> <http://ex/o1> <http://ex/g3> .
<http://ex/s2> <http://ex/p> <http://ex/o1> <http://ex/g1> .
<http://ex/s1> <http://ex/q> <http://ex/x> <http://ex/g2> .
<http://ex/s3> <http://ex/q> <http://ex/x> <http://ex/g1> .
<http://ex/s4> <http://ex/q> <http://ex/x> .
<http://ex/s4> <http://ex/p> <http://ex/o1> .
<http://ex/s4> <http://ex/p> <http://ex/o1> <http://ex/g1> .
"""

EX_PREFIX = "PREFIX : <http://ex/> "

#: Per shape: whether the default graph is the union of the graphs, the query,
#: the rows rdflib answers, the probe the pushdown takes when it probes, and
#: the `limit` of the fully bound counts that probe makes.
PROBE_MULTIPLICITY = {
    # The union default graph is a set of triples: a triple several graphs
    # hold is one solution. `s1` and `s4` have both triples, in several graphs.
    "bgp-join": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x . ?s :p :o1 }",
        2,
        "_probe_join",
        1,
    ),
    "group-join": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { { ?s :q :x } { ?s :p :o1 } }",
        2,
        "_probe_join",
        1,
    ),
    # `s3` has no `p`: its row is padded, once.
    "optional": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x OPTIONAL { ?s :p :o1 } }",
        3,
        "_probe_join",
        1,
    ),
    # The condition rejects `s1`: its row is padded, not repeated.
    "optional-condition": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x OPTIONAL { ?s :p :o1 FILTER(?s != :s1) } }",
        3,
        "_probe_join",
        1,
    ),
    # A fixed graph holds at most one quad of a triple: `s1` and `s4` have the
    # `p` triple in other graphs too, which the probe does not read.
    "graph-variable": (
        True,
        EX_PREFIX + "SELECT ?g ?s WHERE { GRAPH ?g { ?s :q :x . ?s :p :o1 } }",
        2,
        "_probe_join",
        1,
    ),
    "named-graph": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { GRAPH :g2 { ?s :q :x . ?s :p :o1 } }",
        1,
        "_probe_join",
        1,
    ),
    "default-graph": (
        False,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x . ?s :p :o1 }",
        1,
        "_probe_join",
        1,
    ),
    # EXISTS is boolean: the rows of the block, `s1` once.
    "exists": (
        True,
        EX_PREFIX + "SELECT ?s WHERE { ?s :q :x FILTER EXISTS { ?s :p :o1 } }",
        2,
        "_probe_exists",
        1,
    ),
}


#: Data-side spellings, as the store's dictionary holds them (lowercase
#: language tags, minimal escapes); None is an unbound variable.
SPELLINGS = [
    typed("5", "integer"),
    typed("01", "integer"),
    typed("+7", "integer"),
    typed(" 7", "integer"),
    typed("1_0", "integer"),
    typed("1.5", "integer"),
    typed("abc", "integer"),
    typed("0x10", "integer"),
    typed("99999999999999999999", "integer"),
    typed("-3", "integer"),
    typed("1.5", "decimal"),
    typed("5", "decimal"),
    typed("NaN", "double"),
    typed("INF", "double"),
    typed("-INF", "double"),
    typed("1e2", "double"),
    typed("5", "float"),
    typed("7", "byte"),
    typed("300", "byte"),
    typed("5", "int"),
    typed("5", "short"),
    typed("5", "unsignedByte"),
    typed("-1", "nonNegativeInteger"),
    typed("5", "positiveInteger"),
    typed("true", "boolean"),
    typed("True", "boolean"),
    typed("1", "boolean"),
    typed("maybe", "boolean"),
    typed("2020-01-01T00:00:00", "dateTime"),
    typed("2020-01-01", "date"),
    '"5"',
    '"05"',
    '""',
    '"abc"',
    '"Abc"',
    '"abc"@en',
    '"abc"@en-us',
    '"5"@en',
    '"a\\"b"',
    '"a\\nb"',
    '"x"^^<http://ex.org/dt>',
    "<http://ex.org/x>",
    "<http://ex.org/y>",
    "_:b0",
    "<http://www.w3.org/1999/02/22-rdf-syntax-ns#nil>",
    None,
]
