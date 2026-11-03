"""FILTER expressions evaluated over term codes.

A ``Filter`` node's expression is split into its top-level conjuncts (rdflib
folds every FILTER of a group into one ``ConditionalAndExpression``; a row
passes iff every conjunct is true, so the split is exact). Each conjunct is
classified by the block variables it references: none (evaluated once), one
(a per-variable predicate applied to the pattern scan that binds it, before
any join), or several (applied on the joined rows). Either way a conjunct is
evaluated **per distinct value**, never per row, through two routes:

- **fast**: a whitelist of expression shapes compiled to a predicate over the
  parsed spelling (:class:`~vortex_rdflib.terms.TermView`). Every leaf
  answers as SPARQL 1.1 does (:mod:`.values`; ``docs/pushdown.md``, "SPARQL
  semantics"): ``True``, ``False``, ``ERROR`` (a type error, which a filter
  turns into false) or ``UNKNOWN`` — a comparison of values of an XML Schema
  type SPARQL 1.1 leaves to the implementation, deferred, alone, to
- **generic**: rdflib's own evaluator (``_ebv``) on a ``FrozenBindings`` with
  the value decoded to an rdflib term, at rdflib's per-evaluation cost, but
  paid once per distinct value. It answers as rdflib does, and takes every
  conjunct outside the whitelist.

``&&`` and ``||`` follow SPARQL's truth tables (§17.2): ``false && E`` and
``E && false`` are false, ``true || E`` and ``E || true`` are true, any other
error propagates, and an ``UNKNOWN`` operand leaves the answer ``UNKNOWN``
unless another settles it.

How each single-variable conjunct is decided is settled once, when it is
compiled (:func:`_compile_conjunct`), into typed fields, each ``None`` while
its switch is off: ``kinds``, the term kinds a test of the variable's kind
alone is true for, which decides every code without decoding it; ``keep``,
an IRI equality or kinds whose codes run together, which the pushdown turns
into a keep on the native match (:func:`.native.native_restriction`), so
the rows that fail never cross into Python — a probed pattern carries a
range into each probe and tests a code set on the rows the probe returns;
and ``codes``, a predicate vortex-rdf answers over the distinct codes a
query produced (:func:`.native.evaluate`, called from
:func:`evaluate_column`), never over the whole dictionary, the codes it
leaves undecided going down the two routes above.

The modules: :mod:`.exprs` walks expression trees, :mod:`.values` holds
SPARQL's value semantics over term views, :mod:`.compiler` compiles the fast
route, :mod:`.native` recognizes and decides the native shapes; this module
splits a FILTER into conjuncts and evaluates them.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdflib.plugins.sparql.evalutils import _ebv as rdflib_ebv
from rdflib.plugins.sparql.sparql import FrozenBindings

from ..settings import SETTINGS
from ..terms import TermView, parse_spelling
from . import compiler, exprs, native, values
from .native import FilterCodes, IriIn, KindRange
from .values import UNKNOWN


@dataclass(slots=True)
class Conjunct:
    """A conjunct of a FILTER over the block variables ``vars`` (in order),
    every other variable it reads fixed to the term ``consts`` gives it, and
    how it is decided — each set by :func:`_compile_conjunct` alone, ``None``
    while its switch is off: ``fast``, the fast route
    (``SETTINGS.fast_filters``); for a single variable, ``kinds``, the term
    kinds a test of its kind alone is true for (``code_predicates``),
    ``keep``, a keep on the native match (``native_filters``;
    ``code_predicates`` too), and ``codes``, a predicate ``filter_codes``
    decides (``native_filters``)."""

    expr: Any
    vars: tuple
    fast: Callable | None  # env -> True | False | UNKNOWN
    consts: dict = field(default_factory=dict)  # visible ctx-bound vars -> rdflib terms
    ctx: Any = None
    kinds: frozenset | None = None
    keep: IriIn | KindRange | None = None
    codes: FilterCodes | None = None

    def native_kind(self) -> str | None:
        """What decides it natively, for the trace: a ``filter_codes``
        kind, ``iri_in`` or ``kind_range``; ``None`` for nothing."""
        if self.codes is not None:
            return self.codes.kind
        if isinstance(self.keep, IriIn):
            return "iri_in"
        return None if self.keep is None else "kind_range"

    def generic(self, bound: dict) -> bool:
        """rdflib's own answer for one value assignment (``bound`` maps the
        conjunct's variables to decoded terms; unbound ones are absent)."""
        bindings = dict(self.consts)
        bindings.update(bound)
        return rdflib_ebv(self.expr, FrozenBindings(self.ctx, bindings))


@dataclass(slots=True)
class ExistsConjunct:
    conjunct: Conjunct
    negate: bool
    body: Any  # the translated algebra of the EXISTS group


@dataclass(slots=True)
class FilterPlan:
    constant: list  # conjuncts referencing no block variable
    per_var: dict  # block variable -> conjuncts referencing only it
    tuples: list  # conjuncts referencing several block variables
    exists: list  # ExistsConjunct: (NOT) EXISTS conjuncts, semi/anti-joins


def analyze_filter(node, block_vars, ctx) -> FilterPlan:
    """Split a ``Filter`` node's expression into conjuncts and route them.

    Visibility follows rdflib's ``evalFilter``: the expression sees the block's
    own variables, the context's bindings that are in the node's ``_vars`` or
    in the query's ``initBindings`` (the rest was forgotten), and everything
    when the filter sits directly inside an EXISTS body
    (``no_isolated_scope``).
    """
    outer = dict(ctx.bindings.items())
    init = ctx.initBindings or {}
    if getattr(node, "no_isolated_scope", False):
        visible = outer
    else:
        allowed = set(node._vars or ()) | set(init)
        visible = {v: term for v, term in outer.items() if v in allowed}
    return analyze_expr(node.expr, block_vars, ctx, visible)


#: A view of each term kind, ``""`` (no term) included, for a predicate
#: that reads its variable's kind alone.
_KIND_VIEWS = tuple(TermView(kind, "") for kind in (*native.KIND_ORDER, ""))


def _compile_conjunct(expr, variables: tuple, consts: dict, ctx) -> Conjunct:
    """``expr`` as a conjunct of ``variables``, every other variable it reads
    fixed to the term ``consts`` gives it, or unbound — the one place that
    decides, by the switches, how it is answered: its fast route compiled
    over those constants (none when the fast routes are off, a constant has
    no term view or the expression is outside the whitelist); for a single
    variable, the kinds it is true for when it reads the variable's kind
    alone (evaluated once per kind), and its native decision — a keep (an
    IRI equality, or kinds that run together) or a ``filter_codes``
    predicate (:func:`.native.native_shape`)."""
    fast = compiler.compile_fast(expr, variables, consts) if SETTINGS.fast_filters else None
    kinds = keep = codes = None
    if len(variables) == 1:
        if fast is not None and SETTINGS.code_predicates and compiler.is_kind_only(expr):
            kinds = frozenset(view.kind for view in _KIND_VIEWS if fast((view,)) is True)
        if SETTINGS.fast_filters and SETTINGS.native_filters:
            shape = native.native_shape(expr, variables[0], consts)
            if isinstance(shape, FilterCodes):
                codes = shape
            elif shape is not None:
                keep = shape if SETTINGS.code_predicates else None
            elif kinds is not None:
                keep = native.kind_range(kinds)
    return Conjunct(expr, variables, fast, consts, ctx, kinds, keep, codes)


def analyze_expr(expr, block_vars, ctx, visible: dict) -> FilterPlan:
    """Route the conjuncts of a boolean expression over ``block_vars``;
    ``visible`` maps the context-bound variables the expression may see to
    their terms, every other non-block variable is unbound.

    A conjunct holding an EXISTS under another operator, with a MINUS in its
    body, is left to rdflib (``NotImplementedError``): it would be decided
    with only the conjunct's variables bound, where rdflib binds the whole
    row, which that MINUS then always shares."""
    block_set = set(block_vars)
    plan = FilterPlan([], {}, [], [])
    for conjunct_expr in exprs.flatten_and(expr, []):
        expr = conjunct_expr
        if exprs.is_impure(expr):
            raise NotImplementedError
        shape = exprs.exists_shape(expr)
        if shape is None and exprs.has_minus(expr):
            raise NotImplementedError
        referenced = exprs.expr_vars(expr)
        variables = tuple(v for v in referenced if v in block_set)
        consts = {v: visible[v] for v in referenced if v in visible and v not in block_set}
        conjunct = _compile_conjunct(expr, variables, consts, ctx)
        if shape is not None:
            plan.exists.append(ExistsConjunct(conjunct, *shape))
        elif not variables:
            plan.constant.append(conjunct)
        elif len(variables) == 1:
            plan.per_var.setdefault(variables[0], []).append(conjunct)
        else:
            plan.tuples.append(conjunct)
    return plan


def specialize(conjunct: Conjunct, var, consts: dict) -> Conjunct | None:
    """``conjunct`` as a test of ``var`` alone, its other variables fixed to
    the terms ``consts`` gives them, compiled as any conjunct is
    (:func:`_compile_conjunct`) — or ``None`` when vortex-rdf decides it no
    way (no keep, no ``filter_codes`` predicate). Its generic route is
    rdflib's evaluator over the same constants."""
    special = _compile_conjunct(conjunct.expr, (var,), consts, conjunct.ctx)
    return special if special.keep is not None or special.codes is not None else None


def evaluate_constant(conjunct: Conjunct) -> bool:
    """A conjunct without block variables, evaluated once."""
    if conjunct.fast is not None:
        r = conjunct.fast(())
        if r is not UNKNOWN:
            return r
    return conjunct.generic({})


def evaluate_column(store, conjuncts: list, codes) -> set:
    """The subset of ``codes`` (distinct codes of one variable) passing every
    conjunct — ``codes`` itself when it is a set and no conjunct runs.
    A conjunct of term kinds decides each code by its kind, none decoded; one
    with a keep or a ``filter_codes`` predicate is answered by vortex-rdf
    over the codes still in play (:func:`.native.keep_decides`,
    :func:`.native.evaluate`), only the codes it leaves undecided going on;
    the others decode the surviving codes once, in one batch, and parse
    their spellings."""
    remaining = codes if isinstance(codes, set) else set(codes)
    views: dict = {}
    for conjunct in conjuncts:
        if not remaining:
            break
        fast = conjunct.fast
        passed: set = set()
        unknown: list = []
        pending = remaining
        if conjunct.kinds is not None:
            kind_of, kinds = store._file.kind_ranges.kind_of, conjunct.kinds
            passed = {code for code in remaining if kind_of(code) in kinds}
            pending = ()
        elif conjunct.keep is not None:
            passed, pending = native.keep_decides(store, conjunct.keep, remaining)
        elif conjunct.codes is not None:
            passed, pending = native.evaluate(store, conjunct.codes, remaining)
        if not pending:
            pass
        elif fast is None:
            unknown = list(pending)
        else:
            decode_views(store, pending, views)
            for code in pending:
                r = fast((views[code],))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
        if unknown:
            var = conjunct.vars[0]
            # One decode_many; the loop reads the terms it returns.
            terms = store._prime_decode_cache(unknown)
            for code in unknown:
                if conjunct.generic({var: terms[code]}):
                    passed.add(code)
        remaining = passed
    return remaining


def _fast_route(store, fast, views: dict) -> Callable[[tuple], Any]:
    """``fast`` over a code tuple: each code parsed into a view once, through
    ``views``, a dictionary code decoded and a negative (query-constant) one
    taken from the query's foreign terms; an unbound ``None`` stays unbound."""

    def view_of(code):
        view = views.get(code)
        if view is None:
            if code < 0:
                view = views[code] = values.constant_view(store._terms().foreign[code])
            else:
                view = views[code] = parse_spelling(store._spelling(code))
        return view

    def answer(key: tuple):
        return fast(tuple(None if code is None else view_of(code) for code in key))

    return answer


def tuple_predicate(
    store, conjunct: Conjunct, positions: list, shared_views: dict | None = None
) -> Callable[[tuple], bool]:
    """A row predicate for a conjunct over several variables, memoized per
    distinct code tuple; ``positions`` are the variables' row indexes and
    ``shared_views`` a parsed-term cache shared with the caller."""
    memo: dict = {}
    fast = conjunct.fast
    views = {} if shared_views is None else shared_views
    answer = None if fast is None else _fast_route(store, fast, views)
    variables = conjunct.vars

    def predicate(row) -> bool:
        key = tuple(row[i] for i in positions)
        r = memo.get(key)
        if r is None:
            r = UNKNOWN if answer is None else answer(key)
            if r is UNKNOWN:
                r = conjunct.generic(
                    {
                        v: store._decode_term(c)
                        for v, c in zip(variables, key, strict=True)
                        if c is not None
                    }
                )
            memo[key] = r
        return r is True

    return predicate


def constant_predicate(conjunct: Conjunct) -> Callable[[Any], bool]:
    """A single-variable conjunct as a test of a query constant (a VALUES
    table's term, as rdflib binds it), memoized per term: the fast route over
    the constant's view, rdflib's evaluator where that is unknown."""
    memo: dict = {}
    fast = conjunct.fast
    var = conjunct.vars[0]

    def predicate(term) -> bool:
        r = memo.get(term)
        if r is None:
            r = UNKNOWN
            if fast is not None:
                r = fast((values.constant_view(term),))
            if r is UNKNOWN:
                r = conjunct.generic({var: term})
            memo[term] = r
        return r is True

    return predicate


def solution_filter(store, node, ctx) -> Callable[[Any], bool]:
    """A ``Filter`` node's expression as a test of rdflib's own solutions —
    the rows of a pattern the pushdown handed back to rdflib — answered by
    the pushdown's FILTER semantics, so the FILTER does not depend on which
    route solved its pattern.

    Each solution is tested from its own terms, each read as a data term is
    (:func:`values.constant_view`): nothing is kept per solution, so the
    query's memory does not grow with the rows it filters. A typed literal
    rdflib normalized as it was decoded (``"042"^^xsd:integer`` reads
    ``"42"``) is read as rdflib wrote it. A conjunct holding an EXISTS is
    rdflib's, over the whole solution, as rdflib's ``evalFilter`` binds it.
    An expression the pushdown hands back whole (a random or fresh-node
    call) raises ``NotImplementedError``."""
    tests: list[Callable[[Any], bool]] = []
    for conjunct_expr in exprs.flatten_and(node.expr, []):
        if exprs.is_impure(conjunct_expr):
            raise NotImplementedError
        if exprs.has_exists(conjunct_expr):
            tests.append(lambda bound, expr=conjunct_expr: rdflib_ebv(expr, bound))
            continue
        referenced = tuple(exprs.expr_vars(conjunct_expr))
        conjunct = _compile_conjunct(conjunct_expr, referenced, {}, ctx)
        if not referenced:
            passes = evaluate_constant(conjunct)
            tests.append(lambda bound, passes=passes: passes)
            continue
        tests.append(_solution_test(conjunct, referenced))
    isolated = not getattr(node, "no_isolated_scope", False)

    def keep(solution) -> bool:
        bound = solution.forget(ctx, _except=node._vars) if isolated else solution
        return all(test(bound) for test in tests)

    return keep


def _solution_test(conjunct: Conjunct, variables: tuple) -> Callable[[Any], bool]:
    """``conjunct`` as a test of one solution: its fast route over the views
    of the solution's terms, rdflib's evaluator where that route leaves it
    undecided."""
    fast = conjunct.fast

    def test(bound) -> bool:
        terms = [bound.get(var) for var in variables]
        r = UNKNOWN
        if fast is not None:
            r = fast(tuple(None if t is None else values.constant_view(t) for t in terms))
        if r is UNKNOWN:
            r = conjunct.generic(
                {var: t for var, t in zip(variables, terms, strict=True) if t is not None}
            )
        return r is True

    return test


def tuple_fast_reject_predicate(
    store, conjunct: Conjunct, positions: list, shared_views: dict | None = None
) -> Callable[[tuple], bool]:
    """Keep rows unless the exact fast route proves a conjunct false.

    UNKNOWN remains visible to the normal final predicate, preserving the
    generic RDFLib fallback for values outside the fast path's domain.
    """
    fast = conjunct.fast
    if fast is None:
        raise ValueError("tuple_fast_reject_predicate requires a fast conjunct")
    memo: dict = {}
    answer = _fast_route(store, fast, {} if shared_views is None else shared_views)

    def predicate(row) -> bool:
        key = tuple(row[index] for index in positions)
        result = memo.get(key)
        if result is None:
            result = memo[key] = answer(key)
        return result is not False

    return predicate


def decode_views(store, codes, views: dict) -> None:
    """Parse into ``views`` every dictionary code of ``codes`` it lacks,
    decoding them in one batch."""
    missing = [code for code in codes if code not in views]
    if not missing:
        return
    for code, spelling in zip(missing, store._spellings(missing), strict=True):
        views[code] = parse_spelling(spelling)


def prime_tuple_views(store, rows, positions, views: dict) -> None:
    """Decode, in one batch, the dictionary codes ``rows`` hold at
    ``positions`` that ``views`` lacks — for tuple predicates about to read
    every row, which would otherwise decode them one call at a time."""
    codes = {row[position] for row in rows for position in positions}
    codes.discard(None)
    decode_views(store, [code for code in codes if code >= 0], views)
