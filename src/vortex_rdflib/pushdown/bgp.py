"""Basic graph patterns: one pattern matched whole, several joined by the
incremental planner (:mod:`.planner`).
"""

from .. import trace
from . import algebra, matching, planner
from .pushed import NOTHING, Pushed
from .relation import Relation
from .tracing import _BgpStats, _trace_bgp_complete, _trace_pattern_complete, _trace_pattern_start


def _solve_bgp(
    ctx, store, triples, scope, pushed: Pushed = NOTHING, limit=None, finishable=False
) -> Relation:
    """Evaluate a BGP by matching one seed and probing connected patterns.

    The filter conjuncts that are native keeps (see
    :func:`filters.native.native_restriction`) become keeps on each pattern's native
    match before anything is counted (see :func:`matching._push_native_restrictions`);
    the others are evaluated over the codes the pattern brings
    (:func:`filters.evaluate_column`): vortex-rdf decides those with a native
    shape first, and only the codes it leaves undecided go to the Python
    routes. A single pattern with no such residual conjunct takes ``limit``
    into its match as well (see :func:`matching._limit_reaches_match`).

    With ``finishable`` (the relation goes straight to the emitter) and the
    block in the graph the context has made active — the graph rdflib's
    ``evalBGP`` reads — the planner may leave the patterns past a join over
    the pair budget to rdflib: the relation then carries their triples as
    ``rest``.
    """
    elapsed = trace.stopwatch()
    trace.event("bgp_start", pattern_count=len(triples), initial_bound_variable_count=0)
    if not triples:
        # One empty solution, and nothing ran.
        rel, stats, elapsed = Relation.from_rows((), [()]), _BgpStats(), lambda: 0
    elif len(triples) == 1:
        rel, stats = _solve_pattern(ctx, store, triples[0], scope, pushed.per_var, limit)
    else:
        rel, stats = _solve_patterns(ctx, store, triples, scope, pushed, finishable)
    _trace_bgp_complete(len(triples), rel, stats, elapsed())
    return rel


def _solve_pattern(ctx, store, triple, scope, var_preds, limit) -> tuple[Relation, _BgpStats]:
    """A BGP of one pattern: its native match, restricted by the conjuncts
    the match does not take, as a relation."""
    shape = _trace_pattern_start(ctx, 0, triple)
    pat = matching.resolve(ctx, store, triple, scope, var_preds, index=0)
    if not matching._limit_reaches_match(store, pat, pat.deferred):
        limit = None
    stats = _BgpStats()
    stats.initial_calls, stats.native_ns = matching._match_resolved_pattern(store, pat, limit=limit)
    _trace_pattern_complete(0, shape, pat.nrows, stats.native_ns)
    clock = trace.stopwatch()
    rel = matching.restricted(store, pat, (), stats)
    stats.join_ns = max(0, clock() - stats.restriction_ns)
    return rel, stats


def _solve_patterns(
    ctx, store, triples, scope, pushed: Pushed, finishable
) -> tuple[Relation, _BgpStats]:
    """A BGP of several patterns, joined by the incremental planner
    (:func:`_join_incremental_bgp`). Each pattern takes the conjuncts that
    are native keeps into its match; the other conjuncts of its variables
    are deferred to the codes it brings."""
    patterns = []
    for index, triple in enumerate(triples):
        shape = _trace_pattern_start(ctx, index, triple)
        patterns.append(matching.resolve(ctx, store, triple, scope, pushed.per_var, index=index))
        _trace_pattern_complete(index, shape, None, 0)
    stats = _BgpStats()
    clock = trace.stopwatch()
    finishable = finishable and scope == algebra._active_scope(ctx, store)
    rel = planner._join_incremental_bgp(store, patterns, stats, pushed, finishable, triples)
    stats.join_ns = max(0, clock() - stats.native_ns - stats.restriction_ns)
    return rel, stats
