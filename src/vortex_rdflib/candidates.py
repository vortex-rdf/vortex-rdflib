"""FILTER predicates evaluated by vortex-rdf over the codes a query produced.

A single-variable conjunct with a native shape (:func:`filters.native_shape`)
is answered by ``TermDict.filter_codes`` over the distinct codes its variable
actually takes — the codes of a matched column, of the join step that binds
the variable, or of the rows the probes reached — never over the whole
dictionary, and nothing is remembered between calls. vortex-rdf answers each
code passed, failed or undecided; the undecided ones go on to the Python
routes (:func:`filters.evaluate_column`).

Two kinds of shape never reach ``filter_codes``: a kind test (``isIRI``,
``isBlank``, ``isLiteral``) is its kind's code range, and an IRI equality
(``?v = <iri>``, ``sameTerm``, ``?v IN (<iri>, ...)``) the codes
``encode_many`` gives its IRIs. Both are keeps on the native match
(:func:`keep_of`, :func:`filters.native_restriction`), and both are decided
here too when a specialized conjunct meets codes directly.

Each native kind reads some term kinds only, and rdflib's answer for the
others needs no native call: the literal predicates fail every IRI and blank
node (``!=`` passes them), the string functions fail them too unless their
text is ``STR(?v)``, which reads an IRI as its string — sent — and a blank
node as its label — left to the Python routes. Only the codes a predicate
reads are sent, sorted and unique as ``filter_codes`` requires.
"""

import time
from collections.abc import Callable
from typing import NamedTuple

from vortex_rdf import U64Column

from .terms import BLANK, IRI, LITERAL


class NativeShape(NamedTuple):
    """A FILTER conjunct as a native predicate of one variable:
    ``TermDict.filter_codes(kind, arg, codes, flags=, case=, as_str=)``, or a
    kind test or IRI equality (:data:`KIND_TESTS`, :data:`IRI_IN`)."""

    kind: str
    #: A constant's spelling, a pattern, a tag — or, for IRI_IN, the IRIs'.
    arg: str | tuple[str, ...]
    #: REGEX flags, only those rdflib maps (``i``, ``s``, ``m``).
    flags: str = ""
    #: ``"lower"``/``"upper"`` for a text under ``LCASE``/``UCASE``.
    case: str | None = None
    #: The text is ``STR(?v)`` rather than ``?v`` itself.
    as_str: bool = False


#: The kind tests, each its kind's code range.
KIND_TESTS = {"is_literal": LITERAL, "is_iri": IRI, "is_blank": BLANK}
#: IRI equality: true for exactly the IRIs ``NativeShape.arg`` spells.
IRI_IN = "iri_in"
#: The string functions; their text is ``?v`` under rdflib's ``string()``
#: (string literals only) or, with ``as_str``, ``STR(?v)``.
STRING_KINDS = frozenset({"regex", "contains", "strstarts", "strends"})

#: The pushdown's query trace (``pushdown._trace_native_filter``), installed
#: when that module is imported; called after every ``filter_codes`` call
#: with the kind and how many codes were sent, passed and left undecided.
TRACE: Callable[[str, int, int, int, int], None] | None = None


def keep_of(store, shape: NativeShape):
    """The keep ``shape`` is on a native match — its kind's code range for a
    kind test, the ascending codes of its IRIs for an IRI equality (one
    ``encode_many``; an IRI the dictionary lacks has no code, as it can match
    no stored term) — or ``None`` for any other shape. A spelling the native
    parser refuses raises ``ValueError``."""
    if shape.kind in KIND_TESTS:
        return store._kind_ranges()[KIND_TESTS[shape.kind]]
    if shape.kind == IRI_IN:
        spellings = list(shape.arg)
        found = store._dict.encode_many(spellings) if spellings else []
        return U64Column(sorted({code for code in found if code is not None}))
    return None


def evaluate(store, shape: NativeShape, codes) -> tuple[set, list]:
    """Of ``codes`` — distinct codes of one variable, in any order — the set
    that passes ``shape`` and the codes left undecided, for the Python
    routes; every other code fails."""
    try:
        keep = keep_of(store, shape)
    except ValueError:
        return set(), list(codes)
    if isinstance(keep, tuple):
        lo, hi = keep
        return {code for code in codes if lo <= code < hi}, []
    if keep is not None:
        allowed = set(memoryview(keep).cast("Q").tolist())
        return {code for code in codes if code in allowed}, []
    ranges = store._kind_ranges()
    literal_lo, literal_hi = ranges[LITERAL]
    iri_lo, iri_hi = ranges[IRI]
    blank_lo, blank_hi = ranges[BLANK]
    passed: set = set()
    undecided: list = []
    if shape.kind == "str_prefix" or (shape.kind in STRING_KINDS and shape.as_str):
        # STR(?v): an IRI reads as its string, a blank node as its label.
        undecided.extend(code for code in codes if blank_lo <= code < blank_hi)
        inside = sorted(
            code for code in codes if literal_lo <= code < literal_hi or iri_lo <= code < iri_hi
        )
    else:
        if shape.kind == "num_ne":
            # rdflib's != is true for an IRI or a blank node against a number.
            passed.update(
                code for code in codes if iri_lo <= code < iri_hi or blank_lo <= code < blank_hi
            )
        inside = sorted(code for code in codes if literal_lo <= code < literal_hi)
    if not inside:
        return passed, undecided
    assert isinstance(shape.arg, str)
    options: dict = {}
    if shape.flags:
        options["flags"] = shape.flags
    if shape.case is not None:
        options["case"] = shape.case
    if shape.as_str or shape.kind == "str_prefix":
        # str_prefix is STRSTARTS(STR(?v), ...): filter_codes reads an IRI or a
        # typed literal as STR() only under as_str.
        options["as_str"] = True
    started = time.perf_counter_ns()
    try:
        yes, maybe = store._dict.filter_codes(shape.kind, shape.arg, U64Column(inside), **options)
    except ValueError:
        # An argument vortex-rdf refuses (a constant outside its value
        # model): every candidate goes to the Python routes.
        undecided.extend(inside)
        return passed, undecided
    passed.update(memoryview(yes).cast("Q").tolist())
    undecided.extend(memoryview(maybe).cast("Q").tolist())
    if TRACE is not None:
        TRACE(shape.kind, len(inside), len(yes), len(maybe), time.perf_counter_ns() - started)
    return passed, undecided
