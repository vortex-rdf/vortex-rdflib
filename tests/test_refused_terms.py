"""A pattern naming a term vortex-rdf's parser refuses matches nothing (#26):
no file holds such a term, so ``triples()``, ``contexts()`` and ``in`` answer
nothing rather than raising, on every layout, file-backed and in memory. The
check (:func:`~vortex_rdflib.terms.spelling_verdict`, then
:func:`~vortex_rdflib.boundary.refused`) decides common spellings locally and
asks vortex-rdf's own parser about the rest; its local verdicts are checked
against vortex-rdf over a generated set of spellings."""

from collections import Counter
from itertools import product

import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from tests.support import native_spy
from vortex_rdf import VortexRdfStore, serialize_rdf

from vortex_rdflib import VortexRdflibStore, terms
from vortex_rdflib.boundary import PARSE_CHECK_SUBJECT
from vortex_rdflib.terms import canonical_spelling, spelling_verdict

EX = "http://ex.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"
A, P, Q, X = URIRef(EX + "a"), URIRef(EX + "p"), URIRef(EX + "q"), Literal("x")
LAYOUTS = ["dictionary", "default", "typed-object"]

REFUSED = [
    URIRef("http://x/%zz"),
    URIRef("http://x/\ud800"),
    URIRef("http://x/a#b#c"),
    URIRef("http://[x/a"),
    URIRef("http://x/a\x7f"),
    Literal("x", datatype=URIRef("http://x/%zz")),
    Literal("x", lang="en-a"),
]


def _file(tmp_path, layout: str, extra: str = "") -> str:
    source = tmp_path / f"{layout}.nt"
    data = f'<{EX}a> <{EX}p> "x" .\n<{EX}a> <{EX}q> <{EX}b> .\n{extra}'
    source.write_text(data, encoding="utf-8")
    path = tmp_path / f"{layout}.vortex"
    serialize_rdf(str(source), str(path), layout=layout)
    return str(path)


def _graph(tmp_path, layout: str, in_memory: bool, extra: str = "") -> Graph:
    return Graph(store=VortexRdflibStore(_file(tmp_path, layout, extra), in_memory=in_memory))


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("term", REFUSED, ids=lambda term: repr(str(term))[:24])
def test_a_pattern_naming_a_refused_term_matches_nothing(tmp_path, layout, in_memory, term):
    store = VortexRdflibStore(_file(tmp_path, layout), in_memory=in_memory)
    graph = Graph(store=store)
    patterns = [(None, None, term), (A, P, term)]
    if isinstance(term, URIRef):
        patterns += [(term, None, None), (None, term, None), (term, P, X), (A, term, X)]
    for pattern in patterns:
        assert list(graph.triples(pattern)) == [], pattern
        assert list(store.contexts(pattern)) == [], pattern
    assert (A, P, term) not in graph


@pytest.mark.parametrize("layout", ["dictionary", "default"])
def test_a_graph_named_by_a_refused_iri_is_empty(tmp_path, layout):
    """A guard: a context naming a refused IRI already names no graph."""
    source = tmp_path / "quads.nq"
    source.write_text(f"<{EX}a> <{EX}p> <{EX}b> <{EX}g1> .\n", encoding="utf-8")
    path = tmp_path / "quads.vortex"
    serialize_rdf(str(source), str(path), layout=layout)
    dataset = Dataset(store=VortexRdflibStore(str(path)), default_union=True)
    named = dataset.graph(URIRef("http://x/%zz"))
    assert len(named) == 0
    assert list(named.triples((None, None, None))) == []


def test_common_spellings_are_decided_locally():
    for spelling in [
        f"<{EX}a>",
        f"<{EX}a%20b?q=1#f>",
        "<urn:isbn:0451450523>",
        "<http://ex.org:8080/a>",
        '"x"',
        '"x"@en',
        '"x"@en-gb',
        f'"5"^^<{XSD}integer>',
        "_:b0",
        "",
        f"<{EX}café>",
        "<http://dbpedia.org/resource/S\u00e3o_Paulo>",
        f"<{EX}\u65e5\u672c?q=\u00e9#\u00e9>",
        f'"x"^^<{EX}\u00e9>',
        '"x"@zh-hans',
        '"x"@zh-hans-cn',
        '"x"@de-ch-1996',
        '"x"@en-us-x-twain',
        '"x"@es-419',
    ]:
        assert spelling_verdict(spelling) is False, spelling
    for spelling in ["<rel>", "<http://x/\ud800>", '"\ud800"', '"x"^^<rel>']:
        assert spelling_verdict(spelling) is True, spelling
    for spelling in [
        "<http://x/%zz>",
        "<http://x/a#b#c>",
        f"<{EX}\ue000>",
        "<http://\u00e9x.org/a>",
        '"x"@en-a',
        "<http://[::1]/a>",
        "<http://h:abc/>",
        "<http://h:/>",
    ]:
        assert spelling_verdict(spelling) is None, spelling


def _spellings() -> list[str]:
    """IRIs, literals and blank nodes, well formed and not, as the pattern
    builders spell them."""
    schemes = ["http", "urn", "a", "x-y.z+w", "1http"]
    authorities = [
        "",
        "//ex.org",
        "//ex.org:80",
        "//ex.org:",
        "//u@ex.org",
        "//u@h@x",
        "//[::1]",
        "//ex.org:8a",
        "//\u00e9x.org",
    ]
    paths = ["", "/a", "/%41", "/%zz", "/%4", "/café", "/a\x7f", "/[x]", "/a b", "/\ud800"]
    # Non-ASCII: RFC 3987's ucschar (Latin, CJK, an astral emoji), and code
    # points outside it (a C1 control, private use, a noncharacter, the
    # specials, a tag character) or barred from IRIs (bidi formatting).
    paths += [
        "/S\u00e3o",
        "/\u65e5\u672c",
        "/\u00a0",
        "/\U0001f600",
        "/\U000e1000",
        "/\u0085",
        "/\ue000",
        "/\ufdd0",
        "/\ufff0",
        "/\U000e0001",
        "/\u200e",
        "/\u202e",
    ]
    queries = ["", "?q=1", "?%zz", "?\u00e9", "?\ue000"]
    fragments = ["", "#f", "#f#g", "#\u00e9", "#\ue000"]
    spellings = [
        f"<{scheme}:{authority}{path}{query}{fragment}>"
        for scheme, authority, path, query, fragment in product(
            schemes, authorities, paths, queries, fragments
        )
    ]
    lexicals = ["", "x", "é", "\n", '"', "\\", "\x00", "\ud800", "abc"]
    tags = ["en", "en-GB", "zh-Hans", "en-a", "en-x", "x-private", "abcdefghi"]
    tags += ["zh-Hans-CN", "de-CH-1996", "sr-Latn-RS", "en-US-x-twain", "es-419"]
    tags += ["de-1996-1996", "en-a-bbb", "i-klingon", "zh-yue", "en-GB-oed", "en-x-a-b"]
    datatypes = [f"{XSD}integer", f"{XSD}string", "http://x/%zz", f"{EX}é"]
    for lexical in lexicals:
        literals = [Literal(lexical)]
        for tag in tags:
            try:
                literals.append(Literal(lexical, lang=tag))
            except ValueError:
                pass
        literals += [Literal(lexical, datatype=URIRef(datatype)) for datatype in datatypes]
        spellings += [canonical_spelling(literal) for literal in literals]
    spellings += [f"_:{label}" for label in ["b0", "N1a2", "a.b", "a-b", "_x", "1x"]]
    return spellings


def _vortex_refuses(native, spelling: str) -> bool:
    try:
        native.count_quads(PARSE_CHECK_SUBJECT, None, spelling, None)
    except ValueError:
        return True
    return False


def test_the_local_verdict_agrees_with_vortex_rdf(tmp_path):
    native = VortexRdfStore(_file(tmp_path, "default"))
    seen: Counter = Counter()
    for spelling in _spellings():
        verdict = spelling_verdict(spelling)
        seen[verdict] += 1
        if verdict is not None:
            assert verdict == _vortex_refuses(native, spelling), spelling
    assert min(seen[True], seen[False], seen[None]) >= 10, seen


def test_the_ascii_forms_decide_an_ascii_spelling_as_the_full_forms_do():
    """The safe forms an ASCII spelling is checked against are the full
    forms without the characters beyond ASCII, which such a spelling cannot
    hold: on every ASCII spelling of the corpus the two agree."""
    forms = {
        "iri": (terms._SAFE_ASCII_IRI, terms._SAFE_IRI),
        "literal": (terms._SAFE_ASCII_LITERAL, terms._SAFE_LITERAL),
    }
    seen = Counter()
    for spelling in filter(str.isascii, _spellings()):
        for name, (ascii_form, full_form) in forms.items():
            matched = full_form.fullmatch(spelling) is not None
            assert (ascii_form.fullmatch(spelling) is not None) == matched, spelling
            seen[name, matched] += 1
    assert min(seen[key] for key in product(forms, [True, False])) >= 50, seen


def test_an_ascii_spelling_is_decided_without_the_full_forms(monkeypatch):
    """The full forms hold RFC 3987's ranges beyond ASCII, which the regex
    engine tests character by character: a pattern's constants, ASCII in
    the common case, are checked against the ASCII forms alone."""

    class Unused:
        def fullmatch(self, *args):
            raise AssertionError("an ASCII spelling reached a full form")

    monkeypatch.setattr(terms, "_SAFE_IRI", Unused())
    monkeypatch.setattr(terms, "_SAFE_LITERAL", Unused())
    for spelling in [f"<{EX}a>", "<urn:isbn:0451450523>", '"x"@en-gb', f'"5"^^<{XSD}integer>']:
        assert spelling_verdict(spelling) is False, spelling
    for spelling in ["<rel>", '"x"^^<rel>']:
        assert spelling_verdict(spelling) is True, spelling


def test_ordinary_terms_reach_no_parse_check(tmp_path):
    graph = _graph(tmp_path, "dictionary", False)
    spy = native_spy(graph)
    list(graph.triples((A, P, None)))
    assert not [args for args in spy.args if args[:1] == (PARSE_CHECK_SUBJECT,)]


def test_an_unusual_valid_term_is_matched_without_a_parse_check(tmp_path):
    """The store asks only after vortex-rdf refused a pattern: a term the
    local check leaves undecided costs nothing when vortex-rdf takes it."""
    graph = _graph(tmp_path, "dictionary", False, extra=f'<{EX}café> <{EX}p> "y" .\n')
    spy = native_spy(graph)
    assert len(list(graph.triples((URIRef(EX + "café"), None, None)))) == 1
    assert not [args for args in spy.args if args[:1] == (PARSE_CHECK_SUBJECT,)]


def test_a_non_ascii_constant_is_resolved_without_a_parse_check(tmp_path):
    """The pushdown screens a pattern's constants before it matches: an IRI
    of RFC 3987's characters and a tagged literal of a common BCP 47 tag are
    decided locally, so the query makes no extra native call."""
    graph = _graph(tmp_path, "dictionary", False, extra=f'<{EX}café> <{EX}p> "y"@zh-hans .\n')
    spy = native_spy(graph)
    rows = graph.query(f'SELECT ?p WHERE {{ <{EX}café> ?p "y"@zh-Hans }}')
    assert len(list(rows)) == 1
    assert not [args for args in spy.args if args[:1] == (PARSE_CHECK_SUBJECT,)]


@pytest.mark.parametrize("layout", ["dictionary", "default"])
def test_a_refusal_is_confirmed_by_vortex_rdf_before_it_matches_nothing(tmp_path, layout):
    """A pattern vortex-rdf refuses answers nothing once vortex-rdf's own
    parser confirms the term (``<http://x/%zz>`` is left undecided locally);
    any other error from the read stands."""
    graph = _graph(tmp_path, layout, False)
    spy = native_spy(graph)
    assert list(graph.triples((URIRef("http://x/%zz"), None, None))) == []
    assert len([args for args in spy.args if args[:1] == (PARSE_CHECK_SUBJECT,)]) == 1
