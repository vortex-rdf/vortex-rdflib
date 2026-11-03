"""The native term predicates (``TermDict.filter_codes``) must give SPARQL
1.1's answer wherever they give a definite one: a FILTER conjunct mapped to
one (``filters.native.native_shape``) is answered by vortex-rdf over the codes
a query produced (``native.evaluate``), so a disagreement would be a wrong
answer, not a slow one. The answers are Oxigraph's (:mod:`tests.oracle`); the
matrix of spellings is the fast route's (``test_filters``) plus the shapes the
native value model treats specially: decimals against doubles, integers past
2^53, 64-bit bounds, language subtags."""

from types import SimpleNamespace

import pytest
from rdflib.term import Variable
from tests.corpus import SPELLINGS
from tests.oracle import departs, spec_answer
from tests.support import CTX, RecordingTermDict, W, filter_expr, kind_ranges, typed
from vortex_rdf import U64Column, serialize_rdf

from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown, trace
from vortex_rdflib.filters import native
from vortex_rdflib.filters.native import FilterCodes
from vortex_rdflib.terms import BLANK, IRI, LITERAL, parse_spelling

V = Variable("v")
XSD = "http://www.w3.org/2001/XMLSchema#"

EXTRA_SPELLINGS = [
    typed("5.0", "double"),
    typed("4.25", "double"),
    typed("4.5", "decimal"),
    typed("-0", "integer"),
    typed("00005", "integer"),
    typed("5.", "decimal"),
    typed(".5", "decimal"),
    typed("1E1", "double"),
    typed("9007199254740993", "integer"),
    typed("9007199254740993", "double"),
    typed("123456789012345678901234567890123456789012", "integer"),
    typed("0.1", "float"),
    typed("1e400", "double"),
    typed("5", "long"),
    typed("9223372036854775808", "long"),
    typed("-9223372036854775809", "long"),
    typed("18446744073709551615", "unsignedLong"),
    typed("18446744073709551616", "unsignedLong"),
    typed("-1", "unsignedLong"),
    typed("-129", "byte"),
    typed("http://ex.org/x", "anyURI"),
    typed("2020", "gYear"),
    '"x"@fr',
    '"x"@en-gb-oed',
    '"x"@de-ch',
    '"http://ex.org/z"',
    '"\\u00E9t\\u00E9"',
    '"tab\\there"',
    "<http://ex.org/x/y>",
    "<urn:x>",
    "_:abc",
    # string-function texts: trailing newlines, case pairs, case folding
    # outside ASCII (ß, ſ, the Kelvin sign), a control character, and typed
    # literals whose rdflib lexical form is not the stored one ("042" -> "42")
    '"line\\n"',
    '"ab\\n"',
    '"Ab\\nc\\n"@en',
    '"école"@fr',
    '"ÉCOLE"',
    '"straße"',
    '"STRASSE"',
    '"ſ"',
    '"K"',
    '"a\\u0001b"',
    '"word in a sentence"',
    '"ABC"@en-gb',
    typed("042", "integer"),
    typed("1.50", "decimal"),
]
ALL_SPELLINGS = [s for s in SPELLINGS + EXTRA_SPELLINGS if s is not None]

#: Expressions the mapping must take, each with the predicate it maps to.
MAPPED: list[tuple[str, tuple | native.IriIn]] = [
    ("datatype(?v) = xsd:integer", ("datatype", XSD + "integer")),
    ("xsd:integer = datatype(?v)", ("datatype", XSD + "integer")),
    ("datatype(?v) = xsd:string", ("datatype", XSD + "string")),
    ("datatype(?v) = rdf:langString", ("datatype", f"{filters.values.RDF_LANGSTRING}")),
    ("datatype(?v) = <http://ex.org/dt>", ("datatype", "http://ex.org/dt")),
    ('lang(?v) = "en"', ("lang", "en")),
    ('"en" = lang(?v)', ("lang", "en")),
    ('lang(?v) = ""', ("lang", "")),
    ('lang(?v) = "en-us"', ("lang", "en-us")),
    ('langMatches(lang(?v), "EN")', ("lang_matches", "EN")),
    ('langMatches(lang(?v), "*")', ("lang_matches", "*")),
    ('langMatches(lang(?v), "en-US")', ("lang_matches", "en-US")),
    ('strstarts(str(?v), "http")', ("str_prefix", "http", "", None, True)),
    ('strstarts(str(?v), "http://ex.org/x")', ("str_prefix", "http://ex.org/x", "", None, True)),
    ('strstarts(str(?v), "a")', ("str_prefix", "a", "", None, True)),
    ('strstarts(str(?v), "")', ("str_prefix", "", "", None, True)),
    ('strstarts(str(?v), "é")', ("str_prefix", "é", "", None, True)),
    ('strstarts(str(?v), "tab\\t")', ("str_prefix", "tab\t", "", None, True)),
    ('strstarts(str(?v), "b"^^xsd:string)', ("str_prefix", "b", "", None, True)),
]
_INT = f'"5"^^<{XSD}integer>'
for _op, _kind in filters.native._NUM_KINDS.items():
    MAPPED.append((f"?v {_op} 5", (_kind, _INT)))
    MAPPED.append((f"5 {filters.native._SWAPPED[_op]} ?v", (_kind, _INT)))
for _constant, _spelled in [
    ("5.5", f'"5.5"^^<{XSD}decimal>'),
    ("1e1", f'"10.0"^^<{XSD}double>'),
    ("-1", f'"-1"^^<{XSD}integer>'),
    ("1e100", f'"1e+100"^^<{XSD}double>'),
    ('"5"^^xsd:int', f'"5"^^<{XSD}int>'),
    ('"5"^^xsd:long', f'"5"^^<{XSD}long>'),
]:
    MAPPED.append((f"?v < {_constant}", ("num_lt", _spelled)))
    MAPPED.append((f"?v >= {_constant}", ("num_ge", _spelled)))
    MAPPED.append((f"{_constant} = ?v", ("num_eq", _spelled)))

#: The string functions, each with the native shape it maps to:
#: REGEX, CONTAINS, STRSTARTS and STRENDS over ?v or STR(?v), either under
#: LCASE/UCASE. Only flags i, s and m reach vortex-rdf.
STRING_MAPPED = [
    ('regex(?v, "^a")', FilterCodes("regex", "^a")),
    ('regex(?v, "b$")', FilterCodes("regex", "b$")),
    ('regex(?v, "c$", "m")', FilterCodes("regex", "c$", "m")),
    ('regex(?v, "a.b", "s")', FilterCodes("regex", "a.b", "s")),
    ('regex(?v, "^AB", "i")', FilterCodes("regex", "^AB", "i")),
    ('regex(?v, "s", "i")', FilterCodes("regex", "s", "i")),
    ('regex(?v, "ſ", "i")', FilterCodes("regex", "ſ", "i")),
    ('regex(?v, "\\\\w+ \\\\w+")', FilterCodes("regex", "\\w+ \\w+")),
    ('regex(?v, "^[^a-z]")', FilterCodes("regex", "^[^a-z]")),
    ('regex(str(?v), "^ab")', FilterCodes("regex", "^ab", as_str=True)),
    ('regex(str(?v), "^4")', FilterCodes("regex", "^4", as_str=True)),
    ('regex(lcase(?v), "^abc$")', FilterCodes("regex", "^abc$", case="lower")),
    (
        'regex(ucase(str(?v)), "STRASSE")',
        FilterCodes("regex", "STRASSE", case="upper", as_str=True),
    ),
    ('contains(?v, "b")', FilterCodes("contains", '"b"')),
    ('contains(?v, "b"@en)', FilterCodes("contains", '"b"@en')),
    ('contains(?v, "x"^^xsd:string)', FilterCodes("contains", '"x"')),
    ('contains(str(?v), "+")', FilterCodes("contains", '"+"', as_str=True)),
    ('contains(lcase(?v), "école")', FilterCodes("contains", '"école"', case="lower")),
    ('strstarts(?v, "a")', FilterCodes("strstarts", '"a"')),
    ('strstarts(?v, "a"@en)', FilterCodes("strstarts", '"a"@en')),
    ('strstarts(ucase(?v), "AB")', FilterCodes("strstarts", '"AB"', case="upper")),
    (
        'strstarts(lcase(str(?v)), "http")',
        FilterCodes("strstarts", '"http"', case="lower", as_str=True),
    ),
    ('strends(?v, "\\n")', FilterCodes("strends", '"\\n"')),
    ('strends(str(?v), ".0")', FilterCodes("strends", '".0"', as_str=True)),
    ('strends(lcase(str(?v)), "e")', FilterCodes("strends", '"e"', case="lower", as_str=True)),
]
MAPPED += STRING_MAPPED

#: IRI equality: true for exactly the listed IRIs, a keep of their codes; an
#: IRI the dictionary lacks contributes none.
IRI_MAPPED = [
    ("?v = <http://ex.org/x>", native.IriIn(("<http://ex.org/x>",))),
    ("<http://ex.org/x> = ?v", native.IriIn(("<http://ex.org/x>",))),
    ("sameTerm(?v, <http://ex.org/x>)", native.IriIn(("<http://ex.org/x>",))),
    ("sameTerm(<urn:x>, ?v)", native.IriIn(("<urn:x>",))),
    (
        "?v IN (<http://ex.org/x>, <urn:x>, <http://ex.org/x>)",
        native.IriIn(("<http://ex.org/x>", "<urn:x>")),
    ),
    ("?v IN (<http://ex.org/absent>)", native.IriIn(("<http://ex.org/absent>",))),
    ("?v IN ()", native.IriIn(())),
]
MAPPED += IRI_MAPPED

#: Tests of a term's kind alone, and the kinds each is true for: decided by
#: kind, and a keep when those kinds run together.
KIND_MAPPED: list[tuple[str, set]] = [
    ("isIRI(?v)", {IRI}),
    ("isURI(?v)", {IRI}),
    ("isBlank(?v)", {BLANK}),
    ("isLiteral(?v)", {LITERAL}),
    ("isIRI(?v) || isBlank(?v)", {IRI, BLANK}),
    ("isLiteral(?v) || isBlank(?v)", {LITERAL, BLANK}),
    ("!isLiteral(?v)", {IRI, BLANK, ""}),
]


def _decide(store, decision, codes) -> tuple[set, list]:
    """``decision`` over ``codes``: the codes it passes and the codes it
    leaves undecided."""
    return decision.split(store, codes)


def _one_range(kinds) -> bool:
    """Whether ``kinds`` are a run of the kinds' code order, no ``""``."""
    if not kinds or "" in kinds:
        return False
    positions = sorted(native.KIND_ORDER.index(kind) for kind in kinds)
    return positions[-1] - positions[0] + 1 == len(positions)


#: Shapes the mapping must leave to the Python routes: the native layer
#: would answer some of them differently from the specification.
UNMAPPED = [
    # ranges the native basic filtering leaves undecided: a "*" subtag,
    # padding, the empty range
    'langMatches(lang(?v), "*-US")',
    'langMatches(lang(?v), "en-*")',
    'langMatches(lang(?v), " en")',
    'langMatches(lang(?v), "")',
    # a language-tagged range or prefix is an error for every value
    'langMatches(lang(?v), "de-CH"@fr)',
    'strstarts(str(?v), "a"@en)',
    'lang(?v) = "en"@fr',
    # constants that are no well-typed number, and shapes no native kind is
    '?v < "abc"^^xsd:integer',
    '?v < "300"^^xsd:byte',
    '?v < "5"',
    "?v < true",
    "?v IN (5, 6)",
    "datatype(?v) != xsd:integer",
    "?v + 1 > 3",
    "!isIRI(?v)",
    # an xsd:float constant: vortex-rdf compares it as a double, where XPath
    # rounds it to single precision
    '?v < "5"^^xsd:float',
    '"5"^^xsd:float = ?v',
    # vortex-rdf compares a constant's language tag as written, SPARQL
    # case-insensitively: an upper-case tag stays with the Python routes
    'contains(?v, "a"@EN)',
    # STR() drops the tag, so a tagged constant is an error for every value
    'contains(str(?v), "a"@en)',
    # an invalid pattern, an error for every value
    'regex(?v, "a{2,1}")',
    # flags x and q change how the pattern reads; vortex-rdf ignores them
    'regex(?v, "^[a-c]{2}", "xq")',
    # not a constant pattern, or flags that are not a string
    "regex(?v, str(?v))",
    'regex(?v, "a", 1)',
    # texts outside the grammar
    'strstarts(lcase(lcase(?v)), "a")',
    'contains(str(lcase(?v)), "a")',
    'contains(substr(?v, 1), "a")',
    # IRI equality only: a literal constant compares by value (RDFterm-equal)
    '?v IN (<http://ex.org/x>, "x")',
    'sameTerm(?v, "x")',
    "?v != <http://ex.org/x>",
    "?v NOT IN (<http://ex.org/x>)",
    "?v = ?v",
]


def _subject(index: int, spelling: str) -> str:
    """The subject of the spelling store's statement whose object is
    ``spelling``: the term itself for an IRI or a blank node."""
    return spelling if spelling.startswith(("<", "_:")) else f"<http://ex.org/s{index}>"


@pytest.fixture(scope="module")
def spelling_store(tmp_path_factory):
    """Every spelling of the matrix as an object, in a Dictionary store."""
    directory = tmp_path_factory.mktemp("native-filters")
    nt = directory / "spellings.nt"
    with open(nt, "w", encoding="utf-8") as f:
        for i, spelling in enumerate(ALL_SPELLINGS):
            f.write(f"{_subject(i, spelling)} <http://ex.org/p> {spelling} .\n")
    out = directory / "spellings.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return VortexRdflibStore(str(out), in_memory=True)


@pytest.mark.parametrize(("sparql_expr", "expected"), MAPPED, ids=[m[0] for m in MAPPED])
def test_expression_maps_to_its_native_predicate(sparql_expr, expected):
    if not isinstance(expected, native.IriIn):
        expected = FilterCodes(*expected)
    assert filters.native.native_shape(filter_expr(sparql_expr), V, {}) == expected


@pytest.mark.parametrize(("sparql_expr", "kinds"), KIND_MAPPED, ids=[k[0] for k in KIND_MAPPED])
def test_a_kind_test_is_decided_by_the_kinds_it_holds_for(spelling_store, sparql_expr, kinds):
    """A test of the term's kind alone is evaluated once per kind: the kinds
    it is true for decide every code, as the specification answers, and are
    a keep on the native match when their codes run together."""
    conjunct = filters._compile_conjunct(filter_expr(sparql_expr), (V,), {}, CTX)
    assert conjunct.decision == native.Kinds(frozenset(kinds))
    assert conjunct.keep is (conjunct.decision if _one_range(kinds) else None)
    assert filters.native.native_shape(filter_expr(sparql_expr), V, {}) is None
    kind_of = spelling_store._file.kind_ranges.kind_of
    for code in sorted(_all_codes(spelling_store)):
        stored = spelling_store._dict.decode(code)
        if departs(sparql_expr, stored):
            continue
        assert (kind_of(code) in kinds) == spec_answer(sparql_expr, {"v": stored}), stored


def test_a_conjunct_is_hashable_by_identity():
    """Two conjuncts of one expression are two conjuncts: a relation names
    the ones every row passes (``Relation.applied``) by identity."""
    expr = filter_expr("?v < ?w")
    a = filters._compile_conjunct(expr, (V, W), {}, CTX)
    b = filters._compile_conjunct(expr, (V, W), {}, CTX)
    assert len({a, b}) == 2 and a in {a} and b not in {a}


def test_a_kind_decision_is_one_range_keep(spelling_store):
    """A test of the term's kind alone is decided by the kinds it holds for;
    kinds whose codes run together are also its keep on the native match,
    one code range, and kinds apart are none."""
    conjunct = filters._compile_conjunct(filter_expr("isIRI(?v) || isBlank(?v)"), (V,), {}, CTX)
    assert conjunct.decision == native.Kinds(frozenset({IRI, BLANK}))
    assert conjunct.keep is conjunct.decision
    ranges = kind_ranges(spelling_store)
    keep = native.keep_of(spelling_store, conjunct.keep)
    assert native.is_range(keep) and keep == (ranges.range(IRI)[0], ranges.range(BLANK)[1])
    apart = filters._compile_conjunct(filter_expr("isLiteral(?v) || isBlank(?v)"), (V,), {}, CTX)
    assert apart.decision == native.Kinds(frozenset({LITERAL, BLANK}))
    assert apart.keep is None


@pytest.mark.parametrize("sparql_expr", UNMAPPED)
def test_expression_outside_the_native_shapes_is_not_mapped(sparql_expr):
    assert filters.native.native_shape(filter_expr(sparql_expr), V, {}) is None


def _all_codes(store) -> set:
    return {store._dict.encode(spelling) for spelling in ALL_SPELLINGS}


@pytest.mark.parametrize("sparql_expr", [m[0] for m in MAPPED])
def test_native_candidates_follow_the_spec(spelling_store, sparql_expr):
    """Every spelling of the matrix at once, as one query's candidate codes:
    each code vortex-rdf decides gets SPARQL 1.1's answer; the codes it leaves
    undecided go to the Python routes."""
    expr = filter_expr(sparql_expr)
    shape = filters.native.native_shape(expr, V, {})
    assert shape is not None
    codes = _all_codes(spelling_store)
    passed, undecided = _decide(spelling_store, shape, codes)
    undecided = set(undecided)
    assert passed <= codes and undecided <= codes and not passed & undecided
    decided = 0
    for code in sorted(codes - undecided):
        stored = spelling_store._dict.decode(code)
        if departs(sparql_expr, stored):
            continue
        decided += 1
        expected = spec_answer(sparql_expr, {"v": stored})
        assert (code in passed) == expected, (sparql_expr, stored)
    assert decided > 0, sparql_expr


def test_wide_integers_are_left_undecided(spelling_store):
    """rdflib bounds neither xsd:long nor xsd:unsignedLong from above and
    compares such a literal by value; vortex-rdf holds them to 64 bits and
    leaves the ones past it undecided."""
    wide = {
        spelling_store._dict.encode(typed(lexical, datatype))
        for lexical, datatype in [
            ("9223372036854775808", "long"),
            ("-9223372036854775809", "long"),
            ("18446744073709551616", "unsignedLong"),
        ]
    }
    for kind in ("num_lt", "num_gt", "num_eq"):
        shape = FilterCodes(kind, f'"1e+100"^^<{XSD}double>')
        passed, undecided = native.evaluate(spelling_store, shape, wide)
        assert not passed and set(undecided) == wide


def test_a_refused_argument_leaves_every_candidate_to_the_python_routes(
    spelling_store, monkeypatch
):
    codes = _all_codes(spelling_store)
    lo, hi = kind_ranges(spelling_store).range(LITERAL)
    monkeypatch.setattr(
        spelling_store, "_dict", RecordingTermDict(spelling_store._dict, refuse=True)
    )
    shape = FilterCodes("num_lt", f'"1e400"^^<{XSD}double>')
    passed, undecided = native.evaluate(spelling_store, shape, codes)
    assert not passed
    assert set(undecided) == {code for code in codes if lo <= code < hi}


def test_only_the_codes_a_predicate_reads_are_sent(spelling_store, monkeypatch):
    """vortex-rdf fails every code in neither answer set, so the codes outside
    a predicate's domain are decided here — `!=` passes every IRI and blank
    node, `<` fails them — and only literals are sent, sorted and unique, as
    filter_codes requires: the xsd:float codes looked up first, and an
    ordering's passed codes checked for numbers after."""
    codes = _all_codes(spelling_store)
    lo, hi = kind_ranges(spelling_store).range(LITERAL)
    nonliterals = {code for code in codes if not lo <= code < hi}
    assert nonliterals
    recording = RecordingTermDict(spelling_store._dict)
    monkeypatch.setattr(spelling_store, "_dict", recording)
    five = f'"5"^^<{XSD}integer>'
    passed_ne, _ = native.evaluate(spelling_store, FilterCodes("num_ne", five), codes)
    passed_lt, _ = native.evaluate(spelling_store, FilterCodes("num_lt", five), codes)
    assert nonliterals <= passed_ne
    assert not nonliterals & passed_lt
    kinds = [kind for kind, _, _, _ in recording.calls]
    assert kinds == ["datatype", "num_ne", "datatype", "num_lt", "num_ne"]
    for sent in recording.sent:
        assert sent == sorted(set(sent))
        assert not set(sent) & nonliterals


@pytest.mark.parametrize(
    "constant",
    [
        f'"5"^^<{XSD}integer>',
        f'"0"^^<{XSD}integer>',
        f'"-1"^^<{XSD}integer>',
        f'"4.5"^^<{XSD}decimal>',
        f'"1e+100"^^<{XSD}double>',
    ],
)
def test_num_eq_and_num_ne_pass_only_numbers(spelling_store, constant):
    """The contract an ordering's check for numbers relies on
    (``native._numbers``): every code ``num_eq`` or ``num_ne`` passes is a
    well-typed number."""
    codes = sorted(code for code in _all_codes(spelling_store) if _is_literal(spelling_store, code))
    for kind in ("num_eq", "num_ne"):
        passed, _ = spelling_store._dict.filter_codes(kind, constant, U64Column(codes))
        for code in memoryview(passed).cast("Q").tolist():
            stored = spelling_store._dict.decode(code)
            number = filters.values.numeric_operand(parse_spelling(stored))
            assert number is not None, (kind, stored)


def _is_literal(store, code: int) -> bool:
    lo, hi = kind_ranges(store).range(LITERAL)
    return lo <= code < hi


def test_str_of_a_blank_node_is_left_to_the_python_routes(spelling_store, monkeypatch):
    """rdflib reads STR(_:abc) as "abc": under STR() the blank nodes are never
    sent, they come back undecided, while the IRIs are sent."""
    codes = _all_codes(spelling_store)
    ranges = kind_ranges(spelling_store)
    blanks = {c for c in codes if ranges.kind_of(c) == BLANK}
    iris = {c for c in codes if ranges.kind_of(c) == IRI}
    assert blanks and iris
    recording = RecordingTermDict(spelling_store._dict)
    monkeypatch.setattr(spelling_store, "_dict", recording)
    _, undecided = native.evaluate(
        spelling_store, FilterCodes("str_prefix", "a", as_str=True), codes
    )
    assert blanks <= set(undecided)
    [sent] = recording.sent
    assert not set(sent) & blanks and iris <= set(sent)


def test_an_argument_vortex_rdf_cannot_take_leaves_every_candidate_undecided(spelling_store):
    """filter_codes raises ValueError (here UnicodeEncodeError, a lone
    surrogate) for an argument it cannot take: every candidate it would have
    read goes to the Python routes, for every string kind."""
    codes = _all_codes(spelling_store)
    lo, hi = kind_ranges(spelling_store).range(LITERAL)
    literals = {code for code in codes if lo <= code < hi}
    for shape in (
        FilterCodes("regex", "a|\ud800"),
        FilterCodes("contains", '"\ud800"'),
        FilterCodes("strstarts", '"\ud800"', as_str=True),
    ):
        passed, undecided = native.evaluate(spelling_store, shape, codes)
        assert not passed and literals <= set(undecided), shape


class _RecordingLookups:
    """A TermDict that records every spelling looked up by ``encode_many``."""

    def __init__(self, inner):
        self._inner = inner
        self.looked_up: list[str] = []

    def encode_many(self, spellings):
        self.looked_up.extend(spellings)
        return self._inner.encode_many(spellings)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    "sparql_expr",
    ["?v = <foo>", "sameTerm(<foo>, ?v)", "?v IN (<1http://x>, <urn:x>)"],
)
def test_an_iri_without_the_parsers_form_is_never_looked_up(
    spelling_store, monkeypatch, sparql_expr
):
    """A relative IRI, or one whose scheme starts with a digit, has no form
    the native parser takes (``terms.unparseable_iri``): the mapping leaves
    its equality to the Python routes without asking the dictionary, and the
    answer is the specification's."""
    from rdflib import Graph

    assert native.native_shape(filter_expr(sparql_expr), V, {}) is None
    recording = _RecordingLookups(spelling_store._dict)
    monkeypatch.setattr(spelling_store, "_dict", recording)
    register_sparql_pushdown()
    query = f"SELECT ?s WHERE {{ ?s <http://ex.org/p> ?v FILTER({sparql_expr}) }}"
    kept = {row[0].n3() for row in Graph(store=spelling_store).query(query)}
    expected = {"<urn:x>"} if "urn:x" in sparql_expr else set()
    assert kept == expected
    assert not {"<foo>", "<1http://x>"} & set(recording.looked_up)


@pytest.mark.parametrize("refused", ["<http://x/%zz>", "<http://ex.org/\ud800>"])
def test_an_iri_the_parser_refuses_names_no_term(spelling_store, refused):
    """An IRI of the parser's form that it still refuses — a bad percent
    escape, a lone surrogate — is no term of any file (the parser that wrote
    the file refuses it too), so it is a miss, as an IRI the dictionary lacks
    is: the keep holds the other IRIs' codes, and a keep on the match and a
    keep over a query's codes decide every term alike."""
    codes = _all_codes(spelling_store)
    urn = spelling_store._dict.encode("<urn:x>")
    keep = native.IriIn((refused, "<urn:x>"))
    assert memoryview(native.keep_of(spelling_store, keep)).cast("Q").tolist() == [urn]
    assert native.keep_decides(spelling_store, keep, codes) == ({urn}, [])
    restriction, residual = native.native_restriction(spelling_store, [SimpleNamespace(keep=keep)])
    assert memoryview(restriction).cast("Q").tolist() == [urn]
    assert not residual


@pytest.mark.parametrize(
    "shape",
    [
        native.IriIn(("<http://ex.org/x>", "<urn:x>")),
        FilterCodes("num_ne", f'"5"^^<{XSD}integer>'),
        FilterCodes("num_lt", f'"5"^^<{XSD}integer>'),
        FilterCodes("str_prefix", "a", as_str=True),
        FilterCodes("regex", "a"),
        FilterCodes("regex", "a|\ud800"),
    ],
    ids=[
        "iri_in",
        "num_ne",
        "num_lt",
        "str_prefix",
        "regex",
        "regex-refused",
    ],
)
def test_a_code_that_is_no_term_is_left_undecided_under_every_shape(spelling_store, shape):
    """The empty graph name (code 0), a code past the dictionary's end and a
    negative code lie in none of the kinds' ranges: whatever the shape, they
    go to the Python routes, which fail on them, and the verdicts on the
    terms are what they are without them. (A test of the term's kind alone
    decides them itself, by whether it holds for no term: ``native.Kinds``.)"""
    dictionary = spelling_store._dict
    assert dictionary.decode(0) == ""
    not_terms = {0, len(dictionary), len(dictionary) + 7, -1}
    codes = _all_codes(spelling_store)
    passed, undecided = _decide(spelling_store, shape, codes)
    passed_with, undecided_with = _decide(spelling_store, shape, codes | not_terms)
    assert passed_with == passed
    assert sorted(undecided_with) == sorted([*undecided, *not_terms])


def test_ctx_bound_constant_side_is_evaluated_by_rdflib():
    """The constant side of a comparison may be an expression over visible
    ctx-bound variables (BSBM Explore Q5's `?ref + N`): rdflib computes it."""
    from rdflib import Literal

    expr = filter_expr("?v < ?w + 10")
    shape = filters.native.native_shape(expr, V, {Variable("w"): Literal(5)})
    assert shape == FilterCodes("num_lt", f'"15"^^<{XSD}integer>')
    # an error on the constant side is no constant
    assert filters.native.native_shape(expr, V, {Variable("w"): Literal("x")}) is None
    assert filters.native.native_shape(expr, V, {}) is None


QUERY_CONJUNCTS = (
    [m[0] for m in MAPPED]
    + [k[0] for k in KIND_MAPPED]
    + [
        'isIRI(?v) && strstarts(str(?v), "http://ex.org/x")',
        "datatype(?v) = xsd:integer && ?v < 5 && ?v > -5",
        'lang(?v) = "en" && langMatches(lang(?v), "en")',
        "?v < 5 && ?v > 1e100",
        'regex(?v, "a{2,1}")',
    ]
)


# rdflib warns each time it builds the matrix's "maybe"^^xsd:boolean, an
# ill-typed literal kept on purpose, for a row it returns.
@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
@pytest.mark.parametrize("sparql_expr", QUERY_CONJUNCTS)
def test_pushed_filter_query_follows_the_spec(spelling_store, in_memory, sparql_expr):
    """End to end: the FILTER, pushed into the native match as a keep, keeps
    the subjects whose object SPARQL 1.1 keeps — with the undecided values
    resolved on the Python routes."""
    from rdflib import Graph

    graph = Graph(store=VortexRdflibStore(spelling_store.path, in_memory=in_memory))
    query = (
        "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
        "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
        f"SELECT ?s ?v WHERE {{ ?s <http://ex.org/p> ?v FILTER({sparql_expr}) }}"
    )
    register_sparql_pushdown()
    skipped, expected = set(), set()
    for i, spelling in enumerate(ALL_SPELLINGS):
        subject = _subject(i, spelling)
        if departs(sparql_expr, spelling):
            skipped.add(subject)
        elif spec_answer(sparql_expr, {"v": spelling}):
            expected.add(subject)
    kept = {row[0].n3() for row in graph.query(query)}
    assert kept - skipped == expected


def test_a_mid_size_code_set_goes_to_the_native_layer_whole(tmp_path, monkeypatch, capsys):
    """On a file-backed store the 100 IRIs of an IN list reach the native
    match as the exact code set, and the answer is rdflib's."""
    import json

    from rdflib import Graph

    from test_pushdown import both_ways

    nt = tmp_path / "s.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p> <http://ex.org/o{i}> .\n" for i in range(300)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "s.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=False)
    store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    subjects = ", ".join(f"<http://ex.org/s{i}>" for i in range(0, 200, 2))
    query = f"SELECT ?s WHERE {{ ?s <http://ex.org/p> ?o FILTER(?s IN ({subjects})) }}"
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(Graph(store=store), query)
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 100
    events = [
        json.loads(line[len(trace.PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(trace.PREFIX)
    ]
    match = next(
        e
        for e in events
        if e["event"] == "native_call_complete" and e["operation"] == "match_codes"
    )
    assert match["keep"] == {"0": {"codes": 100}}
