"""Basic graph patterns: one pattern matched whole, several joined by the
incremental planner (:func:`_join_incremental_bgp`) — counted, seeded with
the smallest, extended one connected pattern at a time by probes, a
keep-scan or a hash join within the pair budget, a last step over it
streamed (:mod:`.streaming`) — and their trace events.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .. import boundary, filters, trace
from ..settings import SETTINGS
from . import algebra, matching, probes, relation, streaming
from .matching import Pattern
from .relation import OverBudget, Relation


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


def _solve_bgp(
    ctx, store, triples, scope, var_preds=None, limit=None, finishable=False
) -> Relation:
    """Evaluate a BGP by matching one seed and probing connected patterns.

    The filter conjuncts that are native keeps (see
    :func:`filters.native.native_restriction`) become keeps on each pattern's native
    match before anything is counted (see :func:`_push_native_restrictions`);
    the others are evaluated over the codes the pattern brings
    (:func:`filters.evaluate_column`): vortex-rdf decides those with a native
    shape first, and only the codes it leaves undecided go to the Python
    routes. A single pattern with no such residual conjunct takes ``limit``
    into its match as well (see :func:`_limit_reaches_match`).

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
        rel, stats = _solve_pattern(ctx, store, triples[0], scope, var_preds, limit)
    else:
        rel, stats = _solve_patterns(ctx, store, triples, scope, var_preds, finishable)
    _trace_bgp_complete(len(triples), rel, stats, elapsed())
    return rel


def _solve_pattern(ctx, store, triple, scope, var_preds, limit) -> tuple[Relation, _BgpStats]:
    """A BGP of one pattern: its native match, restricted by the conjuncts
    the match does not take, as a relation."""
    shape = _trace_pattern_start(ctx, 0, triple)
    pat = matching._pattern_terms(ctx, store, *triple, scope=scope, index=0)
    residual = matching._push_native_restrictions(store, pat, var_preds) if var_preds else {}
    if not matching._limit_reaches_match(store, pat, residual):
        limit = None
    stats = _BgpStats()
    stats.initial_calls, stats.native_ns = matching._match_resolved_pattern(store, pat, limit=limit)
    _trace_pattern_complete(0, shape, pat.nrows, stats.native_ns)
    clock = trace.stopwatch()
    if residual:
        matching._restrict_pattern(store, pat, residual)
    stats.restriction_ns = clock()
    clock = trace.stopwatch()
    rel = matching._rel_from_pattern(pat)
    stats.join_ns = clock()
    return rel, stats


def _solve_patterns(
    ctx, store, triples, scope, var_preds, finishable
) -> tuple[Relation, _BgpStats]:
    """A BGP of several patterns, joined by the incremental planner
    (:func:`_join_incremental_bgp`). Each pattern takes the conjuncts that
    are native keeps into its match; the other conjuncts of its variables
    are deferred to the codes it brings."""
    patterns = []
    for index, triple in enumerate(triples):
        shape = _trace_pattern_start(ctx, index, triple)
        pat = matching._pattern_terms(ctx, store, *triple, scope=scope, index=index)
        if var_preds:
            pending = {v: var_preds[v] for v in pat.varpos if v in var_preds}
            if pending:
                pending = matching._push_native_restrictions(store, pat, pending)
            if pending:
                pat.deferred = pending
        patterns.append(pat)
        _trace_pattern_complete(index, shape, None, 0)
    stats = _BgpStats()
    clock = trace.stopwatch()
    finishable = finishable and scope == algebra._active_scope(ctx, store)
    rel = _join_incremental_bgp(store, patterns, stats, var_preds, finishable, triples)
    stats.join_ns = max(0, clock() - stats.native_ns - stats.restriction_ns)
    return rel, stats


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


def _count_patterns(store, patterns, counts: dict) -> int:
    """Set the patterns' ``estimate`` — their planning counts (see
    :func:`_count_pattern`) — counting the quad patterns ``counts`` does not
    hold yet, in static rank order, and stop at the first zero, which
    empties the block: the patterns left uncounted keep no ``estimate``.

    Once a pattern counts at most ``SETTINGS.probe_fanout`` rows, every pattern not
    yet counted that shares a variable with it is left uncounted: joined
    after it, it is probed unless it is small,
    which a count capped at the decision's threshold settles at its join
    step (:func:`_join_incremental_bgp`).

    The patterns are counted one at a time, except the scans: enough of them
    (``SETTINGS.scan_batch_min``, ``SETTINGS.memory_scan_batch_min``) are counted last, in
    one ``count_quads_many`` batch that runs them concurrently, one GIL
    release for all — after the point counts (``OpenFile.point_lookup``: those
    the native layer answers without a scan), any of which may still end the
    block first. Returns the counts made."""
    if not SETTINGS.count_order:  # VORTEX_RDF_COUNT_ORDER=0: static estimates, no native counts
        for pat in patterns:
            pat.estimate = 0 if pat.unsatisfiable else matching._static_estimate(pat)
        return 0
    before = len(counts)
    ordered = sorted(patterns, key=matching._pattern_static_rank)
    minimum = SETTINGS.memory_scan_batch_min if store.in_memory else SETTINGS.scan_batch_min
    scans: list = []
    if len(patterns) >= minimum:
        scans = [
            pat
            for pat in ordered
            if not pat.unsatisfiable
            and pat.n3 not in counts
            and not store._file.point_lookup(pat.n3)
        ]
        if len({pat.n3 for pat in scans}) < minimum:
            scans = []
    batched = {id(pat) for pat in scans}
    small: set = set()  # the variables of the patterns counted at most SETTINGS.probe_fanout rows
    for pat in ordered:
        if id(pat) in batched:
            continue
        if small.intersection(pat.varpos):
            continue
        pat.estimate = matching._count_pattern(store, pat, counts)
        if pat.estimate == 0:
            return len(counts) - before
        if pat.estimate <= SETTINGS.probe_fanout:
            small.update(pat.varpos)
    rest = [pat for pat in scans if not small.intersection(pat.varpos)]
    if len({pat.n3 for pat in rest}) >= minimum:
        keys = list(dict.fromkeys(pat.n3 for pat in rest))
        counts.update(zip(keys, boundary.native(store).count_quads_many(keys), strict=True))
        for pat in rest:
            pat.estimate = counts[pat.n3]
    else:
        for pat in rest:
            pat.estimate = matching._count_pattern(store, pat, counts)
            if pat.estimate == 0:
                return len(counts) - before
    return len(counts) - before


def _join_incremental_bgp(
    store, patterns, stats: _BgpStats, pushed=None, finishable=False, triples=()
) -> Relation:
    """Seed with the smallest pattern, then extend one connected pattern at
    a time.

    The patterns are counted before anything is matched, in static rank
    order up to the first zero — the scans together, in one concurrent
    native batch, when there are enough of them (:func:`_count_patterns`) —
    so a counted pattern that selects nothing ends the block before any
    match. A pattern sharing a variable with one of at most ``SETTINGS.probe_fanout``
    rows is left uncounted until its step, where it is counted only up to
    ``running * SETTINGS.probe_fanout + 1`` rows: a count that reaches the cap is
    large, and probes. The smallest counted pattern is matched and is the
    seed. Each further step takes the smallest pattern sharing a variable
    with the relation so far (the smallest of all for a cross product; an
    uncounted pattern ranks last). When the relation is at least
    ``SETTINGS.probe_fanout`` times smaller than the pattern's count, the step joins
    the pattern per row of the relation: it re-probes it natively per row
    (``_probe_join``) where each probe is a point lookup
    (``OpenFile.point_lookup``), and where each would scan the store it matches
    the pattern once, narrowed to the codes the relation binds its shared
    variables to (a keep-scan, :func:`_keep_scan`). Any other pattern is
    matched once. A matched pattern is hash-joined — columnar while the
    shapes allow, as rows otherwise.
    A pattern's native keep narrows it on whichever path it takes; its
    deferred per-variable restrictions apply to the variables the relation
    does not bind yet: a variable already bound was restricted by the
    pattern that bound it, and the join keeps its rows to those codes.
    Before a step, a tuple conjunct the relation binds all but one variable
    of may become a restriction of the pattern binding that one
    (:func:`_specialize_tuple_conjuncts`).

    A step over ``SETTINGS.pair_budget`` — a step whose pairs, counted once its
    two sides' product passes the budget, are more than the budget, a
    keep-scan whose match passes the budget, or a probe step whose rows pass
    a quarter of it — is traced as ``bgp_over_budget`` and is not built. A
    probe step over its tally is planned again, as a keep-scan. When a step
    over the budget is the last, a hash step or keep-scan sharing one
    variable, it streams instead (:func:`_streamed_step`): the relation
    returned is its pieces, gathered as they are pulled. Such a step whose
    pattern counts more rows than the budget and than the relation, or
    whose keep-scan came back over the budget, streams without reading the
    pattern whole: it reads it in chunks narrowed by its native keep
    (:func:`_read_in_chunks`), which a pattern that needs its whole columns
    cannot be (:func:`_chunkable`). A step works on a copy of its pattern
    and keeps it only once it completes, so any other step over the budget
    puts the pattern back as it was planned — its match, if it was read
    whole, left in the memo — and the next step takes the first other
    pattern sharing a variable with the relation that provably fits
    (:func:`_fits`), which records the patterns it was reordered around;
    those are tried again after it. Once none fits, the patterns left are
    rdflib's to finish per row of the relation so far — the relation's
    ``rest``, their ``triples`` (:func:`_finish_with_rdflib`) — where its
    ``evalBGP`` evaluates them as the block would: the relation goes
    straight to the emitter (``finishable``, which the caller grants only in
    the active graph) and no FILTER conjunct was pushed into the patterns,
    which it would not apply. Otherwise the block goes back to rdflib
    (``NotImplementedError``).
    """
    counts: dict[tuple, int] = {}
    memo: dict[tuple, tuple] = {}
    tuples = pushed.tuples if pushed is not None else ()
    views = pushed.views if pushed is not None else {}

    clock = trace.stopwatch()
    if any(pat.unsatisfiable for pat in patterns):
        # A literal bound into subject or predicate position, or a keep that
        # is an empty code column (IRIs the dictionary lacks, an empty ``IN``
        # list, lists with no IRI in common): the block is empty whatever the
        # other patterns count.
        for pat in patterns:
            pat.estimate = 0
    else:
        stats.estimate_calls += _count_patterns(store, patterns, counts)
    stats.native_ns += clock()
    # A pattern left uncounted (next to a small one, or past a zero count)
    # ranks last.
    remaining = sorted(
        patterns,
        key=lambda pat: (
            math.inf if pat.estimate is None else pat.estimate,
            matching._pattern_static_rank(pat),
        ),
    )
    # An empty block still names every variable of the patterns.
    if not remaining or remaining[0].estimate == 0:
        schema: tuple = ()
        for pat in patterns:
            schema += tuple(v for v in pat.varpos if v not in schema)
        _trace_plan_complete(len(patterns), [], None, stats, [])
        return Relation.from_rows(schema, [])

    seed = remaining.pop(0)
    calls, native_ns = matching._match_resolved_pattern(store, seed, memo)
    stats.initial_calls += calls
    stats.native_ns += native_ns
    clock = trace.stopwatch()
    _materialize_eager_restrictions(store, seed, ())
    stats.restriction_ns += clock()
    # The relation so far: a columnar or row body, then — after a streamed
    # last step — its pieces.
    rel = matching._rel_from_pattern(seed)
    order = [seed.index]
    trace.event(
        "bgp_seed_complete",
        original_pattern_index=order[0],
        cardinality_source="matched",
        estimated_rows=seed.estimate,
        matched_rows=seed.nrows,
        native_call_count=calls,
        native_match_ns=native_ns,
    )
    rel, tuples = _after_step(store, rel, tuples, remaining, views)

    execution_index = 1
    # The patterns put back over the budget against the relation as it
    # stands: until a step completes, the next one takes another that fits.
    put_back: list[Pattern] = []
    while remaining and rel.nrows:
        running = rel.nrows
        connected = (pat for pat in remaining if any(v in rel.schema for v in pat.varpos))
        if put_back:
            pat = next(
                (
                    pat
                    for pat in connected
                    if all(pat is not back for back in put_back) and _fits(rel, pat, memo, store)
                ),
                None,
            )
            if pat is None:
                break
        else:
            pat = next(connected, remaining[0])
        position = next(i for i, other in enumerate(remaining) if other is pat)
        del remaining[position]
        shared = [v for v in pat.varpos if v in rel.schema]
        elapsed = trace.stopwatch()
        count_limit = None
        if pat.estimate is None:
            # Next to a pattern of at most SETTINGS.probe_fanout rows, so not
            # counted when planned: counted here, only as far as the
            # probe-or-match decision needs; a capped answer means probe. The
            # count is the pattern's, kept whether or not the step completes.
            count_limit = running * SETTINGS.probe_fanout + 1
            clock = trace.stopwatch()
            pat.estimate = matching._count_pattern(store, pat, counts, limit=count_limit)
            # An answer at the cap is a lower bound of the count (_fits).
            pat.estimate_capped = pat.estimate >= count_limit
            stats.native_ns += clock()
            stats.estimate_calls += 1
        # The step works on a copy and keeps it only once it completes: a
        # step over the budget leaves the pattern as it was planned — unread,
        # its keep and deferred restrictions its own — for a later step.
        work = pat.copy()
        specialized = ()
        if tuples and pushed is not None:
            # Another block of the group may have applied some since.
            tuples = tuple(c for c in tuples if id(c) not in pushed.applied)
            specialized = _specialize_tuple_conjuncts(store, work, rel, tuples, pushed)
        step = _JoinStep(execution_index, work, running, pat.estimate, count_limit, len(shared))
        joined = None
        for tier in _tiers(store, rel, work, shared, last=not remaining):
            joined = _TIER_STEPS[tier](store, rel, work, memo, step, stats)
            if joined is not None:
                break
        if joined is None:
            # Over the budget every way it joins: back where it was planned,
            # as it was — its match left in the memo, and the tuple
            # conjuncts the step specialized onto its copy still pending.
            remaining.insert(position, pat)
            put_back.append(pat)
            continue
        rel = joined
        # The step completed: the tuple conjuncts it specialized are applied.
        if specialized:
            assert pushed is not None
            pushed.applied.update(id(conjunct) for conjunct, _, _ in specialized)
            tuples = tuple(c for c in tuples if id(c) not in pushed.applied)
        _trace_join_step(step, rel, put_back, elapsed())
        put_back.clear()
        order.append(pat.index)
        if rel.pieces is not None:
            break
        rel, tuples = _after_step(store, rel, tuples, remaining, views)
        execution_index += 1

    if put_back:
        # No pattern left fits. rdflib's evalBGP finishes them per row as the
        # block would only where the rows go straight to the emitter and no
        # FILTER conjunct was pushed into the patterns: it would not apply it.
        if not finishable or (pushed is not None and (pushed or pushed.tuples)):
            raise NotImplementedError
        rel.rest = tuple(triples[pat.index] for pat in remaining)
    elif remaining:
        # The relation ran empty before these patterns were joined: the
        # schema names their variables, which no column carries.
        schema = rel.schema
        for pat in remaining:
            schema += tuple(v for v in pat.varpos if v not in schema)
        rel = Relation.from_rows(schema, [])
    rest = [pat.index for pat in remaining] if put_back else []
    _trace_plan_complete(len(patterns), order + [pat.index for pat in remaining], seed, stats, rest)
    return rel


def _specialize_tuple_conjuncts(store, pat: Pattern, rel: Relation, tuples, pushed) -> list:
    """Turn the tuple conjuncts the relation all but decides into
    per-variable conjuncts of ``pat``.

    A pending conjunct whose variables the relation binds — all but one,
    which ``pat`` binds — is a test of that one variable once each of the
    others holds a single value in every row: a band
    ``?v < ?ref + N && ?v > ?ref - N`` once an anchor pattern has bound
    ``?ref``. With those values substituted (rdflib's own evaluator computes
    the constant side, ``?ref + N``), a shape vortex-rdf decides
    (:func:`filters.specialize`) joins ``pat``'s deferred restrictions and is
    evaluated over the codes ``pat`` brings — the rows its match returns, or
    the rows its probes reach. Every row the BGP then yields passes the
    conjunct, which the step marks applied once it completes: neither the
    mid-join prune nor the FILTER above evaluates it again. ``pat`` is the
    step's copy, so a step that goes back over the budget leaves the
    pattern without them.

    Returns what it specialized, ``(conjunct, variable, per-variable
    conjunct)`` each.
    """
    specialized: list = []
    single: dict = {}
    for conjunct in tuples:
        if id(conjunct) in pushed.applied:
            continue
        free = [v for v in conjunct.vars if v not in rel.schema]
        if len(free) != 1 or free[0] not in pat.varpos:
            continue
        var = free[0]
        consts = dict(conjunct.consts)
        for other in conjunct.vars:
            if other == var:
                continue
            if other not in single:
                single[other] = _single_code(rel, rel.schema.index(other))
            if single[other] is None:
                break
            consts[other] = store._decode_term(single[other])
        else:
            proxy = filters.specialize(conjunct, var, consts)
            if proxy is None:
                continue
            deferred = pat.deferred
            pat.deferred = {**deferred, var: [*deferred.get(var, ()), proxy]}
            specialized.append((conjunct, var, proxy))
            if trace.active() is not None:
                trace.event(
                    "bgp_tuple_specialized",
                    original_pattern_index=pat.index,
                    variable=str(var),
                    native_predicate=proxy.native_kind(),
                )
    return specialized


def _single_code(rel: Relation, index: int) -> int | None:
    """The one code a non-empty relation holds in column ``index`` in every
    row, or ``None`` when it holds several."""
    distinct = rel.native_column(index).distinct()
    return memoryview(distinct).cast("Q")[0] if len(distinct) == 1 else None


def _after_step(store, rel: Relation, tuples, remaining, views) -> tuple[Relation, tuple]:
    """What follows the seed and every completed step: the ablation's term
    round trip (``VORTEX_RDF_DECODE_EACH_STEP``,
    :func:`relation._decode_each_step`), then the mid-join prune
    (:func:`_prune_early`). Returns the relation and the tuple conjuncts
    still pending."""
    if SETTINGS.decode_each_step:
        rel = relation._decode_each_step(store, rel)
    return _prune_early(store, rel, tuples, remaining, views)


def _prune_early(store, rel: Relation, pending, remaining, views) -> tuple[Relation, tuple]:
    """Prune a BGP's running relation with the pending tuple conjuncts its
    schema covers, when that pays. Evaluating a conjunct costs more per row
    than a join step spends on the row, so pruning pays only by letting a
    remaining pattern be probed instead of matched whole — the planner's own
    test, `survivors * SETTINGS.probe_fanout < estimate`. The rows stream, evaluation
    stops once too many survive for that, and the conjunct is left to the
    FILTER above the BGP, which streams it over a columnar body, so a LIMIT
    stops it early.

    ``views`` is the parsed-term cache it shares with that FILTER. Returns
    ``(rel, pending)``: the relation, pruned or as it was, and the conjuncts
    not yet evaluated.
    """
    ready, waiting = [], []
    for conjunct in pending:
        (ready if all(v in rel.schema for v in conjunct.vars) else waiting).append(conjunct)
    if not ready:
        return rel, pending
    probeable = max(
        (
            pat.estimate
            for pat in remaining
            if pat.estimate is not None and any(v in rel.schema for v in pat.varpos)
        ),
        default=0,
    )
    if SETTINGS.probe_fanout >= probeable:  # not even one survivor would be probed
        return rel, pending
    started = trace.stopwatch()
    predicates = tuple(
        filters.tuple_fast_reject_predicate(
            store, conjunct, [rel.schema.index(variable) for variable in conjunct.vars], views
        )
        for conjunct in ready
    )
    input_rows = rel.nrows
    kept = []
    evaluated = 0
    deferred = False
    for row in relation._body_rows(rel):
        evaluated += 1
        if all(predicate(row) for predicate in predicates):
            kept.append(row)
            if len(kept) * SETTINGS.probe_fanout >= probeable:
                deferred = True
                break
    trace.event(
        "bgp_prune_complete",
        conjunct_count=len(ready),
        probe_estimate=probeable,
        input_rows=input_rows,
        evaluated_rows=evaluated,
        output_rows=input_rows if deferred else len(kept),
        pruned=not deferred,
        elapsed_ns=started(),
    )
    if deferred:
        return rel, tuple(waiting)
    return Relation.from_rows(rel.schema, kept), tuple(waiting)


def _fits(rel: Relation, pat: Pattern, memo: dict, store) -> bool:
    """Whether joining ``pat`` into the relation — a columnar or row body —
    provably stays within ``SETTINGS.pair_budget``, from what is held
    already: no pattern is counted or matched here.

    It does when the relation's rows times the pattern's count fit, a count
    that did not stop at a cap: a capped count is a lower bound and proves
    nothing, while one that leaves out the pattern's keep bounds its match
    from above. It does too when the step would hash-join the pattern, its
    match is in ``memo``, and the pairs ``join_count`` counts between that
    match and the relation on their first shared variable fit: the step
    makes no more, its restrictions and any other shared variable only
    narrowing them. A pattern the step would probe qualifies only through
    the product, and one the step has yet to count not at all.
    """
    estimate = pat.estimate
    if estimate is None:
        return False
    running = rel.nrows
    if not pat.estimate_capped and running * estimate <= SETTINGS.pair_budget:
        return True
    if running * SETTINGS.probe_fanout < estimate:
        return False
    match = memo.get(matching._pattern_key(pat))
    if match is None:
        return False
    var = next(v for v in pat.varpos if v in rel.schema)
    key = rel.native_column(rel.schema.index(var))
    return key.join_count(match[pat.varpos[var][0]]) <= SETTINGS.pair_budget


def _step_rule(store, running: int, pat: Pattern, shared: Sequence) -> str:
    """How a counted pattern joins a relation of ``running`` rows on its
    ``shared`` variables: per row of the relation when the relation is at
    least ``SETTINGS.probe_fanout`` times smaller than the pattern's count —
    ``"probe"`` where each probe is a point lookup
    (:meth:`OpenFile.point_lookup`), else ``"keep_scan"``, one match narrowed
    to the relation's codes rather than a scan per row — and ``"hash"``, its
    match hash-joined, otherwise. A probe step stops at
    :func:`_probe_budget` rows."""
    estimate = pat.estimate
    assert estimate is not None, "a step counts its pattern before it plans the join"
    if shared and running * SETTINGS.probe_fanout < estimate:
        cheap = store._file.point_lookup(probes._probe_shape(pat, shared))
        return "probe" if cheap else "keep_scan"
    return "hash"


def _probe_budget() -> int:
    """The rows a probe step makes before it is planned again as a
    keep-scan: a quarter of ``SETTINGS.pair_budget``."""
    return SETTINGS.pair_budget // 4


def _tiers(store, rel: Relation, work: Pattern, shared: list, last: bool) -> tuple[str, ...]:
    """The strategies a join step tries, in order, until one joins within
    the budget: the step rule's (:func:`_step_rule`), a probe step's
    followed by a keep-scan. The last step sharing one variable streams
    (tier 1, :func:`_stream_step`): from the start when its pattern is too
    large to read whole (:func:`streaming._read_in_chunks`), else once the
    others are over the budget."""
    streams = last and len(shared) == 1
    rule = _step_rule(store, rel.nrows, work, shared)
    if rule == "probe":
        tiers: tuple[str, ...] = ("probe", "keep_scan")
    elif rule == "keep_scan":
        tiers = ("keep_scan",)
    elif streams and streaming._read_in_chunks(work, rel.nrows):
        return ("stream",)
    else:
        tiers = ("hash",)
    return (*tiers, "stream") if streams else tiers


def _probe_step(store, rel: Relation, work: Pattern, memo, step, stats) -> Relation | None:
    """Join ``work`` per row of the relation by its probes
    (:func:`probes._probe_join`); ``None`` once their rows pass
    :func:`_probe_budget`, with the probes sent counted."""
    step.strategy, step.source, step.probe_calls = "probe", "probed", step.running
    clock = trace.stopwatch()
    try:
        schema, rows = probes._probe_join(
            store, rel.schema, rel.row_list(), work, max_rows=_probe_budget()
        )
    except probes.RowsOverBudget as over:
        # Only the probes sent before the tally passed count.
        schema, rows, step.probe_calls = (), None, over.calls
    step.probe_ns = clock()
    stats.probe_calls += step.probe_calls
    stats.native_ns += step.probe_ns
    if rows is None:
        _trace_over_budget(step, None)
        return None
    joined = Relation.from_rows(schema, rows)
    step.match_rows, step.restricted_rows = step.estimate, joined.nrows
    return joined


def _keep_scan_step(store, rel: Relation, work: Pattern, memo, step, stats) -> Relation | None:
    """Join ``work`` by one match narrowed to the relation's codes
    (:func:`_keep_scan`), hash-joined; ``None`` when the match or the join
    passes the budget."""
    step.strategy, step.source = "keep_scan", "matched"
    calls, native_ns, fits = _keep_scan(store, work, rel)
    stats.initial_calls += calls
    stats.native_ns += native_ns
    if not fits:
        _trace_over_budget(step, None)
        return None
    return _hash_join_step(rel, _restricted(store, rel, work, step, stats), step)


def _hash_step(store, rel: Relation, work: Pattern, memo, step, stats) -> Relation | None:
    """Match ``work`` whole, through the memo, and hash-join it; ``None``
    when the join passes the budget."""
    step.source = "matched"
    calls, native_ns = matching._match_resolved_pattern(store, work, memo)
    stats.initial_calls += calls
    stats.native_ns += native_ns
    right = _restricted(store, rel, work, step, stats)
    step.strategy = relation._hash_kind(rel, right)
    return _hash_join_step(rel, right, step)


def _stream_step(store, rel: Relation, work: Pattern, memo, step, stats) -> Relation | None:
    """Tier 1: the last step, sharing one variable, as pieces gathered while
    they are pulled (:func:`streaming._streamed_step`) — fed from chunks of
    the pattern narrowed by its native keep while it is unread, from slices
    once its match is held (read by an earlier strategy, or held by the
    memo). ``None`` for an unread pattern that needs its whole columns
    (:func:`streaming._chunkable`)."""
    if work.cols is None and not streaming._chunkable(work):
        return None
    first = step.source is None
    step.strategy = "stream"
    schema, pieces = streaming._streamed_step(store, rel, work, memo)
    if work.cols is None:
        step.feed, step.source = "chunks", "chunked"
        step.match_rows = step.restricted_rows = None
        stats.initial_calls += 1
    else:
        step.feed = "slices"
        if first:
            # The memo held the pattern's match: it was read whole.
            step.source = "matched"
            step.match_rows = step.restricted_rows = work.nrows
    return Relation(schema, None, None, 0, pieces=pieces)


#: Each strategy a join step may try (:func:`_tiers`), by name.
_TIER_STEPS = {
    "probe": _probe_step,
    "keep_scan": _keep_scan_step,
    "hash": _hash_step,
    "stream": _stream_step,
}


def _restricted(store, rel: Relation, work: Pattern, step, stats) -> Relation:
    """A matched pattern as the relation a hash join takes: its deferred
    restrictions applied over the variables the relation does not bind
    (:func:`_materialize_eager_restrictions`), its rows before and after
    them noted on the step."""
    clock = trace.stopwatch()
    step.match_rows = _materialize_eager_restrictions(store, work, rel.schema)
    stats.restriction_ns += clock()
    step.restricted_rows = work.nrows
    return matching._rel_from_pattern(work)


def _hash_join_step(rel: Relation, right: Relation, step) -> Relation | None:
    """The hash join of a step (:func:`relation._hash_join`); ``None`` past
    the budget, its pairs traced."""
    try:
        joined, step.pair_count = relation._hash_join(rel, right)
    except OverBudget as over:
        step.pair_count = over.pairs
        _trace_over_budget(step, over.pairs)
        return None
    return joined


def _keep_scan(store, pat: Pattern, rel: Relation) -> tuple[int, int, bool]:
    """Match ``pat`` once, narrowed to the codes the relation — a columnar
    or row body — binds its shared variables to: a step whose probes would
    each scan the store scans it once, and joins the match as a hash step
    does.

    Each shared variable's distinct codes, ascending, become the native keep
    at every position it takes, on top of any keep already there
    (:func:`_set_native_keep`): the match leaves out only rows that no row of
    the relation joins, and the join pairs the others, quad by quad, as the
    probes would have. The match goes through :func:`_match_resolved_pattern`
    without the memo: no other step narrows the pattern by these codes, and
    a memoized match would hold its columns until the plan ends. A
    pattern whose count may pass ``SETTINGS.pair_budget`` — one over it, or a
    count stopped at its cap, a lower bound — is matched only up to one row
    past the budget: a match that reaches it is over the budget and is
    dropped, the pattern left unread. Returns the native calls made, their
    time, as :func:`_match_resolved_pattern` does, and whether the match is
    within the budget."""
    for var in pat.varpos:
        if var in rel.schema:
            codes = relation._distinct_codes(rel.native_column(rel.schema.index(var)))
            matching._keep_only(pat, var, sorted(codes))
    estimate = pat.estimate
    assert estimate is not None, "a step counts its pattern before it keep-scans it"
    if not pat.estimate_capped and estimate <= SETTINGS.pair_budget:
        return (*matching._match_resolved_pattern(store, pat), True)
    limit = SETTINGS.pair_budget + 1
    calls, native_ns = matching._match_resolved_pattern(store, pat, limit=limit)
    if pat.nrows == limit:
        pat.clear_match()
        return calls, native_ns, False
    return calls, native_ns, True


@dataclass(slots=True)
class _JoinStep:
    """One step of a BGP's join, for its trace events: ``pat`` joined into
    the relation of ``running`` rows (step ``execution_index`` of the plan),
    the count it was planned with (``estimate``, counted at the step up to
    ``count_limit`` when it was not planned), the variables it shares, and
    what the step did — its ``strategy`` and where its rows came from
    (``source``), each set by the strategy that tries it (:func:`_tiers`),
    how a streamed step is fed, its pairs, its pattern's rows before and
    after their restrictions, and its probes."""

    execution_index: int
    pat: Pattern
    running: int
    estimate: int
    count_limit: int | None
    shared_count: int
    strategy: str | None = None
    source: str | None = None
    feed: str | None = None
    pair_count: int | None = None
    match_rows: int | None = None
    restricted_rows: int | None = None
    probe_calls: int = 0
    probe_ns: int = 0


def _trace_join_step(step: _JoinStep, rel: Relation, put_back: list, elapsed_ns: int) -> None:
    """Trace a completed join step as ``bgp_join_step_complete``: the step,
    the patterns it was reordered around, and the relation it made."""
    if trace.active() is None:
        return
    trace.event(
        "bgp_join_step_complete",
        execution_index=step.execution_index,
        original_pattern_index=step.pat.index,
        strategy=step.strategy,
        feed=step.feed,
        reordered_around=[back.index for back in put_back] or None,
        cardinality_source=step.source,
        input_rows=step.running,
        estimated_rows=step.estimate,
        estimate_limit=step.count_limit,
        pair_count=step.pair_count,
        pattern_match_rows=step.match_rows,
        pattern_restricted_rows=step.restricted_rows,
        native_initial_match_count=int(step.source != "probed"),
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


def _materialize_eager_restrictions(store, pat: Pattern, bound) -> int:
    """Apply a matched pattern's deferred per-variable restrictions — over
    the variables not in ``bound``, the relation it is about to join —
    compacting it natively to the rows that pass, so its real size, not the
    match's, is what the join sees. Returns the match's rows before them."""
    input_rows = pat.nrows
    pending, pat.deferred = pat.deferred, {}
    if pending:
        pending = {v: conjuncts for v, conjuncts in pending.items() if v not in bound}
    if pending:
        matching._restrict_pattern(store, pat, pending)
    return input_rows
