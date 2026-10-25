"""Typed literals alias where rdflib's normalization makes two spellings one
term (``"042"^^xsd:integer`` and ``"42"^^xsd:integer`` are both ``42``).
The alias pass of ``DISTINCT`` and ``COUNT`` builds only the spellings rdflib
may rewrite, and finds each one's canonical twin by its spelling: the
by-spelling check is compared with rdflib's normalization over generated
lexical forms, and the alias classes with rdflib's term equality over a
generated store."""

import logging
import random

import pytest
import rdflib
from rdflib import Graph, Literal, URIRef
from rdflib.term import _toPythonMapping
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import both_ways, run
from vortex_rdflib import VortexRdflibStore, query_scope
from vortex_rdflib.terms import LITERAL, canonical_spelling, escape, parse_spelling

#: rdflib remarks on the ill-typed values generated here on purpose.
pytestmark = [
    pytest.mark.filterwarnings("ignore:Parsing weird boolean:UserWarning"),
    pytest.mark.filterwarnings("ignore:Serializing weird numerical:UserWarning"),
]

XSD = "http://www.w3.org/2001/XMLSchema#"
INTEGER = f"{XSD}integer"

#: Datatypes rdflib has no converter for: ``Literal`` keeps their lexical
#: forms as written.
UNCONVERTED = (
    "http://ex.org/dt",
    "urn:x-unit:metre",
    f"{XSD}NCName",
    "http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/USD",
    "http://www.opengis.net/ont/geosparql#wktLiteral",
)
#: Datatypes rdflib converts, besides xsd:integer.
CONVERTED = tuple(
    f"{XSD}{name}"
    for name in ("int", "decimal", "double", "boolean", "date", "token", "normalizedString")
)
#: Lexical forms of the converted datatypes, several of them spellings of one value.
CONVERTED_FORMS = (
    "1.50", "1.5", "01.5", "+1.5", "1e2", "100.0", "1.0E2", "true", "1", "false", "0",
    "2020-01-01", " 7 ", "a  b", "x\ty",
)  # fmt: skip


def _integer_form(rng: random.Random) -> str:
    """A lexical form around xsd:integer's canonical ones: as is, signed,
    zero-padded, padded with whitespace, with an underscore, a fraction or an
    exponent, or random characters of those and non-ASCII digits."""
    digits = str(
        rng.randrange(30) if rng.random() < 0.5 else rng.randrange(10 ** rng.randint(1, 25))
    )
    return rng.choice(
        (
            digits,
            f"-{digits}",
            f"+{digits}",
            f"0{digits}",
            f"-0{digits}",
            f" {digits}",
            f"{digits}\n",
            f"{digits[0]}_{digits}",
            f"{digits}.0",
            f"{digits}e1",
            "".join(rng.choice("0123456789+-_. e٤４") for _ in range(rng.randint(0, 6))),
        )
    )


def _text(rng: random.Random) -> str:
    """Random text: digits, signs, quotes, backslashes, whitespace, controls,
    non-ASCII."""
    alphabet = 'az09 -+."\\\n\té日\U0001f600\x01\x7f'
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))


def _typed(lexical: str, datatype: str) -> str:
    """The store's spelling of a typed literal."""
    return f'"{escape(lexical)}"^^<{datatype}>'


def _reference_term(spelling: str) -> Literal:
    """The term rdflib's constructors make from a literal spelling."""
    view = parse_spelling(spelling)
    assert view.kind == LITERAL and view.lex is not None
    if view.dt is not None:
        return Literal(view.lex, datatype=URIRef(view.dt))
    return Literal(view.lex, lang=view.lang)


def test_the_by_spelling_check_agrees_with_rdflib(caplog):
    """`_rewritable` passes over a typed spelling only if rdflib keeps it as
    written, and over the datatypes it decides by spelling it misses none
    rdflib keeps with a value: it passes over every canonical xsd:integer
    form and every form of a datatype rdflib cannot convert. It builds the
    other xsd:integer forms, ill-typed ones included (kept, with no value),
    and every form of the other datatypes."""
    caplog.set_level(logging.ERROR, logger="rdflib.term")  # an ill-typed form logs a warning
    converted = {str(datatype) for datatype in _toPythonMapping if datatype is not None}
    assert {INTEGER, *CONVERTED} <= converted and not converted & set(UNCONVERTED)
    rng = random.Random(20261013)
    cases = [(_integer_form(rng), INTEGER) for _ in range(2500)]
    # Canonical, but past int()'s digit limit: rdflib keeps it with no value.
    huge = len(cases)
    cases.append(("1" + "0" * 5000, INTEGER))
    cases += [(_text(rng), rng.choice(UNCONVERTED)) for _ in range(1000)]
    cases += [
        (rng.choice((_integer_form(rng), _text(rng), *CONVERTED_FORMS)), rng.choice(CONVERTED))
        for _ in range(1000)
    ]
    spellings = [_typed(lexical, datatype) for lexical, datatype in cases]
    built = {i for i, _ in pd.aliasing._rewritable(range(len(spellings)), spellings)}
    counts = {"skipped": 0, "rewritten": 0, "ill-typed": 0}
    for i, ((lexical, datatype), spelling) in enumerate(zip(cases, spellings, strict=True)):
        term = Literal(lexical, datatype=URIRef(datatype))
        kept = str(term) == lexical
        if i not in built:
            # Never passes over a spelling rdflib rewrites.
            assert kept and canonical_spelling(term) == spelling, spelling
        if datatype in UNCONVERTED:
            assert i not in built, spelling
        elif datatype == INTEGER:
            if kept and term.value is not None:
                # Misses none rdflib keeps.
                assert i not in built, spelling
                counts["skipped"] += 1
            elif kept:
                counts["ill-typed"] += 1
            else:
                counts["rewritten"] += 1
        else:
            assert i in built, spelling
    assert huge not in built
    assert counts["skipped"] > 400 and counts["rewritten"] > 1000 and counts["ill-typed"] > 400


def test_without_rdflibs_converter_table_every_typed_spelling_is_built(monkeypatch):
    """With no ``_toPythonMapping`` to read, as in an rdflib release without
    it, nothing is known to be kept as written: the alias pass builds every
    typed spelling."""
    spellings = [
        f'"42"^^<{INTEGER}>',
        f'"042"^^<{INTEGER}>',
        '"x"^^<http://ex.org/dt>',
        f'"1.5"^^<{XSD}decimal>',
        '"plain"',
        '"x"@en',
    ]
    assert [i for i, _ in pd.aliasing._rewritable(range(6), spellings)] == [1, 3]
    monkeypatch.setattr(pd.aliasing, "_toPythonMapping", None)
    assert [i for i, _ in pd.aliasing._rewritable(range(6), spellings)] == [0, 1, 2, 3]


@pytest.fixture(scope="module")
def generated_store(tmp_path_factory) -> VortexRdflibStore:
    """A store whose objects are generated typed literals — integer forms and
    the canonical twins of some, forms of converted datatypes, forms of
    unconverted ones — and a few plain and language-tagged literals."""
    rng = random.Random(1307)
    logger = logging.getLogger("rdflib.term")
    level = logger.level
    logger.setLevel(logging.ERROR)  # an ill-typed form logs a warning
    try:
        objects = []
        for _ in range(600):
            roll = rng.random()
            if roll < 0.55:
                lexical, datatype = _integer_form(rng), INTEGER
            elif roll < 0.8:
                lexical, datatype = rng.choice(CONVERTED_FORMS), rng.choice(CONVERTED)
            else:
                lexical, datatype = _text(rng), rng.choice(UNCONVERTED)
            objects.append(_typed(lexical, datatype))
            if rng.random() < 0.4:
                objects.append(canonical_spelling(Literal(lexical, datatype=URIRef(datatype))))
    finally:
        logger.setLevel(level)
    objects += ['"plain"', '"42"', '"x"@en', '"x"@en-us']
    d = tmp_path_factory.mktemp("aliases")
    nt = d / "aliases.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p> {spelling} .\n"
            for i, spelling in enumerate(objects)
        ),
        encoding="utf-8",
    )
    out = d / "aliases.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return VortexRdflibStore(str(out), in_memory=True)


def _partition(codes, key) -> set:
    classes: dict = {}
    for code in codes:
        classes.setdefault(key(code), set()).add(code)
    return {frozenset(members) for members in classes.values()}


@pytest.mark.parametrize("calls", ["one", "reversed", "shuffled-chunks"])
def test_alias_classes_are_rdflibs_term_equality(generated_store, caplog, calls):
    """Two codes, from the dictionary or query constants outside it, share an
    alias key exactly when rdflib's terms for them are equal: whether the
    codes are classified in one call or across a query's calls, in any
    order."""
    caplog.set_level(logging.ERROR, logger="rdflib.term")
    store = generated_store
    literal_lo, iri_lo, _ = store._term_kind_bounds()
    local = list(range(literal_lo, iri_lo))
    spellings = dict(zip(local, store._dict.decode_many(local), strict=True))
    reference = {code: _reference_term(spelling) for code, spelling in spellings.items()}
    typed = sorted({t for t in reference.values() if t.datatype is not None}, key=str)
    rng = random.Random(calls)
    # Query constants the dictionary lacks: some equal to stored terms read
    # from another spelling, some new.
    constants = rng.sample(typed, 40) + [
        Literal("123456789", datatype=URIRef(INTEGER)),
        Literal("-77", datatype=URIRef(INTEGER)),
        Literal("never", datatype=URIRef(UNCONVERTED[0])),
    ]
    scope = query_scope.QueryScope()
    token = query_scope.enter(scope)
    try:
        foreign_terms = [
            term for term in constants if store._dict.encode(canonical_spelling(term)) is None
        ]
        foreign = {store._foreign_code(term): term for term in foreign_terms}
        codes = local + list(foreign)
        if calls == "reversed":
            batches = [codes[::-1]]
        elif calls == "shuffled-chunks":
            shuffled = rng.sample(codes, len(codes))
            batches = [shuffled[i : i + 37] for i in range(0, len(shuffled), 37)]
        else:
            batches = [codes]
        aliases: dict = {}
        for batch in batches:
            pd.aliasing._alias_codes(store, batch, store._terms().foreign, aliases)
    finally:
        query_scope.leave(token)
    expected = _partition(codes, lambda c: reference[c] if c >= 0 else foreign[c])
    assert len(foreign) > 5 and sum(len(members) > 1 for members in expected) > 50
    assert _partition(codes, lambda c: aliases.get(c, c)) == expected


def test_query_constants_look_up_no_twin(generated_store, monkeypatch):
    """A query constant's code is negative because its canonical spelling is
    not in the dictionary, so the alias pass looks no twin up for it."""
    store = generated_store
    lookups = []

    class _Lookups:
        def __init__(self, inner):
            self._inner = inner

        def encode_many(self, spellings):
            lookups.extend(spellings)
            return self._inner.encode_many(spellings)

        def encode(self, spelling):
            lookups.append(spelling)
            return self._inner.encode(spelling)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    scope = query_scope.QueryScope()
    token = query_scope.enter(scope)
    try:
        constants = [
            Literal("123456789", datatype=URIRef(INTEGER)),
            Literal("1.25", datatype=URIRef(f"{XSD}decimal")),
            Literal("never", datatype=URIRef(UNCONVERTED[0])),
        ]
        assert all(store._dict.encode(canonical_spelling(term)) is None for term in constants)
        codes = [store._foreign_code(term) for term in constants]
        monkeypatch.setattr(store, "_dict", _Lookups(store._dict))
        aliases: dict = {}
        pd.aliasing._alias_codes(store, codes, store._terms().foreign, aliases)
    finally:
        query_scope.leave(token)
    assert len(set(codes)) == 3 and all(code < 0 for code in codes)
    assert lookups == [] and aliases == {}


def test_canonical_twins_look_up_alone_after_a_rejected_batch_and_check_the_term(
    tmp_path, monkeypatch
):
    """When ``encode_many`` rejects the batch, each spelling is looked up on
    its own, and one the parser rejects finds nothing; a code found counts as
    a twin only if it decodes to the same term."""
    nt = tmp_path / "twins.nt"
    nt.write_text(
        f'<http://ex.org/s> <http://ex.org/p> "42"^^<{INTEGER}> .\n'
        '<http://ex.org/s> <http://ex.org/p> "x"@en .\n',
        encoding="utf-8",
    )
    out = tmp_path / "twins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    real = store._dict
    assert real is not None
    encode = real.encode
    forty_two = Literal("042", datatype=URIRef(INTEGER))  # rdflib reads 42
    rejected = Literal("7", datatype=URIRef("http://ex.org/dt"))
    read = [(forty_two, f'"042"^^<{INTEGER}>'), (rejected, '"07"^^<http://ex.org/dt>')]

    class _Stub:
        def __init__(self, encode_many, encode):
            self.encode_many, self.encode = encode_many, encode

        def __getattr__(self, name):
            return getattr(real, name)

    def reject_batch(spellings):
        raise ValueError("a spelling of the batch is not a term")

    def reject_one(spelling):
        if spelling == canonical_spelling(rejected):
            raise ValueError("not a term")
        return encode(spelling)

    monkeypatch.setattr(store, "_dict", _Stub(reject_batch, reject_one))
    assert pd.aliasing._canonical_twins(store, read) == {forty_two: encode(f'"42"^^<{INTEGER}>')}

    def another_term(spellings):
        return [encode('"x"@en') for _ in spellings]

    monkeypatch.setattr(store, "_dict", _Stub(another_term, encode))
    assert pd.aliasing._canonical_twins(store, read[:1]) == {}


def test_the_alias_pass_builds_only_spellings_rdflib_may_rewrite(tmp_path, monkeypatch):
    """Canonical xsd:integer literals and those of a datatype rdflib cannot
    convert are classified by spelling, without a term; a spelling rdflib
    rewrites is built, and so is the canonical twin it finds."""
    lines = [
        f'"1"^^<{INTEGER}>',
        f'"-3"^^<{INTEGER}>',
        f'"42"^^<{INTEGER}>',
        f'"042"^^<{INTEGER}>',
        f'"+007"^^<{INTEGER}>',
        '"x"^^<http://ex.org/dt>',
        f'"1.5"^^<{XSD}decimal>',
        '"plain"',
        '"x"@en',
    ]
    nt = tmp_path / "built.nt"
    nt.write_text(
        "".join(f"<http://ex.org/s{i}> <http://ex.org/p> {o} .\n" for i, o in enumerate(lines)),
        encoding="utf-8",
    )
    out = tmp_path / "built.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    assert store._dict is not None
    built = []
    original = store._from_n3_safe
    monkeypatch.setattr(store, "_from_n3_safe", lambda s: built.append(s) or original(s))
    literal_lo, iri_lo, _ = store._term_kind_bounds()
    codes = list(range(literal_lo, iri_lo))
    scope = query_scope.QueryScope()
    token = query_scope.enter(scope)
    try:
        aliases: dict = {}
        pd.aliasing._alias_codes(store, codes, {}, aliases)
    finally:
        query_scope.leave(token)
    assert sorted(built) == sorted(
        [f'"042"^^<{INTEGER}>', f'"+007"^^<{INTEGER}>', f'"1.5"^^<{XSD}decimal>']
        + [f'"42"^^<{INTEGER}>']  # 042's canonical twin, decoded to confirm it
    )
    code_of = dict(zip(store._dict.decode_many(codes), codes, strict=True))
    assert aliases == {code_of[f'"042"^^<{INTEGER}>']: code_of[f'"42"^^<{INTEGER}>']}
    graph = Graph(store=store)
    for sparql in (
        "SELECT DISTINCT ?o WHERE { ?s <http://ex.org/p> ?o }",
        "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE { ?s <http://ex.org/p> ?o }",
    ):
        with_pushdown, without_pushdown = both_ways(graph, sparql)
        assert with_pushdown == without_pushdown, sparql


def test_without_normalization_distinct_keeps_042_and_42_apart(tmp_path, monkeypatch):
    """With ``rdflib.NORMALIZE_LITERALS`` off, "042" and "42" are two
    xsd:integer terms: DISTINCT keeps both, with the pushdown and without
    it, as rdflib's own graph of the same file does."""
    nt = tmp_path / "unnormalized.nt"
    nt.write_text(
        f'<http://ex.org/a> <http://ex.org/p> "042"^^<{INTEGER}> .\n'
        f'<http://ex.org/b> <http://ex.org/p> "42"^^<{INTEGER}> .\n',
        encoding="utf-8",
    )
    out = tmp_path / "unnormalized.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    monkeypatch.setattr(rdflib, "NORMALIZE_LITERALS", False)
    expected = Graph()
    expected.parse(str(nt), format="nt")
    sparql = "SELECT DISTINCT ?o WHERE { ?s <http://ex.org/p> ?o }"
    answer = run(expected, sparql)
    assert [str(row[0]) for row in answer] == ["042", "42"]
    graph = Graph(store=VortexRdflibStore(str(out), in_memory=True))
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown == answer


_GENERATED_QUERIES = [
    "SELECT DISTINCT ?o WHERE { ?s <http://ex.org/p> ?o }",
    "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE { ?s <http://ex.org/p> ?o }",
    "SELECT ?o (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/p> ?o } GROUP BY ?o",
    f"""SELECT DISTINCT ?o WHERE {{
        {{ VALUES ?o {{ "0007"^^<{INTEGER}> "123456789"^^<{INTEGER}> "+1.5"^^<{XSD}decimal> }} }}
        UNION {{ ?s <http://ex.org/p> ?o }} }}""",
]


@pytest.mark.parametrize("sparql", _GENERATED_QUERIES)
def test_generated_aliases_answer_as_rdflib(generated_store, caplog, sparql):
    caplog.set_level(logging.ERROR, logger="rdflib.term")
    with_pushdown, without_pushdown = both_ways(Graph(store=generated_store), sparql)
    assert with_pushdown == without_pushdown
