"""The trace events of a BGP's plan (:mod:`vortex_rdflib.trace`): its
patterns, its join steps, the steps over the pair budget and the plan, from
what solving it did (:class:`_BgpStats`) and what each step did
(:class:`_JoinStep`)."""

from dataclasses import dataclass

from .. import trace
from . import relation
from .matching import Pattern
from .relation import Relation


@dataclass(slots=True)
class _BgpStats:
    """What solving a BGP did, for its trace events: the native calls it
    made — the patterns' matches (``initial_calls``) and the probes
    (``probe_calls``) — and the planning counts (``estimate_calls``, made
    for a BGP of several patterns only), the native calls' time, the
    restrictions' and the joins'."""

    initial_calls: int = 0
    probe_calls: int = 0
    estimate_calls: int = 0
    native_ns: int = 0
    restriction_ns: int = 0
    join_ns: int = 0


def _trace_pattern_start(ctx, index: int, triple) -> dict | None:
    """Trace a BGP pattern's start as ``bgp_pattern_start``, with its shape
    in the context; returns the shape, for :func:`_trace_pattern_complete`
    (``None`` when the query is not traced)."""
    if trace.active() is None:
        return None
    resolved = [ctx[term] for term in triple]
    bound = [value is not None for value in resolved]
    variables = [term for term, value in zip(triple, resolved, strict=True) if value is None]
    shape = {
        "bound_subject": bound[0],
        "bound_predicate": bound[1],
        "bound_object": bound[2],
        "variable_count": len(set(variables)),
        "repeated_variable_count": len(variables) - len(set(variables)),
        "pattern_shape": "".join("B" if item else "V" for item in bound),
    }
    trace.event("bgp_pattern_start", original_pattern_index=index, **shape)
    return shape


def _trace_pattern_complete(
    index: int, shape: dict | None, matched_rows: int | None, native_ns: int
) -> None:
    """Trace a BGP pattern as ``bgp_pattern_complete``: matched now, with
    its rows and match time, or left unmatched for the planner (``None``)."""
    if shape is None:
        return
    trace.event(
        "bgp_pattern_complete",
        original_pattern_index=index,
        cardinality_source="unmatched" if matched_rows is None else "matched",
        native_call_count=int(matched_rows is not None),
        matched_rows=matched_rows,
        native_match_ns=native_ns,
        restriction_ns=0,
        relation_extend_ns=0,
        other_ns=0,
        elapsed_ns=native_ns,
        timing_reconciled=True,
        **shape,
    )


def _trace_bgp_complete(pattern_count: int, rel: Relation, stats: _BgpStats, elapsed: int) -> None:
    """Trace a solved BGP as ``bgp_complete``: its relation, its native
    calls, and its time split into the native calls', the restrictions',
    the joins' and the rest, which add up to the whole."""
    if trace.active() is None:
        return
    known = stats.native_ns + stats.restriction_ns + stats.join_ns
    other_ns = max(0, elapsed - known)
    # Only a BGP of several patterns is planned, and counted to plan it.
    planned = {"estimate_calls": stats.estimate_calls} if pattern_count > 1 else {}
    trace.event(
        "bgp_complete",
        pattern_count=pattern_count,
        output_rows=relation._relation_rows(rel),
        output_columns=len(rel.schema),
        native_call_count=stats.initial_calls + stats.probe_calls,
        native_initial_match_count=stats.initial_calls,
        native_probe_call_count=stats.probe_calls,
        **planned,
        native_match_ns=stats.native_ns,
        restriction_ns=stats.restriction_ns,
        join_ns=stats.join_ns,
        other_ns=other_ns,
        elapsed_ns=known + other_ns,
        timing_reconciled=True,
    )


@dataclass(slots=True)
class _JoinStep:
    """One step of a BGP's join, for its trace events: ``pat`` joined into
    the relation of ``running`` rows (step ``execution_index`` of the plan),
    the count it was planned with (``estimate``, counted at the step up to
    ``count_limit`` when it was not planned), the variables it shares, and
    what the step did — its ``strategy``, set by each strategy it tries
    (:func:`_tiers`), how a streamed step is fed, its pairs, its pattern's
    rows before and after their restrictions, and its probes. Where its
    rows came from follows (:func:`_trace_join_step`)."""

    execution_index: int
    pat: Pattern
    running: int
    estimate: int
    count_limit: int | None
    shared_count: int
    strategy: str | None = None
    feed: str | None = None
    pair_count: int | None = None
    match_rows: int | None = None
    restricted_rows: int | None = None
    probe_calls: int = 0
    probe_ns: int = 0


def _trace_join_step(step: _JoinStep, rel: Relation, put_back: list, elapsed_ns: int) -> None:
    """Trace a completed join step as ``bgp_join_step_complete``: the step,
    the patterns it was reordered around, and the relation it made. Its
    rows came from its probes, from chunks of its pattern, or from its
    pattern's match."""
    if trace.active() is None:
        return
    if step.strategy == "probe":
        source = "probed"
    else:
        source = "chunked" if step.feed == "chunks" else "matched"
    trace.event(
        "bgp_join_step_complete",
        execution_index=step.execution_index,
        original_pattern_index=step.pat.index,
        strategy=step.strategy,
        feed=step.feed,
        reordered_around=[back.index for back in put_back] or None,
        cardinality_source=source,
        input_rows=step.running,
        estimated_rows=step.estimate,
        estimate_limit=step.count_limit,
        pair_count=step.pair_count,
        pattern_match_rows=step.match_rows,
        pattern_restricted_rows=step.restricted_rows,
        native_initial_match_count=int(source != "probed"),
        native_probe_call_count=step.probe_calls,
        native_probe_ns=step.probe_ns,
        output_rows=None if rel.pieces is not None else rel.nrows,
        output_columns=len(rel.schema),
        shared_variable_count=step.shared_count,
        elapsed_ns=elapsed_ns,
    )


def _trace_over_budget(step: _JoinStep, pairs: int | None) -> None:
    """Trace a join step over ``SETTINGS.pair_budget`` as ``bgp_over_budget``:
    its place in the plan, its strategy, its pair count (``None`` where it
    stopped on its rows), the relation's rows and the pattern's count."""
    trace.event(
        "bgp_over_budget",
        execution_index=step.execution_index,
        original_pattern_index=step.pat.index,
        strategy=step.strategy,
        pair_count=pairs,
        input_rows=step.running,
        estimated_rows=step.estimate,
    )


def _trace_plan_complete(
    pattern_count: int, order: list, seed: Pattern | None, stats: _BgpStats, rest: list
) -> None:
    """Trace a BGP's plan as ``bgp_plan_complete``: the order its patterns
    joined in, its seed (``None`` when a count emptied it first), its native
    calls and the patterns left to rdflib (``rest``, by index)."""
    if trace.active() is None:
        return
    trace.event(
        "bgp_plan_complete",
        pattern_count=pattern_count,
        execution_order=order,
        seed_pattern_index=None if seed is None else seed.index,
        seed_selection="empty_count" if seed is None else "smallest_count",
        estimate_calls=stats.estimate_calls,
        native_initial_match_count=stats.initial_calls,
        native_probe_call_count=stats.probe_calls,
        rest_pattern_indices=rest or None,
    )
