"""FILTER predicates evaluated by vortex-rdf over the codes a query produced.

A single-variable conjunct with a native shape (:func:`native_shape`)
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
(:func:`keep_of`, :func:`native_restriction`), and both are decided
here too when a specialized conjunct meets codes directly.

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
import time
from bisect import bisect_left
from collections.abc import Callable, Collection
from typing import NamedTuple

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Literal, URIRef
from vortex_rdf import U64Column

from .. import ablation
from ..settings import SETTINGS
from ..terms import BLANK, IRI, LITERAL, canonical_spelling
from . import compiler, exprs, values
from .exprs import _VAR_LIKE
from .values import _COMPILE_ERRORS, _RDF_NIL, _XSD_STRING, NUMERIC_TYPES


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


#: The pushdown's query trace (``pushdown.tracing._trace_native_filter``), installed
#: when that module is imported; called after every ``filter_codes`` call
#: that answers, with the kind, how many codes were sent, passed and left
#: undecided, and the call's nanoseconds.
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


def _not_terms(codes: Collection[int], ranges: dict) -> list:
    """The codes that are no term's: the empty graph name (below the term
    kinds' ranges), a code past the dictionary's end, a negative one. The
    ranges are adjacent (literals, IRIs, blank nodes), so the terms are the
    codes from the lowest bound to the highest, and ``min`` and ``max`` clear
    a collection of terms without a test per code."""
    low = min(lo for lo, _ in ranges.values())
    high = max(hi for _, hi in ranges.values())
    if min(codes) >= low and max(codes) < high:
        return []
    return [code for code in codes if not low <= code < high]


def evaluate(store, shape: NativeShape, codes: Collection[int]) -> tuple[set, list]:
    """Of ``codes`` — a collection of distinct codes of one variable, in any
    order — the set that passes ``shape`` and the codes left undecided, for
    the Python routes; every other code fails. A code that is no term's (the
    empty graph name, a code past the dictionary's end, a negative one) is
    undecided under every shape."""
    if not codes:
        return set(), []
    try:
        keep = keep_of(store, shape)
    except ValueError:
        return set(), list(codes)
    ranges = store._kind_ranges()
    undecided = _not_terms(codes, ranges)
    if isinstance(keep, tuple):
        lo, hi = keep
        return {code for code in codes if lo <= code < hi}, undecided
    if keep is not None:
        allowed = set(memoryview(keep).cast("Q").tolist())
        return {code for code in codes if code in allowed}, undecided
    literal_lo, literal_hi = ranges[LITERAL]
    iri_lo, iri_hi = ranges[IRI]
    blank_lo, blank_hi = ranges[BLANK]
    passed: set = set()
    # str_prefix is STRSTARTS(STR(?v), ...): it reads STR() like the string
    # kinds under as_str, and filter_codes reads an IRI or a typed literal as
    # STR() only under as_str.
    as_str = shape.kind == "str_prefix" or (shape.kind in STRING_KINDS and shape.as_str)
    if as_str:
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
    if as_str:
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


#: Language ranges on which the native basic filtering (RFC 4647 §3.3.1) and
#: rdflib's `_lang_range_check` agree: `*`, or ASCII alphanumeric subtags.
_LANG_RANGE = re.compile(r"\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _call_on(node, name: str, var) -> bool:
    """Whether ``node`` is the builtin ``name`` applied to ``var`` itself."""
    return isinstance(node, CompValue) and node.name == name and node.arg == var


def _string_literal(term) -> bool:
    """rdflib's ``string()``: a plain, ``xsd:string`` or language-tagged
    literal."""
    return isinstance(term, Literal) and (
        term.datatype is None or str(term.datatype) == _XSD_STRING
    )


def _string_constant(term) -> bool:
    """A plain or ``xsd:string`` literal: what the native string predicates
    take as their argument."""
    return _string_literal(term) and term.language is None


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
    if isinstance(node, _VAR_LIKE) and node == var:
        return case, False
    if _call_on(node, "Builtin_STR", var):
        return case, True
    return None


def _string_function_shape(expr, var, consts) -> NativeShape | None:
    """CONTAINS, STRSTARTS or STRENDS of ``var``'s text and a string constant.

    rdflib's ``_compatibleStrings`` makes a tagged constant demand the text's
    own tag, compared as written: ``STR()`` text has none (an error for every
    value) and a stored tag is lower-case, so a tag that is not lower-case
    never matches either. Both stay with the Python routes."""
    text = _text_shape(expr.arg1, var)
    constant = _constant_term(expr.arg2, consts)
    if text is None or not _string_literal(constant):
        return None
    case, as_str = text
    language = constant.language
    if language is not None and (as_str or language != language.lower()):
        return None
    kind = _STRING_FUNCTIONS[expr.name]
    return NativeShape(kind, canonical_spelling(constant), case=case, as_str=as_str)


def _regex_shape(expr, var, consts) -> NativeShape | None:
    """REGEX of ``var``'s text with a constant string pattern and flags.

    rdflib compiles the pattern with Python's ``re`` and raises for every
    string text when that fails, so such a pattern stays with rdflib. Only
    the flags rdflib maps (``i``, ``s``, ``m``) are kept; it ignores the
    rest."""
    text = _text_shape(expr.text, var)
    pattern = _constant_term(expr.pattern, consts)
    flags = None if expr.flags is None else _constant_term(expr.flags, consts)
    if text is None or not _string_literal(pattern):
        return None
    if expr.flags is not None and not _string_literal(flags):
        return None
    kept, compile_flags = values._regex_flags(flags)
    try:
        re.compile(str(pattern), compile_flags)
    except _COMPILE_ERRORS:
        return None
    case, as_str = text
    return NativeShape("regex", str(pattern), flags=kept, case=case, as_str=as_str)


#: IRIs ``canonical_spelling`` spells as an N-Triples IRIREF.
_SPELLABLE_IRI = re.compile(r'[^\x00-\x20<>"{}|^`\\]*')


def _iri_spelling(node, consts) -> str | None:
    """The spelling of the IRI an operand without block variables evaluates
    to, or ``None`` when it is not an IRI the dictionary could hold."""
    iri = _constant_term(node, consts)
    if not isinstance(iri, URIRef) or not _SPELLABLE_IRI.fullmatch(str(iri)):
        return None
    return canonical_spelling(iri)


def _iri_equality(left, right, var, consts) -> NativeShape | None:
    """``var = <iri>`` or ``<iri> = var`` (``=`` and ``sameTerm`` alike): true
    for exactly that IRI — rdflib's ``eq`` and ``==`` are term equality for
    an IRI, and no literal or blank node equals one."""
    for side, other in ((left, right), (right, left)):
        if isinstance(side, _VAR_LIKE) and side == var:
            spelling = _iri_spelling(other, consts)
            return None if spelling is None else NativeShape(IRI_IN, (spelling,))
    return None


def _iri_list(left, right, var, consts) -> NativeShape | None:
    """``var IN (<iri>, ...)``: true for exactly those IRIs. rdflib's parser
    spells an empty list as rdf:nil, which its evaluator reads back as the
    empty list."""
    if not (isinstance(left, _VAR_LIKE) and left == var):
        return None
    if not isinstance(right, list):
        return NativeShape(IRI_IN, ()) if right == _RDF_NIL else None
    spellings = []
    for node in right:
        spelling = _iri_spelling(node, consts)
        if spelling is None:
            return None
        spellings.append(spelling)
    return NativeShape(IRI_IN, tuple(dict.fromkeys(spellings)))


def _constant_term(node, consts: dict):
    """The rdflib term an operand without block variables evaluates to: a
    constant, a visible ctx-bound variable, or an expression over those,
    which rdflib's own evaluator computes. ``None`` for an error or a value
    that is not a term."""
    if isinstance(node, (Literal, URIRef)):
        return node
    if isinstance(node, _VAR_LIKE):
        term = consts.get(node)
        return term if isinstance(term, (Literal, URIRef)) else None
    if not isinstance(node, CompValue) or any(v not in consts for v in exprs.expr_vars(node)):
        return None
    try:
        result = node.eval(dict(consts))
    except Exception:  # noqa: BLE001 - not a value this route can rely on
        return None
    return result if isinstance(result, (Literal, URIRef)) else None


def native_shape(expr, var, consts: dict) -> NativeShape | None:
    """:func:`_native_shape`, except that with ``VORTEX_RDF_CODE_PREDICATES=0`` a kind test or an
    IRI equality is not one: it is decided over decoded values, not as a code range or code set."""
    shape = _native_shape(expr, var, consts)
    if (
        shape is not None
        and not ablation.CODE_PREDICATES
        and (shape.kind in KIND_TESTS or shape.kind == IRI_IN)
    ):
        return None
    return shape


def _native_shape(expr, var, consts: dict) -> NativeShape | None:
    """The native predicate (:class:`NativeShape`)
    that ``expr`` is as a test of ``var`` alone — any other variable seen only
    through ``consts`` — or ``None``.

    The shapes: ``isIRI``/``isURI``/``isBlank``/``isLiteral`` of ``var``;
    ``datatype(var) = <iri>`` and ``lang(var) = "tag"`` (either way round);
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
    if name in ("Builtin_isIRI", "Builtin_isURI", "Builtin_isBLANK", "Builtin_isLITERAL"):
        if expr.arg != var:
            return None
        kind = {"Builtin_isBLANK": "is_blank", "Builtin_isLITERAL": "is_literal"}.get(
            name, "is_iri"
        )
        return NativeShape(kind, "")
    if name == "Builtin_LANGMATCHES":
        if not _call_on(expr.arg1, "Builtin_LANG", var):
            return None
        # rdflib's `string()` admits a language-tagged range: its lexical
        # form is the range either way.
        rng = _constant_term(expr.arg2, consts)
        if not _string_literal(rng):
            return None
        return NativeShape("lang_matches", str(rng)) if _LANG_RANGE.fullmatch(str(rng)) else None
    if name == "Builtin_STRSTARTS" and _call_on(expr.arg1, "Builtin_STR", var):
        prefix = _constant_term(expr.arg2, consts)
        if _string_constant(prefix):
            return NativeShape("str_prefix", str(prefix))
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
                return NativeShape("datatype", str(datatype))
            if _call_on(call, "Builtin_LANG", var):
                tag = _constant_term(other, consts)
                return NativeShape("lang", str(tag)) if _string_constant(tag) else None
        iri = _iri_equality(left, right, var, consts)
        if iri is not None:
            return iri
    kind = _NUM_KINDS.get(op)
    if kind is None:
        return None
    if isinstance(left, _VAR_LIKE) and left == var:
        constant = _constant_term(right, consts)
    elif isinstance(right, _VAR_LIKE) and right == var:
        constant = _constant_term(left, consts)
        kind = _NUM_KINDS[_SWAPPED[op]]
    else:
        return None
    # A constant inside the value model both sides share — rdflib compares a
    # typed query constant by its value even out of its type's bounds (see
    # `_constant_view`), which the native layer refuses to.
    if (
        not isinstance(constant, Literal)
        or str(constant.datatype) not in NUMERIC_TYPES
        or values.numeric_value(compiler.view_of_node(constant)) is None
    ):
        return None
    return NativeShape(kind, canonical_spelling(constant))


def _sorted_contains(view, code: int) -> bool:
    index = bisect_left(view, code)
    return index < len(view) and view[index] == code


def native_restriction(store, conjuncts: list) -> tuple:
    """``(keep, residual)`` for one variable's conjuncts. ``keep`` admits
    exactly the codes passing every conjunct vortex-rdf answers with a keep
    (:func:`keep_of`) — ``None`` when there is none — and
    ``residual`` are the other conjuncts, evaluated over the codes the query
    produces (:func:`evaluate_column`)."""
    if not (SETTINGS.fast_filters and SETTINGS.native_filters):
        return None, conjuncts
    keeps, residual = [], []
    for conjunct in conjuncts:
        keep = None
        if conjunct.native is not None:
            try:
                keep = keep_of(store, conjunct.native)
            except ValueError:
                keep = None  # a constant the native parser refuses
        if keep is None:
            residual.append(conjunct)
        else:
            keeps.append(keep)
    return (intersect_keeps(keeps) if keeps else None), residual


def intersect_keeps(keeps: list):
    """The intersection of native keeps: ranges intersect as ranges, code
    columns (ascending, unique) through the native merge join, and a range
    over a column slices it."""
    ranges = [keep for keep in keeps if isinstance(keep, tuple)]
    columns = sorted((keep for keep in keeps if not isinstance(keep, tuple)), key=len)
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
    if isinstance(keep, tuple):
        lo, hi = keep
        return lambda code: lo <= code < hi
    view = memoryview(keep).cast("Q")
    return lambda code: _sorted_contains(view, code)
