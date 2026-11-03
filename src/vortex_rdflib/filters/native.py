"""FILTER predicates evaluated by vortex-rdf over the codes a query produced.

A single-variable conjunct with a native shape (:func:`native_shape`)
is answered by ``TermDict.filter_codes`` over the distinct codes its variable
actually takes — the codes of a matched column, of the join step that binds
the variable, or of the rows the probes reached — never over the whole
dictionary, and nothing is remembered between calls. vortex-rdf answers each
code passed, failed or undecided; the undecided ones go on to the Python
routes (:func:`filters.evaluate_column`).

Two kinds of decision never reach ``filter_codes``: a test of the term's
kind alone is decided by the kinds it holds for (:class:`Kinds`), each code
by its kind's range, and an IRI equality (``?v = <iri>``, ``sameTerm``,
``?v IN (<iri>, ...)``) by the codes the dictionary gives its IRIs
(:class:`IriIn`). Both are keeps on the native match too — kinds whose
codes run together are one code range — (:func:`keep_of`,
:func:`native_restriction`). A conjunct's decision splits the codes a query
produced into those it passes and those it leaves undecided
(``split``).

Each native kind reads some term kinds only, and rdflib's answer for the
others needs no native call: the literal predicates fail every IRI and blank
node (``!=`` passes them), the string functions fail them too unless their
text is ``STR(?v)``, which reads an IRI as its string — sent — and a blank
node as its label — left to the Python routes. Only the codes a predicate
reads are sent, sorted and unique as ``filter_codes`` requires. A code in
none of the term kinds' ranges (the empty graph name, a code past the
dictionary's end, a negative one) is no term: it is left undecided under every
shape.

Which conjuncts have a native shape is :func:`native_shape`; the keeps the
native match takes, :func:`native_restriction`.
"""

import re
from bisect import bisect_left
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import ClassVar, NamedTuple

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Literal, URIRef
from vortex_rdf import U64Column

from .. import boundary, trace
from ..terms import (
    BLANK,
    IRI,
    LITERAL,
    KindRanges,
    canonical_spelling,
    is_range,
    unparseable_iri,
    view_of,
)
from . import values
from .exprs import VAR_LIKE
from .values import FLOAT, NUMERIC_TYPES, RDF_NIL


class FilterCodes(NamedTuple):
    """A FILTER conjunct as a predicate of one variable that vortex-rdf
    decides over the codes a query produced:
    ``TermDict.filter_codes(kind, arg, codes, flags=, case=, as_str=)``."""

    kind: str
    #: A constant's spelling, a pattern, a tag, a range.
    arg: str
    #: REGEX flags: only ``i``, ``s`` and ``m``, which vortex-rdf reads as XPath does.
    flags: str = ""
    #: ``"lower"``/``"upper"`` for a text under ``LCASE``/``UCASE``.
    case: str | None = None
    #: The text is ``STR(?v)`` rather than ``?v`` itself.
    as_str: bool = False

    def split(self, store, codes: Collection[int]) -> tuple[set, list]:
        """The codes it passes and the codes it leaves undecided
        (:func:`evaluate`)."""
        return evaluate(store, self, codes)


@dataclass(frozen=True, slots=True)
class IriIn:
    """IRI equality, a keep on the native match: true for exactly the IRIs
    ``spellings`` spell (``?v = <iri>``, ``sameTerm``, ``?v IN (<iri>, ...)``)."""

    spellings: tuple[str, ...]
    #: Its name in the query trace.
    kind: ClassVar[str] = "iri_in"

    def split(self, store, codes: Collection[int]) -> tuple[set, list]:
        """The codes it passes and the codes it leaves undecided
        (:func:`keep_decides`)."""
        return keep_decides(store, self, codes)


#: The term kinds in the order their codes run (literals, IRIs, blank nodes:
#: their spellings start ``"``, ``<``, ``_:``).
KIND_ORDER = (LITERAL, IRI, BLANK)


@dataclass(frozen=True, slots=True)
class Kinds:
    """A test of a term's kind alone, decided by the kinds it holds for —
    ``""`` among them when it holds for no term, the codes in no kind's
    range (the empty graph name) — each code by the range it falls in, none
    decoded."""

    kinds: frozenset[str]
    #: Its name in the query trace, as a keep.
    kind: ClassVar[str] = "kind_range"

    @property
    def one_range(self) -> bool:
        """Whether the kinds are a run of :data:`KIND_ORDER`, so their codes
        are one range: a keep on the native match. Not for kinds apart, for
        none, or with ``""`` among them — the codes of no term, which no
        kind's range holds."""
        if not self.kinds or "" in self.kinds:
            return False
        positions = sorted(KIND_ORDER.index(kind) for kind in self.kinds)
        return positions[-1] - positions[0] + 1 == len(positions)

    def split(self, store, codes: Collection[int]) -> tuple[set, list]:
        """The codes of its kinds; none is left undecided."""
        kind_of = store._file.kind_ranges.kind_of
        return {code for code in codes if kind_of(code) in self.kinds}, []


#: The string functions; their text is ``?v`` under rdflib's ``string()``
#: (string literals only) or, with ``as_str``, ``STR(?v)``.
STRING_KINDS = frozenset({"regex", "contains", "strstarts", "strends"})


def keep_of(store, keep: IriIn | Kinds):
    """The keep a native match takes for ``keep``: the code range of its
    kinds (:attr:`Kinds.one_range`), or the ascending codes of its IRIs
    (:func:`boundary.codes_of`; an IRI the dictionary lacks or the native
    parser refuses has no code, as it can match no stored term)."""
    if isinstance(keep, Kinds):
        ranges = store._file.kind_ranges
        bounds = [ranges.range(kind) for kind in keep.kinds]
        return min(lo for lo, _ in bounds), max(hi for _, hi in bounds)
    found = boundary.codes_of(store, list(keep.spellings)) if keep.spellings else []
    return U64Column(sorted({code for code in found if code is not None}))


def keep_decides(store, keep: IriIn, codes: Collection[int]) -> tuple[set, list]:
    """Of ``codes``, the set ``keep`` admits and the codes of no term (the
    empty graph name, a code past the dictionary's end, a negative one),
    left undecided for the Python routes; every other code fails."""
    if not codes:
        return set(), []
    member = keep_member(keep_of(store, keep))
    return {code for code in codes if member(code)}, _not_terms(codes, store._file.kind_ranges)


def _not_terms(codes: Collection[int], ranges: KindRanges) -> list:
    """The codes that are no term's: the empty graph name (below the term
    kinds' ranges), a code past the dictionary's end, a negative one. The
    ranges are adjacent and in byte order (literals, IRIs, blank nodes), so
    the terms are the codes from the literals' first to the blank nodes'
    end, and ``min`` and ``max`` clear a collection of terms without a test
    per code."""
    low = ranges.range(LITERAL)[0]
    high = ranges.range(BLANK)[1]
    if min(codes) >= low and max(codes) < high:
        return []
    return [code for code in codes if not low <= code < high]


def evaluate(store, shape: FilterCodes, codes: Collection[int]) -> tuple[set, list]:
    """Of ``codes`` — a collection of distinct codes of one variable, in any
    order — the set that passes ``shape`` and the codes left undecided, for
    the Python routes; every other code fails. A code that is no term's (the
    empty graph name, a code past the dictionary's end, a negative one) is
    undecided under every shape.

    The codes ``filter_codes`` reads (:func:`_partition`) go to it in one
    call, around which the kind's rule (:data:`_RULES`) diverts the codes it
    would answer otherwise than SPARQL before the call and checks the ones
    it passed after it."""
    if not codes:
        return set(), []
    rule = _RULES.get(shape.kind, _PLAIN)
    passed, undecided, inside = _partition(store, codes, shape.as_str, rule.passes_non_literals)
    column = U64Column(inside) if inside else None
    if column is not None and rule.pre is not None:
        inside, column = rule.pre(store, inside, column, undecided)
    if column is not None:
        passed.update(_filter_codes(store, shape, rule, inside, column, undecided))
    return passed, undecided


def _partition(store, codes: Collection[int], as_str: bool, passes_non_literals: bool):
    """``(passed, undecided, inside)`` before any call: the codes no term has
    undecided, and ``inside`` the ones ``filter_codes`` reads, ascending —
    the literals, and under ``as_str`` (``STR(?v)``, which reads an IRI as
    its string) the IRIs too, a blank node then left undecided, as rdflib
    reads it as its label. Every IRI and blank node passes a kind that
    ``passes_non_literals`` (no literal, so ``!=`` a number, §17.4.1.7)."""
    ranges = store._file.kind_ranges
    undecided = _not_terms(codes, ranges)
    literal_lo, literal_hi = ranges.range(LITERAL)
    iri_lo, iri_hi = ranges.range(IRI)
    blank_lo, blank_hi = ranges.range(BLANK)
    passed: set = set()
    if as_str:
        undecided.extend(code for code in codes if blank_lo <= code < blank_hi)
        inside = sorted(
            code for code in codes if literal_lo <= code < literal_hi or iri_lo <= code < iri_hi
        )
    else:
        if passes_non_literals:
            passed.update(
                code for code in codes if iri_lo <= code < iri_hi or blank_lo <= code < blank_hi
            )
        inside = sorted(code for code in codes if literal_lo <= code < literal_hi)
    return passed, undecided, inside


def _filter_codes(store, shape: FilterCodes, rule, inside: list, column, undecided: list) -> list:
    """The codes of ``column`` (``inside``, as a list) the one
    ``filter_codes`` call passes, after the rule's check; the codes it
    leaves undecided join ``undecided`` — every one, for an argument
    vortex-rdf refuses (a constant outside its value model)."""
    elapsed = trace.stopwatch()
    try:
        yes, maybe = boundary.term_dict(store).filter_codes(
            shape.kind, shape.arg, column, flags=shape.flags, case=shape.case, as_str=shape.as_str
        )
    except ValueError:
        undecided.extend(inside)
        return []
    yes_codes = memoryview(yes).cast("Q").tolist()
    undecided.extend(memoryview(maybe).cast("Q").tolist())
    if yes_codes and rule.post is not None:
        yes_codes = rule.post(store, shape, yes, yes_codes, undecided)
    trace.event(
        "native_filter_complete",
        kind=shape.kind,
        candidate_codes=len(inside),
        passed_codes=len(yes),
        undecided_codes=len(maybe),
        elapsed_ns=elapsed(),
    )
    return yes_codes


def _codes(store, kind: str, arg: str, column: U64Column) -> U64Column | None:
    """The codes of ``column`` that ``kind`` passes, or ``None`` when
    vortex-rdf refuses the argument."""
    try:
        return boundary.term_dict(store).filter_codes(kind, arg, column)[0]
    except ValueError:
        return None


def _divert_floats(store, inside: list, column: U64Column, undecided: list) -> tuple:
    """Before a numeric comparison: vortex-rdf compares an xsd:float as a
    double, where XPath rounds it to single precision, so the xsd:float
    codes go to the Python routes (every code, when vortex-rdf refuses the
    lookup). The codes left to send, and their column — ``None`` for
    none."""
    floats = _codes(store, "datatype", FLOAT, column)
    if floats is None:
        undecided.extend(inside)
        return [], None
    if not len(floats):
        return inside, column
    diverted = set(memoryview(floats).cast("Q").tolist())
    undecided.extend(diverted)
    inside = [code for code in inside if code not in diverted]
    return inside, U64Column(inside) if inside else None


def _only_numbers(store, shape: FilterCodes, yes: U64Column, yes_codes: list, undecided: list):
    """After an ordering: of the codes it passed, the numbers; the others go
    to the Python routes. vortex-rdf orders any literal against a number by
    datatype IRI, where SPARQL errs unless both are numbers (§17.3).
    ``num_ne`` passes only numbers, so a passed code is one when ``num_ne``
    or, for ``<=`` and ``>=``, ``num_eq`` passes it; the two are disjoint,
    so their sizes tell whether they cover every code without reading one."""
    found = []
    for kind in ("num_ne", "num_eq") if shape.kind in ("num_le", "num_ge") else ("num_ne",):
        column = _codes(store, kind, shape.arg, yes)
        if column is None:
            undecided.extend(yes_codes)
            return []
        found.append(column)
    if sum(len(column) for column in found) == len(yes):
        return yes_codes
    numbers = {code for column in found for code in memoryview(column).cast("Q").tolist()}
    undecided.extend(code for code in yes_codes if code not in numbers)
    return [code for code in yes_codes if code in numbers]


class _Rule(NamedTuple):
    """What :func:`evaluate` does for a kind around its one ``filter_codes``
    call: whether every IRI and blank node passes it without one
    (``passes_non_literals``), the codes it diverts to the Python routes
    before the call (``pre``) and the check of the codes the call passed
    (``post``). The numeric kinds need both until vortex-rdf can compare as
    SPARQL does (vortex-rdf/vortex-rdf#141), when the call alone will do."""

    passes_non_literals: bool = False
    pre: Callable | None = None
    post: Callable | None = None


_PLAIN = _Rule()


#: The relational operators as native numeric predicates, and the operator
#: that reads the same comparison with its operands swapped.
_NUM_KINDS = {
    "<": "num_lt",
    "<=": "num_le",
    ">": "num_gt",
    ">=": "num_ge",
    "=": "num_eq",
    "!=": "num_ne",
}


_SWAPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}


#: The numeric comparisons that order.
ORDER_KINDS = frozenset({"num_lt", "num_le", "num_gt", "num_ge"})


#: Each kind's rule, :data:`_PLAIN` for the kinds without one.
_RULES = {
    "num_eq": _Rule(pre=_divert_floats),
    "num_ne": _Rule(passes_non_literals=True, pre=_divert_floats),
    **dict.fromkeys(ORDER_KINDS, _Rule(pre=_divert_floats, post=_only_numbers)),
}


#: Language ranges the native basic filtering (RFC 4647 §3.3.1) decides as
#: SPARQL's langMatches does: `*`, or ASCII alphanumeric subtags.
_LANG_RANGE = re.compile(r"\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _call_on(node, name: str, var) -> bool:
    """Whether ``node`` is the builtin ``name`` applied to ``var`` itself."""
    return isinstance(node, CompValue) and node.name == name and node.arg == var


#: The string functions with a native kind, by rdflib's builtin name.
_STRING_FUNCTIONS = {
    "Builtin_CONTAINS": "contains",
    "Builtin_STRSTARTS": "strstarts",
    "Builtin_STRENDS": "strends",
}


#: The case wrappers a native string kind takes as its ``case``.
_CASE_WRAPPERS = {"Builtin_LCASE": "lower", "Builtin_UCASE": "upper"}


def _text_shape(node, var) -> tuple[str | None, bool] | None:
    """How a string function's text reads ``var``: ``(case, as_str)`` for
    ``var``, ``STR(var)`` and ``LCASE``/``UCASE`` of either — ``None`` for
    anything else."""
    case = None
    if isinstance(node, CompValue) and node.name in _CASE_WRAPPERS:
        case, node = _CASE_WRAPPERS[node.name], node.arg
    if isinstance(node, VAR_LIKE) and node == var:
        return case, False
    if _call_on(node, "Builtin_STR", var):
        return case, True
    return None


def _string_function_shape(expr, var, consts) -> FilterCodes | None:
    """CONTAINS, STRSTARTS or STRENDS of ``var``'s text and a string constant.

    rdflib's ``_compatibleStrings`` makes a tagged constant demand the text's
    own tag, compared as written: ``STR()`` text has none (an error for every
    value) and a stored tag is lower-case, so a tag that is not lower-case
    never matches either. Both stay with the Python routes."""
    text = _text_shape(expr.arg1, var)
    constant = _constant_term(expr.arg2, consts)
    if text is None or constant is None or not values.string_like(view_of(constant)):
        return None
    case, as_str = text
    language = constant.language
    if language is not None and (as_str or language != language.lower()):
        return None
    kind = _STRING_FUNCTIONS[expr.name]
    return FilterCodes(kind, canonical_spelling(constant), case=case, as_str=as_str)


def _regex_shape(expr, var, consts) -> FilterCodes | None:
    """REGEX of ``var``'s text with a pattern and flags read once, as the
    fast route reads them (:func:`values.regex_spec`). A pattern or flags
    every call errs on, a pattern only rdflib can compile, and XPath's ``x``
    and ``q``, which vortex-rdf does not read, stay with the Python
    routes."""
    text = _text_shape(expr.text, var)
    if text is None:
        return None
    spec = values.regex_spec(expr, consts)
    if not isinstance(spec, values.RegexSpec) or "x" in spec.flags or "q" in spec.flags:
        return None
    case, as_str = text
    return FilterCodes("regex", spec.pattern, flags=spec.flags, case=case, as_str=as_str)


def _iri_spelling(node, consts) -> str | None:
    """The spelling of the IRI an operand without block variables evaluates
    to, or ``None`` when it is not an IRI of the form the native parser takes
    (:func:`~vortex_rdflib.terms.unparseable_iri`)."""
    iri = _constant_term(node, consts)
    if not isinstance(iri, URIRef) or unparseable_iri(iri):
        return None
    return canonical_spelling(iri)


def _iri_equality(left, right, var, consts) -> IriIn | None:
    """``var = <iri>`` or ``<iri> = var`` (``=`` and ``sameTerm`` alike): true
    for exactly that IRI — rdflib's ``eq`` and ``==`` are term equality for
    an IRI, and no literal or blank node equals one."""
    for side, other in ((left, right), (right, left)):
        if isinstance(side, VAR_LIKE) and side == var:
            spelling = _iri_spelling(other, consts)
            return None if spelling is None else IriIn((spelling,))
    return None


def _iri_list(left, right, var, consts) -> IriIn | None:
    """``var IN (<iri>, ...)``: true for exactly those IRIs. rdflib's parser
    spells an empty list as rdf:nil, which its evaluator reads back as the
    empty list."""
    if not (isinstance(left, VAR_LIKE) and left == var):
        return None
    if not isinstance(right, list):
        return IriIn(()) if right == RDF_NIL else None
    spellings = []
    for node in right:
        spelling = _iri_spelling(node, consts)
        if spelling is None:
            return None
        spellings.append(spelling)
    return IriIn(tuple(dict.fromkeys(spellings)))


def _constant_term(node, consts: dict):
    """The rdflib term an operand without block variables evaluates to
    (:func:`values.fold`), or ``None`` — for an error too."""
    term = values.fold(node, consts)
    return term if isinstance(term, (Literal, URIRef)) else None


def native_shape(expr, var, consts: dict) -> IriIn | FilterCodes | None:
    """The native decision ``expr`` is as a test of ``var`` alone — any other
    variable seen only through ``consts`` — an IRI equality as a keep on the
    native match (:class:`IriIn`), a predicate ``filter_codes`` decides
    (:class:`FilterCodes`), or ``None``. A test of the term's kind is
    neither: its kinds are read off the compiled predicate
    (``filters._compile_conjunct``).

    The shapes: ``datatype(var) = <iri>`` and ``lang(var) = "tag"`` (either way round);
    ``langMatches(lang(var), "range")``; ``strstarts(str(var), "prefix")``;
    ``REGEX``, ``CONTAINS``, ``STRSTARTS`` and ``STRENDS`` of ``var`` or
    ``STR(var)``, either under ``LCASE``/``UCASE``, with a constant pattern or
    string; ``var = <iri>``, ``<iri> = var``, ``sameTerm`` of ``var`` and an
    IRI, and ``var IN (<iri>, ...)``; and ``var`` compared with a well-formed
    numeric literal by any of ``< <= > >= = !=`` (either way round). vortex-rdf
    answers each over the codes a query produced with rdflib's own rules
    wherever it gives a definite answer (``tests/test_native_filters.py`` pins
    that against rdflib's evaluator) and leaves every other code undecided.
    """
    if not isinstance(expr, CompValue):
        return None
    name = expr.name
    if name == "Builtin_LANGMATCHES":
        if not _call_on(expr.arg1, "Builtin_LANG", var):
            return None
        # A range is a simple literal (§17.4.3.12); any other is an error
        # for every value, which the Python routes answer.
        rng = _constant_term(expr.arg2, consts)
        if rng is None or not values.simple_literal(view_of(rng)):
            return None
        return FilterCodes("lang_matches", str(rng)) if _LANG_RANGE.fullmatch(str(rng)) else None
    if name == "Builtin_STRSTARTS" and _call_on(expr.arg1, "Builtin_STR", var):
        prefix = _constant_term(expr.arg2, consts)
        if prefix is not None and values.simple_literal(view_of(prefix)):
            return FilterCodes("str_prefix", str(prefix), as_str=True)
    if name in _STRING_FUNCTIONS:
        return _string_function_shape(expr, var, consts)
    if name == "Builtin_REGEX":
        return _regex_shape(expr, var, consts)
    if name == "Builtin_sameTerm":
        return _iri_equality(expr.arg1, expr.arg2, var, consts)
    if name != "RelationalExpression" or expr.other is None:
        return None
    op, left, right = expr.op, expr.expr, expr.other
    if op == "IN":
        return _iri_list(left, right, var, consts)
    if isinstance(right, list):
        return None
    if op == "=":
        for call, other in ((left, right), (right, left)):
            if _call_on(call, "Builtin_DATATYPE", var):
                datatype = _constant_term(other, consts)
                if not isinstance(datatype, URIRef):
                    return None
                return FilterCodes("datatype", str(datatype))
            if _call_on(call, "Builtin_LANG", var):
                tag = _constant_term(other, consts)
                if tag is None or not values.simple_literal(view_of(tag)):
                    return None
                return FilterCodes("lang", str(tag))
        iri = _iri_equality(left, right, var, consts)
        if iri is not None:
            return iri
    kind = _NUM_KINDS.get(op)
    if kind is None:
        return None
    if isinstance(left, VAR_LIKE) and left == var:
        constant = _constant_term(right, consts)
    elif isinstance(right, VAR_LIKE) and right == var:
        constant = _constant_term(left, consts)
        kind = _NUM_KINDS[_SWAPPED[op]]
    else:
        return None
    # A well-typed number, but no xsd:float: vortex-rdf compares one as a
    # double, where XPath rounds it to single precision first.
    if (
        not isinstance(constant, Literal)
        or str(constant.datatype) not in NUMERIC_TYPES
        or str(constant.datatype) == FLOAT
        or values.numeric_value(view_of(constant)) is None
    ):
        return None
    return FilterCodes(kind, canonical_spelling(constant))


def _sorted_contains(view, code: int) -> bool:
    index = bisect_left(view, code)
    return index < len(view) and view[index] == code


def native_restriction(store, conjuncts: list) -> tuple:
    """``(keep, residual)`` for one variable's conjuncts. ``keep`` admits
    exactly the codes passing every conjunct with a keep
    (``Conjunct.keep``, :func:`keep_of`) — ``None`` when there is none — and
    ``residual`` are the other conjuncts, evaluated over the codes the query
    produces (:func:`evaluate_column`)."""
    keeps, residual = [], []
    for conjunct in conjuncts:
        if conjunct.keep is None:
            residual.append(conjunct)
        else:
            keeps.append(keep_of(store, conjunct.keep))
    return (intersect_keeps(keeps) if keeps else None), residual


def intersect_keeps(keeps: list):
    """The intersection of native keeps: ranges intersect as ranges, code
    columns (ascending, unique) through the native merge join, and a range
    over a column slices it."""
    ranges = [keep for keep in keeps if is_range(keep)]
    columns = sorted((keep for keep in keeps if not is_range(keep)), key=len)
    if ranges:
        lo = max(lo for lo, _ in ranges)
        hi = max(lo, min(hi for _, hi in ranges))
    if not columns:
        return (lo, hi)
    result = columns[0]
    for other in columns[1:]:
        if not len(result):
            break
        left, _ = result.join_indices(other)
        result = result.take(left)
    if ranges:
        view = memoryview(result).cast("Q")
        start, stop = bisect_left(view, lo), bisect_left(view, hi)
        if (start, stop) != (0, len(view)):
            result = U64Column(view[start:stop])
    return result


def keep_member(keep) -> Callable[[int], bool]:
    """A native keep as a test of one code, for rows that did not come
    through a keep-narrowed native match."""
    if is_range(keep):
        lo, hi = keep
        return lambda code: lo <= code < hi
    view = memoryview(keep).cast("Q")
    return lambda code: _sorted_contains(view, code)
