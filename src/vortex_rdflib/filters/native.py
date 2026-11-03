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
from bisect import bisect_left
from collections.abc import Callable, Collection
from typing import NamedTuple

from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Literal, URIRef
from vortex_rdf import U64Column

from .. import boundary, trace
from ..settings import SETTINGS
from ..terms import BLANK, IRI, LITERAL, KindRanges, canonical_spelling
from . import exprs, values
from .exprs import VAR_LIKE
from .values import FLOAT, NUMERIC_TYPES, RDF_NIL


class NativeShape(NamedTuple):
    """A FILTER conjunct as a native predicate of one variable:
    ``TermDict.filter_codes(kind, arg, codes, flags=, case=, as_str=)``, or a
    kind test or IRI equality (:data:`KIND_TESTS`, :data:`IRI_IN`)."""

    kind: str
    #: A constant's spelling, a pattern, a tag — or, for IRI_IN, the IRIs'.
    arg: str | tuple[str, ...]
    #: REGEX flags: only ``i``, ``s`` and ``m``, which vortex-rdf reads as XPath does.
    flags: str = ""
    #: ``"lower"``/``"upper"`` for a text under ``LCASE``/``UCASE``.
    case: str | None = None
    #: The text is ``STR(?v)`` rather than ``?v`` itself.
    as_str: bool = False


#: The kind tests, each its kind's code range.
KIND_TESTS = {"is_literal": LITERAL, "is_iri": IRI, "is_blank": BLANK}
_KIND_SHAPES = {kind: name for name, kind in KIND_TESTS.items()}


#: IRI equality: true for exactly the IRIs ``NativeShape.arg`` spells.
IRI_IN = "iri_in"


#: The string functions; their text is ``?v`` under rdflib's ``string()``
#: (string literals only) or, with ``as_str``, ``STR(?v)``.
STRING_KINDS = frozenset({"regex", "contains", "strstarts", "strends"})


def keep_of(store, shape: NativeShape):
    """The keep ``shape`` is on a native match — its kind's code range for a
    kind test, the ascending codes of its IRIs for an IRI equality (one
    ``encode_many``; an IRI the dictionary lacks has no code, as it can match
    no stored term) — or ``None`` for any other shape. A spelling the native
    parser refuses raises ``ValueError``."""
    if shape.kind in KIND_TESTS:
        return store._file.kind_ranges.range(KIND_TESTS[shape.kind])
    if shape.kind == IRI_IN:
        spellings = list(shape.arg)
        found = boundary.term_dict(store).encode_many(spellings) if spellings else []
        return U64Column(sorted({code for code in found if code is not None}))
    return None


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
    ranges = store._file.kind_ranges
    undecided = _not_terms(codes, ranges)
    if isinstance(keep, tuple):
        lo, hi = keep
        return {code for code in codes if lo <= code < hi}, undecided
    if keep is not None:
        allowed = set(memoryview(keep).cast("Q").tolist())
        return {code for code in codes if code in allowed}, undecided
    literal_lo, literal_hi = ranges.range(LITERAL)
    iri_lo, iri_hi = ranges.range(IRI)
    blank_lo, blank_hi = ranges.range(BLANK)
    passed: set = set()
    # Under as_str (STR(?v): str_prefix always, the string kinds when asked)
    # filter_codes reads an IRI or a typed literal as STR().
    as_str = shape.as_str
    if as_str:
        # STR(?v): an IRI reads as its string, a blank node as its label.
        undecided.extend(code for code in codes if blank_lo <= code < blank_hi)
        inside = sorted(
            code for code in codes if literal_lo <= code < literal_hi or iri_lo <= code < iri_hi
        )
    else:
        if shape.kind == "num_ne":
            # An IRI or a blank node is no literal, so != a number (§17.4.1.7).
            passed.update(
                code for code in codes if iri_lo <= code < iri_hi or blank_lo <= code < blank_hi
            )
        inside = sorted(code for code in codes if literal_lo <= code < literal_hi)
    if not inside:
        return passed, undecided
    column = U64Column(inside)
    if shape.kind in NUMERIC_KINDS:
        # vortex-rdf compares an xsd:float as a double, where XPath rounds it
        # to single precision: those go to the Python routes.
        floats = _codes(store, "datatype", FLOAT, column)
        if floats is None:
            undecided.extend(inside)
            return passed, undecided
        if len(floats):
            diverted = set(memoryview(floats).cast("Q").tolist())
            undecided.extend(diverted)
            inside = [code for code in inside if code not in diverted]
            if not inside:
                return passed, undecided
            column = U64Column(inside)
    assert isinstance(shape.arg, str)
    options: dict = {}
    if shape.flags:
        options["flags"] = shape.flags
    if shape.case is not None:
        options["case"] = shape.case
    if as_str:
        options["as_str"] = True
    elapsed = trace.stopwatch()
    try:
        yes, maybe = boundary.term_dict(store).filter_codes(
            shape.kind, shape.arg, column, **options
        )
    except ValueError:
        # An argument vortex-rdf refuses (a constant outside its value
        # model): every candidate goes to the Python routes.
        undecided.extend(inside)
        return passed, undecided
    yes_codes = memoryview(yes).cast("Q").tolist()
    undecided.extend(memoryview(maybe).cast("Q").tolist())
    if shape.kind in ORDER_KINDS and yes_codes:
        numbers = _numbers(store, shape, yes)
        if numbers is not None:
            undecided.extend(code for code in yes_codes if code not in numbers)
            yes_codes = [code for code in yes_codes if code in numbers]
    passed.update(yes_codes)
    trace.event(
        "native_filter_complete",
        kind=shape.kind,
        candidate_codes=len(inside),
        passed_codes=len(yes),
        undecided_codes=len(maybe),
        elapsed_ns=elapsed(),
    )
    return passed, undecided


def _codes(store, kind: str, arg: str, column: U64Column) -> U64Column | None:
    """The codes of ``column`` that ``kind`` passes, or ``None`` when
    vortex-rdf refuses the argument."""
    try:
        return boundary.term_dict(store).filter_codes(kind, arg, column)[0]
    except ValueError:
        return None


def _numbers(store, shape: NativeShape, ordered: U64Column) -> set | None:
    """Of the codes an ordering shape passed, the set that are numbers, or
    ``None`` when every one is.

    vortex-rdf orders any literal against a number by datatype IRI, where
    SPARQL errs unless both are numbers (§17.3). ``num_ne`` passes only
    numbers, so a passed code is one when ``num_ne`` or, for ``<=`` and
    ``>=``, ``num_eq`` passes it; the two are disjoint, so their sizes tell
    whether they cover every code without reading one."""
    assert isinstance(shape.arg, str)
    found = []
    for kind in ("num_ne", "num_eq") if shape.kind in ("num_le", "num_ge") else ("num_ne",):
        column = _codes(store, kind, shape.arg, ordered)
        if column is None:
            return set()
        found.append(column)
    if sum(len(column) for column in found) == len(ordered):
        return None
    return {code for column in found for code in memoryview(column).cast("Q").tolist()}


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


#: The numeric comparisons, and those of them that order.
NUMERIC_KINDS = frozenset(_NUM_KINDS.values())
ORDER_KINDS = frozenset({"num_lt", "num_le", "num_gt", "num_ge"})


#: Language ranges the native basic filtering (RFC 4647 §3.3.1) decides as
#: SPARQL's langMatches does: `*`, or ASCII alphanumeric subtags.
_LANG_RANGE = re.compile(r"\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


def _call_on(node, name: str, var) -> bool:
    """Whether ``node`` is the builtin ``name`` applied to ``var`` itself."""
    return isinstance(node, CompValue) and node.name == name and node.arg == var


def _string_literal(term) -> bool:
    """A string literal SPARQL's string functions take (:func:`values.string_like`)."""
    return term is not None and values.string_like(values.constant_view(term))


def _string_constant(term) -> bool:
    """A simple literal: what the native string predicates take as their
    argument (:func:`values.simple_literal`)."""
    return term is not None and values.simple_literal(values.constant_view(term))


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
    """REGEX of ``var``'s text with a pattern and flags read once, as the
    fast route reads them (:func:`values.regex_spec`). A pattern or flags
    every call errs on, a pattern only rdflib can compile, and XPath's ``x``
    and ``q``, which vortex-rdf does not read, stay with the Python
    routes."""
    text = _text_shape(expr.text, var)
    if text is None:
        return None
    pattern = values.fold(expr.pattern, consts)
    flags = None if expr.flags is None else values.fold(expr.flags, consts)
    if pattern is None or (expr.flags is not None and flags is None):
        return None
    spec = values.regex_spec(pattern, flags)
    if not isinstance(spec, values.RegexSpec) or "x" in spec.flags or "q" in spec.flags:
        return None
    case, as_str = text
    return NativeShape("regex", spec.pattern, flags=spec.flags, case=case, as_str=as_str)


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
        if isinstance(side, VAR_LIKE) and side == var:
            spelling = _iri_spelling(other, consts)
            return None if spelling is None else NativeShape(IRI_IN, (spelling,))
    return None


def _iri_list(left, right, var, consts) -> NativeShape | None:
    """``var IN (<iri>, ...)``: true for exactly those IRIs. rdflib's parser
    spells an empty list as rdf:nil, which its evaluator reads back as the
    empty list."""
    if not (isinstance(left, VAR_LIKE) and left == var):
        return None
    if not isinstance(right, list):
        return NativeShape(IRI_IN, ()) if right == RDF_NIL else None
    spellings = []
    for node in right:
        spelling = _iri_spelling(node, consts)
        if spelling is None:
            return None
        spellings.append(spelling)
    return NativeShape(IRI_IN, tuple(dict.fromkeys(spellings)))


def _constant_term(node, consts: dict):
    """The rdflib term an operand without block variables evaluates to
    (:func:`values.fold`), or ``None`` — for an error too."""
    term = values.fold(node, consts)
    return term if isinstance(term, (Literal, URIRef)) else None


def native_shape(expr, var, consts: dict) -> NativeShape | None:
    """:func:`_native_shape`, except that with ``VORTEX_RDF_CODE_PREDICATES=0`` a kind test or an
    IRI equality is not one: it is decided over decoded values, not as a code range or code set."""
    shape = _native_shape(expr, var, consts)
    if (
        shape is not None
        and not SETTINGS.code_predicates
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
    tested = exprs.KIND_BUILTINS.get(name)
    if tested is not None:
        return NativeShape(_KIND_SHAPES[tested], "") if expr.arg == var else None
    if name == "Builtin_LANGMATCHES":
        if not _call_on(expr.arg1, "Builtin_LANG", var):
            return None
        # A range is a simple literal (§17.4.3.12); any other is an error
        # for every value, which the Python routes answer.
        rng = _constant_term(expr.arg2, consts)
        if not _string_constant(rng):
            return None
        return NativeShape("lang_matches", str(rng)) if _LANG_RANGE.fullmatch(str(rng)) else None
    if name == "Builtin_STRSTARTS" and _call_on(expr.arg1, "Builtin_STR", var):
        prefix = _constant_term(expr.arg2, consts)
        if _string_constant(prefix):
            return NativeShape("str_prefix", str(prefix), as_str=True)
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
        or values.numeric_value(values.constant_view(constant)) is None
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
