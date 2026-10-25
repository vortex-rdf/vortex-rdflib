"""FILTER expressions evaluated over term codes.

A ``Filter`` node's expression is split into its top-level conjuncts (rdflib
folds every FILTER of a group into one ``ConditionalAndExpression``; a row
passes iff every conjunct is true, so the split is exact). Each conjunct is
classified by the block variables it references: none (evaluated once), one
(a per-variable predicate applied to the pattern scan that binds it, before
any join), or several (applied on the joined rows). Either way a conjunct is
evaluated **per distinct value**, never per row, through two routes:

- **fast**: a whitelist of expression shapes compiled to a predicate over the
  parsed spelling (:class:`~vortex_rdflib.terms.TermView`). Every leaf mirrors
  the exact rdflib code path (``operators.py``, ``Literal.__gt__``/``eq``)
  and answers ``True``, ``False``, ``ERROR`` (rdflib would raise, which a
  filter turns into false) or ``UNKNOWN`` — the value is outside the domain
  the fast path reproduces exactly (an ill-typed number, a datatype rdflib
  orders by its own rules, ...) and is deferred, alone, to
- **generic**: rdflib's own evaluator (``_ebv``) on a ``FrozenBindings`` with
  the value decoded to an rdflib term. Semantics-exact by construction, at
  rdflib's per-evaluation cost, but paid once per distinct value.

``UNKNOWN`` is sticky through ``&&``/``||``/``!`` except where rdflib itself
short-circuits first (``false && ?`` is false, ``true || ?`` is true).

A single-variable conjunct of a shape vortex-rdf evaluates (:func:`native_shape`
lists them) also carries that predicate (``Conjunct.native``, a
:class:`.native.NativeShape`). A kind test is its kind's
code range and an IRI equality its IRIs' codes, which the pushdown turns into
a ``keep`` on the native match (:func:`native_restriction`), so the rows that
fail never cross into Python. The one exception is a probed pattern: it
carries a range into each probe and tests a code set on the rows the probe
returns.
Every other native shape is answered by vortex-rdf over the distinct codes a
query produced (:func:`.native.evaluate`, called from
:func:`evaluate_column`), never over the whole dictionary, and only the codes
it leaves undecided go down the two routes above.

The modules: :mod:`.exprs` walks expression trees, :mod:`.values` holds
rdflib's value semantics over term views, :mod:`.compiler` compiles the fast
route, :mod:`.native` recognizes and decides the native shapes; this module
splits a FILTER into conjuncts and evaluates them.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdflib.plugins.sparql.evalutils import _ebv as rdflib_ebv
from rdflib.plugins.sparql.sparql import FrozenBindings

from ..settings import SETTINGS
from ..terms import TermView, kind_of, parse_spelling
from . import compiler, exprs, native
from .compiler import _NotFast
from .native import NativeShape
from .values import UNKNOWN


@dataclass(slots=True)
class Conjunct:
    expr: Any
    vars: tuple  # the block variables it references, in order
    fast: Callable | None  # env -> True | False | UNKNOWN
    kind_only: bool
    consts: dict = field(default_factory=dict)  # visible ctx-bound vars -> rdflib terms
    ctx: Any = None
    # The native predicate a single-variable conjunct is (see `native_shape`),
    # or None.
    native: NativeShape | None = None

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


def _compile_conjunct(
    expr, variables: tuple, consts: dict, ctx, native: NativeShape | None
) -> Conjunct:
    """``expr`` as a conjunct of ``variables``, every other variable it reads
    fixed to the term ``consts`` gives it, or unbound: its fast route compiled
    over those constants (none when the fast routes are off, a constant has no
    term view or the expression is outside the whitelist), kind-only when it
    reads nothing but term kinds. ``native`` is the predicate vortex-rdf
    decides it by, if any."""
    fast = None
    if SETTINGS.fast_filters:
        try:
            const_views = {v: compiler.view_of_node(term) for v, term in consts.items()}
        except _NotFast:
            const_views = None
        if const_views is not None:
            fast = compiler.compile_fast(expr, variables, const_views)
    return Conjunct(
        expr, variables, fast, fast is not None and compiler.is_kind_only(expr), consts, ctx, native
    )


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
    for conjunct_expr in exprs._flatten_and(expr, []):
        expr = conjunct_expr
        if exprs._is_impure(expr):
            raise NotImplementedError
        shape = exprs.exists_shape(expr)
        if shape is None and exprs.has_minus(expr):
            raise NotImplementedError
        referenced = exprs.expr_vars(expr)
        variables = tuple(v for v in referenced if v in block_set)
        consts = {v: visible[v] for v in referenced if v in visible and v not in block_set}
        predicate = None
        if len(variables) == 1 and SETTINGS.fast_filters and SETTINGS.native_filters:
            predicate = native.native_shape(expr, variables[0], consts)
        conjunct = _compile_conjunct(expr, variables, consts, ctx, predicate)
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
    the terms ``consts`` gives them: a single-variable conjunct carrying the
    native shape that makes it (:func:`native_shape`), its fast route compiled
    over those constants — or ``None`` when it has no native shape that way.
    Its generic route is rdflib's evaluator over the same constants, so every
    value it decides is rdflib's answer."""
    shape = native.native_shape(conjunct.expr, var, consts)
    if shape is None:
        return None
    return _compile_conjunct(conjunct.expr, (var,), consts, conjunct.ctx, shape)


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
    Kind-only predicates never decode; a conjunct with a native shape is
    answered by vortex-rdf over the codes still in play
    (:func:`evaluate`), and only the codes it leaves undecided go
    on; the others decode the surviving codes once, in one batch, and parse
    their spellings."""
    remaining = codes if isinstance(codes, set) else set(codes)
    views: dict = {}
    bounds = None
    for conjunct in conjuncts:
        if not remaining:
            break
        fast = conjunct.fast
        passed: set = set()
        unknown: list = []
        pending = remaining
        if conjunct.native is not None and not conjunct.kind_only and SETTINGS.native_filters:
            passed, pending = native.evaluate(store, conjunct.native, remaining)
        if not pending:
            pass
        elif fast is None:
            unknown = list(pending)
        elif conjunct.kind_only:
            if bounds is None:
                bounds = store._term_kind_bounds()
            for code in pending:
                r = fast((TermView(kind_of(code, bounds), None),))
                if r is True:
                    passed.add(code)
                elif r is UNKNOWN:
                    unknown.append(code)
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
                view = views[code] = compiler.view_of_node(store._terms().foreign[code])
            else:
                spelling = store._dict.decode(code)
                if spelling is None:
                    raise ValueError(f"term code {code} is not in the store dictionary")
                view = views[code] = parse_spelling(spelling)
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
    the constant's view, rdflib's evaluator where that is unknown, a number
    the fast route refuses included (see :func:`_constant_view`)."""
    memo: dict = {}
    fast = conjunct.fast
    var = conjunct.vars[0]

    def predicate(term) -> bool:
        r = memo.get(term)
        if r is None:
            r = UNKNOWN
            if fast is not None:
                view = compiler._constant_view(term)
                if view is not UNKNOWN:
                    r = fast((view,))
            if r is UNKNOWN:
                r = conjunct.generic({var: term})
            memo[term] = r
        return r is True

    return predicate


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
    for code, spelling in zip(missing, store._dict.decode_many(missing), strict=True):
        if spelling is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        views[code] = parse_spelling(spelling)


def prime_tuple_views(store, rows, positions, views: dict) -> None:
    """Decode, in one batch, the dictionary codes ``rows`` hold at
    ``positions`` that ``views`` lacks — for tuple predicates about to read
    every row, which would otherwise decode them one call at a time."""
    codes = {row[position] for row in rows for position in positions}
    codes.discard(None)
    decode_views(store, [code for code in codes if code >= 0], views)
