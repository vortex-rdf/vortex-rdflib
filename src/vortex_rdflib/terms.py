"""N-Triples term spellings as the store's dictionary holds them.

The dictionary stores every term in its canonical N-Triples spelling: IRIs
as ``<iri>``, blank nodes as ``_:label``, literals as ``"lexical"``,
``"lexical"@tag`` (tag lowercased) or ``"lexical"^^<datatype>``
(``^^xsd:string`` dropped), with the minimal escape set
``\\" \\\\ \\n \\r \\t \\b \\f`` plus ``\\uXXXX`` for control characters.
Codes are the terms' positions in byte order, so the empty graph name ``""``
(if present) comes first, then every literal (``"`` is 0x22), then every IRI
(``<`` is 0x3C), then every blank node (``_`` is 0x5F) — which is what makes
a term's kind a range test on its code.
"""

import re

LITERAL = "literal"
IRI = "iri"
BLANK = "blank"

_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[tbnrf\"'\\])")


def _unescape_match(match: re.Match) -> str:
    body = match.group(1)
    if body[0] in "uU":
        return chr(int(body[1:], 16))
    return _ESCAPES[body]


def unescape(raw: str) -> str:
    """The lexical form of an N-Triples string body."""
    if "\\" not in raw:
        return raw
    return _ESCAPE_RE.sub(_unescape_match, raw)


class TermView:
    """A parsed spelling.

    ``kind`` is one of ``LITERAL``, ``IRI``, ``BLANK``. For a literal ``lex``
    is the unescaped lexical form, ``dt`` the datatype IRI (``None`` for
    plain and language-tagged literals) and ``lang`` the language tag; for
    an IRI ``lex`` is the IRI, for a blank node the label. A kind-only view
    (``lex`` None) is enough for the predicates that never look at the
    spelling.
    """

    __slots__ = ("kind", "lex", "dt", "lang")

    def __init__(self, kind: str, lex: str | None, dt: str | None = None, lang: str | None = None):
        self.kind = kind
        self.lex = lex
        self.dt = dt
        self.lang = lang

    def __repr__(self) -> str:
        return f"TermView({self.kind}, {self.lex!r}, dt={self.dt!r}, lang={self.lang!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, TermView) and (self.kind, self.lex, self.dt, self.lang) == (
            other.kind,
            other.lex,
            other.dt,
            other.lang,
        )

    def __hash__(self) -> int:
        return hash((self.kind, self.lex, self.dt, self.lang))


def parse_spelling(spelling: str) -> TermView:
    """Parse a canonical N-Triples spelling into a ``TermView``.

    The closing quote of a literal is found from the end: a plain literal
    ends with ``"``, a typed one with ``>`` after the last ``"^^<``, a
    language-tagged one with the tag after the last ``"@`` — a quote inside
    the lexical form is always escaped, so the last delimiter is the real one.
    """
    if spelling.startswith('"'):
        if spelling.endswith('"'):
            return TermView(LITERAL, unescape(spelling[1:-1]))
        if spelling.endswith(">"):
            cut = spelling.rfind('"^^<')
            if cut > 0:
                return TermView(LITERAL, unescape(spelling[1:cut]), dt=spelling[cut + 4 : -1])
        cut = spelling.rfind('"@')
        if cut > 0:
            return TermView(LITERAL, unescape(spelling[1:cut]), lang=spelling[cut + 2 :])
        raise ValueError(f"malformed literal spelling: {spelling!r}")
    if spelling.startswith("<") and spelling.endswith(">"):
        return TermView(IRI, spelling[1:-1])
    if spelling.startswith("_:"):
        return TermView(BLANK, spelling[2:])
    raise ValueError(f"unrecognized term spelling: {spelling!r}")


def kind_bounds(term_dict) -> tuple[int, int, int]:
    """``(literal_lo, iri_lo, blank_lo)``: the first code of each term kind.

    Three binary searches over ``decode`` (about 17 decodes each); the
    dictionary is immutable, so the store caches the result.
    """
    size = len(term_dict)

    def lower_bound(first_char: str) -> int:
        lo, hi = 0, size
        while lo < hi:
            mid = (lo + hi) // 2
            spelling = term_dict.decode(mid)
            if spelling is None or spelling[:1] < first_char:
                lo = mid + 1
            else:
                hi = mid
        return lo

    return lower_bound('"'), lower_bound("<"), lower_bound("_")


def kind_of(code: int, bounds: tuple[int, int, int]) -> str:
    """A code's term kind from the kind bounds, without decoding it (the
    empty graph name, below every kind, answers ``""``)."""
    literal_lo, iri_lo, blank_lo = bounds
    if code >= blank_lo:
        return BLANK
    if code >= iri_lo:
        return IRI
    if code >= literal_lo:
        return LITERAL
    return ""
