"""rdflib's value semantics over term views (:class:`~vortex_rdflib.terms.TermView`):
the numeric values and the equality and order rdflib's ``Literal`` gives two
terms, and the effective boolean value — each answering ``True``,
``False``, ``ERROR`` (rdflib raises) or ``UNKNOWN`` (outside what the fast
route reproduces exactly).
"""

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from rdflib.plugins.sparql.datatypes import XSD_DTs
from rdflib.term import URIRef

from ..terms import LITERAL, TermView


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


ERROR = _Sentinel("ERROR")


UNKNOWN = _Sentinel("UNKNOWN")


_XSD = "http://www.w3.org/2001/XMLSchema#"


_XSD_STRING = _XSD + "string"


_XSD_BOOLEAN = _XSD + "boolean"


_XSD_INTEGER = _XSD + "integer"


_RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"


#: How rdflib's parser spells ``IN ()``; its evaluator reads it back as the
#: empty list.
_RDF_NIL = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")


_XML_COMPARABLE = frozenset(
    {
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#HTML",
    }
)


_XSD_DTS = frozenset(str(dt) for dt in XSD_DTs)


# rdflib's numeric datatypes with the converter Literal.__new__ applies
# (XSDToPython) and the well-formedness check that decides `ill_typed`.
_INT_LIKE = frozenset(
    _XSD + name
    for name in (
        "integer",
        "nonPositiveInteger",
        "negativeInteger",
        "nonNegativeInteger",
        "positiveInteger",
        "long",
        "unsignedLong",
        "int",
        "short",
        "byte",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    )
)


_DECIMAL = _XSD + "decimal"


_FLOATS = frozenset({_XSD + "float", _XSD + "double"})


NUMERIC_TYPES = _INT_LIKE | {_DECIMAL} | _FLOATS


_RANGES = {
    _XSD + "int": (-2147483648, 2147483647),
    _XSD + "short": (-32768, 32767),
    _XSD + "byte": (-128, 127),
    _XSD + "unsignedInt": (0, 4294967295),
    _XSD + "unsignedShort": (0, 65535),
    _XSD + "unsignedByte": (0, 255),
}


_SIGNS: dict[str, Callable[[int], bool]] = {
    _XSD + "nonNegativeInteger": lambda v: v >= 0,
    _XSD + "positiveInteger": lambda v: v > 0,
    _XSD + "nonPositiveInteger": lambda v: v <= 0,
    _XSD + "negativeInteger": lambda v: v < 0,
    _XSD + "unsignedLong": lambda v: v >= 0,
}


def convert(view: TermView):
    """The raw converter result rdflib stores as ``Literal.value`` for a
    numeric datatype, ``None`` where the conversion fails."""
    dt, lex = view.dt, view.lex
    if lex is None:
        return None
    try:
        if dt in _INT_LIKE:
            return int(lex)
        if dt == _DECIMAL:
            return Decimal(lex)
        if dt in _FLOATS:
            return float(lex)
    except (ValueError, InvalidOperation, TypeError):
        return None
    return None


def numeric_value(view: TermView):
    """The value a literal contributes to rdflib's numeric fast path — a
    numeric datatype, a converting lexical form and a well-formed value —
    or ``None`` when rdflib would leave that path."""
    if view.kind != LITERAL or view.dt not in NUMERIC_TYPES:
        return None
    value = convert(view)
    if value is None:
        return None
    dt = view.dt
    bounds = _RANGES.get(dt)
    if bounds is not None and not (bounds[0] <= value <= bounds[1]):
        return None
    sign = _SIGNS.get(dt)
    if sign is not None and not sign(value):
        return None
    return value


def _bool_value(view: TermView) -> bool:
    # rdflib's _parseBoolean: anything but 1/true (case-insensitive) is False.
    return (view.lex or "").lower() in ("1", "true")


def _string_like(view) -> bool:
    """rdflib's ``string()``: a plain, language-tagged or xsd:string literal."""
    return (
        isinstance(view, TermView)
        and view.kind == LITERAL
        and (view.dt is None or view.dt == _XSD_STRING)
    )


def _num_compare(a, b, op: str):
    # A NaN meeting a Decimal raises decimal.InvalidOperation in Python and
    # rdflib propagates it; those values are left to rdflib.
    nan = any(v != v for v in (a, b))
    if nan and any(isinstance(v, Decimal) for v in (a, b)):
        return UNKNOWN
    if op == ">":
        return a > b
    return a == b


def _gt(a: TermView, b: TermView):
    """``Literal.__gt__`` for two literals: the numeric fast path, else the
    datatype-IRI order; everything after that is rdflib's own business."""
    va, vb = numeric_value(a), numeric_value(b)
    if va is not None and vb is not None:
        return _num_compare(va, vb, ">")
    dta, dtb = a.dt or _XSD_STRING, b.dt or _XSD_STRING
    if dta != dtb:
        return dta > dtb
    return UNKNOWN


def _eq(a: TermView, b: TermView):
    """``Literal.eq`` for two literals (value equality)."""
    va, vb = numeric_value(a), numeric_value(b)
    if va is not None and vb is not None:
        return _num_compare(va, vb, "==")
    if (a.lang or "").lower() != (b.lang or "").lower():
        return False
    dta, dtb = a.dt or _XSD_STRING, b.dt or _XSD_STRING
    if dta == _XSD_STRING and dtb == _XSD_STRING:
        return a.lex == b.lex
    if dta in _XML_COMPARABLE and dtb in _XML_COMPARABLE:
        return UNKNOWN
    if dta != dtb:
        return False
    if dta == _XSD_BOOLEAN:
        return _bool_value(a) == _bool_value(b)
    if dta in NUMERIC_TYPES:
        ca, cb = convert(a), convert(b)
        if ca is not None and cb is not None:
            return _num_compare(ca, cb, "==")
        # Without both values rdflib falls back to the lexical forms and
        # raises ("cannot know") when they differ.
        return True if a.lex == b.lex else ERROR
    # Other datatypes: equal spellings are equal values; anything else
    # depends on rdflib's parser for that datatype.
    return True if a.lex == b.lex else UNKNOWN


def _value_eq(a, b):
    """``x.eq(y)`` for two terms of any kind (the ``=`` operator)."""
    if a.kind == LITERAL and b.kind == LITERAL:
        return _eq(a, b)
    if a.kind != b.kind:
        return False  # a literal equals no other node; IRIs and bnodes are type-strict
    return a.lex == b.lex


def term_eq(a, b):
    """``x == y`` (RDF term equality, ``__eq__``). Exact for IRIs, blank
    nodes and string-like literals; other literals normalize their lexical
    form in rdflib, so unequal spellings may still be equal terms."""
    if a.kind != b.kind:
        return False
    if a.kind != LITERAL:
        return a.lex == b.lex
    if a.dt != b.dt or (a.lang or "").lower() != (b.lang or "").lower():
        return False
    if a.lex == b.lex:
        return True
    if a.dt is None or a.dt == _XSD_STRING:
        return False
    return UNKNOWN


def _truth(val):
    """rdflib's ``EBV`` of an evaluated value (bool, term view, unbound)."""
    if val is True or val is False or val is ERROR or val is UNKNOWN:
        return val
    if val is None or val.kind != LITERAL:
        return ERROR
    dt = val.dt
    if dt == _XSD_BOOLEAN:
        return _bool_value(val)
    if dt is None or dt == _XSD_STRING:
        return len(val.lex) > 0
    if dt in NUMERIC_TYPES:
        value = convert(val)
        return ERROR if value is None else bool(value)
    return UNKNOWN


def _not(val):
    if val is True or val is False:
        return not val
    return val


_REGEX_FLAGS = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}


#: What ``re.compile`` raises for a pattern it cannot compile: a syntax error,
#: a repetition count past its limit, nesting past the recursion limit.
_COMPILE_ERRORS = (re.error, OverflowError, RecursionError)


def _regex_flags(flags) -> tuple[str, int]:
    """The flags of a REGEX flags literal (``None`` for none) that rdflib maps
    (``i``, ``s``, ``m``; it ignores every other letter), as those letters and
    as ``re`` flags."""
    kept = "".join(flag for flag in ("" if flags is None else str(flags)) if flag in _REGEX_FLAGS)
    compile_flags = 0
    for flag in kept:
        compile_flags |= _REGEX_FLAGS[flag]
    return kept, compile_flags
