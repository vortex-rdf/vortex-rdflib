"""The answers SPARQL 1.1 §17 gives the FILTERs the pushdown evaluates itself
(docs/pushdown.md, "SPARQL semantics"): where rdflib's evaluator differs from
the specification, the pushdown follows the specification.

Two tables: ``SPEC_CASES``, written here, and the FILTER tests of the W3C
SPARQL test suite (``data/w3c_filter_cases.json``; see ``data/W3C-NOTICE.md``).
Every answer is checked with the pushdown on; terms are compared as vortex-rdf
keeps them (language tags in lower case, ``xsd:string`` as a simple literal,
blank nodes as ``_:b``).
"""

import json
import pathlib
from collections import Counter
from dataclasses import dataclass

import pytest
from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.util import from_n3
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore

EX = "http://ex.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIXES = f"PREFIX ex: <{EX}> PREFIX xsd: <{XSD}> "
XSD_STRING = URIRef(XSD + "string")


@dataclass(frozen=True)
class SpecCase:
    """``query`` over ``data`` (N-Triples or N-Quads) answers ``expected``: its
    rows, each a tuple of term spellings in projection order, in any order."""

    name: str
    data: str
    query: str
    expected: list[tuple]


def _integers(*values) -> str:
    return "".join(f'<{EX}s{v}> <{EX}p> "{v}"^^<{XSD}integer> .\n' for v in values)


#: s0..s9 with ex:p their number, and ex:flag false/true/false/true on s0/s3/s6/s9.
NUMBERS = _integers(*range(10)) + "".join(
    f'<{EX}s{i}> <{EX}flag> "{flag}"^^<{XSD}boolean> .\n'
    for i, flag in ((0, "false"), (3, "true"), (6, "false"), (9, "true"))
)

#: s0 = 0, s7 = 7 and b = "300"^^xsd:byte, ill-typed (a byte is at most 127).
ILL_TYPED = _integers(0, 7) + f'<{EX}b> <{EX}p> "300"^^<{XSD}byte> .\n'

#: One subject per effective-boolean-value case (SPARQL 1.1 §17.2.2).
EBV = "".join(
    f"<{EX}{name}> <{EX}q> {term} .\n"
    for name, term in (
        ("nan", f'"NaN"^^<{XSD}double>'),
        ("zero", f'"0"^^<{XSD}integer>'),
        ("ill-int", f'"xyz"^^<{XSD}integer>'),
        ("upper-true", f'"TRUE"^^<{XSD}boolean>'),
        ("one-bool", f'"1"^^<{XSD}boolean>'),
        ("unknown", f'"a"^^<{EX}t>'),
        ("date", f'"2008-01-01"^^<{XSD}date>'),
        ("lang", '"abc"@en'),
        ("empty", '""'),
        ("iri", f"<{EX}i>"),
    )
)


def _terms(**terms) -> str:
    """One ``ex:p`` statement per subject name and object spelling."""
    return "".join(f"<{EX}{name}> <{EX}p> {term} .\n" for name, term in terms.items())


#: One subject per numeric type, and one per operand arithmetic refuses.
ARITH = _terms(
    i5=f'"5"^^<{XSD}integer>',
    d5=f'"5"^^<{XSD}decimal>',
    f5=f'"5"^^<{XSD}float>',
    e5=f'"5"^^<{XSD}double>',
    b7=f'"7"^^<{XSD}byte>',
    nan=f'"NaN"^^<{XSD}double>',
    space=f'" 7"^^<{XSD}integer>',
    big=f'"300"^^<{XSD}byte>',
    str='"5"',
    date=f'"2020-01-01"^^<{XSD}date>',
    iri=f"<{EX}x>",
)

#: xsd:float values whose single precision shows: 0.1 is not the double 0.1,
#: 16777217 rounds to 16777216, and 3.4028235E38 rounds to the largest float.
FLOATS = _terms(
    f01=f'"0.1"^^<{XSD}float>',
    e01=f'"0.1"^^<{XSD}double>',
    f2p24=f'"16777217"^^<{XSD}float>',
    max=f'"3.4028235E38"^^<{XSD}float>',
    # Just above the midpoint 1 + 2**-24 of the floats 1 and 1 + 2**-23: its
    # nearest double is that midpoint, which rounds to even (1); the float
    # nearest the number itself is 1 + 2**-23.
    above=f'"1.000000059604644775390625000001"^^<{XSD}float>',
)


def _subjects(*names) -> list[tuple]:
    return [(f"<{EX}{name}>",) for name in names]


def _s(*numbers) -> list[tuple]:
    return _subjects(*(f"s{n}" for n in numbers))


SPEC_CASES: list[SpecCase] = [
    # §17.2: T || E = T, F && E = F, E && F = F; ! of an error is an error.
    SpecCase(
        "or-unbound",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o > 5 || ?nope) }",
        _s(6, 7, 8, 9),
    ),
    SpecCase(
        "optional-or-bound",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o OPTIONAL { ?s ex:flag ?f } FILTER(!bound(?f) || ?f) }",
        _s(1, 2, 3, 4, 5, 7, 8, 9),
    ),
    SpecCase(
        "not-and-unbound",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(!(?o > 5 && ?nope)) }",
        _s(0, 1, 2, 3, 4, 5),
    ),
    SpecCase(
        "and-error-first",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(!(?nope && ?o > 5)) }",
        _s(0, 1, 2, 3, 4, 5),
    ),
    # A comparison's value is an xsd:boolean term (§17.3).
    SpecCase(
        "sameTerm-bool",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(sameTerm(?o < 5, true)) }",
        _s(0, 1, 2, 3, 4),
    ),
    SpecCase(
        "in-bool",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER((?o < 5) IN (true)) }",
        _s(0, 1, 2, 3, 4),
    ),
    SpecCase(
        "eq-bool",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER((?o < 5) = true) }",
        _s(0, 1, 2, 3, 4),
    ),
    SpecCase(
        "builtins-of-bool",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(datatype(?o < 5) = xsd:boolean "
        '&& str(?o < 5) = "true" && isLiteral(?o < 5) && lang(?o < 5) = "" '
        "&& !isNumeric(?o < 5) && !isIRI(?o < 5)) }",
        _s(0, 1, 2, 3, 4),
    ),
    # IN is `=` over the list (§17.4.1.9): value equality, and an error when
    # nothing matches and some comparison errs; NOT IN is its negation.
    SpecCase(
        "in-numeric-value",
        NUMBERS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o IN (1.0)) }",
        _s(1),
    ),
    SpecCase(
        "not-in-unknown-type",
        _integers(0, 1) + f'<{EX}u> <{EX}p> "a"^^<{EX}t> .\n',
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o NOT IN (1)) }",
        _s(0),
    ),
    # Ill-typed literals have no value: comparing one is a type error (§17.3).
    SpecCase(
        "values-illtyped",
        NUMBERS,
        'SELECT ?n WHERE { VALUES ?n { "300"^^xsd:byte UNDEF } FILTER(?n > 5) }',
        [],
    ),
    SpecCase(
        "values-illtyped-join",
        NUMBERS,
        'SELECT ?n ?s WHERE { VALUES ?n { "300"^^xsd:byte } ?s ex:p ?o FILTER(?n > ?o) }',
        [],
    ),
    SpecCase(
        "values-illtyped-not",
        NUMBERS,
        'SELECT ?n WHERE { VALUES ?n { "300"^^xsd:byte } FILTER(!(?n > 5)) }',
        [],
    ),
    SpecCase(
        "illtyped-data-not-gt",
        ILL_TYPED,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(!(?o > 5)) }",
        _s(0),
    ),
    SpecCase(
        "illtyped-data-eq",
        ILL_TYPED,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = 300) }",
        [],
    ),
    SpecCase(
        "illtyped-data-ne",
        ILL_TYPED,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o != 300) }",
        _s(0, 7),
    ),
    SpecCase(
        "illtyped-data-in",
        ILL_TYPED,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o IN (300)) }",
        [],
    ),
    SpecCase(
        "illtyped-data-sameterm",
        ILL_TYPED,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(sameTerm(?o, "300"^^xsd:byte)) }',
        _subjects("b"),
    ),
    SpecCase(
        "isnumeric-illtyped",
        ILL_TYPED + f'<{EX}x> <{EX}p> "xyz"^^<{XSD}integer> .\n',
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(isNumeric(?o)) }",
        _s(0, 7),
    ),
    # Effective boolean value (§17.2.2): ill-typed and NaN numerics and
    # ill-typed booleans are false; unknown datatypes, dates and IRIs err.
    SpecCase(
        "ebv",
        EBV,
        "SELECT ?s WHERE { ?s ex:q ?v FILTER(?v) }",
        _subjects("lang", "one-bool"),
    ),
    SpecCase(
        "ebv-not",
        EBV,
        "SELECT ?s WHERE { ?s ex:q ?v FILTER(!?v) }",
        _subjects("nan", "zero", "ill-int", "upper-true", "empty"),
    ),
    # Terms are compared as written: "01"^^xsd:integer is not the term
    # "1"^^xsd:integer, and its STR() is "01" (§17.4.1.8, §17.4.2.5).
    SpecCase(
        "sameterm-noncanonical",
        _integers(1) + f'<{EX}s01> <{EX}p> "01"^^<{XSD}integer> .\n',
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(sameTerm(?o, 1)) }",
        _s(1),
    ),
    SpecCase(
        "str-noncanonical",
        _integers(1) + f'<{EX}s01> <{EX}p> "01"^^<{XSD}integer> .\n',
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(STR(?o) = "01") }',
        _s("01"),
    ),
    # Ordering is defined for numerics, strings, booleans and dateTimes, each
    # against its own kind; anything else is a type error (§17.3).
    SpecCase(
        "string-gt-number",
        _integers(7) + f'<{EX}str> <{EX}p> "6" .\n<{EX}lang> <{EX}p> "6"@en .\n',
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o > 5) }",
        _s(7),
    ),
    SpecCase(
        "number-lt-string",
        NUMBERS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o < "abc") }',
        [],
    ),
    SpecCase(
        "lang-lt",
        f'<{EX}a> <{EX}p> "a"@en .\n',
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o < "z"@en) }',
        [],
    ),
    SpecCase(
        "constant-comparison",
        NUMBERS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER("5" > 3) }',
        [],
    ),
    # A typed constant is read as written, so an ill-typed one has no value
    # either (rdflib's parser leaves it unflagged and compares the number).
    SpecCase(
        "illtyped-constant",
        NUMBERS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o < "300"^^xsd:byte) }',
        [],
    ),
    # xsd:integer is unbounded; a type derived from it is its own datatype, so
    # "5"^^xsd:int is not the term 5 (XML Schema §3.3.13; §17.4.1.8).
    SpecCase(
        "integer-unbounded",
        _terms(big=f'"99999999999999999999"^^<{XSD}integer>'),
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o > 9223372036854775807) }",
        _subjects("big"),
    ),
    SpecCase(
        "derived-datatype",
        _terms(int=f'"5"^^<{XSD}int>', integer=f'"5"^^<{XSD}integer>'),
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(datatype(?o) = xsd:int) }",
        _subjects("int"),
    ),
    SpecCase(
        "derived-sameterm",
        _terms(int=f'"5"^^<{XSD}int>', integer=f'"5"^^<{XSD}integer>'),
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(sameTerm(?o, 5)) }",
        _subjects("integer"),
    ),
    # NaN compares false, not in error, so `!` turns it true (XPath
    # op:numeric-less-than).
    SpecCase(
        "nan-not-lt",
        EBV,
        "SELECT ?s WHERE { ?s ex:q ?v FILTER(!(?v < 5)) }",
        _subjects("nan"),
    ),
    # An invalid pattern is an error (§17.4.3.14, XPath FORX0002): `!` of it
    # is an error too, so neither keeps a row.
    SpecCase(
        "regex-invalid",
        f'<{EX}a> <{EX}p> "abc" .\n',
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(regex(?o, "(") || !regex(?o, "a{2,1}")) }',
        [],
    ),
    # `+` and `-` take numbers (§17.3): XPath promotes both operands to the
    # wider of integer < decimal < float < double (F&O §B.1), and anything
    # else — an ill-typed number, a string, a date, an IRI — is a type error.
    SpecCase(
        "arith-promotion",
        ARITH,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o + 1 IN (6, 8)) }",
        _subjects("i5", "d5", "f5", "e5", "b7"),
    ),
    SpecCase(
        "arith-type",
        ARITH,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(datatype(?o + 1.0) = xsd:decimal) }",
        _subjects("i5", "d5", "b7"),
    ),
    SpecCase(
        "arith-operands",
        ARITH,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o + 1 > 3) }",
        _subjects("i5", "d5", "f5", "e5", "b7"),
    ),
    SpecCase(
        "arith-ebv",
        ARITH,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o - 5) }",
        _subjects("b7"),
    ),
    # A decimal or an integer meets an xsd:float as the nearest float, and an
    # xsd:float meets an xsd:double as the double it is (F&O §B.1).
    SpecCase(
        "float-decimal-eq",
        FLOATS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = 0.1) }",
        _subjects("f01", "e01"),
    ),
    SpecCase(
        "float-double-eq",
        FLOATS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = "0.1"^^xsd:double) }',
        _subjects("e01"),
    ),
    SpecCase(
        "float-integer-eq",
        FLOATS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = 16777217) }",
        _subjects("f2p24"),
    ),
    SpecCase(
        "float-rounding",
        FLOATS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o > 1 && ?o < 2) }",
        _subjects("above"),
    ),
    SpecCase(
        "float-rounding-promoted",
        FLOATS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = "1.00000011920928955078125"^^xsd:float) }',
        _subjects("above"),
    ),
    SpecCase(
        "decimal-rounding-promoted",
        FLOATS,
        "SELECT ?s WHERE { ?s ex:p ?o FILTER(?o = 1.000000059604644775390625000001) }",
        _subjects("above"),
    ),
    SpecCase(
        "float-max",
        FLOATS,
        'SELECT ?s WHERE { ?s ex:p ?o FILTER(?o > 1e38 && ?o < "INF"^^xsd:double) }',
        _subjects("max"),
    ),
]


def _spelling(term) -> str | None:
    if term is None:
        return None
    if isinstance(term, BNode):
        return "_:b"
    if isinstance(term, Literal):
        if term.language:
            return Literal(str(term), lang=term.language.lower()).n3()
        if term.datatype == XSD_STRING:
            return Literal(str(term)).n3()
    return term.n3()


def _as_returned(spelling: str | None) -> str | None:
    """An expected term's spelling as the store returns that term: built by
    rdflib, which normalizes a literal's lexical form (``"01"^^xsd:integer``
    comes back as ``"1"``)."""
    if spelling is None or spelling == "_:b":
        return spelling
    return _spelling(from_n3(spelling))


class _Stores:
    """One dictionary-layout store per distinct dataset, built on demand."""

    def __init__(self, directory: pathlib.Path):
        self.directory = directory
        self.built: dict[str, VortexRdflibStore] = {}

    def __call__(self, data: str) -> VortexRdflibStore:
        store = self.built.get(data)
        if store is None:
            n = len(self.built)
            source = self.directory / f"data-{n}.nq"
            source.write_text(data, encoding="utf-8")
            out = self.directory / f"data-{n}.vortex"
            serialize_rdf(str(source), str(out), layout="dictionary", format="nquads")
            store = self.built[data] = VortexRdflibStore(str(out))
        return store


@pytest.fixture(scope="module")
def stores(tmp_path_factory) -> _Stores:
    return _Stores(tmp_path_factory.mktemp("filter-spec"))


@pytest.mark.parametrize("case", SPEC_CASES, ids=lambda case: case.name)
def test_filter_follows_the_spec(stores, case):
    dataset = Dataset(store=stores(case.data))
    result = dataset.query(PREFIXES + case.query)
    rows = [tuple(_spelling(row[v]) for v in result.vars) for row in result]
    assert sorted(rows, key=str) == sorted(case.expected, key=str)


W3C_CASES = json.loads(
    (pathlib.Path(__file__).parent / "data" / "w3c_filter_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", W3C_CASES, ids=lambda case: case["name"])
def test_w3c_filter_case(stores, case):
    dataset = Dataset(store=stores(case["data"]))
    result = dataset.query(case["query"], base=case["base"])
    expected = case["expected"]
    if expected["type"] == "ASK":
        assert result.askAnswer == expected["answer"]
        return
    got = Counter(
        tuple(sorted((str(v), _spelling(row[v])) for v in result.vars if row[v] is not None))
        for row in result
    )
    want = Counter()
    for row, n in expected["rows"]:
        want[tuple(sorted((var, _as_returned(spelling)) for var, spelling in row))] += n
    assert got == want
