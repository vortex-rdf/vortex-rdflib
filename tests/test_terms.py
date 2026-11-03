"""Term spellings as the dictionary holds them (:mod:`vortex_rdflib.terms`)."""

import pytest
from rdflib.term import BNode, Literal, URIRef

from vortex_rdflib import terms

#: Literal spellings with no lexical form or suffix a literal has.
_MALFORMED_LITERALS = ['"abc', '"abc"@', '"abc"^^<x', '"abc"x', '"']


@pytest.mark.parametrize("spelling", _MALFORMED_LITERALS)
def test_every_reader_of_a_spelling_refuses_a_malformed_literal(spelling):
    """The term builders and the FILTER's parser split a literal one way —
    its closing quote is its last ``"`` — so they refuse the same spellings."""
    readers = (terms.parse_spelling, terms._term_by_constructors, terms._term_built_directly)
    for read in readers:
        with pytest.raises(ValueError):
            read(spelling)


@pytest.mark.parametrize(
    ("spelling", "view"),
    [
        ('"a\\"b"', terms.TermView(terms.LITERAL, 'a"b')),
        ('"x"@en-us', terms.TermView(terms.LITERAL, "x", lang="en-us")),
        ('"1"^^<http://ex.org/dt>', terms.TermView(terms.LITERAL, "1", dt="http://ex.org/dt")),
        ('""', terms.TermView(terms.LITERAL, "")),
    ],
)
def test_parse_spelling_splits_at_the_last_quote(spelling, view):
    assert terms.parse_spelling(spelling) == view


XSD = "http://www.w3.org/2001/XMLSchema#"


@pytest.mark.parametrize(
    "node",
    [
        Literal('a"b\\c\nd\te\rf'),
        Literal("bell\x07 and nul\x00 and del\x7f"),
        Literal("x", lang="EN-us"),
        Literal("x", datatype=URIRef(XSD + "string")),
        Literal("x"),
        Literal(""),
        Literal("5", datatype=URIRef(XSD + "integer")),
        Literal("042", datatype=URIRef(XSD + "integer")),
        Literal("abc", datatype=URIRef(XSD + "integer")),
        Literal("1", datatype=URIRef("http://ex.org/dt")),
        Literal("été"),
        URIRef("http://ex.org/a"),
        URIRef("urn:x"),
        BNode("b0"),
    ],
    ids=repr,
)
def test_view_of_reads_a_term_as_its_spelling_does(node):
    """A query constant's view, built from the term: what parsing the
    spelling the dictionary would hold for it gives — the lexical form as
    rdflib left it, a lowercase language tag, no xsd:string."""
    assert terms.view_of(node) == terms.parse_spelling(terms.canonical_spelling(node))


@pytest.mark.parametrize("value", [1, "x", None])
def test_view_of_refuses_what_is_no_rdf_term(value):
    with pytest.raises(TypeError):
        terms.view_of(value)
