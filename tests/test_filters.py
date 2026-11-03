"""The fast FILTER predicates answer as SPARQL 1.1 does on every value: a
matrix of literal spellings x expressions, each fast answer compared with
Oxigraph's (:mod:`tests.oracle`), where Oxigraph follows the specification.
Two matrices, one per route — one variable (the per-variable predicate
applied to a pattern scan) and every ordered pair of spellings for two (the
row predicate applied to joined rows). No value is left to rdflib."""

import pytest
from rdflib import Graph
from rdflib.term import Literal, URIRef, Variable
from tests.corpus import SPELLINGS
from tests.oracle import departs, spec_answer
from tests.support import V, W, filter_expr, typed

from vortex_rdflib import filters
from vortex_rdflib.terms import BLANK, IRI, LITERAL, KindRanges, TermView, parse_spelling

XSD = "http://www.w3.org/2001/XMLSchema#"


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


def fast_answer(predicate, spelling):
    view = None if spelling is None else parse_spelling(spelling)
    return predicate((view,))


@pytest.mark.parametrize("sparql_expr", EXPRESSIONS)
def test_fast_route_follows_the_spec(sparql_expr):
    expr = filter_expr(sparql_expr)
    predicate = filters.compiler.compile_fast(expr, (V,), {})
    assert predicate is not None, "expression is in the whitelist"
    checked = 0
    for spelling in SPELLINGS:
        fast = fast_answer(predicate, spelling)
        assert fast is not filters.values.UNKNOWN, (sparql_expr, spelling)
        if departs(sparql_expr, spelling):
            continue
        checked += 1
        assert fast == spec_answer(sparql_expr, {"v": spelling}), (sparql_expr, spelling)
    assert checked > 0, sparql_expr


@pytest.mark.parametrize("sparql_expr", TWO_VAR_EXPRESSIONS)
def test_two_variable_fast_route_follows_the_spec(sparql_expr):
    """The same check over every *ordered pair* of spellings — the route a
    conjunct takes when it references two block variables. It is also the
    only route on which one operand can be unbound while another carries a
    value."""
    expr = filter_expr(sparql_expr)
    predicate = filters.compiler.compile_fast(expr, (V, W), {})
    assert predicate is not None, "expression is in the whitelist"
    # 46x46 lookups of each spelling: parse every one once.
    views = {s: None if s is None else parse_spelling(s) for s in SPELLINGS}
    checked = 0
    for left in SPELLINGS:
        for right in SPELLINGS:
            fast = predicate((views[left], views[right]))
            assert fast is not filters.values.UNKNOWN, (sparql_expr, left, right)
            if departs(sparql_expr, left, right):
                continue
            checked += 1
            expected = spec_answer(sparql_expr, {"v": left, "w": right})
            assert fast == expected, (sparql_expr, left, right)
    assert checked > 0, sparql_expr


@pytest.mark.parametrize(
    ("sparql_expr", "spelling", "expected"),
    [
        ("?v < 5", typed("5", "integer"), False),
        ("?v <= 5", typed("5", "integer"), True),
        ("?v < 5", typed("01", "integer"), True),
        ("?v < 5", typed("1.5", "decimal"), True),
        ("?v < 5", typed("NaN", "double"), False),  # NaN is less than nothing
        ("?v > 1", typed("1e2", "double"), True),
        ("?v = 5", typed("5", "decimal"), True),
        ("?v < 5", '"abc"', False),  # a string and a number do not order: an error
        ("?v > 5", '"abc"', False),
        ("?v = <http://ex.org/x>", "<http://ex.org/x>", True),
        ("?v = <http://ex.org/x>", '"http://ex.org/x"', False),
        ("datatype(?v) = xsd:integer", typed("abc", "integer"), True),
        ('lang(?v) = "en"', '"abc"@en', True),
        ('langMatches(lang(?v), "EN")', '"abc"@en-us', True),
        ("isNumeric(?v)", typed("abc", "integer"), False),  # ill-typed: no number
        ("isNumeric(?v)", None, False),
        ("bound(?v)", None, False),
        ('regex(str(?v), "^A", "i")', '"abc"', True),
        ('strstarts(str(?v), "http")', "<http://ex.org/x>", True),
        ("?v < 5 && isLiteral(?v)", "<http://ex.org/x>", False),
        ("?v + 1 > 3", typed("5", "integer"), True),
        ("?v - 10 < 0", typed("7", "byte"), True),  # a derived type sums as xsd:integer
        ("?v + 1 = 6", typed("5", "unsignedInt"), True),
    ],
)
def test_fast_route_answers_the_common_shapes(sparql_expr, spelling, expected):
    """The shapes the benchmark queries use must not fall back to rdflib."""
    predicate = filters.compiler.compile_fast(filter_expr(sparql_expr), (V,), {})
    assert predicate is not None
    assert fast_answer(predicate, spelling) is expected


@pytest.mark.parametrize(
    "sparql_expr",
    [
        "coalesce(?v, 0) = 1",
        "regex(?v, str(?v))",
        'ucase(?v) = "A"',
    ],
)
def test_outside_the_whitelist_is_not_compiled(sparql_expr):
    assert filters.compiler.compile_fast(filter_expr(sparql_expr), (V,), {}) is None


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        (typed("2.5", "decimal"), True),  # 3.5, an xsd:decimal
        (typed("1e2", "double"), True),
        (typed("2.5", "float"), True),
        (typed("NaN", "double"), False),  # NaN + 1 is NaN, greater than nothing
        (typed("abc", "integer"), False),  # ill-typed: no number, an error
        (typed("300", "byte"), False),  # out of range: ill-typed
        (typed("2020-01-01", "date"), False),  # no date arithmetic in SPARQL 1.1
        ('"5"', False),
        ("<http://ex.org/x>", False),
        (None, False),
    ],
)
def test_additive_follows_the_numeric_tower(spelling, expected):
    """``+`` and ``-`` answer every operand themselves (§17.3): numbers in the
    wider of their types, anything else an error. None goes to rdflib."""
    predicate = filters.compiler.compile_fast(filter_expr("?v + 1 > 3"), (V,), {})
    assert predicate is not None
    assert fast_answer(predicate, spelling) is expected


#: Sums the specification types but leaves the lexical form of open: the
#: fast route writes the form rdflib's evaluator writes for the same value.
SUMS = [
    (typed("1.5", "decimal"), typed("4.5", "decimal")),
    (typed("5", "decimal"), typed("1", "integer")),
    (typed("5", "float"), typed("1", "integer")),
    (typed("0.25", "float"), typed("0", "integer")),
    (typed("1e2", "double"), typed("1", "integer")),
    (typed("1e100", "double"), typed("0", "integer")),
    (typed("7", "byte"), typed("1", "byte")),
    (typed("-3", "integer"), typed("10", "integer")),
]


@pytest.mark.parametrize(("left", "right"), SUMS)
def test_a_sum_is_spelled_as_rdflib_spells_it(left, right):
    values = filters.values
    total = values.add(
        values.number(parse_spelling(left)), values.number(parse_spelling(right)), False
    )
    [row] = Graph().query(f"SELECT ?r WHERE {{ BIND({left} + {right} AS ?r) }}")
    assert values.number_term(total) == parse_spelling(row[0].n3())


@pytest.mark.parametrize(
    "sparql_expr",
    [
        '?v < "300"^^xsd:byte',
        '?v > "-1"^^xsd:nonNegativeInteger',
        '"256"^^xsd:unsignedByte >= ?v',
    ],
)
def test_an_out_of_range_typed_constant_orders_nothing(sparql_expr):
    """A typed constant is read as written, so one outside its type's range is
    ill-typed: it has no value, and every ordering with it is an error (rdflib's
    parser leaves it unflagged and compares the number)."""
    predicate = filters.compiler.compile_fast(filter_expr(sparql_expr), (V,), {})
    assert predicate is not None
    for spelling in SPELLINGS:
        assert fast_answer(predicate, spelling) is False, spelling


def test_an_out_of_range_typed_constant_equals_only_itself():
    predicate = filters.compiler.compile_fast(filter_expr('?v = "300"^^xsd:byte'), (V,), {})
    assert predicate is not None
    for spelling in SPELLINGS:
        assert fast_answer(predicate, spelling) is (spelling == typed("300", "byte")), spelling


def test_ctx_bound_constants_are_substituted():
    expr = filter_expr("?v = ?w")
    five = Literal("5", datatype=URIRef(f"{XSD}integer"))
    predicate = filters.compiler.compile_fast(expr, (V,), {Variable("w"): five})
    assert predicate is not None
    assert fast_answer(predicate, typed("05", "integer")) is True
    assert fast_answer(predicate, typed("6", "integer")) is False
    unbound = filters.compiler.compile_fast(expr, (V,), {})
    assert unbound is not None
    assert fast_answer(unbound, typed("5", "integer")) is False  # unbound ?w is an error


def test_fast_reject_predicate_keeps_a_row_the_filter_keeps_over_an_unbound_variable(tmp_path):
    """The mid-join pre-filter may drop only rows the FILTER drops. With `?w`
    unbound, `!bound(?w) || ?v < ?w` holds, so the row is kept — the
    missing code is not looked up as a term."""
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore

    five = typed("5", "integer")
    nt = tmp_path / "five.nt"
    nt.write_text(f"<http://ex.org/s> <http://ex.org/p> {five} .\n", encoding="utf-8")
    out = tmp_path / "five.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out))
    dictionary = store._dict
    assert dictionary is not None  # the code path is active
    expr = filter_expr("!bound(?w) || ?v < ?w")
    conjunct = filters.Conjunct(
        expr, (V, W), filters.compiler.compile_fast(expr, (V, W), {}), False
    )
    keep = filters.tuple_fast_reject_predicate(store, conjunct, [0, 1])
    assert keep((dictionary.encode(five), None)) is True


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


def test_kind_ranges_partition_the_dictionary(tmp_path):
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
    ranges = KindRanges.from_dictionary(term_dict)
    opened = store._file
    assert opened is not None and opened.kind_ranges is not None
    assert [opened.kind_ranges.range(k) for k in (LITERAL, IRI, BLANK)] == [
        ranges.range(k) for k in (LITERAL, IRI, BLANK)
    ]
    for code in range(len(term_dict)):
        spelling = term_dict.decode(code)
        assert spelling is not None
        expected = {'"': LITERAL, "<": IRI, "_": BLANK}.get(spelling[:1], "")
        assert ranges.kind_of(code) == expected, (code, spelling)


#: An XML Schema type SPARQL 1.1 names no operator for: what its values equal
#: and how they order is left to the implementation (§17.4.1.7), and rdflib's
#: evaluator decides it, as it always has. What SPARQL 1.1 does fix holds:
#: no ordering against another type, no effective boolean value.
YEAR = typed("2020", "gYear")


@pytest.mark.parametrize(
    ("sparql_expr", "expected"),
    [
        ("?v != 5", filters.values.UNKNOWN),
        ('?v = "2020"', filters.values.UNKNOWN),
        ('?v = "2021"^^xsd:gYear', filters.values.UNKNOWN),
        ('?v < "2021"^^xsd:gYear', filters.values.UNKNOWN),
        ("?v IN (1, 2)", filters.values.UNKNOWN),
        ('?v = "2020"^^xsd:gYear', True),  # the same term
        ('?v = "x"@en', False),  # a language-tagged string equals no other literal
        ("?v = <http://ex.org/x>", False),
        ('?v = "x"^^<http://ex.org/dt>', False),  # an error: an unknown datatype
        ("?v < 5", False),  # no ordering across types: an error
        ('?v < "12:00:00"^^xsd:time', False),
        ("?v", False),  # no effective boolean value: an error
        ("datatype(?v) = xsd:gYear", True),
    ],
)
def test_a_type_sparql_leaves_open_is_compared_by_rdflib(sparql_expr, expected):
    predicate = filters.compiler.compile_fast(filter_expr(sparql_expr), (V,), {})
    assert predicate is not None
    assert fast_answer(predicate, YEAR) is expected


def test_a_filter_over_rdflib_rows_keeps_nothing_per_row(vortex_files):
    """The FILTER above a block rdflib solved tests each solution from its
    own terms: it registers none of them with the query and memoizes no
    answer, so the query's memory does not grow with the rows it filters."""
    from rdflib.plugins.sparql.algebra import translateQuery
    from rdflib.plugins.sparql.parser import parseQuery
    from rdflib.plugins.sparql.sparql import FrozenBindings, QueryContext

    from vortex_rdflib import VortexRdflibStore, filters, query_scope

    store = VortexRdflibStore(str(vortex_files["dictionary"]))
    node = translateQuery(parseQuery("SELECT * WHERE { ?s ?p ?v FILTER(?v > 10) }")).algebra.p.p
    ctx = QueryContext(graph=Graph(store=store), initBindings={})
    v = Variable("v")
    token = query_scope.enter(query_scope.QueryScope())
    try:
        keep = filters.solution_filter(store, node, ctx)
        kept = [n for n in range(100) if keep(FrozenBindings(ctx, {v: Literal(n)}))]
        terms = store._terms()
        assert kept == list(range(11, 100))
        assert terms.foreign == {} and terms.foreign_codes == {}
    finally:
        query_scope.leave(token)
