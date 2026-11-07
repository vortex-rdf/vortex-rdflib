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

    ``kind`` is one of ``LITERAL``, ``IRI``, ``BLANK`` — or ``""`` for no
    term, in the views a test of a term's kind alone is asked about. For a
    literal ``lex`` is the unescaped lexical form, ``dt`` the datatype IRI
    (``None`` for plain and language-tagged literals) and ``lang`` the
    language tag; for an IRI ``lex`` is the IRI, for a blank node the label.
    """

    __slots__ = ("kind", "lex", "dt", "lang")

    def __init__(self, kind: str, lex: str, dt: str | None = None, lang: str | None = None):
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

    A literal's closing quote is its last ``"`` — a quote inside the lexical
    form is escaped, and neither a tag nor a datatype IRI holds one — and
    what follows it is nothing, ``@tag`` or ``^^<iri>``: the split every
    reader of a spelling makes (:func:`_term_by_constructors`).
    """
    if spelling.startswith('"'):
        end = spelling.rfind('"')
        suffix = spelling[end + 1 :]
        if end > 0:
            if not suffix:
                return TermView(LITERAL, unescape(spelling[1:end]))
            if suffix.startswith("^^<") and suffix.endswith(">"):
                return TermView(LITERAL, unescape(spelling[1:end]), dt=suffix[3:-1])
            if suffix.startswith("@") and len(suffix) > 1:
                return TermView(LITERAL, unescape(spelling[1:end]), lang=suffix[1:])
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


def view_of(node) -> TermView:
    """An rdflib term as the term view its stored spelling parses to
    (``parse_spelling(canonical_spelling(node))``, built directly): a
    literal's lexical form as rdflib left it, well-typed or not, its
    language tag lowercased, ``xsd:string`` dropped."""
    if isinstance(node, URIRef):
        return TermView(IRI, str(node))
    if isinstance(node, BNode):
        return TermView(BLANK, str(node))
    if isinstance(node, Literal):
        if node.language:
            return TermView(LITERAL, str(node), lang=node.language.lower())
        datatype = node.datatype
        if datatype is None or str(datatype) == _XSD_STRING:
            return TermView(LITERAL, str(node))
        return TermView(LITERAL, str(node), dt=str(datatype))
    raise TypeError(f"not an RDF term: {node!r}")


#: The form of every IRI vortex-rdf parses: a scheme, then none of the
#: characters no IRI holds.
_IRI_FORM = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\x00-\x20<>"{}|^`\\]*')


def unparseable_iri(node) -> bool:
    """Whether ``node`` is an IRI without the form of every IRI vortex-rdf
    parses — a relative one (no BASE), say. It is no term of a file, and the
    native parser rejects its spelling."""
    return isinstance(node, URIRef) and _IRI_FORM.fullmatch(node) is None


#: The characters of an ASCII IRI (RFC 3986) besides its delimiters: the
#: unreserved, the sub-delims and "%" (whose escapes are checked apart).
_IRI_CHARS = r"A-Za-z0-9\-._~!$&'()*+,;=%"
_SEGMENT = rf"[{_IRI_CHARS}:@]*"
#: An absolute ASCII IRI vortex-rdf always parses: a scheme, then an
#: authority (user info, a host that is no IP literal, a port of digits) and
#: its path, or a path that does not start with "//"; a query; at most one
#: "#". Character classes, not alternations per character, keep it a scan.
_SAFE_IRI_FORM = (
    rf"<[A-Za-z][A-Za-z0-9+.\-]*:"
    rf"(?://(?:[{_IRI_CHARS}:]*@)?[{_IRI_CHARS}]*(?::[0-9]+)?(?:/{_SEGMENT})*"
    rf"|(?!//){_SEGMENT}(?:/{_SEGMENT})*)"
    rf"(?:\?[{_IRI_CHARS}:@/?]*)?(?:#[{_IRI_CHARS}:@/?]*)?>"
)
_SAFE_IRI = re.compile(_SAFE_IRI_FORM)
#: A literal as ``canonical_spelling`` spells it, with no suffix, a simple
#: language tag or such a datatype.
_SAFE_LITERAL = re.compile(
    rf'"[^"\\]*(?:\\.[^"\\]*)*"(?:@[a-z]{{2,3}}(?:-[a-z]{{2}}|-[0-9]{{3}})?|\^\^{_SAFE_IRI_FORM})?'
)
_SAFE_BLANK = re.compile(r"_:[A-Za-z0-9]+")
#: A "%" that does not start an escape of two hexadecimal digits.
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def spelling_verdict(spelling: str) -> bool | None:
    """Whether vortex-rdf's parser refuses the N-Triples ``spelling`` of a
    pattern term (as ``canonical_spelling`` makes it), where that is decided
    here: ``True`` refused — a surrogate, or an IRI (a literal's datatype
    included) without the form every IRI it parses has — ``False`` accepted —
    the default graph's name ``""``, or a spelling of a shape it always parses
    — and ``None`` for vortex-rdf to decide
    (:func:`vortex_rdflib.boundary.refused`). ``tests/test_refused_terms.py``
    checks the local verdicts against vortex-rdf."""
    if not spelling:
        return False
    if not spelling.isascii():
        try:
            spelling.encode()
        except UnicodeEncodeError:
            return True  # a surrogate: it cannot reach vortex-rdf at all
    first = spelling[0]
    if first == "<":
        if _SAFE_IRI.fullmatch(spelling) and (
            "%" not in spelling or _BAD_ESCAPE.search(spelling) is None
        ):
            return False
        return True if _IRI_FORM.fullmatch(spelling, 1, len(spelling) - 1) is None else None
    if first == '"':
        close = spelling.rfind('"')
        if _SAFE_LITERAL.fullmatch(spelling) and (
            "%" not in spelling or _BAD_ESCAPE.search(spelling, close) is None
        ):
            return False
        datatype = close + 4  # past '"^^<'
        if (
            spelling.startswith("^^<", close + 1)
            and _IRI_FORM.fullmatch(spelling, datatype, len(spelling) - 1) is None
        ):
            return True
        return None
    if first == "_" and _SAFE_BLANK.fullmatch(spelling):
        return False
    return None


class KindRanges:
    """Each term kind's half-open code range in one dictionary: codes rank
    spellings in byte order and every spelling of a kind starts alike, so a
    kind is one range (``TermDict.prefix_range`` of ``"``, ``<`` and ``_:``)
    — the keep a kind test pushes into a match — and a code's kind is the
    range it falls in. A file's are read once (``Codes.kind_ranges``)."""

    __slots__ = (
        "_ranges",
        "_literal_lo",
        "_literal_hi",
        "_iri_lo",
        "_iri_hi",
        "_blank_lo",
        "_blank_hi",
    )

    def __init__(self, literal: tuple[int, int], iri: tuple[int, int], blank: tuple[int, int]):
        self._ranges = {LITERAL: literal, IRI: iri, BLANK: blank}
        self._literal_lo, self._literal_hi = literal
        self._iri_lo, self._iri_hi = iri
        self._blank_lo, self._blank_hi = blank

    @classmethod
    def from_dictionary(cls, term_dict) -> "KindRanges":
        """The ranges of ``term_dict``: three native prefix searches."""
        return cls(
            tuple(term_dict.prefix_range('"')),
            tuple(term_dict.prefix_range("<")),
            tuple(term_dict.prefix_range("_:")),
        )

    def range(self, kind: str) -> tuple[int, int]:
        """The half-open code range of ``kind``: ``LITERAL``, ``IRI`` or
        ``BLANK``."""
        return self._ranges[kind]

    def kind_of(self, code: int) -> str:
        """The kind of the term ``code`` stands for, without decoding it;
        ``""`` for a code in no kind's range: the default graph's empty name,
        a code past the dictionary's end, a negative one."""
        if self._literal_lo <= code < self._literal_hi:
            return LITERAL
        if self._iri_lo <= code < self._iri_hi:
            return IRI
        if self._blank_lo <= code < self._blank_hi:
            return BLANK
        return ""


def is_range(keep) -> bool:
    """Whether a native keep — what narrows a native match at one position —
    is a code range, ``(lo, hi)`` half-open (a kind's, :class:`KindRanges`),
    rather than a column of ascending codes."""
    return isinstance(keep, tuple)


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
    backslash before ``x41`` decodes to a backslash and ``x41``. A literal
    is split as :func:`parse_spelling` splits it."""
    if len(value) >= 2 and value[0] == "<" and value[-1] == ">":
        return URIRef(value[1:-1])
    if value.startswith("_:"):
        return BNode(value[2:])
    end = value.rfind('"')
    if not value.startswith('"') or end <= 0:
        raise ValueError(f"Could not parse returned RDF term as N-Triples: {value!r}")
    return _literal_by_constructors(unescape(value[1:end]), value[end + 1 :], value)


def _literal_by_constructors(lexical: str, suffix: str, value: str) -> Literal:
    """The literal of an unescaped lexical form and the suffix its spelling
    ``value`` has after the closing quote, built by rdflib's constructors; a
    suffix no literal has raises ``ValueError``."""
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
    characters vortex-rdf's parser never lets into an IRI), a blank node
    (``BNode`` adds nothing to a label), a plain or language-tagged literal
    without escapes (``Literal`` normalizes nothing in it), and an
    xsd:integer literal without escapes that ``int()`` reads (``Literal``
    takes that int as its value and spells it as the int, a non-canonical
    form only while ``rdflib.NORMALIZE_LITERALS`` holds).
    Every other spelling goes to the constructors split once, so no kind
    builds slower than through `_term_by_constructors` but an ill-typed
    xsd:integer, which first fails ``int()``. Assumes rdflib's own
    xsd:integer binding (``int``), as the native filters do."""
    first = value[:1]
    if first == "<" and value[-1:] == ">":
        return _new_str(URIRef, value[1:-1])
    end = value.rfind('"') if first == '"' else 0
    if end <= 0:
        if first == "_" and value[1:2] == ":":
            return _new_str(BNode, value[2:])
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
        # `_term_builder` picks this builder only when rdflib has the check.
        check = _is_valid_langtag
        if check is not None and check(language):
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
            number = None
        if number is None:
            # Ill-typed: rdflib keeps it as written, with no value. Built
            # outside the handler, so the warning rdflib logs chains nothing.
            return Literal(lexical, datatype=XSD_INTEGER)
        if not rdflib.NORMALIZE_LITERALS and CANONICAL_INTEGER.fullmatch(lexical) is None:
            # Not normalizing (read per call), rdflib keeps it as written.
            return Literal(lexical, datatype=XSD_INTEGER)
        term = _new_str(Literal, number)
        term._language = None
        term._datatype = XSD_INTEGER
        term._value = number
        term._ill_typed = False
        return term
    return _literal_by_constructors(lexical, suffix, value)


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
    f'"1_0"{INTEGER_SUFFIX}',
    f'"٤٢"{INTEGER_SUFFIX}',
    f'" 7"{INTEGER_SUFFIX}',
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
