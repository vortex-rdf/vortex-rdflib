"""Where rdflib's evaluator breaks SPARQL 1.1 on a corpus query
(:mod:`tests.corpus`) over the fixture (:data:`RDFLIB_DIVERGES`), and the
specification's answer (:func:`spec_corrected`)."""

from dataclasses import dataclass

from tests.support import row_key, typed

from vortex_rdflib import VortexRdflibStore


@dataclass(frozen=True)
class Divergence:
    """Where rdflib's evaluator breaks SPARQL 1.1 on a corpus query over the
    fixture: the rows the specification drops from rdflib's answer and the
    rows it adds (each a tuple of N-Triples spellings, ``""`` for unbound),
    or the ASK answer; and why (docs/pushdown.md, "SPARQL semantics")."""

    why: str
    drop: tuple = ()
    add: tuple = ()
    answer: bool | None = None


_ERIN, _DAVE, _GINA, _BOB = (f"<http://ex.org/{name}>" for name in ("erin", "dave", "gina", "bob"))


_ABC = typed("abc", "integer")


_NAN = typed("NaN", "double")


_BYTE7 = typed("7", "byte")


_QUOTE = '"q\\"uote"'


_ILL_TYPED = (
    'erin\'s age "abc"^^xsd:integer is ill-typed: it has no value, so comparing it '
    "is a type error (§17.3)"
)


_NAN_ORDER = "gina's score is NaN, less than nothing and greater than nothing (XPath)"


_AS_WRITTEN = (
    'dave\'s age is the term "042"^^xsd:integer as written: not the term 42, and its STR '
    'is "042" (§17.4.1.8, §17.4.2.5); rdflib normalizes the lexical form as it builds the '
    "term, which is also why the row shows 42"
)


#: The corpus queries rdflib answers against the specification, keyed by their
#: text with its whitespace collapsed.
RDFLIB_DIVERGES: dict[str, Divergence] = {
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a >= 42) }": Divergence(
        _ILL_TYPED, drop=((_ERIN, _ABC),)
    ),
    "SELECT ?x ?v WHERE { ?x <http://ex.org/score> ?v FILTER(?v < 100) }": Divergence(
        _NAN_ORDER, drop=((_GINA, _NAN),)
    ),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a IN (42, 7)) }": Divergence(
        'IN compares by value (§17.4.1.9): gina\'s "7"^^xsd:byte is 7',
        add=((_GINA, _BYTE7),),
    ),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a NOT IN (42)) }": Divergence(
        _ILL_TYPED, drop=((_ERIN, _ABC),)
    ),
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(sameTerm(?a, 42)) }": Divergence(
        _AS_WRITTEN, drop=((_DAVE,),)
    ),
    "SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(isNumeric(?a)) }": Divergence(
        "an ill-typed literal is no number (§17.4.2.4)", drop=((_ERIN,),)
    ),
    'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(str(?a) = "42") }': Divergence(
        _AS_WRITTEN, drop=((_DAVE,),)
    ),
    "SELECT ?a ?b WHERE { ?x <http://ex.org/age> ?a . ?y <http://ex.org/age> ?b "
    "FILTER(?a < ?b) }": Divergence(_ILL_TYPED, drop=((_BYTE7, _ABC),)),
    "SELECT ?x ?n WHERE { ?x <http://ex.org/name> ?n . ?x <http://ex.org/age> ?a "
    "FILTER(?a > 10) }": Divergence(_ILL_TYPED, drop=((_ERIN, _QUOTE),)),
    "SELECT ?x WHERE { ?x <http://ex.org/code> ?c FILTER(?c < 5) }": Divergence(
        'erin\'s code "x"^^<http://ex.org/dt> has an unknown datatype, which does not '
        "order (§17.3)",
        drop=((_ERIN,),),
    ),
    "ASK { ?x <http://ex.org/age> ?a FILTER(?a > 100) }": Divergence(_ILL_TYPED, answer=False),
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) }": Divergence(
        _ILL_TYPED, drop=((_ABC,),)
    ),
    "SELECT (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) }": Divergence(
        _ILL_TYPED, drop=((typed("4", "integer"),),), add=((typed("3", "integer"),),)
    ),
    "SELECT ?x ?n ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a "
    "FILTER(?a > 10) } }": Divergence(
        _ILL_TYPED, drop=((_ERIN, _QUOTE, _ABC),), add=((_ERIN, _QUOTE, ""),)
    ),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } "
    "FILTER(?a > 10) }": Divergence(_ILL_TYPED, drop=((_ERIN, _ABC),)),
    "SELECT ?x WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } "
    "FILTER(?a > 10 || !bound(?a)) }": Divergence(
        _ILL_TYPED + "; an error || false is an error (§17.2)", drop=((_ERIN,),)
    ),
    "SELECT ?x ?n ?a WHERE { { ?x <http://ex.org/name> ?n } { ?x <http://ex.org/age> ?a "
    "FILTER(?a > 10) } }": Divergence(_ILL_TYPED, drop=((_ERIN, _QUOTE, _ABC),)),
    "SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER EXISTS { ?x <http://ex.org/age> ?a "
    "FILTER(?a > 10) } }": Divergence(_ILL_TYPED, drop=((_ERIN,),)),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) } ORDER BY DESC(?a)": (
        Divergence(_ILL_TYPED, drop=((_ERIN, _ABC),))
    ),
    "SELECT ?x ?a ?s WHERE { { ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . "
    "?x <http://ex.org/name> ?n } { ?x <http://ex.org/name> ?m } FILTER(?a < ?s) }": Divergence(
        _NAN_ORDER, drop=((_GINA, _BYTE7, _NAN),)
    ),
    "SELECT ?x ?a ?w WHERE { ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . "
    "?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/weight> ?w } FILTER(?a < ?s) }": (
        Divergence(_NAN_ORDER, drop=((_GINA, _BYTE7, ""),))
    ),
    "SELECT ?x ?a ?s WHERE { ?x <http://ex.org/age> ?a . ?x <http://ex.org/name> ?n "
    "OPTIONAL { ?x <http://ex.org/score> ?s } FILTER(!bound(?s) || ?a < ?s) }": Divergence(
        _NAN_ORDER, drop=((_GINA, _BYTE7, _NAN),)
    ),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/score> ?s . ?x <http://ex.org/age> ?a . "
    "?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/weight> ?w } FILTER(?a < ?s) }": (
        Divergence(_NAN_ORDER, drop=((_GINA, _BYTE7),))
    ),
    'SELECT ?x WHERE { ?x <http://ex.org/name> ?n FILTER(contains(?n, "o"@EN)) }': Divergence(
        'language tags compare case-insensitively (RDF 1.1 §3.3): "o"@EN and "Bob"@en '
        "share a tag, so CONTAINS takes them (§17.4.3.1.1)",
        add=((_BOB,), (_GINA,)),
    ),
    'SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(strstarts(str(?a), "4")) }': (
        Divergence(_AS_WRITTEN, drop=((_DAVE, typed("42", "integer")),))
    ),
    'SELECT ?s WHERE { ?s ?p ?o FILTER(regex(str(?s), "^b")) }': Divergence(
        "STR of a blank node is a type error (§17.4.2.5)", drop=(("_:b0",), ("_:b0",))
    ),
    "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?a > 0) } ORDER BY DESC(?a) ?x": (
        Divergence(_ILL_TYPED, drop=((_ERIN, _ABC),))
    ),
}


def spec_corrected(sparql: str, answer):
    """The SPARQL 1.1 answer to a corpus query over the fixture, from
    rdflib's evaluator's ``answer`` (as :func:`~tests.support.run` or
    :func:`~tests.support.run_ordered` gives it): unchanged unless rdflib
    breaks the specification on the query (:data:`RDFLIB_DIVERGES`). Every
    row the table drops must be in rdflib's answer."""
    divergence = RDFLIB_DIVERGES.get(" ".join(sparql.split()))
    if divergence is None:
        return answer
    if divergence.answer is not None:
        assert answer is (not divergence.answer), sparql
        return divergence.answer
    rows = list(answer)
    for spellings in divergence.drop:
        keys = [row_key(row) for row in rows]
        assert spellings in keys, (sparql, spellings)
        del rows[keys.index(spellings)]
    if divergence.add:
        rows += [
            tuple(None if s == "" else VortexRdflibStore._from_n3_safe(s) for s in spellings)
            for spellings in divergence.add
        ]
        rows.sort(key=row_key)
    return rows
