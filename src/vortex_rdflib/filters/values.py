"""SPARQL 1.1's value semantics over term views (:class:`~vortex_rdflib.terms.TermView`):
the value spaces of literals, the relational operators (§17.3), the effective
boolean value (§17.2.2), ``sameTerm`` and ``REGEX`` (XPath's flags and
anchors). Each answers ``True``, ``False``, ``ERROR`` (a type error) or, for
an operand the fast route cannot evaluate, ``UNKNOWN``.

Where rdflib's evaluator differs, these follow the specification
(``docs/pushdown.md``, "SPARQL semantics"). A literal is read as written: its
lexical form must be in its datatype's lexical space (XML Schema), else it is
ill-typed and has no value.
"""

import math
import re
import struct
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal, InvalidOperation
from fractions import Fraction

from rdflib.plugins.sparql.datatypes import XSD_DTs
from rdflib.term import URIRef

from ..terms import LITERAL, TermView, canonical_spelling, parse_spelling


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


_XSD_DATETIME = _XSD + "dateTime"


_XSD_DATE = _XSD + "date"


_RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"


#: How rdflib's parser spells ``IN ()``; its evaluator reads it back as the
#: empty list.
_RDF_NIL = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")


_XSD_DTS = frozenset(str(dt) for dt in XSD_DTs)


#: The datatypes derived from xsd:integer, each with its lexical form
#: ``[+-]?[0-9]+`` and, where the type bounds it, its value range.
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


_FLOAT = _XSD + "float"


_DOUBLE = _XSD + "double"


NUMERIC_TYPES = _INT_LIKE | {_DECIMAL, _FLOAT, _DOUBLE}


_RANGES = {
    _XSD + "long": (-(2**63), 2**63 - 1),
    _XSD + "int": (-(2**31), 2**31 - 1),
    _XSD + "short": (-32768, 32767),
    _XSD + "byte": (-128, 127),
    _XSD + "unsignedLong": (0, 2**64 - 1),
    _XSD + "unsignedInt": (0, 2**32 - 1),
    _XSD + "unsignedShort": (0, 65535),
    _XSD + "unsignedByte": (0, 255),
    _XSD + "nonNegativeInteger": (0, None),
    _XSD + "positiveInteger": (1, None),
    _XSD + "nonPositiveInteger": (None, 0),
    _XSD + "negativeInteger": (None, -1),
}


_INTEGER_LEX = re.compile(r"[+-]?[0-9]+")
_DECIMAL_LEX = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_FLOAT_LEX = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?INF|NaN")


#: The ranks of XPath's numeric type promotion (F&O §B.1): an operation on
#: two numbers promotes both to the wider type, integer < decimal < float <
#: double. Every type derived from xsd:integer ranks as xsd:integer.
_INTEGER_RANK, _DECIMAL_RANK, _FLOAT_RANK, _DOUBLE_RANK = range(4)


_RANK = dict.fromkeys(_INT_LIKE, _INTEGER_RANK) | {
    _DECIMAL: _DECIMAL_RANK,
    _FLOAT: _FLOAT_RANK,
    _DOUBLE: _DOUBLE_RANK,
}


#: Exact decimal arithmetic: xsd:decimal has no precision limit.
_EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)


def _single(value: float) -> float:
    """A double rounded to IEEE single precision, as xsd:float holds it: to
    the nearest float, ties to even, and to infinity past the largest."""
    try:
        return struct.unpack("f", struct.pack("f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _adjacent_single(single: float, toward: float) -> float:
    """The float next to ``single`` in the direction of ``toward``."""
    if single == 0:
        return math.copysign(struct.unpack("f", struct.pack("I", 1))[0], toward)
    (bits,) = struct.unpack("I", struct.pack("f", single))
    bits += 1 if (toward > single) == (single > 0) else -1
    return struct.unpack("f", struct.pack("I", bits))[0]


def _nearest_single(double: float, exact) -> float:
    """The float nearest a number given as its nearest double and as a
    callable for its exact value (an ``int`` or a ``Decimal``).

    Rounding the double again misleads only when the double is exactly
    halfway between two floats; there the exact value decides."""
    single = _single(double)
    if single == double or math.isinf(double) or math.isnan(double):
        return single
    if math.isinf(single):
        largest = struct.unpack("f", struct.pack("I", 0x7F7FFFFF))[0]
        low, high = sorted((math.copysign(largest, double), math.copysign(2.0**128, double)))
    else:
        low, high = sorted((single, _adjacent_single(single, double)))
    if double != (low + high) / 2:
        return single
    gap = Fraction(exact()) - Fraction(double)
    if gap == 0:
        return single
    nearer = low if gap < 0 else high
    return math.copysign(math.inf, nearer) if abs(nearer) == 2.0**128 else nearer


def _parsed_number(dt: str, lex: str):
    """The value of a numeric lexical form of type ``dt``, or ``None``."""
    if dt in _INT_LIKE:
        if not _INTEGER_LEX.fullmatch(lex):
            return None
        value = int(lex)
        lo, hi = _RANGES.get(dt, (None, None))
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            return None
        return value
    if dt == _DECIMAL:
        if not _DECIMAL_LEX.fullmatch(lex):
            return None
        try:
            return Decimal(lex)
        except InvalidOperation:
            return None
    if not _FLOAT_LEX.fullmatch(lex):
        return None
    value = float(lex.replace("INF", "inf"))
    return _nearest_single(value, lambda: Decimal(lex)) if dt == _FLOAT else value


def numeric_operand(view: TermView):
    """``(rank, value)`` of a well-typed numeric literal — the value an
    ``int``, a ``Decimal``, or a ``float`` (an xsd:float held at single
    precision) — or ``None`` for anything else: not a literal, another
    datatype, a lexical form outside its type's lexical space, or a value
    outside its type's range."""
    dt, lex = view.dt, view.lex
    if view.kind != LITERAL or dt is None or lex is None:
        return None
    rank = _RANK.get(dt)
    if rank is None:
        return None
    value = _parsed_number(dt, lex)
    return None if value is None else (rank, value)


def numeric_value(view: TermView):
    """The value of a well-typed numeric literal (:func:`numeric_operand`),
    or ``None``."""
    operand = numeric_operand(view)
    return None if operand is None else operand[1]


def _promote(value, rank: int, to: int):
    """A number of one rank as the wider type ``to`` holds it: an integer
    becomes a decimal exactly, and an integer or a decimal the nearest float
    or double; a float is already a double."""
    if rank == to or to == _DOUBLE_RANK and rank == _FLOAT_RANK:
        return value
    if to == _DECIMAL_RANK:
        return Decimal(value)
    try:
        double = float(value)
    except OverflowError:  # an integer past the double range
        double = math.copysign(math.inf, value)
    return _nearest_single(double, lambda: value) if to == _FLOAT_RANK else double


def _common(a: tuple, b: tuple):
    """The values of two numeric operands, both promoted to the wider type."""
    (ra, x), (rb, y) = a, b
    rank = max(ra, rb)
    return _promote(x, ra, rank), _promote(y, rb, rank)


def number(value):
    """The ``(rank, value)`` an evaluated value contributes to ``+`` or
    ``-``, or ``ERROR``: only a well-typed numeric literal is a number
    (§17.3); an unbound variable, a boolean, any other term or an error is a
    type error."""
    if value is UNKNOWN:
        return UNKNOWN
    if not isinstance(value, TermView):
        return ERROR
    operand = numeric_operand(value)
    return ERROR if operand is None else operand


def add(a: tuple, b: tuple, subtract: bool) -> tuple:
    """``A + B`` (or ``A - B``) over two numeric operands, in the wider of
    their types: exact for integers and decimals, IEEE for floats and
    doubles (a float sum computed in double precision and rounded once, which
    gives the single-precision sum)."""
    rank = max(a[0], b[0])
    x, y = _common(a, b)
    if rank == _DECIMAL_RANK:
        return rank, _EXACT.subtract(x, y) if subtract else _EXACT.add(x, y)
    total = x - y if subtract else x + y
    return rank, _single(total) if rank == _FLOAT_RANK else total


def _float_spelling(value: float, single: bool) -> str:
    """A float or double as Python spells it ("6.0", "1e+100"), with the
    fewest digits that read back as the same value; XML Schema's spellings of
    the special values."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "INF" if value > 0 else "-INF"
    if not single:
        return repr(value)
    for digits in range(1, 10):
        text = f"{value:.{digits}g}"
        if _single(float(text)) == value:
            break
    return text if "." in text or "e" in text else text + ".0"


#: The datatype of an arithmetic result of each rank.
_RANK_TYPE = (_XSD_INTEGER, _DECIMAL, _FLOAT, _DOUBLE)


def number_term(operand: tuple) -> TermView:
    """An arithmetic result as the literal it is: its type the operands'
    wider one, its lexical form the one rdflib writes for that value, where
    the specification leaves the form open."""
    rank, value = operand
    if rank == _INTEGER_RANK:
        lex = str(value)
    elif rank == _DECIMAL_RANK:
        lex = format(value, "f")
    else:
        lex = _float_spelling(value, rank == _FLOAT_RANK)
    return TermView(LITERAL, lex, dt=_RANK_TYPE[rank])


# --- dateTime and date: XML Schema's partial order -------------------------

_DATETIME_LEX = re.compile(
    r"(-?(?:[1-9][0-9]{3,}|0[0-9]{3}))-([0-9]{2})-([0-9]{2})"
    r"T([0-9]{2}):([0-9]{2}):([0-9]{2}(?:\.[0-9]+)?)(Z|[+-][0-9]{2}:[0-9]{2})?"
)
_DATE_LEX = re.compile(
    r"(-?(?:[1-9][0-9]{3,}|0[0-9]{3}))-([0-9]{2})-([0-9]{2})(Z|[+-][0-9]{2}:[0-9]{2})?"
)
#: How far a value without a timezone can lie from UTC: 14 hours each way.
_FOURTEEN_HOURS = 14 * 3600


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 of a proleptic Gregorian date (any year)."""
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _month_days(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _timezone_seconds(tz: str | None):
    """A timezone's offset in seconds, ``None`` for none, or ``ERROR``."""
    if tz is None:
        return None
    if tz == "Z":
        return 0
    hours, minutes = int(tz[1:3]), int(tz[4:6])
    if minutes > 59 or hours > 14 or (hours == 14 and minutes):
        return ERROR
    offset = hours * 3600 + minutes * 60
    return -offset if tz[0] == "-" else offset


def _instant(year, month, day, hour, minute, second: Decimal, tz):
    """``(seconds, has_timezone)``: seconds since the epoch, in UTC when the
    value has a timezone, else as local time; ``None`` for an invalid date."""
    if not 1 <= month <= 12 or not 1 <= day <= _month_days(year, month):
        return None
    if hour == 24:
        if minute or second:
            return None
    elif hour > 23 or minute > 59 or second >= 60:
        return None
    offset = _timezone_seconds(tz)
    if offset is ERROR:
        return None
    seconds = Decimal(_days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60)
    seconds += second
    if offset is not None:
        seconds -= offset
    return seconds, offset is not None


def _datetime_value(view: TermView):
    m = None if view.lex is None else _DATETIME_LEX.fullmatch(view.lex)
    if m is None:
        return None
    year, month, day, hour, minute = (int(g) for g in m.group(1, 2, 3, 4, 5))
    return _instant(year, month, day, hour, minute, Decimal(m.group(6)), m.group(7))


def _date_value(view: TermView):
    m = None if view.lex is None else _DATE_LEX.fullmatch(view.lex)
    if m is None:
        return None
    year, month, day = (int(g) for g in m.group(1, 2, 3))
    return _instant(year, month, day, 0, 0, Decimal(0), m.group(4))


def _instant_order(a, b):
    """-1, 0 or 1 for two ``(seconds, has_timezone)`` instants, or ``ERROR``
    when XML Schema leaves them unordered: one has a timezone, the other
    does not, and they lie within 14 hours of each other."""
    (sa, tza), (sb, tzb) = a, b
    if tza == tzb:
        return (sa > sb) - (sa < sb)
    if tza:
        if sa < sb - _FOURTEEN_HOURS:
            return -1
        if sa > sb + _FOURTEEN_HOURS:
            return 1
        return ERROR
    if sa + _FOURTEEN_HOURS < sb:
        return -1
    if sa - _FOURTEEN_HOURS > sb:
        return 1
    return ERROR


# --- value spaces ------------------------------------------------------------

_NUMERIC, _STRING, _LANG, _BOOLEAN, _DATETIME, _DATE = range(6)


def _left_open(view: TermView) -> bool:
    """A literal of an XML Schema type SPARQL 1.1 names no operator for (time,
    the durations, gYear and the other XSD types besides the numbers, strings,
    booleans, dateTimes and dates compared here): the specification leaves
    what its values equal, and how they order, to the implementation
    (§17.4.1.7), and rdflib's evaluator decides it."""
    dt = view.dt
    return (
        dt is not None
        and dt.startswith(_XSD)
        and dt not in NUMERIC_TYPES
        and dt not in (_XSD_STRING, _XSD_BOOLEAN, _XSD_DATETIME, _XSD_DATE)
    )


def literal_value(view: TermView):
    """``(space, value)`` of a literal in a value space SPARQL compares, or
    ``None`` for one it does not: an unknown datatype, or an ill-typed
    literal of a known one."""
    if view.lang:
        return _LANG, (view.lex, view.lang.lower())
    dt = view.dt
    if dt is None or dt == _XSD_STRING:
        return _STRING, view.lex
    if dt in NUMERIC_TYPES:
        operand = numeric_operand(view)
        return None if operand is None else (_NUMERIC, operand)
    if dt == _XSD_BOOLEAN:
        if view.lex in ("true", "1"):
            return _BOOLEAN, True
        if view.lex in ("false", "0"):
            return _BOOLEAN, False
        return None
    if dt == _XSD_DATETIME:
        value = _datetime_value(view)
        return None if value is None else (_DATETIME, value)
    if dt == _XSD_DATE:
        value = _date_value(view)
        return None if value is None else (_DATE, value)
    return None


def constant_view(node) -> TermView:
    """A query constant (an rdflib term) as a term view, read as a data term
    is: its lexical form as rdflib's parser left it, well-typed or not."""
    return parse_spelling(canonical_spelling(node))


def _string_like(view) -> bool:
    """A string literal SPARQL's string functions accept: simple, xsd:string
    or language-tagged."""
    return (
        isinstance(view, TermView)
        and view.kind == LITERAL
        and (view.dt is None or view.dt == _XSD_STRING)
    )


def same_term(a: TermView, b: TermView) -> bool:
    """``sameTerm``: the same RDF term, as written (a simple literal and its
    xsd:string twin are one term; language tags compare case-insensitively)."""
    if a.kind != b.kind or a.lex != b.lex:
        return False
    if a.kind != LITERAL:
        return True
    dta = None if a.dt == _XSD_STRING else a.dt
    dtb = None if b.dt == _XSD_STRING else b.dt
    return dta == dtb and (a.lang or "").lower() == (b.lang or "").lower()


def value_eq(a: TermView, b: TermView):
    """``A = B`` over two terms.

    Two literals in one value space compare by value (numbers after type
    promotion; dates in XML Schema's partial order). Two literals in
    different known value spaces are unequal. A literal SPARQL has no value
    for (an unknown datatype, an ill-typed literal) equals only itself, is
    unequal to a language-tagged literal when it has no language tag of its
    own, and is otherwise a type error (RDFterm-equal, §17.4.1.7) — or
    ``UNKNOWN``, for rdflib's evaluator, when it is of a type SPARQL leaves
    open (:func:`_left_open`) and the other literal one SPARQL compares or
    leaves open too. IRIs and blank nodes equal only themselves."""
    if a.kind != LITERAL or b.kind != LITERAL:
        return a.kind == b.kind and a.lex == b.lex
    va, vb = literal_value(a), literal_value(b)
    if va is None or vb is None:
        if same_term(a, b):
            return True
        if bool(a.lang) != bool(b.lang):
            return False
        if (_left_open(a) or _left_open(b)) and all(
            value is not None or _left_open(view) for view, value in ((a, va), (b, vb))
        ):
            return UNKNOWN
        return ERROR
    (space, x), (other, y) = va, vb
    if space != other:
        return False
    if space == _NUMERIC:
        x, y = _common(x, y)
        return x == y
    if space in (_DATETIME, _DATE):
        order = _instant_order(x, y)
        return ERROR if order is ERROR else order == 0
    return x == y


def _ordered(a: TermView, b: TermView):
    """``(A < B, A = B)`` for two terms of one ordered value space (numbers,
    strings, booleans, dateTimes, dates), ``UNKNOWN`` for two of one type
    SPARQL leaves open (:func:`_left_open`), or ``ERROR``."""
    if a.kind != LITERAL or b.kind != LITERAL:
        return ERROR
    va, vb = literal_value(a), literal_value(b)
    if va is None or vb is None or va[0] != vb[0] or va[0] == _LANG:
        return UNKNOWN if a.dt == b.dt and _left_open(a) else ERROR
    space, x, y = va[0], va[1], vb[1]
    if space == _NUMERIC:
        x, y = _common(x, y)
        return x < y, x == y
    if space in (_DATETIME, _DATE):
        order = _instant_order(x, y)
        return ERROR if order is ERROR else (order < 0, order == 0)
    return x < y, x == y


def compare(op: str, a: TermView, b: TermView):
    """A relational operator (``= != < > <= >=``) over two terms (§17.3).
    ``A <= B`` is ``A < B || A = B`` within one ordered value space, as the
    operator mapping table defines it, so a NaN is no less than, equal to or
    greater than anything."""
    if op == "=":
        return value_eq(a, b)
    if op == "!=":
        return logical_not(value_eq(a, b))
    if op in ("<", "<="):
        order = _ordered(a, b)
    else:
        order = _ordered(b, a)
    if order is ERROR or order is UNKNOWN:
        return order
    less, equal = order
    return less or equal if op in ("<=", ">=") else less


def truth(value):
    """The effective boolean value of an evaluated value (§17.2.2): a bool, a
    term view, ``None`` (unbound), ``ERROR`` or ``UNKNOWN``. A boolean is its
    value and a string its non-emptiness; a number is false for zero and NaN;
    an ill-typed boolean or number is false; anything else is an error."""
    if value is True or value is False or value is ERROR or value is UNKNOWN:
        return value
    if value is None or value.kind != LITERAL:
        return ERROR
    if value.lang:
        return len(value.lex) > 0
    dt = value.dt
    if dt is None or dt == _XSD_STRING:
        return len(value.lex) > 0
    if dt == _XSD_BOOLEAN:
        return value.lex in ("true", "1")
    if dt in NUMERIC_TYPES:
        number = numeric_value(value)
        if number is None:
            return False
        if isinstance(number, float) and math.isnan(number):
            return False
        return number != 0
    return ERROR


def logical_not(value):
    if value is True or value is False:
        return not value
    return value


# --- REGEX: XPath's flags and metacharacters ---------------------------------

#: XPath's REGEX flags (F&O §5.6.1.1) and the ``re`` flags they set.
_REGEX_FLAGS = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE, "x": 0, "q": 0}


#: What ``re.compile`` raises for a pattern it cannot compile: a syntax error,
#: a repetition count past its limit, nesting past the recursion limit.
COMPILE_ERRORS = (re.error, OverflowError, RecursionError)


#: XPath escapes Python's ``re`` lacks: Unicode categories and blocks
#: (``\p{Lu}``) and XML name characters (``\i``, ``\c``).
_XPATH_ONLY_ESCAPE = re.compile(r"\\[pPiIcC]")


#: XPath's whitespace, which flag ``x`` removes outside character classes.
_XPATH_SPACE = frozenset("\t\n\r ")


def regex_flags(flags: str | None):
    """A REGEX flags string checked against XPath's (``s m i x q``), or
    ``ERROR`` when it holds any other character."""
    flags = "" if flags is None else flags
    return flags if all(flag in _REGEX_FLAGS for flag in flags) else ERROR


def _xpath_pattern(pattern: str, flags: str) -> str:
    """An XPath regular expression (``fn:matches``) as a Python one: ``x``
    drops whitespace outside character classes, and ``$`` matches only at the
    very end unless ``m`` (Python's also matches before a final newline)."""
    out = []
    depth = 0
    chars = iter(pattern)
    for char in chars:
        if char == "\\":
            out.append(char + next(chars, ""))
            continue
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif depth == 0:
            if "x" in flags and char in _XPATH_SPACE:
                continue
            if char == "$" and "m" not in flags:
                char = r"\Z"
        out.append(char)
    return "".join(out)


def compile_regex(pattern: str, flags: str | None):
    """A REGEX pattern and its flags compiled as XPath reads them, or
    ``ERROR`` for invalid flags or a pattern XPath rejects too (§17.4.3.14:
    an invalid pattern is an error). With ``q`` the pattern is matched
    literally, and only ``i`` still applies.

    A pattern XPath accepts but ``re`` cannot compile — an XPath-only escape
    (``\\p{Lu}``), a repetition count past ``re``'s limit, nesting past the
    recursion limit — raises what ``re`` raised (:data:`COMPILE_ERRORS`): it
    cannot be evaluated here."""
    flags = regex_flags(flags)
    if flags is ERROR:
        return ERROR
    compile_flags = 0
    if "q" in flags:
        source = re.escape(pattern)
        if "i" in flags:
            compile_flags = re.IGNORECASE
    else:
        source = _xpath_pattern(pattern, flags)
        for flag in flags:
            compile_flags |= _REGEX_FLAGS[flag]
    try:
        return re.compile(source, compile_flags)
    except re.error:
        if _XPATH_ONLY_ESCAPE.search(pattern):
            raise
        return ERROR
