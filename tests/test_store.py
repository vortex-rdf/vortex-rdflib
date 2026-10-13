import logging
import random
import re
import subprocess
import sys

import pytest
import rdflib
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, ConjunctiveGraph
from rdflib.plugins.parsers import ntriples
from rdflib.term import Node
from vortex_rdf import U64Column, serialize_rdf

import vortex_rdflib.pushdown as pd
import vortex_rdflib.terms as terms_mod
from conftest import GRAPH_1, GRAPH_2, LAYOUTS
from test_pushdown import _counting_term_dict, both_ways
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown, unregister_sparql_pushdown
from vortex_rdflib.terms import escape, parse_spelling

FOAF_NAME = URIRef("http://xmlns.com/foaf/0.1/name")
FOAF_KNOWS = URIRef("http://xmlns.com/foaf/0.1/knows")
ALICE = URIRef("http://ex.org/alice")
BOB = URIRef("http://ex.org/bob")
G1 = URIRef(GRAPH_1)
G2 = URIRef(GRAPH_2)


@pytest.fixture(params=LAYOUTS)
def graph(vortex_files, request):
    return Graph(store=VortexRdflibStore(str(vortex_files[request.param])))


@pytest.fixture(params=LAYOUTS)
def quad_store(vortex_quad_files, request):
    return VortexRdflibStore(str(vortex_quad_files[request.param]))


def test_len(graph):
    assert len(graph) == 5


def test_triples_pattern(graph):
    names = list(graph.triples((None, FOAF_NAME, None)))
    assert len(names) == 3
    assert (ALICE, FOAF_NAME, Literal("Alice")) in names

    bob = set(graph.triples((BOB, None, None)))
    assert bob == {
        (BOB, FOAF_NAME, Literal("Bob", lang="en")),
        (BOB, URIRef("http://ex.org/age"), Literal(42)),
    }


def test_join_position_guard(graph):
    # A literal propagated into subject position is unsatisfiable, not an error.
    assert list(graph.triples((Literal("Alice"), None, None))) == []
    assert list(graph.triples((None, Literal("Alice"), None))) == []


def test_sparql_single_pattern(graph):
    query = "SELECT ?n WHERE { <http://ex.org/alice> <http://xmlns.com/foaf/0.1/name> ?n }"
    res = list(graph.query(query))
    assert res == [(Literal("Alice"),)]


def test_sparql_join(graph):
    res = list(
        graph.query(
            """SELECT ?n WHERE {
                <http://ex.org/alice> <http://xmlns.com/foaf/0.1/knows> ?x .
                ?x <http://xmlns.com/foaf/0.1/name> ?n
            }"""
        )
    )
    assert res == [(Literal("Bob", lang="en"),)]


def test_sparql_typed_literal(graph):
    res = list(graph.query("SELECT ?a WHERE { ?s <http://ex.org/age> ?a }"))
    assert len(res) == 1
    assert res[0][0].toPython() == 42


def test_literal_object_patterns(graph):
    # Bound-object lookups with language tags and datatypes go through the
    # native pattern parser and must not degrade to simple literals.
    assert list(graph.triples((None, None, Literal("Bob", lang="en")))) == [
        (BOB, FOAF_NAME, Literal("Bob", lang="en"))
    ]
    assert list(graph.triples((None, None, Literal("Bob")))) == []
    assert list(graph.triples((None, None, Literal(42)))) == [
        (BOB, URIRef("http://ex.org/age"), Literal(42))
    ]


def test_blank_node_subject(graph):
    rows = list(graph.triples((None, None, Literal("Anon"))))
    assert len(rows) == 1
    assert isinstance(rows[0][0], BNode)


def test_read_only(graph):
    with pytest.raises(NotImplementedError):
        graph.store.add((ALICE, FOAF_NAME, Literal("x")), None)
    with pytest.raises(NotImplementedError):
        graph.store.remove((None, None, None))
    with pytest.raises(NotImplementedError):
        graph.store.remove_graph(graph)


# ─── named graphs ───────────────────────────────────────────────────────────


def test_contexts_are_the_files_graphs(quad_store):
    names = {context.identifier for context in quad_store.contexts()}
    assert names == {G1, G2, DATASET_DEFAULT_GRAPH_ID}


def test_contexts_of_a_triple(quad_store):
    # Alice's name is in both named graphs, her `knows` in only the first.
    assert {c.identifier for c in quad_store.contexts((ALICE, FOAF_NAME, Literal("Alice")))} == {
        G1,
        G2,
    }
    assert {c.identifier for c in quad_store.contexts((ALICE, FOAF_KNOWS, BOB))} == {G1}
    assert list(quad_store.contexts((ALICE, FOAF_NAME, Literal("nobody")))) == []


def test_len_per_graph(quad_store):
    dataset = Dataset(store=quad_store)
    assert len(dataset.graph(G1)) == 2
    assert len(dataset.graph(G2)) == 2
    assert len(dataset.default_graph) == 2
    assert len(quad_store) == 6  # the union counts quads, not distinct triples


def test_len_of_an_absent_graph(quad_store):
    assert len(Dataset(store=quad_store).graph(URIRef("http://ex.org/g/absent"))) == 0


def test_quads_carry_their_graph(quad_store):
    quads = set(Dataset(store=quad_store).quads((None, None, None, None)))
    assert (ALICE, FOAF_NAME, Literal("Alice"), G1) in quads
    assert (ALICE, FOAF_NAME, Literal("Alice"), G2) in quads
    assert (ALICE, FOAF_KNOWS, BOB, G2) not in quads
    assert len(quads) == 6


def test_graph_restricts_the_match(quad_store):
    dataset = Dataset(store=quad_store)
    assert set(dataset.graph(G1).triples((None, None, None))) == {
        (ALICE, FOAF_NAME, Literal("Alice")),
        (ALICE, FOAF_KNOWS, BOB),
    }
    assert set(dataset.graph(G2).triples((ALICE, None, None))) == {
        (ALICE, FOAF_NAME, Literal("Alice"))
    }
    # A ground pattern is an existence check per graph, not over the union.
    assert list(dataset.graph(G2).triples((ALICE, FOAF_KNOWS, BOB))) == []
    assert list(dataset.graph(G1).triples((ALICE, FOAF_KNOWS, BOB))) == [(ALICE, FOAF_KNOWS, BOB)]


def test_bare_graph_is_the_union_view(quad_store):
    """rdflib names an identifier-less graph with a blank node, which names
    nothing the file holds; that is the whole-store view the README shows."""
    whole = Graph(store=quad_store)
    assert len(whole) == 6
    # Alice's name is in two graphs, so the union yields it twice.
    assert len(list(whole.triples((ALICE, FOAF_NAME, None)))) == 2


def test_ground_pattern_union_carries_each_graphs_context(quad_store):
    """A fully-ground pattern under the union yields once per graph holding
    the triple, each row with that graph's own context — served from the
    graph column alone, no term materialized."""
    rows = list(quad_store.triples((ALICE, FOAF_NAME, Literal("Alice"))))
    assert [triple for triple, _ in rows] == [(ALICE, FOAF_NAME, Literal("Alice"))] * 2
    assert {ctx[0].identifier for _, ctx in rows} == {G1, G2}
    assert list(quad_store.triples((ALICE, FOAF_NAME, Literal("nobody")))) == []


def test_union_match_in_one_graph_shares_one_context(quad_store):
    """A union match whose rows all live in one graph yields one shared
    context tuple (the single-distinct-graph loop), with that graph's name."""
    rows = list(quad_store.triples((ALICE, FOAF_KNOWS, None)))
    assert rows, "fixture holds alice-knows rows"
    contexts = {id(ctx) for _, ctx in rows}
    assert len(contexts) == 1
    assert {ctx[0].identifier for _, ctx in rows} == {G1}


def test_union_match_across_graphs_keeps_per_row_contexts(quad_store):
    rows = list(quad_store.triples((None, FOAF_NAME, None)))
    by_graph: dict = {}
    for (s_, _, _), ctx in rows:
        by_graph.setdefault(ctx[0].identifier, set()).add(s_)
    assert set(by_graph) >= {G1, G2}
    assert ALICE in by_graph[G1] and ALICE in by_graph[G2]


def test_default_graph_is_not_a_named_graph(quad_store):
    dataset = Dataset(store=quad_store, default_union=True)
    named = {
        str(row.g) for row in dataset.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    }
    assert named == {GRAPH_1, GRAPH_2}
    # ...but the union default graph does include it.
    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 6


def test_dataset_without_union_sees_only_the_default_graph(quad_store):
    dataset = Dataset(store=quad_store)
    assert len(list(dataset.query("SELECT * WHERE { ?s ?p ?o }"))) == 2


def test_union_view_is_keyed_on_default_union_not_the_class(quad_store):
    """A view whose default graph is the union of the others sees every graph,
    whatever class it is.

    rdflib's deprecated `ConjunctiveGraph` is the one such view that exists
    besides `Dataset`, and it is *always* a union — including when it carries
    an identifier of its own, which recognizing only `Dataset` would have
    read as a named graph.
    """

    class _UnionView:
        default_union = True
        identifier = URIRef("http://ex.org/g/1")

    assert quad_store._graph_n3(_UnionView()) is None

    with pytest.warns(DeprecationWarning):
        legacy = ConjunctiveGraph(store=quad_store, identifier=URIRef("http://ex.org/g/1"))
    assert quad_store._graph_n3(legacy) is None
    assert len(list(legacy.triples((None, None, None)))) == 6


def test_add_graph_is_a_no_op(quad_store):
    # A Dataset calls it whenever it hands out a graph, so it must not raise —
    # and it must not invent a graph either.
    dataset = Dataset(store=quad_store)
    dataset.graph(URIRef("http://ex.org/g/new"))
    assert {c.identifier for c in quad_store.contexts()} == {G1, G2, DATASET_DEFAULT_GRAPH_ID}


def test_dictionary_graph_uses_code_path(vortex_files, monkeypatch):
    monkeypatch.delenv("VORTEX_RDF_IN_MEMORY", raising=False)
    store = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert store._dict is not None  # code path active

    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    disabled = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert disabled._dict is None  # string fallback forced

    # Both paths yield identical triples.
    got_codes = sorted(Graph(store=store).triples((None, None, None)))
    got_strings = sorted(Graph(store=disabled).triples((None, None, None)))
    assert got_codes == got_strings and len(got_codes) == 5


def test_file_store_dictionary_is_memory_mapped(vortex_files, monkeypatch):
    """A file-backed store's dictionary stays in its mapped file whatever
    VORTEX_RDF_DICT_MAX_RESIDENT_BYTES says; in_memory loads it with the store."""
    monkeypatch.delenv("VORTEX_RDF_IN_MEMORY", raising=False)
    monkeypatch.setenv("VORTEX_RDF_DICT_MAX_RESIDENT_BYTES", str(1 << 40))
    mapped = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert mapped._dict is not None and mapped._dict.file_backed
    loaded = VortexRdflibStore(str(vortex_files["dictionary"]), in_memory=True)
    assert loaded._dict is not None and not loaded._dict.file_backed
    assert sorted(Graph(store=mapped).triples((None, None, None))) == sorted(
        Graph(store=loaded).triples((None, None, None))
    )


def test_residency_budget_is_gone(vortex_files):
    """max_resident_bytes is not accepted: passing it fails loudly instead of
    disappearing into **kwargs."""
    with pytest.raises(TypeError, match="max_resident_bytes"):
        VortexRdflibStore(str(vortex_files["dictionary"]), max_resident_bytes=4096)
    assert not hasattr(VortexRdflibStore(str(vortex_files["dictionary"])), "max_resident_bytes")


def test_in_memory_graph_equality(vortex_files):
    on_file = Graph(store=VortexRdflibStore(str(vortex_files["dictionary"])))
    in_mem = Graph(store=VortexRdflibStore(str(vortex_files["dictionary"]), in_memory=True))
    assert len(in_mem) == len(on_file) == 5
    assert sorted(in_mem.triples((None, None, None))) == sorted(on_file.triples((None, None, None)))


def test_layout_alias_and_detection(vortex_files):
    # The aliased layout names are accepted as labels; the detected layout wins.
    store = VortexRdflibStore(str(vortex_files["dictionary"]), layout="cottas-native-ids")
    assert store.layout == "dictionary"
    store = VortexRdflibStore(str(vortex_files["default"]), layout="cottas-native-strings")
    assert store.layout == "default"


def test_from_n3_safe_dbpedia_escaped_apostrophe():
    # Language-tagged literal with a non-canonical \' escape (DBpedia quirk).
    term = VortexRdflibStore._from_n3_safe('"L\\\'agent"@fr')
    assert term == Literal("L'agent", lang="fr")


def test_literals_decode_as_the_ntriples_parser_reads_them(tmp_path):
    """Stored literals become the terms rdflib's N-Triples parser makes from
    the same file: escapes such as an escaped backslash before ``x41`` stay
    as written, in results and in the FILTER values rdflib evaluates."""
    lines = [
        '<http://ex.org/s1> <http://ex.org/p> "a\\\\x41" .',
        '<http://ex.org/s2> <http://ex.org/p> "b\\\\u0041"@en .',
        '<http://ex.org/s3> <http://ex.org/p> "t\\tq\\"it\'s \\u00e9" .',
        '<http://ex.org/s4> <http://ex.org/p> "c\\\\"^^<http://ex.org/dt> .',
    ]
    nt = tmp_path / "escapes.nt"
    nt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "escapes.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    expected = Graph()
    expected.parse(str(nt), format="nt")
    assert len(expected) == 4
    query = 'SELECT ?s WHERE { ?s <http://ex.org/p> ?o FILTER(contains(str(?o), "\\\\x4")) }'
    for in_memory in (True, False):
        graph = Graph(store=VortexRdflibStore(str(out), in_memory=in_memory))
        assert sorted(graph.triples((None, None, None))) == sorted(expected)
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown == [(URIRef("http://ex.org/s1"),)]


def test_literal_decoding_ignores_the_ntriples_validate_flag(monkeypatch):
    """rdflib's N-Triples parser has a module-wide ``validate`` flag that
    makes its ``unquote`` reject non-ASCII text; the store decodes the same
    spellings whatever the flag says."""
    monkeypatch.setattr(ntriples, "validate", True)
    assert VortexRdflibStore._from_n3_safe('"café"@fr') == Literal("café", lang="fr")
    assert VortexRdflibStore._from_n3_safe('"a\\\\x41"') == Literal("a\\x41")


def test_from_n3_safe_reads_iris_and_blank_nodes():
    assert VortexRdflibStore._from_n3_safe("<http://ex.org/a>") == URIRef("http://ex.org/a")
    assert VortexRdflibStore._from_n3_safe("_:b1") == BNode("b1")


@pytest.mark.parametrize(
    "spelling",
    ["", "<", '"abc', '"abc"@', '"abc"^^<x', "abc"],
    ids=["empty", "lone-bracket", "open-quote", "empty-tag", "open-datatype", "bare-text"],
)
def test_from_n3_safe_rejects_a_malformed_spelling(spelling):
    with pytest.raises(ValueError, match=re.escape(repr(spelling))):
        VortexRdflibStore._from_n3_safe(spelling)


XSD = "http://www.w3.org/2001/XMLSchema#"
INTEGER = f"<{XSD}integer>"
#: Literal text: the characters the store escapes, controls, non-ASCII.
_LITERAL_TEXT = 'aZ09 -.,"\\\n\t\r\x01\x7fé日\U0001f600'
#: IRI text: characters an IRI holds, non-ASCII included.
_IRI_TEXT = "aZ09-._~:/?#[]@!$&'()*+,;=%é日\U0001f600"
_TAGS = ("en", "en-us", "fr", "zh-hant-tw", "de-1996", "EN-GB", "x-private")
_OTHER_TYPED = (
    ("1.50", f"{XSD}decimal"),
    ("1e2", f"{XSD}double"),
    ("true", f"{XSD}boolean"),
    ("1", f"{XSD}boolean"),
    ("2020-01-01", f"{XSD}date"),
    ("42", f"{XSD}int"),
    ("x  y", f"{XSD}token"),
    ("abc", f"{XSD}integer"),
)


def _generated_spellings(rng: random.Random, n: int) -> list[str]:
    """Store spellings of every kind `_from_n3_safe` reads: IRIs, plain and
    language-tagged literals with and without escapes, canonical and other
    xsd:integer forms, other datatypes, blank nodes."""

    def text(alphabet: str) -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))

    spellings = []
    for _ in range(n):
        kind = rng.randrange(8)
        if kind == 0:
            spellings.append(f"<http://ex.org/{text(_IRI_TEXT)}>")
        elif kind == 1:
            spellings.append(f'"{escape(text(_LITERAL_TEXT))}"')
        elif kind == 2:
            spellings.append(f'"{escape(text(_LITERAL_TEXT))}"@{rng.choice(_TAGS)}')
        elif kind == 3:
            spellings.append(f'"{rng.randint(-(10**25), 10**25)}"^^{INTEGER}')
        elif kind == 4:
            prefix = rng.choice(("0", "+", "-0", " ", ""))
            suffix = rng.choice(("", " ", ".0", "_1"))
            spellings.append(f'"{prefix}{rng.randrange(10**6)}{suffix}"^^{INTEGER}')
        elif kind == 5:
            lexical, datatype = rng.choice(_OTHER_TYPED)
            spellings.append(f'"{lexical}"^^<{datatype}>')
        elif kind == 6:
            spellings.append(f"_:b{rng.randrange(10**6)}")
        else:
            spellings.append(f'"{escape(text(_LITERAL_TEXT))}"^^<http://ex.org/dt>')
    return spellings


def _by_rdflib(spelling: str) -> Node:
    """The term rdflib's constructors make from a spelling."""
    if spelling.startswith("<"):
        return URIRef(spelling[1:-1])
    if spelling.startswith("_:"):
        return BNode(spelling[2:])
    view = parse_spelling(spelling)
    if view.dt is not None:
        return Literal(view.lex, datatype=URIRef(view.dt))
    return Literal(view.lex, lang=view.lang)


def _spy_on_constructors(monkeypatch) -> list:
    """Record the class of every URIRef, BNode and Literal constructor call."""
    constructed = []
    for cls in (URIRef, BNode, Literal):
        original = cls.__new__

        def spy(kind, *args, _original=original, **kwargs):
            constructed.append(kind)
            return _original(kind, *args, **kwargs)

        monkeypatch.setattr(cls, "__new__", spy)
    return constructed


def test_terms_built_directly_are_rdflibs(monkeypatch, caplog):
    """IRIs, blank nodes, and plain, language-tagged and xsd:integer
    literals without escapes (an integer that int() reads) are built
    without rdflib's constructors; over generated spellings of every kind,
    each term is the one the constructors make, in everything rdflib
    exposes."""
    caplog.set_level(logging.ERROR, logger="rdflib.term")  # "abc"^^xsd:integer logs a warning
    # The import-time check passes on the rdflib this package pins.
    assert VortexRdflibStore._from_n3_safe is terms_mod._term_built_directly
    rng = random.Random(20261013)
    spellings = _generated_spellings(rng, 3000) + [
        f'"0"^^{INTEGER}',
        f'"-0"^^{INTEGER}',
        f'"1{"0" * 5000}"^^{INTEGER}',  # canonical, past int()'s digit limit: no value
        f'" 7 "^^{INTEGER}',
        f'"1\\u0032"^^{INTEGER}',
        f'"12a"^^{INTEGER}',
        '""',
        '""@en',
    ]
    constructed = _spy_on_constructors(monkeypatch)
    direct = 0
    for spelling in spellings:
        constructed.clear()
        term = VortexRdflibStore._from_n3_safe(spelling)
        direct += not constructed
        expected = _by_rdflib(spelling)
        assert type(term) is type(expected), spelling
        assert term == expected and hash(term) == hash(expected), spelling
        assert str(term) == str(expected) and term.n3() == expected.n3(), spelling
        if isinstance(expected, Literal):
            assert isinstance(term, Literal)
            for name in ("value", "datatype", "language", "ill_typed"):
                got, want = getattr(term, name), getattr(expected, name)
                assert type(got) is type(want) and got == want, (spelling, name)
    assert direct > 800


def test_an_ill_typed_integer_logs_as_through_the_constructors(caplog):
    """rdflib logs one warning for an ill-typed xsd:integer; built by the
    direct path, its exception chains nothing from the path's own int()."""
    caplog.set_level(logging.WARNING, logger="rdflib.term")
    terms_mod._term_built_directly(f'"abc"^^{INTEGER}')
    (record,) = caplog.records
    assert record.exc_info is not None and record.exc_info[1].__context__ is None


@pytest.mark.parametrize("lexical", ["042", "+5", " 7", "-0", "42"])
def test_without_normalization_an_integer_is_built_as_rdflib_keeps_it(monkeypatch, lexical):
    """With ``rdflib.NORMALIZE_LITERALS`` switched off after import, rdflib
    keeps a non-canonical xsd:integer as written; the direct path reads the
    switch at each call and builds the same term."""
    monkeypatch.setattr(rdflib, "NORMALIZE_LITERALS", False)
    spelling = f'"{lexical}"^^{INTEGER}'
    term = terms_mod._term_built_directly(spelling)
    assert terms_mod._same_term(term, terms_mod._term_by_constructors(spelling))
    assert str(term) == lexical


@pytest.mark.parametrize("failure", ["mismatch", "error", "no-langtag-check"])
def test_a_failed_self_check_leaves_every_term_to_rdflibs_constructors(monkeypatch, failure):
    """At import, `_term_builder` builds a fixed sample both ways. When the
    terms differ, or the direct build fails — as after an rdflib release
    that changes what ``Literal`` sets — or rdflib has no language-tag check
    to call, every term goes through rdflib's constructors."""
    spellings = ["<http://ex.org/a>", "_:b1", '"plain"', '"x"@en', f'"42"^^{INTEGER}']
    constructed = _spy_on_constructors(monkeypatch)
    assert terms_mod._term_builder() is terms_mod._term_built_directly
    constructed.clear()
    for spelling in spellings:
        terms_mod._term_built_directly(spelling)
    assert constructed == []
    if failure == "mismatch":
        monkeypatch.setattr(terms_mod, "_same_term", lambda a, b: False)
    elif failure == "no-langtag-check":
        monkeypatch.setattr(terms_mod, "_is_valid_langtag", None)
    else:

        def broken(value):
            raise AttributeError("'Literal' object has no attribute '_ill_typed'")

        monkeypatch.setattr(terms_mod, "_term_built_directly", broken)
    build = terms_mod._term_builder()
    assert build is terms_mod._term_by_constructors
    for spelling in spellings:
        constructed.clear()
        term = build(spelling)
        assert type(term) in constructed and term == _by_rdflib(spelling)


def test_an_rdflib_without_the_private_names_still_imports():
    """``rdflib.term._is_valid_langtag`` and ``_toPythonMapping`` are looked
    up, not imported: with both gone the package imports, the store builds
    terms with rdflib's constructors, and the alias pass has no converter
    table. A fresh interpreter, so the deletion stays there."""
    code = (
        "import sys\n"
        "import vortex_rdflib\n"  # rdflib's own modules, imported with their names
        "for name in [m for m in sys.modules if m.startswith('vortex_rdflib')]:\n"
        "    del sys.modules[name]\n"
        "import rdflib.term\n"
        "del rdflib.term._is_valid_langtag, rdflib.term._toPythonMapping\n"
        "from vortex_rdflib import pushdown, store, terms\n"
        "assert store.VortexRdflibStore._from_n3_safe is terms._term_by_constructors\n"
        "assert pushdown._toPythonMapping is None\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_no_path_store():
    store = VortexRdflibStore()
    assert len(store) == 0
    assert list(store.triples((None, None, None))) == []


# ─── triples() decodes a chunk at a time ────────────────────────────────────

_CHUNKED_GRAPHS = ("<http://ex.org/g/1>", "<http://ex.org/g/2>", "<http://ex.org/g/3>", "")
P0 = URIRef("http://ex.org/p0")
P9 = URIRef("http://ex.org/p9")
S0 = URIRef("http://ex.org/s/0")
EVERYWHERE = URIRef("http://ex.org/everywhere")


@pytest.fixture(scope="module")
def chunked_quads(tmp_path_factory):
    """Quads whose matches span several decode chunks: 400 subjects in one
    graph first (whole chunks in one graph), then 2,600 spread over three
    named graphs and the default graph (chunks mixing graphs), two
    predicates each, and one triple every graph holds."""
    rng = random.Random(11)

    def quad(s: str, p: str, o: str, g: str) -> str:
        return f"{s} {p} {o} {g} .\n" if g else f"{s} {p} {o} .\n"

    def obj(i: int) -> str:
        return rng.choice(
            (
                f"<http://ex.org/o{i % 300}>",
                f'"v{i % 500}"',
                f'"w{i}"@en',
                f'"{i % 200}"^^{INTEGER}',
            )
        )

    lines = []
    for i in range(400):
        for p in range(2):
            lines.append(
                quad(f"<http://ex.org/a/{i}>", f"<http://ex.org/p{p}>", obj(i), _CHUNKED_GRAPHS[0])
            )
    for i in range(2600):
        graph = rng.choice(_CHUNKED_GRAPHS)
        for p in range(2):
            lines.append(quad(f"<http://ex.org/s/{i}>", f"<http://ex.org/p{p}>", obj(i), graph))
    for graph in _CHUNKED_GRAPHS:
        lines.append(quad(f"<{S0}>", f"<{P9}>", f"<{EVERYWHERE}>", graph))
    d = tmp_path_factory.mktemp("chunked")
    nq = d / "chunked.nq"
    nq.write_text("".join(lines), encoding="utf-8")
    out = d / "chunked.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


def _chunk_bounds(rows: int) -> list[tuple[int, int]]:
    bounds, start, size = [], 0, pd._CHUNK_START
    while start < rows:
        bounds.append((start, min(start + size, rows)))
        start, size = bounds[-1][1], min(size * 4, pd._CHUNK_MAX)
    return bounds


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_triples_yield_the_rows_and_contexts_of_the_match(chunked_quads, monkeypatch, in_memory):
    """Decoded a chunk at a time, triples() yields what the string path
    yields, in its order, each row with its graph's one context tuple: over
    the union (chunks in one graph and chunks across graphs), in a named
    graph, in the default graph, and for ground patterns."""
    store = VortexRdflibStore(str(chunked_quads), in_memory=in_memory)
    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    oracle = VortexRdflibStore(str(chunked_quads), in_memory=in_memory)
    assert store._dict is not None and oracle._dict is None
    contexts = {
        "none": lambda s: None,
        "bare-graph": lambda s: Graph(store=s),
        "union-dataset": lambda s: Dataset(store=s, default_union=True),
        "named": lambda s: Graph(store=s, identifier=URIRef("http://ex.org/g/2")),
        "default": lambda s: Graph(store=s, identifier=DATASET_DEFAULT_GRAPH_ID),
    }
    patterns = [
        (None, None, None),
        (None, P0, None),
        (S0, None, None),
        (None, None, Literal("v7")),
        (S0, P9, EVERYWHERE),
        (S0, P9, URIRef("http://ex.org/nowhere")),
    ]
    for name, context in contexts.items():
        for pattern in patterns:
            got = list(store.triples(pattern, context(store)))
            want = list(oracle.triples(pattern, context(oracle)))
            rows = [(triple, ctx[0].identifier) for triple, ctx in got]
            assert rows == [(triple, ctx[0].identifier) for triple, ctx in want], (name, pattern)
            shared: dict = {}
            assert all(shared.setdefault(ctx[0].identifier, ctx) is ctx for _, ctx in got)
    union = [ctx[0].identifier for _, ctx in store.triples((None, None, None))]
    graphs_per_chunk = [len(set(union[i:j])) for i, j in _chunk_bounds(len(union))]
    assert len(union) == 6004 and len(graphs_per_chunk) == 5
    assert 1 in graphs_per_chunk and max(graphs_per_chunk) == 4


def _quads_file(tmp_path, host: str) -> str:
    """300 subjects of ``host``, two rows each: the first 50 in graph 1, the
    next 200 in graphs 2 and 3, the last 50 in graph 4 — so a full scan
    meets graphs 2 and 3 in its second chunk and graph 4 in its third."""

    def graph(i: int) -> str:
        return f"<http://{host}/g/{1 if i < 50 else 4 if i >= 250 else 2 + i % 2}>"

    nq = tmp_path / f"{host}.nq"
    nq.write_text(
        "".join(
            f"<http://{host}/s/{i:04d}> <http://{host}/p{p}> <http://{host}/o/{i}> {graph(i)} .\n"
            for i in range(300)
            for p in range(2)
        ),
        encoding="utf-8",
    )
    out = tmp_path / f"{host}.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return str(out)


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_a_suspended_triples_call_stays_on_its_file(tmp_path, in_memory):
    """triples() read part-way keeps the dictionary and graph contexts it
    started with: the rows read after ``store.close()``, and after
    ``store.open()`` of another file, still come from the first file, in
    its graphs; the second file then reads as itself."""
    first = _quads_file(tmp_path, "a.example")
    second = _quads_file(tmp_path, "b.example")
    store = VortexRdflibStore(first, in_memory=in_memory)

    def read(rows) -> list:
        return [(triple, ctx[0].identifier) for triple, ctx in rows]

    expected = read(store.triples((None, None, None)))
    rows = store.triples((None, None, None))
    got = read([next(rows)])
    store.close()
    got += read(next(rows) for _ in range(99))  # into the second chunk
    store.open(second)
    got += read(rows)  # the third chunk
    assert got == expected and len({graph for _, graph in got}) == 4
    hosts = {str(triple[0]).split("/")[2] for triple, _ in store.triples((None, None, None))}
    graphs = {str(ctx[0].identifier) for _, ctx in store.triples((None, None, None))}
    assert hosts == {"b.example"} and graphs == {f"http://b.example/g/{i}" for i in range(1, 5)}


@pytest.mark.parametrize("longer", [0, 1, 2, 3])
def test_triples_rejects_match_columns_of_unequal_lengths(chunked_quads, longer):
    """The columns of one native match must be equally long: triples()
    raises on one that is a code longer, as ``zip(strict=True)`` did,
    rather than reading every column by the first one's length."""
    store = VortexRdflibStore(str(chunked_quads), in_memory=True)
    native = store._store()

    class _OneCodeLonger:
        def match_codes(self, *args, **kwargs):
            matched = native.match_codes(*args, **kwargs)
            assert matched is not None
            cols = list(matched)
            codes = memoryview(cols[longer]).cast("Q").tolist()
            cols[longer] = U64Column([*codes, codes[0]])
            return tuple(cols)

        def __getattr__(self, name):
            return getattr(native, name)

    store._native = _OneCodeLonger()  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="unequal lengths"):
        next(store.triples((None, P0, None)))


def test_an_early_stop_decodes_a_chunk_not_the_match(chunked_quads):
    """rdflib's own evaluator stops after a LIMIT's rows or at an ASK's first
    row; triples() has decoded the codes of the first chunk, not those of
    the whole match."""
    graph = Graph(store=VortexRdflibStore(str(chunked_quads)))
    list(graph.store.contexts())  # one context per graph: store state, decoded once
    counting = _counting_term_dict(graph)
    queries = [
        ("SELECT ?s ?o WHERE { ?s <http://ex.org/p0> ?o } LIMIT 3", 3),
        ("SELECT * WHERE { ?s ?p ?o } LIMIT 10", 10),
        ("ASK { ?s <http://ex.org/p0> ?o }", True),
    ]
    unregister_sparql_pushdown()
    try:
        for sparql, answer in queries:
            before = counting.decoded
            result = graph.query(sparql)
            assert (result.askAnswer if result.type == "ASK" else len(list(result))) == answer
            assert counting.decoded - before <= 3 * pd._CHUNK_START, sparql
    finally:
        register_sparql_pushdown()
    before = counting.decoded
    rows = graph.store.triples((None, P0, None))
    next(rows)
    assert counting.decoded - before <= 3 * pd._CHUNK_START
    assert sum(1 for _ in rows) == 2999
    assert counting.decoded - before > 3000  # the whole match's distinct codes
