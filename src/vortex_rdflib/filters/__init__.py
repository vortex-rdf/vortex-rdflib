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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rdflib.plugins.sparql.evalutils import _ebv as rdflib_ebv
from rdflib.plugins.sparql.sparql import FrozenBindings

from .. import nodes
from ..settings import SETTINGS
from ..terms import TermView, parse_spelling, view_of
from . import compiler, exprs, native, values  # noqa: F401 - filters.values is public
from .native import FilterCodes, IriIn, Kinds
from .values import UNKNOWN


@dataclass(slots=True, eq=False)
class Conjunct:
    """A conjunct of a FILTER over the block variables ``vars`` (in order),
    every other variable it reads fixed to the term ``consts`` gives it, and
    how it is decided — each set by :func:`_compile_conjunct` alone, ``None``
    while its switch is off: ``fast``, the fast route
    (``SETTINGS.fast_filters``), and for a single variable its
    ``decision`` over the codes a query produced — the term kinds a test of
    its kind alone holds for (``code_predicates``), an IRI equality
    (``native_filters`` and ``code_predicates``) or a predicate
    ``filter_codes`` decides (``native_filters``) — and ``keep``, the one of
    those a native match takes too (``native_filters``). Two conjuncts are
    two, however alike: a relation names the ones it applied by identity."""

    expr: Any
    vars: tuple
    fast: Callable | None  # env -> True | False | UNKNOWN
    consts: dict = field(default_factory=dict)  # visible ctx-bound vars -> rdflib terms
    ctx: Any = None
    decision: Kinds | IriIn | FilterCodes | None = None
    keep: Kinds | IriIn | None = None

    def decide(self, views: tuple | None, key: Sequence, decode: Callable | None = None) -> bool:
        """Its answer for one assignment of its variables, ``key`` (their
        values in order, ``None`` unbound — terms, or codes ``decode`` turns
        into terms): its fast route over their term views ``views``
        (``None`` while that route is off), rdflib's evaluator where the
        route is off or leaves it undecided."""
        fast = self.fast
        r = UNKNOWN if views is None or fast is None else fast(views)
        if r is UNKNOWN:
            r = self.generic(
                {
                    var: value if decode is None else decode(value)
                    for var, value in zip(self.vars, key, strict=True)
                    if value is not None
                }
            )
        return r is True

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
    #: The context variables the expression sees (:func:`analyze_filter`),
    #: ``None`` for every one.
    allowed: frozenset | None = None

    @property
    def conjuncts(self) -> list:
        """Every conjunct but the EXISTS ones, the constant ones first."""
        return self.constant + self.row_conjuncts

    @property
    def row_conjuncts(self) -> list:
        """The conjuncts that read a row's variables: those over several,
        then those over one."""
        return self.tuples + [c for cs in self.per_var.values() for c in cs]


def analyze_filter(expr, kept: frozenset | None, block_vars, ctx) -> FilterPlan:
    """Split a FILTER's expression (or an OPTIONAL's condition) into
    conjuncts and route them.

    Visibility follows rdflib's ``evalFilter``: the expression sees the block's
    own variables and the context's bindings of the variables rdflib keeps —
    ``kept``, the node's ``_vars`` (none for a condition), and the query's
    ``initBindings`` — the rest forgotten; and every binding when ``kept``
    is ``None``, a filter directly inside an EXISTS body. The plan says which
    (``FilterPlan.allowed``)."""
    outer = dict(ctx.bindings.items())
    if kept is None:
        allowed, visible = None, outer
    else:
        allowed = kept | frozenset(ctx.initBindings or ())
        visible = {v: term for v, term in outer.items() if v in allowed}
    plan = analyze_expr(expr, block_vars, ctx, visible)
    plan.allowed = allowed
    return plan


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
    decision = keep = None
    if len(variables) == 1:
        if fast is not None and SETTINGS.code_predicates and compiler.is_kind_only(expr):
            decision = Kinds(frozenset(view.kind for view in _KIND_VIEWS if fast((view,)) is True))
            if SETTINGS.native_filters and decision.one_range:
                keep = decision
        elif SETTINGS.fast_filters and SETTINGS.native_filters:
            shape = native.native_shape(expr, variables[0], consts)
            if isinstance(shape, FilterCodes):
                decision = shape
            elif shape is not None and SETTINGS.code_predicates:
                decision = keep = shape
    return Conjunct(expr, variables, fast, consts, ctx, decision, keep)


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
    if special.keep is not None or isinstance(special.decision, FilterCodes):
        return special
    return None


def evaluate_constant(conjunct: Conjunct) -> bool:
    """A conjunct without block variables, evaluated once."""
    return conjunct.decide(None if conjunct.fast is None else (), ())


def evaluate_column(store, conjuncts: list, codes) -> set:
    """The subset of ``codes`` (distinct codes of one variable) passing every
    conjunct — ``codes`` itself when it is a set and no conjunct runs.
    A conjunct's decision splits the codes still in play (term kinds decide
    each code by its kind, none decoded; an IRI equality or a
    ``filter_codes`` predicate is answered by vortex-rdf), only the codes it
    leaves undecided going on; the others decode the surviving codes once,
    in one batch, and parse their spellings."""
    remaining = codes if isinstance(codes, set) else set(codes)
    views: dict = {}
    for conjunct in conjuncts:
        if not remaining:
            break
        fast = conjunct.fast
        passed: set = set()
        unknown: list = []
        pending = remaining
        if conjunct.decision is not None:
            passed, pending = conjunct.decision.split(store, remaining)
        if fast is None:
            unknown = list(pending)
        elif pending:
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


def _code_views(store, views: dict) -> Callable[[tuple], tuple]:
    """The term views of a code tuple, for a fast route: each code parsed
    into a view once, through ``views``, a dictionary code decoded and a
    negative (query-constant) one taken from the query's foreign terms; an
    unbound ``None`` stays unbound."""

    def view_at(code):
        view = views.get(code)
        if view is None:
            if code < 0:
                view = views[code] = view_of(store._terms().foreign[code])
            else:
                view = views[code] = parse_spelling(store._opened().codes.spelling(code))
        return view

    def views_of(key: tuple) -> tuple:
        return tuple(None if code is None else view_at(code) for code in key)

    return views_of


def tuple_predicate(
    store, conjunct: Conjunct, positions: list, shared_views: dict | None = None
) -> Callable[[tuple], bool]:
    """A row predicate for a conjunct over several variables, memoized per
    distinct code tuple; ``positions`` are the variables' row indexes and
    ``shared_views`` a parsed-term cache shared with the caller."""
    memo: dict = {}
    views_of = None
    if conjunct.fast is not None:
        views_of = _code_views(store, {} if shared_views is None else shared_views)
    decode = store._decode_term

    def predicate(row) -> bool:
        key = tuple(row[i] for i in positions)
        r = memo.get(key)
        if r is None:
            r = memo[key] = conjunct.decide(
                None if views_of is None else views_of(key), key, decode
            )
        return r

    return predicate


def constant_predicate(conjunct: Conjunct) -> Callable[[Any], bool]:
    """A single-variable conjunct as a test of a query constant (a VALUES
    table's term, as rdflib binds it), memoized per term: the fast route over
    the constant's view, rdflib's evaluator where that is unknown."""
    memo: dict = {}
    fast = conjunct.fast

    def predicate(term) -> bool:
        r = memo.get(term)
        if r is None:
            r = memo[term] = conjunct.decide(None if fast is None else (view_of(term),), (term,))
        return r

    return predicate


def solution_filter(store, node, ctx) -> Callable[[Any], bool]:
    """A ``Filter`` node's expression as a test of rdflib's own solutions —
    the rows of a pattern the pushdown handed back to rdflib — answered by
    the pushdown's FILTER semantics, so the FILTER does not depend on which
    route solved its pattern.

    Each solution is tested from its own terms, each read as a data term is
    (:func:`~vortex_rdflib.terms.view_of`): nothing is kept per solution, so the
    query's memory does not grow with the rows it filters. A typed literal
    rdflib normalized as it was decoded (``"042"^^xsd:integer`` reads
    ``"42"``) is read as rdflib wrote it. A conjunct holding an EXISTS is
    rdflib's, over the whole solution, as rdflib's ``evalFilter`` binds it.
    An expression the pushdown hands back whole (a random or fresh-node
    call) raises ``NotImplementedError``."""
    tests: list[Callable[[Any], bool]] = []
    for conjunct_expr in exprs.flatten_and(nodes.field(node, "expr"), []):
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
    isolated = not nodes.field(node, "no_isolated_scope")
    kept = nodes.field(node, "_vars")

    def keep(solution) -> bool:
        bound = solution.forget(ctx, _except=kept) if isolated else solution
        return all(test(bound) for test in tests)

    return keep


def _solution_test(conjunct: Conjunct, variables: tuple) -> Callable[[Any], bool]:
    """``conjunct`` as a test of one solution: its fast route over the views
    of the solution's terms, rdflib's evaluator where that route leaves it
    undecided."""
    fast = conjunct.fast

    def test(bound) -> bool:
        terms = [bound.get(var) for var in variables]
        views = None
        if fast is not None:
            views = tuple(None if t is None else view_of(t) for t in terms)
        return conjunct.decide(views, terms)

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
    views_of = _code_views(store, {} if shared_views is None else shared_views)

    def predicate(row) -> bool:
        key = tuple(row[index] for index in positions)
        result = memo.get(key)
        if result is None:
            result = memo[key] = fast(views_of(key))
        return result is not False

    return predicate


def restriction_counts(conjuncts) -> dict:
    """The trace fields that say how a variable's conjuncts are decided: how
    many there are, and how many vortex-rdf decides (an IRI equality or a
    ``filter_codes`` predicate), have a fast route, are decided by term kinds
    alone, or by rdflib's evaluator alone."""
    return {
        "predicate_count": len(conjuncts),
        "native_predicate_count": sum(
            isinstance(c.decision, (IriIn, FilterCodes)) for c in conjuncts
        ),
        "fast_predicate_count": sum(c.fast is not None for c in conjuncts),
        "kind_only_predicate_count": sum(isinstance(c.decision, Kinds) for c in conjuncts),
        "generic_only_predicate_count": sum(c.fast is None for c in conjuncts),
    }


def decode_views(store, codes, views: dict) -> None:
    """Parse into ``views`` every dictionary code of ``codes`` it lacks,
    decoding them in one batch."""
    missing = [code for code in codes if code not in views]
    if not missing:
        return
    for code, spelling in zip(missing, store._opened().codes.spellings(missing), strict=True):
        views[code] = parse_spelling(spelling)


def prime_tuple_views(store, rows, positions, views: dict) -> None:
    """Decode, in one batch, the dictionary codes ``rows`` hold at
    ``positions`` that ``views`` lacks — for tuple predicates about to read
    every row, which would otherwise decode them one call at a time."""
    codes = {row[position] for row in rows for position in positions}
    codes.discard(None)
    decode_views(store, [code for code in codes if code >= 0], views)
