"""N-Triples term spellings as the store's dictionary holds them, and the
rdflib terms they stand for.

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
from collections.abc import Callable

import rdflib.term
from rdflib.term import BNode, Literal, Node, URIRef

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


_XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
_ESCAPE_TABLE = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def escape(text: str) -> str:
    """The N-Triples string body the store spells ``text`` with: the minimal
    escape set plus ``\\uXXXX`` for control characters."""
    out = []
    for char in text:
        escaped = _ESCAPE_TABLE.get(char)
        if escaped is not None:
            out.append(escaped)
        elif char < " " or char == "\x7f" or char in "\ufffe\uffff":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


def canonical_spelling(node) -> str:
    """The spelling the dictionary would hold for an rdflib term — the
    inverse of ``parse_spelling`` for what ``encode`` needs: lowercase
    language tags, no ``^^xsd:string``, the store's escapes."""
    if isinstance(node, URIRef):
        return f"<{node}>"
    if isinstance(node, BNode):
        return f"_:{node}"
    if isinstance(node, Literal):
        body = f'"{escape(str(node))}"'
        if node.language:
            return f"{body}@{node.language.lower()}"
        datatype = node.datatype
        if datatype is None or str(datatype) == _XSD_STRING:
            return body
        return f"{body}^^<{datatype}>"
    raise TypeError(f"not an RDF term: {node!r}")


#: The form of every IRI vortex-rdf parses: a scheme, then none of the
#: characters no IRI holds.
_IRI_FORM = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\x00-\x20<>"{}|^`\\]*')


def unparseable_iri(node) -> bool:
    """Whether ``node`` is an IRI without the form of every IRI vortex-rdf
    parses — a relative one (no BASE), say. It is no term of a file, and the
    native parser rejects its spelling."""
    return isinstance(node, URIRef) and _IRI_FORM.fullmatch(node) is None


def kind_bounds(term_dict) -> tuple[int, int, int]:
    """``(literal_lo, iri_lo, blank_lo)``: the first code of each term kind.

    Three native binary searches (``TermDict.lower_bound``: the code of the
    first term not below the needle); the dictionary is immutable, so the
    store caches the result.
    """
    return term_dict.lower_bound('"'), term_dict.lower_bound("<"), term_dict.lower_bound("_")


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


#: xsd:integer, and its suffix after a literal's closing quote.
XSD_INTEGER = URIRef("http://www.w3.org/2001/XMLSchema#integer")
INTEGER_SUFFIX = f"^^<{XSD_INTEGER}>"
#: A canonical xsd:integer lexical form (a minus as the only sign, no
#: leading zero, ASCII digits): rdflib keeps it as written.
CANONICAL_INTEGER = re.compile(r"0|-?[1-9][0-9]*")


def _term_by_constructors(value: str) -> Node:
    """The rdflib term for a stored N-Triples spelling, built by rdflib's
    constructors: an IRI, a blank node, or a literal whose lexical form is
    unescaped by the N-Triples rules (:func:`unescape`), so an escaped
    backslash before ``x41`` decodes to a backslash and ``x41``."""
    if len(value) >= 2 and value[0] == "<" and value[-1] == ">":
        return URIRef(value[1:-1])
    if value.startswith("_:"):
        return BNode(value[2:])
    end = value.rfind('"')
    if not value.startswith('"') or end <= 0:
        raise ValueError(f"Could not parse returned RDF term as N-Triples: {value!r}")
    lexical, suffix = unescape(value[1:end]), value[end + 1 :]
    if not suffix:
        return Literal(lexical)
    if suffix.startswith("@") and len(suffix) > 1:
        return Literal(lexical, lang=suffix[1:])
    if suffix.startswith("^^<") and suffix.endswith(">"):
        return Literal(lexical, datatype=URIRef(suffix[3:-1]))
    raise ValueError(f"Could not parse returned RDF term as N-Triples: {value!r}")


_new_str = str.__new__
#: rdflib's own language-tag check, which ``Literal`` applies; ``None`` in an
#: rdflib release without it, and then the constructors build every term.
_is_valid_langtag = getattr(rdflib.term, "_is_valid_langtag", None)


def _term_built_directly(value: str) -> Node:
    """`_term_by_constructors`, except for the terms whose every attribute
    the spelling gives, made directly with ``str.__new__`` and the slots
    ``Literal.__new__`` sets: an IRI (``URIRef`` only adds a warning about
    characters vortex-rdf's parser never lets into an IRI), a plain or
    language-tagged literal without escapes (``Literal`` normalizes nothing
    in it), and an xsd:integer literal without escapes that ``int()`` reads
    (``Literal`` takes that int as its value and spells it as the int).
    Every other spelling goes to the constructors split once, so no kind
    builds slower than through `_term_by_constructors`."""
    if value.startswith("_:"):
        return BNode(value[2:])
    first = value[:1]
    if first == "<" and value[-1:] == ">":
        return _new_str(URIRef, value[1:-1])
    end = value.rfind('"') if first == '"' else 0
    if end <= 0:
        return _term_by_constructors(value)  # no term spelling: its error
    lexical, suffix = value[1:end], value[end + 1 :]
    if "\\" in lexical:
        lexical = _ESCAPE_RE.sub(_unescape_match, lexical)
    elif not suffix:
        term = _new_str(Literal, lexical)
        term._language = None
        term._datatype = None
        term._value = lexical
        term._ill_typed = None
        return term
    elif suffix[0] == "@":
        language = suffix[1:]
        # Not None: `_term_builder` picks this builder only when rdflib has it.
        if _is_valid_langtag(language):  # ty: ignore[call-non-callable]
            term = _new_str(Literal, lexical)
            term._language = language
            term._datatype = None
            term._value = lexical
            term._ill_typed = None
            return term
    elif suffix == INTEGER_SUFFIX:
        try:
            number = int(lexical)
        except ValueError:
            # Ill-typed: rdflib keeps it as written, with no value.
            return Literal(lexical, datatype=XSD_INTEGER)
        term = _new_str(Literal, number)
        term._language = None
        term._datatype = XSD_INTEGER
        term._value = number
        term._ill_typed = False
        return term
    # The rest of `_term_by_constructors`, on the parts split above.
    if not suffix:
        return Literal(lexical)
    if suffix.startswith("@") and len(suffix) > 1:
        return Literal(lexical, lang=suffix[1:])
    if suffix.startswith("^^<") and suffix.endswith(">"):
        return Literal(lexical, datatype=URIRef(suffix[3:-1]))
    raise ValueError(f"Could not parse returned RDF term as N-Triples: {value!r}")


#: Spellings `_term_builder` builds both ways: each kind
#: `_term_built_directly` makes itself, and kinds next to them it leaves to
#: rdflib's constructors.
_SELF_CHECK = (
    "<http://ex.org/a>",
    "<http://ex.org/été#日>",
    '"plain"',
    '""',
    '"café \U0001f600"',
    '"x"@en',
    '"x"@en-us',
    f'"0"{INTEGER_SUFFIX}',
    f'"42"{INTEGER_SUFFIX}',
    f'"-7"{INTEGER_SUFFIX}',
    f'"12345678901234567890123"{INTEGER_SUFFIX}',
    f'"042"{INTEGER_SUFFIX}',
    f'"-0"{INTEGER_SUFFIX}',
    f'" 7"{INTEGER_SUFFIX}',
    f'"+5"{INTEGER_SUFFIX}',
    f'"1\\u0032"{INTEGER_SUFFIX}',
    '"1e2"^^<http://www.w3.org/2001/XMLSchema#double>',
    '"x"^^<http://ex.org/dt>',
    '"q\\"uote"',
    '"tab\\there"@en',
    "_:b0",
)
_UNSET = object()


def _same_term(a: Node, b: Node) -> bool:
    """Whether two terms are alike in all rdflib exposes: type, string,
    equality, hash and ``n3()``; a literal's value, datatype, language and
    ill-typedness; every slot their classes declare, by value and type."""
    if type(a) is not type(b) or str(a) != str(b) or a != b or hash(a) != hash(b):
        return False
    if a.n3() != b.n3():
        return False
    if isinstance(a, Literal) and isinstance(b, Literal):
        for x, y in (
            (a.value, b.value),
            (a.datatype, b.datatype),
            (a.language, b.language),
            (a.ill_typed, b.ill_typed),
        ):
            if type(x) is not type(y) or x != y:
                return False
    for cls in type(a).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            x, y = getattr(a, name, _UNSET), getattr(b, name, _UNSET)
            if type(x) is not type(y) or x != y:
                return False
    return getattr(a, "__dict__", None) == getattr(b, "__dict__", None)


def _term_builder() -> Callable[[str], Node]:
    """`_term_built_directly` if it builds every spelling of `_SELF_CHECK`
    as rdflib's constructors do, else `_term_by_constructors` for every term
    — as after an rdflib release that changes what ``Literal`` sets."""
    if _is_valid_langtag is None:
        return _term_by_constructors
    for spelling in _SELF_CHECK:
        try:
            same = _same_term(_term_built_directly(spelling), _term_by_constructors(spelling))
        except Exception:
            same = False
        if not same:
            return _term_by_constructors
    return _term_built_directly
