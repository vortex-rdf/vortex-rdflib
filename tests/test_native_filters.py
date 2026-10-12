"""The native term predicates (``TermDict.filter_codes``) must agree with
rdflib's own evaluator wherever they give a definite answer: a FILTER
conjunct mapped to one (``filters.native_shape``) is answered by vortex-rdf
over the codes a query produced (``candidates.evaluate``), so a
disagreement would be a wrong answer, not a slow one. The matrix of
spellings is the fast route's (``test_filters``) plus the shapes the native
value model treats specially: decimals against doubles, integers past 2^53,
64-bit bounds, language subtags."""

import pytest
from rdflib.term import Variable
from vortex_rdf import serialize_rdf

from test_filters import SPELLINGS, filter_expr, rdflib_answer, typed
from vortex_rdflib import VortexRdflibStore, candidates, filters, register_sparql_pushdown
from vortex_rdflib.candidates import NativeShape

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
]
ALL_SPELLINGS = [s for s in SPELLINGS + EXTRA_SPELLINGS if s is not None]

#: Expressions the mapping must take, each with the predicate it maps to.
MAPPED = [
    ("isIRI(?v)", ("is_iri", "")),
    ("isURI(?v)", ("is_iri", "")),
    ("isBlank(?v)", ("is_blank", "")),
    ("isLiteral(?v)", ("is_literal", "")),
    ("datatype(?v) = xsd:integer", ("datatype", XSD + "integer")),
    ("xsd:integer = datatype(?v)", ("datatype", XSD + "integer")),
    ("datatype(?v) = xsd:string", ("datatype", XSD + "string")),
    ("datatype(?v) = rdf:langString", ("datatype", f"{filters._RDF_LANGSTRING}")),
    ("datatype(?v) = <http://ex.org/dt>", ("datatype", "http://ex.org/dt")),
    ('lang(?v) = "en"', ("lang", "en")),
    ('"en" = lang(?v)', ("lang", "en")),
    ('lang(?v) = ""', ("lang", "")),
    ('lang(?v) = "en-us"', ("lang", "en-us")),
    ('langMatches(lang(?v), "EN")', ("lang_matches", "EN")),
    ('langMatches(lang(?v), "*")', ("lang_matches", "*")),
    ('langMatches(lang(?v), "en-US")', ("lang_matches", "en-US")),
    ('langMatches(lang(?v), "de-CH"@fr)', ("lang_matches", "de-CH")),
    ('strstarts(str(?v), "http")', ("str_prefix", "http")),
    ('strstarts(str(?v), "http://ex.org/x")', ("str_prefix", "http://ex.org/x")),
    ('strstarts(str(?v), "a")', ("str_prefix", "a")),
    ('strstarts(str(?v), "")', ("str_prefix", "")),
    ('strstarts(str(?v), "é")', ("str_prefix", "é")),
    ('strstarts(str(?v), "tab\\t")', ("str_prefix", "tab\t")),
    ('strstarts(str(?v), "b"^^xsd:string)', ("str_prefix", "b")),
]
_INT = f'"5"^^<{XSD}integer>'
for _op, _kind in filters._NUM_KINDS.items():
    MAPPED.append((f"?v {_op} 5", (_kind, _INT)))
    MAPPED.append((f"5 {filters._SWAPPED[_op]} ?v", (_kind, _INT)))
for _constant, _spelled in [
    ("5.5", f'"5.5"^^<{XSD}decimal>'),
    ("1e1", f'"10.0"^^<{XSD}double>'),
    ("-1", f'"-1"^^<{XSD}integer>'),
    ("1e100", f'"1e+100"^^<{XSD}double>'),
    ('"5"^^xsd:int', f'"5"^^<{XSD}int>'),
    ('"5"^^xsd:float', f'"5"^^<{XSD}float>'),
    ('"5"^^xsd:long', f'"5"^^<{XSD}long>'),
]:
    MAPPED.append((f"?v < {_constant}", ("num_lt", _spelled)))
    MAPPED.append((f"?v >= {_constant}", ("num_ge", _spelled)))
    MAPPED.append((f"{_constant} = ?v", ("num_eq", _spelled)))

#: Shapes the mapping must leave to the Python routes: the native layer
#: would answer some of them differently from rdflib.
UNMAPPED = [
    # rdflib's langMatches splits the range on "-" and lets "*" match any
    # subtag; the native basic filtering does not.
    'langMatches(lang(?v), "*-US")',
    'langMatches(lang(?v), "en-*")',
    'langMatches(lang(?v), " en")',
    'langMatches(lang(?v), "")',
    # a language-tagged prefix makes strstarts an error for every value
    'strstarts(str(?v), "a"@en)',
    # strstarts without str() is an error on IRIs, which str_prefix answers
    'strstarts(?v, "a")',
    'lang(?v) = "en"@fr',
    # constants rdflib compares by its own rules
    '?v < "abc"^^xsd:integer',
    '?v < "300"^^xsd:byte',
    '?v < "5"',
    "?v < true",
    "?v IN (5, 6)",
    "datatype(?v) != xsd:integer",
    "?v + 1 > 3",
    "!isIRI(?v)",
]


@pytest.fixture(scope="module")
def spelling_store(tmp_path_factory):
    """Every spelling of the matrix as an object, in a Dictionary store."""
    directory = tmp_path_factory.mktemp("native-filters")
    nt = directory / "spellings.nt"
    with open(nt, "w", encoding="utf-8") as f:
        for i, spelling in enumerate(ALL_SPELLINGS):
            subject = spelling if spelling.startswith(("<", "_:")) else f"<http://ex.org/s{i}>"
            f.write(f"{subject} <http://ex.org/p> {spelling} .\n")
    out = directory / "spellings.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return VortexRdflibStore(str(out), in_memory=True)


@pytest.mark.parametrize(("sparql_expr", "expected"), MAPPED, ids=[m[0] for m in MAPPED])
def test_expression_maps_to_its_native_predicate(sparql_expr, expected):
    assert filters.native_shape(filter_expr(sparql_expr), V, {}) == NativeShape(*expected)


@pytest.mark.parametrize("sparql_expr", UNMAPPED)
def test_expression_outside_the_native_shapes_is_not_mapped(sparql_expr):
    assert filters.native_shape(filter_expr(sparql_expr), V, {}) is None


def _all_codes(store) -> set:
    return {store._dict.encode(spelling) for spelling in ALL_SPELLINGS}


@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
@pytest.mark.parametrize("sparql_expr", [m[0] for m in MAPPED])
def test_native_candidates_agree_with_rdflib(spelling_store, sparql_expr):
    """Every spelling of the matrix at once, as one query's candidate codes:
    each code vortex-rdf decides gets rdflib's answer; the codes it leaves
    undecided go to the Python routes."""
    expr = filter_expr(sparql_expr)
    shape = filters.native_shape(expr, V, {})
    assert shape is not None
    codes = _all_codes(spelling_store)
    passed, undecided = candidates.evaluate(spelling_store, shape, codes)
    undecided = set(undecided)
    assert passed <= codes and undecided <= codes and not passed & undecided
    decided = 0
    for code in sorted(codes - undecided):
        decided += 1
        stored = spelling_store._dict.decode(code)
        assert (code in passed) == rdflib_answer(expr, stored), (sparql_expr, stored)
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
        shape = NativeShape(kind, f'"1e+100"^^<{XSD}double>')
        passed, undecided = candidates.evaluate(spelling_store, shape, wide)
        assert not passed and set(undecided) == wide


class _Recording:
    """A TermDict that records every filter_codes call as ``(kind, arg,
    options, candidate codes)`` (or refuses every argument, with
    ``refuse``)."""

    def __init__(self, inner, refuse: bool = False):
        self._inner = inner
        self.refuse = refuse
        self.calls: list[tuple] = []

    @property
    def sent(self) -> list[list[int]]:
        """The candidate codes of each call."""
        return [codes for _, _, _, codes in self.calls]

    def filter_codes(self, kind, arg, codes, **options):
        self.calls.append((kind, arg, options, memoryview(codes).cast("Q").tolist()))
        if self.refuse:
            raise ValueError(f"refused: {kind} {arg}")
        return self._inner.filter_codes(kind, arg, codes, **options)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_refused_argument_leaves_every_candidate_to_the_python_routes(
    spelling_store, monkeypatch
):
    codes = _all_codes(spelling_store)
    lo, hi = spelling_store._kind_ranges()[filters.LITERAL]
    monkeypatch.setattr(spelling_store, "_dict", _Recording(spelling_store._dict, refuse=True))
    shape = NativeShape("num_lt", f'"1e400"^^<{XSD}double>')
    passed, undecided = candidates.evaluate(spelling_store, shape, codes)
    assert not passed
    assert set(undecided) == {code for code in codes if lo <= code < hi}


def test_only_the_codes_a_predicate_reads_are_sent(spelling_store, monkeypatch):
    """vortex-rdf fails every code in neither answer set, so the codes outside
    a predicate's domain are decided here by rdflib's rule and never sent —
    `!=` passes every IRI and blank node, `<` fails them — and what is sent is
    sorted and unique, as filter_codes requires."""
    codes = _all_codes(spelling_store)
    lo, hi = spelling_store._kind_ranges()[filters.LITERAL]
    nonliterals = {code for code in codes if not lo <= code < hi}
    assert nonliterals
    recording = _Recording(spelling_store._dict)
    monkeypatch.setattr(spelling_store, "_dict", recording)
    five = f'"5"^^<{XSD}integer>'
    passed_ne, _ = candidates.evaluate(spelling_store, NativeShape("num_ne", five), codes)
    passed_lt, _ = candidates.evaluate(spelling_store, NativeShape("num_lt", five), codes)
    assert nonliterals <= passed_ne
    assert not nonliterals & passed_lt
    assert len(recording.sent) == 2
    for sent in recording.sent:
        assert sent == sorted(set(sent))
        assert not set(sent) & nonliterals


def test_str_of_a_blank_node_is_left_to_the_python_routes(spelling_store, monkeypatch):
    """rdflib reads STR(_:abc) as "abc": under STR() the blank nodes are never
    sent, they come back undecided, while the IRIs are sent."""
    codes = _all_codes(spelling_store)
    ranges = spelling_store._kind_ranges()
    blanks = {c for c in codes if ranges[filters.BLANK][0] <= c < ranges[filters.BLANK][1]}
    iris = {c for c in codes if ranges[filters.IRI][0] <= c < ranges[filters.IRI][1]}
    assert blanks and iris
    recording = _Recording(spelling_store._dict)
    monkeypatch.setattr(spelling_store, "_dict", recording)
    _, undecided = candidates.evaluate(spelling_store, NativeShape("str_prefix", "a"), codes)
    assert blanks <= set(undecided)
    [sent] = recording.sent
    assert not set(sent) & blanks and iris <= set(sent)


def test_an_argument_vortex_rdf_cannot_take_leaves_every_candidate_undecided(spelling_store):
    """filter_codes raises ValueError (here UnicodeEncodeError, a lone
    surrogate) for an argument it cannot take: every candidate it would have
    read goes to the Python routes, for every string kind and for IRI
    equality."""
    codes = _all_codes(spelling_store)
    lo, hi = spelling_store._kind_ranges()[filters.LITERAL]
    literals = {code for code in codes if lo <= code < hi}
    for shape in (
        NativeShape("regex", "a|\ud800"),
        NativeShape("contains", '"\ud800"'),
        NativeShape("strstarts", '"\ud800"', as_str=True),
    ):
        passed, undecided = candidates.evaluate(spelling_store, shape, codes)
        assert not passed and literals <= set(undecided), shape
    shape = NativeShape("iri_in", ("<http://ex.org/\ud800>",))
    passed, undecided = candidates.evaluate(spelling_store, shape, codes)
    assert not passed and set(undecided) == codes


@pytest.mark.parametrize(
    "shape",
    [
        NativeShape("is_literal", ""),
        NativeShape("is_iri", ""),
        NativeShape("is_blank", ""),
        NativeShape("iri_in", ("<http://ex.org/x>", "<urn:x>")),
        NativeShape("num_ne", f'"5"^^<{XSD}integer>'),
        NativeShape("num_lt", f'"5"^^<{XSD}integer>'),
        NativeShape("str_prefix", "a"),
        NativeShape("regex", "a"),
        NativeShape("regex", "a|\ud800"),
    ],
    ids=[
        "is_literal",
        "is_iri",
        "is_blank",
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
    terms are what they are without them."""
    dictionary = spelling_store._dict
    assert dictionary.decode(0) == ""
    not_terms = {0, len(dictionary), len(dictionary) + 7, -1}
    codes = _all_codes(spelling_store)
    passed, undecided = candidates.evaluate(spelling_store, shape, codes)
    passed_with, undecided_with = candidates.evaluate(spelling_store, shape, codes | not_terms)
    assert passed_with == passed
    assert sorted(undecided_with) == sorted([*undecided, *not_terms])


def test_ctx_bound_constant_side_is_evaluated_by_rdflib():
    """The constant side of a comparison may be an expression over visible
    ctx-bound variables (BSBM Explore Q5's `?ref + N`): rdflib computes it."""
    from rdflib import Literal

    expr = filter_expr("?v < ?w + 10")
    shape = filters.native_shape(expr, V, {Variable("w"): Literal(5)})
    assert shape == NativeShape("num_lt", f'"15"^^<{XSD}integer>')
    # an error on the constant side is no constant
    assert filters.native_shape(expr, V, {Variable("w"): Literal("x")}) is None
    assert filters.native_shape(expr, V, {}) is None


QUERY_CONJUNCTS = [m[0] for m in MAPPED] + [
    'isIRI(?v) && strstarts(str(?v), "http://ex.org/x")',
    "datatype(?v) = xsd:integer && ?v < 5 && ?v > -5",
    'lang(?v) = "en" && langMatches(lang(?v), "en")',
    "?v < 5 && ?v > 1e100",
]


@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
# rdflib's own Literal comparator, on the default-evaluator side, for the
# exotic datatypes of the matrix.
@pytest.mark.filterwarnings("ignore:NotImplemented should not be used:DeprecationWarning")
@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
@pytest.mark.parametrize("sparql_expr", QUERY_CONJUNCTS)
def test_pushed_filter_query_equals_default_evaluator(spelling_store, in_memory, sparql_expr):
    """End to end: the FILTER, pushed into the native match as a keep, gives
    rdflib's answer — with the undecided values resolved on the Python
    routes."""
    from rdflib import Graph

    from test_pushdown import both_ways

    graph = Graph(store=VortexRdflibStore(spelling_store.path, in_memory=in_memory))
    query = (
        "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
        "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
        f"SELECT ?s ?v WHERE {{ ?s <http://ex.org/p> ?v FILTER({sparql_expr}) }}"
    )
    register_sparql_pushdown()
    with_pushdown, without_pushdown = both_ways(graph, query, runner=_outcome)
    assert with_pushdown == without_pushdown


def _outcome(graph, sparql):
    """The answer, or the exception rdflib raises — a NaN double against a
    decimal raises `decimal.InvalidOperation` in rdflib's own comparator,
    and the pushdown, which leaves that value to rdflib, raises it too."""
    from test_pushdown import run

    try:
        return run(graph, sparql)
    except Exception as error:  # noqa: BLE001 - the exception is the answer
        return type(error)
