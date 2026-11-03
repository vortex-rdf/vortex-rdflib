"""SPARQL 1.1's answer to a FILTER over given terms, as Oxigraph computes it.

Oxigraph passes the W3C SPARQL test suite, so it stands in for the
specification where the FILTER routes are checked over many terms at once
(the expression x term matrices). It departs from SPARQL 1.1 and XML Schema in
a few documented ways (:func:`departs`); those pairs are skipped there, and
``tests/test_filter_spec.py`` answers each kind of them from the specification
instead.
"""

import functools
import re

import pyoxigraph

XSD = "http://www.w3.org/2001/XMLSchema#"

PREFIXES = (
    f"PREFIX xsd: <{XSD}> PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
    "PREFIX ex: <http://ex.org/> "
)

_STORE = pyoxigraph.Store()


def spec_answer(sparql_expr: str, bindings: dict[str, str | None]) -> bool:
    """Whether ``FILTER(sparql_expr)`` keeps the solution that binds each
    variable of ``bindings`` to the term it spells (N-Triples; ``None`` is
    unbound). A blank node is bound with ``BNODE(label)``, so one label is one
    node, as in the data."""
    named = [var for var, term in bindings.items() if term is None or not term.startswith("_:")]
    clauses = []
    if named:
        row = " ".join("UNDEF" if bindings[var] is None else str(bindings[var]) for var in named)
        clauses.append(f"VALUES ({' '.join('?' + var for var in named)}) {{ ({row}) }}")
    for var, term in bindings.items():
        if term is not None and term.startswith("_:"):
            clauses.append(f'BIND(BNODE("{term[2:]}") AS ?{var})')
    result = _STORE.query(f"{PREFIXES} ASK {{ {' '.join(clauses)} FILTER({sparql_expr}) }}")
    assert isinstance(result, pyoxigraph.QueryBoolean)
    return bool(result)


#: The xsd:integer types narrower than xsd:integer, and the ranges of those
#: that bound it.
_DERIVED = {
    "nonPositiveInteger": (None, 0),
    "negativeInteger": (None, -1),
    "nonNegativeInteger": (0, None),
    "positiveInteger": (1, None),
    "long": (-(2**63), 2**63 - 1),
    "int": (-(2**31), 2**31 - 1),
    "short": (-32768, 32767),
    "byte": (-128, 127),
    "unsignedLong": (0, 2**64 - 1),
    "unsignedInt": (0, 2**32 - 1),
    "unsignedShort": (0, 65535),
    "unsignedByte": (0, 255),
}

_TYPED = re.compile(r'"(.*)"\^\^<' + re.escape(XSD) + r"(\w+)>")


#: The functions that read a term's identity — its lexical form or datatype —
#: rather than its value.
_IDENTITY = re.compile(r"\b(?:sameTerm|datatype|str)\(")


#: A comparison: `=`, `!=`, an ordering, IN.
_COMPARISON = re.compile(r"[=<>]|\bIN\b")

#: The XML Schema types SPARQL 1.1 compares (the rest it leaves open).
_COMPARED = {
    *_DERIVED,
    "integer",
    "decimal",
    "float",
    "double",
    "string",
    "boolean",
    "dateTime",
    "date",
}


def _left_open(spelling: str) -> bool:
    """A literal of an XML Schema type SPARQL 1.1 names no operator for."""
    typed = re.fullmatch(r'".*"\^\^<' + re.escape(XSD) + r"(\w+)>", spelling)
    return typed is not None and typed.group(1) not in _COMPARED


#: An ordering between two variables.
_ORDERING = re.compile(r"\?\w+\s*[<>]=?\s*\?\w+")


@functools.cache
def held_as(spelling: str) -> str:
    """The term Oxigraph holds for one it is given: it rewrites a literal of a
    datatype it knows to the canonical lexical form ("1"^^xsd:boolean is
    "true", "01"^^xsd:integer is "1") and a type derived from xsd:integer to
    xsd:integer."""
    if spelling.startswith("_:"):
        return spelling
    solutions = _STORE.query(f"SELECT ?t WHERE {{ VALUES ?t {{ {spelling} }} }}")
    assert isinstance(solutions, pyoxigraph.QuerySolutions)
    [solution] = solutions
    return str(solution["t"])


def _integer(lexical: str) -> int | None:
    return int(lexical) if re.fullmatch(r"[+-]?[0-9]+", lexical) else None


def _tag(spelling: str | None) -> str | None:
    if spelling is None or not spelling.startswith('"'):
        return None
    _, _, tail = spelling.rpartition('"')
    return tail[1:].lower() if tail.startswith("@") else None


def departs(sparql_expr: str, *spellings: str | None) -> str | None:
    """Why Oxigraph's answer to ``sparql_expr`` over terms spelled
    ``spellings`` is not SPARQL 1.1's, or ``None`` when it is."""
    for spelling in spellings:
        if spelling is None:
            continue
        typed = _TYPED.fullmatch(spelling)
        lexical, datatype = typed.groups() if typed else (None, None)
        number = None if lexical is None else _integer(lexical)
        if datatype in _DERIVED and number is not None:
            lo, hi = _DERIVED[datatype]
            if (lo is not None and number < lo) or (hi is not None and number > hi):
                return "Oxigraph ignores a derived integer type's range (ill-typed in XSD)"
        if datatype is not None and number is not None and abs(number) >= 2**63:
            return "Oxigraph holds an integer in 64 bits; xsd:integer is unbounded"
        if _IDENTITY.search(sparql_expr) and held_as(spelling) != spelling:
            return "Oxigraph rewrites the term (canonical form; xsd:integer for derived types)"
        if _left_open(spelling) and _COMPARISON.search(sparql_expr):
            return "rdflib compares an XSD type SPARQL 1.1 leaves open (docs/pushdown.md)"
        if lexical == "NaN" and "!(" in sparql_expr:
            return "Oxigraph makes an ordering with NaN an error; XPath makes it false"
        if _tag(spelling) is not None and sparql_expr == "?v":
            return "Oxigraph errs on a language-tagged string's effective boolean value"
    tags = [_tag(spelling) for spelling in spellings]
    if len(tags) > 1 and tags[0] is not None and len(set(tags)) == 1:
        if _ORDERING.search(sparql_expr):
            return "Oxigraph orders language-tagged strings of one tag; SPARQL 1.1 does not"
    return None
