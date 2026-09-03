"""The fast FILTER predicates must agree with rdflib's own evaluator on every
value they answer: a matrix of literal spellings x expressions, each fast
answer other than UNKNOWN compared with `_ebv` over the same term. Two
matrices, one per route — one variable (the per-variable predicate applied to
a pattern scan) and every ordered pair of spellings for two (the row
predicate applied to joined rows)."""

import pytest
from rdflib import Graph
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.evalutils import _ebv
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.sparql import FrozenBindings, QueryContext
from rdflib.term import Variable
from rdflib.util import from_n3

from vortex_rdflib import filters
from vortex_rdflib.terms import BLANK, IRI, LITERAL, TermView, kind_bounds, kind_of, parse_spelling

XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIXES = (
    f"PREFIX xsd: <{XSD}> PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
    "PREFIX ex: <http://ex.org/> "
)
V = Variable("v")
W = Variable("w")


def typed(lexical: str, datatype: str) -> str:
    return f'"{lexical}"^^<{XSD}{datatype}>'


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
    None,
]

EXPRESSIONS = [
    "?v < 5",
    "?v <= 5",
    "?v > 5",
    "?v >= 5",
    "?v = 5",
    "?v != 5",
    "5 < ?v",
    "5 >= ?v",
    "?v < 5.5",
    "?v = 1e1",
    "?v < -1",
    "?v > 1e100",
    '?v < "5"',
    '?v = "5"',
    '?v != "5"',
    '?v = "5"@en',
    '?v = "abc"@EN',
    '?v = "x"^^<http://ex.org/dt>',
    "?v = <http://ex.org/x>",
    "?v != <http://ex.org/x>",
    "?v = true",
    "?v < true",
    '?v IN (5, "5", <http://ex.org/x>)',
    "?v NOT IN (5)",
    "?v IN ()",
    "sameTerm(?v, 5)",
    'sameTerm(?v, "abc"@en)',
    "datatype(?v) = xsd:integer",
    "datatype(?v) != xsd:string",
    "datatype(?v) = rdf:langString",
    'lang(?v) = "en"',
    'lang(?v) = ""',
    'lang(?v) != "en"',
    'langMatches(lang(?v), "EN")',
    'langMatches(lang(?v), "*")',
    'langMatches(lang(?v), "en-US")',
    "isIRI(?v)",
    "isURI(?v)",
    "isBlank(?v)",
    "isLiteral(?v)",
    "isNumeric(?v)",
    "bound(?v)",
    "!bound(?v)",
    'str(?v) = "5"',
    'str(?v) = "http://ex.org/x"',
    'strstarts(?v, "a")',
    'strstarts(str(?v), "http")',
    'contains(?v, "b")',
    'strends(str(?v), "x")',
    'strstarts(?v, "a"@en)',
    'contains(?v, "b"@fr)',
    'regex(?v, "^a")',
    'regex(str(?v), "^A", "i")',
    'regex(?v, "b$", "x")',
    'regex(?v, "a.b", "s")',
    "?v < 5 && isLiteral(?v)",
    "?v < 5 || isIRI(?v)",
    "!(?v < 5)",
    "?v > 1 && ?v < 100",
    "?v < 5 || datatype(?v) = xsd:string",
    "isIRI(?v) || ?v < 5",
    "!isLiteral(?v) && bound(?v)",
    "?v",
    "?v = ?v",
    "?v < ?v",
    "?v != ?v || isBlank(?v)",
    # integer arithmetic: the compiled `+`/`-` route, its result under the
    # builtins that inspect a term, and an operand that is itself a sum
    "?v + 1 > 3",
    "?v - 1 = 4",
    "?v + 1 - 2 > 0",
    "5 - ?v > 0",
    "?v + 1 > ?v",
    "?v + 1",
    "?v + 1 > 3 && ?v < 100",
    "datatype(?v + 1) = xsd:integer",
    "isNumeric(?v - 1)",
    "sameTerm(?v + 1, 6)",
    "?v + 1 IN (6, 7)",
]

#: Expressions over *two* block variables — the `tuple_predicate` route,
#: where a conjunct sees a whole binding tuple rather than one value. The
#: first is BSBM Explore Q5's similarity band, the shape the integer
#: arithmetic route exists for; the rest are the controls it must not break.
TWO_VAR_EXPRESSIONS = [
    "?v < ?w + 120 && ?v > ?w - 120",
    "?v + ?w > 10",
    "?v - ?w = 0",
    "?v - 1 > ?w",
    "?v + ?w + 1 > 10",
    "datatype(?v + ?w) = xsd:integer",
    "?v < ?w",
    "?v = ?w",
    "sameTerm(?v, ?w)",
]


def filter_expr(sparql_expr: str):
    query = translateQuery(
        parseQuery(f"{PREFIXES}SELECT * WHERE {{ ?s ?p ?v FILTER({sparql_expr}) }}")
    )
    node = query.algebra.p.p
    assert node.name == "Filter"
    return node.expr


# rdflib's own contexts always carry an initBindings mapping; an unbound
# variable is looked up there last (Graph.query defaults it to {}).
CTX = QueryContext(graph=Graph(), initBindings={})


RAISES = "rdflib raises"


def rdflib_answer(expr, spelling):
    bindings = {} if spelling is None else {V: from_n3(spelling)}
    try:
        return _ebv(expr, FrozenBindings(CTX, bindings))
    except Exception:  # noqa: BLE001 - rdflib's own bugs (e.g. NaN vs Decimal) are the oracle
        return RAISES


def fast_answer(predicate, spelling):
    view = None if spelling is None else parse_spelling(spelling)
    return predicate((view,))


# `"maybe"^^xsd:boolean` is in the matrix on purpose — an ill-typed literal
# the two routes must still agree on — and rdflib warns each time `from_n3`
# parses one. Scoped to this test so the warning stays a signal anywhere else.
@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
@pytest.mark.parametrize("sparql_expr", EXPRESSIONS)
def test_fast_route_agrees_with_rdflib(sparql_expr):
    expr = filter_expr(sparql_expr)
    predicate = filters.compile_fast(expr, (V,), {})
    assert predicate is not None, "expression is in the whitelist"
    answered = 0
    for spelling in SPELLINGS:
        fast = fast_answer(predicate, spelling)
        if fast is filters.UNKNOWN:
            continue
        answered += 1
        # Where rdflib itself raises, the fast route must have deferred, so
        # the query fails the same way with and without the pushdown.
        assert fast == rdflib_answer(expr, spelling), (sparql_expr, spelling)
    assert answered > 0, sparql_expr


@pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning")
@pytest.mark.parametrize("sparql_expr", TWO_VAR_EXPRESSIONS)
def test_two_variable_fast_route_agrees_with_rdflib(sparql_expr):
    """The same differential over every *ordered pair* of spellings — the
    route a conjunct takes when it references two block variables. It is also
    the only route on which one operand can be unbound while another carries
    a value rdflib would raise on."""
    expr = filter_expr(sparql_expr)
    predicate = filters.compile_fast(expr, (V, W), {})
    assert predicate is not None, "expression is in the whitelist"
    # 45x45 lookups of each spelling: parse every one once.
    terms = {s: None if s is None else from_n3(s) for s in SPELLINGS}
    views = {s: None if s is None else parse_spelling(s) for s in SPELLINGS}
    answered = 0
    for left in SPELLINGS:
        for right in SPELLINGS:
            fast = predicate((views[left], views[right]))
            if fast is filters.UNKNOWN:
                continue
            answered += 1
            bound = {var: terms[s] for var, s in ((V, left), (W, right)) if s is not None}
            try:
                expected = _ebv(expr, FrozenBindings(CTX, bound))
            except Exception:  # noqa: BLE001 - a raise is an answer the fast route must not give
                expected = RAISES
            assert fast == expected, (sparql_expr, left, right)
    assert answered > 0, sparql_expr


@pytest.mark.parametrize(
    ("sparql_expr", "spelling", "expected"),
    [
        ("?v < 5", typed("5", "integer"), False),
        ("?v <= 5", typed("5", "integer"), True),
        ("?v < 5", typed("01", "integer"), True),
        ("?v < 5", typed("1.5", "decimal"), True),
        ("?v < 5", typed("NaN", "double"), True),  # rdflib: not gt and not eq
        ("?v > 1", typed("1e2", "double"), True),
        ("?v = 5", typed("5", "decimal"), True),
        ("?v < 5", '"abc"', False),  # xsd:string sorts after every numeric datatype IRI
        ("?v > 5", '"abc"', True),
        ("?v = <http://ex.org/x>", "<http://ex.org/x>", True),
        ("?v = <http://ex.org/x>", '"http://ex.org/x"', False),
        ("datatype(?v) = xsd:integer", typed("abc", "integer"), True),
        ('lang(?v) = "en"', '"abc"@en', True),
        ('langMatches(lang(?v), "EN")', '"abc"@en-us', True),
        ("isNumeric(?v)", typed("abc", "integer"), True),
        ("isNumeric(?v)", None, False),
        ("bound(?v)", None, False),
        ('regex(str(?v), "^A", "i")', '"abc"', True),
        ('strstarts(str(?v), "http")', "<http://ex.org/x>", True),
        ("?v < 5 && isLiteral(?v)", "<http://ex.org/x>", False),
        ("?v + 1 > 3", typed("5", "integer"), True),
        ("?v - 10 < 0", typed("7", "byte"), True),  # promoted to xsd:integer, as rdflib
        ("?v + 1 = 6", typed("5", "unsignedInt"), True),
    ],
)
def test_fast_route_answers_the_common_shapes(sparql_expr, spelling, expected):
    """The shapes the benchmark queries use must not fall back to rdflib."""
    predicate = filters.compile_fast(filter_expr(sparql_expr), (V,), {})
    assert predicate is not None
    assert fast_answer(predicate, spelling) is expected


@pytest.mark.parametrize(
    ("sparql_expr", "spelling"),
    [
        ("?v < 5", typed("abc", "integer")),  # ill-typed: rdflib's own ordering rules
        ("?v < 5", typed("300", "byte")),  # out of range: ill-typed
        ('str(?v) = "5"', typed("05", "integer")),  # normalized lexical form
        ("?v = 5", typed("1.5", "integer")),
        ("?v < 5", typed("2020-01-01", "date")),
        ("?v", "<http://ex.org/x>"),
    ],
)
def test_fast_route_defers_outside_its_domain(sparql_expr, spelling):
    predicate = filters.compile_fast(filter_expr(sparql_expr), (V,), {})
    assert predicate is not None
    answer = fast_answer(predicate, spelling)
    assert answer is filters.UNKNOWN or answer == rdflib_answer(filter_expr(sparql_expr), spelling)


@pytest.mark.parametrize(
    "sparql_expr",
    [
        "coalesce(?v, 0) = 1",
        "regex(?v, str(?v))",
        'ucase(?v) = "A"',
    ],
)
def test_outside_the_whitelist_is_not_compiled(sparql_expr):
    assert filters.compile_fast(filter_expr(sparql_expr), (V,), {}) is None


@pytest.mark.parametrize(
    "spelling",
    [
        typed("2.5", "decimal"),  # rdflib promotes to xsd:decimal, not integer
        typed("1e2", "double"),
        typed("abc", "integer"),  # ill-typed: rdflib computes with the lexical form
        typed("300", "byte"),  # out of range: rdflib computes with the value anyway
        typed("2020-01-01", "date"),  # rdflib's date arithmetic
        "<http://ex.org/x>",
        None,
    ],
)
def test_additive_outside_the_integer_domain_defers(spelling):
    """Every operand rdflib would treat by a rule the fast route does not
    reproduce has to come back UNKNOWN, not a guess: the value then goes to
    rdflib alone, and a query that raises there raises with the pushdown too."""
    predicate = filters.compile_fast(filter_expr("?v + 1 > 3"), (V,), {})
    assert predicate is not None
    assert fast_answer(predicate, spelling) is filters.UNKNOWN


def test_ctx_bound_constants_are_substituted():
    expr = filter_expr("?v = ?w")
    view = parse_spelling(typed("5", "integer"))
    predicate = filters.compile_fast(expr, (V,), {Variable("w"): view})
    assert predicate is not None
    assert fast_answer(predicate, typed("05", "integer")) is True
    assert fast_answer(predicate, typed("6", "integer")) is False
    unbound = filters.compile_fast(expr, (V,), {})
    assert unbound is not None
    assert fast_answer(unbound, typed("5", "integer")) is False  # unbound ?w is an error


def test_parse_spelling_round_trips_escapes():
    assert parse_spelling('"a\\"b\\n\\u0041"') == TermView(LITERAL, 'a"b\nA')
    assert parse_spelling('"x"@en-us') == TermView(LITERAL, "x", lang="en-us")
    assert parse_spelling(typed("5", "integer")) == TermView(LITERAL, "5", dt=XSD + "integer")
    assert parse_spelling('"a\\"^^<b"^^<http://ex.org/dt>') == TermView(
        LITERAL, 'a"^^<b', dt="http://ex.org/dt"
    )
    assert parse_spelling("<http://ex.org/x>") == TermView(IRI, "http://ex.org/x")
    assert parse_spelling("_:b0") == TermView(BLANK, "b0")


def test_canonical_spelling_round_trips_every_dictionary_term(tmp_path):
    """For every code of a store with escapes, tags and non-ASCII terms:
    encode(canonical_spelling(decoded term)) is the code again."""
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore
    from vortex_rdflib.terms import canonical_spelling

    nt = tmp_path / "spellings.nt"
    nt.write_text(
        '<http://ex.org/a> <http://ex.org/p> "tab\\there" .\n'
        '<http://ex.org/a> <http://ex.org/p> "line\\nbreak\\r" .\n'
        '<http://ex.org/a> <http://ex.org/p> "quote\\"q\\\\bs" .\n'
        '<http://ex.org/a> <http://ex.org/p> "\\u0001\\u001Fcontrols\\u007F" .\n'
        '<http://ex.org/a> <http://ex.org/p> "\u00fcn\u00efc\u00f6d\u00e9 \U0001f600" .\n'
        '<http://ex.org/a> <http://ex.org/p> "x"@EN-us .\n'
        '<http://ex.org/a> <http://ex.org/p> "x"^^<http://www.w3.org/2001/XMLSchema#string> .\n'
        '<http://ex.org/a> <http://ex.org/p> "042"^^<http://www.w3.org/2001/XMLSchema#integer> .\n'
        '<http://ex.org/a> <http://ex.org/p> "1.50"^^<http://www.w3.org/2001/XMLSchema#decimal> .\n'
        '<http://ex.org/a> <http://ex.org/p> "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .\n'
        '<http://ex.org/a> <http://ex.org/p> "x"^^<http://ex.org/dt> .\n'
        "<http://ex.org/\u00fc> <http://ex.org/p> _:b0 .\n"
        '_:b0 <http://ex.org/p> "" .\n',
        encoding="utf-8",
    )
    out = tmp_path / "spellings.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out))
    term_dict = store._dict
    assert term_dict is not None
    for code in range(len(term_dict)):
        spelling = term_dict.decode(code)
        assert spelling is not None
        if spelling == "":
            continue  # the default graph name is no RDF term
        term = store._from_n3_safe(spelling)
        if str(term) != parse_spelling(spelling).lex:
            continue  # rdflib normalized the lexical form ("042" -> "42"): no round trip
        assert canonical_spelling(term) == spelling, (code, spelling, term)
        assert term_dict.encode(canonical_spelling(term)) == code


def test_kind_bounds_partition_the_dictionary(tmp_path):
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore

    nt = tmp_path / "kinds.nt"
    nt.write_text(
        '<http://ex.org/a> <http://ex.org/p> "lit" .\n'
        "<http://ex.org/a> <http://ex.org/p> <http://ex.org/b> .\n"
        "_:x <http://ex.org/p> _:y .\n"
        '<http://ex.org/a> <http://ex.org/q> "z"@en .\n',
        encoding="utf-8",
    )
    out = tmp_path / "kinds.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out))
    term_dict = store._dict
    assert term_dict is not None
    bounds = kind_bounds(term_dict)
    assert bounds == store._term_kind_bounds()
    for code in range(len(term_dict)):
        spelling = term_dict.decode(code)
        assert spelling is not None
        expected = {'"': LITERAL, "<": IRI, "_": BLANK}.get(spelling[:1], "")
        assert kind_of(code, bounds) == expected, (code, spelling)
