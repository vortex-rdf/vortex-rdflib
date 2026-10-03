"""The native term predicates (``TermDict.filter_codes``) must agree with
rdflib's own evaluator wherever they give a definite answer: a FILTER
conjunct mapped to one (``filters.native_shape``) is answered from the
dictionary's partition alone, so a disagreement would be a wrong answer, not
a slow one. The matrix of spellings is the fast route's (``test_filters``)
plus the shapes the native value model treats specially: decimals against
doubles, integers past 2^53, 64-bit bounds, language subtags."""

import pytest
from rdflib.term import Variable
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_filters import SPELLINGS, filter_expr, rdflib_answer, typed
from vortex_rdflib import VortexRdflibStore, filters, register_sparql_pushdown

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
    assert filters.native_shape(filter_expr(sparql_expr), V, {}) == expected


@pytest.mark.parametrize("sparql_expr", UNMAPPED)
def test_expression_outside_the_native_shapes_is_not_mapped(sparql_expr):
    assert filters.native_shape(filter_expr(sparql_expr), V, {}) is None


@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
@pytest.mark.parametrize("sparql_expr", [m[0] for m in MAPPED])
def test_native_verdicts_agree_with_rdflib(spelling_store, sparql_expr):
    """For every spelling the native partition decides, rdflib answers the
    same; the ones it leaves undecided go to the Python routes."""
    expr = filter_expr(sparql_expr)
    shape = filters.native_shape(expr, V, {})
    assert shape is not None
    verdicts = spelling_store._native_verdicts(*shape)
    assert verdicts is not None
    dictionary = spelling_store._dict
    literals = spelling_store._kind_ranges()[filters.LITERAL]
    decided = 0
    for spelling in ALL_SPELLINGS:
        code = dictionary.encode(spelling)
        stored = dictionary.decode(code)
        passed, undecided = verdicts.split([code], literals)
        if undecided:
            continue
        decided += 1
        assert (code in passed) == rdflib_answer(expr, stored), (sparql_expr, stored)
    assert decided > 0, sparql_expr


def test_wide_integers_are_left_undecided(spelling_store):
    """rdflib bounds neither xsd:long nor xsd:unsignedLong from above and
    compares such a literal by value; the native model holds them to 64
    bits. The ones past it must not get a native verdict."""
    wide = {
        spelling_store._dict.encode(typed(lexical, datatype))
        for lexical, datatype in [
            ("9223372036854775808", "long"),
            ("-9223372036854775809", "long"),
            ("18446744073709551616", "unsignedLong"),
        ]
    }
    suspects = set(spelling_store._wide_integer_codes())
    assert wide <= suspects
    assert spelling_store._dict.encode(typed("5", "long")) not in suspects
    for kind in ("num_lt", "num_gt", "num_eq"):
        verdicts = spelling_store._native_verdicts(kind, '"1e+100"^^<' + XSD + "double>")
        literals = spelling_store._kind_ranges()[filters.LITERAL]
        _, undecided = verdicts.split(wide, literals)
        assert set(undecided) == wide


def test_refused_constant_has_no_native_verdicts(spelling_store):
    """A numeric constant outside the native value model (past 64 bits for
    xsd:long) is refused by the native layer: the conjunct stays on the
    Python routes."""
    assert spelling_store._native_verdicts("num_lt", f'"1e400"^^<{XSD}double>') is None
    assert spelling_store._native_verdicts("num_lt", f'"9223372036854775808"^^<{XSD}long>') is None


def test_ctx_bound_constant_side_is_evaluated_by_rdflib():
    """The constant side of a comparison may be an expression over visible
    ctx-bound variables (BSBM Explore Q5's `?ref + N`): rdflib computes it."""
    from rdflib import Literal

    expr = filter_expr("?v < ?w + 10")
    shape = filters.native_shape(expr, V, {Variable("w"): Literal(5)})
    assert shape == ("num_lt", f'"15"^^<{XSD}integer>')
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


def test_mid_size_code_set_goes_to_a_file_scan_as_a_range(tmp_path, monkeypatch, capsys):
    """A file-backed store's scan would evaluate a keep of 33 to 4,096
    codes as one slow `list_contains`: the code set goes as its bounding
    range, and its codes are tested on the rows that range admits."""
    import json

    from rdflib import Graph

    from test_pushdown import both_ways

    nt = tmp_path / "ints.nt"
    nt.write_text(
        "".join(
            f'<http://ex.org/s{i}> <http://ex.org/p> "{i}"^^<{XSD}integer> .\n'
            f'<http://ex.org/s{i}> <http://ex.org/p> "label {i}" .\n'
            for i in range(200)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "ints.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    graph = Graph(store=VortexRdflibStore(str(out), in_memory=False))
    query = (
        "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> SELECT ?s ?v WHERE { "
        "?s <http://ex.org/p> ?v FILTER(datatype(?v) = xsd:integer && ?v < 100) }"
    )
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(graph, query)
    assert with_pushdown == without_pushdown
    assert len(with_pushdown) == 100
    trace = [
        json.loads(line[len(pd._TRACE_PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(pd._TRACE_PREFIX)
    ]
    pushed = next(e for e in trace if e["event"] == "bgp_native_restriction")
    assert pushed["keep"] == {"2": {"codes": 100}}
    match = next(
        e for e in trace if e["event"] == "native_call_complete" and e["operation"] == "match_codes"
    )
    assert set(match["keep"]["2"]) == {"range"}
