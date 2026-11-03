"""Term spellings as the dictionary holds them (:mod:`vortex_rdflib.terms`)."""

import pytest

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
