"""Probing a pattern per row of a relation: the native calls a probe join
makes — one at a time, or in concurrent batches when that pays — and the
join they make (:func:`_probe_join`), and the existence probes of
``FILTER (NOT) EXISTS``.
"""

from collections.abc import Iterator, Sequence
from itertools import islice

from .. import filters
from ..cancel import cancel_kw, cancel_kw_for
from ..settings import SETTINGS
from ..terms import canonical_spelling, unparseable_iri
from . import matching, tracing
from .matching import Pattern
from .relation import _OverBudget


def _probe_exists(store, schema, rows, pat: Pattern) -> list:
    """Whether the pattern, with each row's codes substituted, matches at
    least one quad — counts capped at one row (``count_quads_many``, in
    batches), no row materialized."""
    bound = [(schema.index(v), pos) for v, pos in pat.varpos.items() if v in schema]
    free_positions = {idx for v, pos in pat.varpos.items() if v not in schema for idx in pos}
    keep = {p: spec for p, spec in (pat.nkeep or {}).items() if p in free_positions}
    narrowing = matching._narrowing(keep, limit=1)
    patterns = _substituted_patterns(store, rows, pat, bound)
    out = []
    for start in range(0, len(rows), SETTINGS.probe_batch):
        batch = patterns[start : start + SETTINGS.probe_batch]
        probes = [(n3, narrowing) for n3 in batch if n3 is not None]
        answers = iter(_run_probes(store, probes, count=True))
        out.extend(n3 is not None and next(answers) > 0 for n3 in batch)
    return out


def _substituted_patterns(store, rows, pat: Pattern, bound) -> list:
    """Per row, the pattern's quad with the row's codes substituted at the
    ``bound`` positions — or ``None`` when no quad can match it."""
    # One GIL-released batch decode covers every code the probes will bind.
    n3_cache = _probe_spellings(store, rows, bound)
    patterns = []
    for row in rows:
        n3 = list(pat.n3)
        satisfiable = True
        for row_idx, positions in bound:
            term = n3_cache[row[row_idx]]
            for idx in positions:
                # The dictionary stores canonical N-Triples forms: a literal
                # ('"') cannot occupy subject or predicate position or name a
                # graph, and only an IRI ('<') can be a predicate — such a
                # binding simply has no continuation, nor has a constant with
                # no spelling (an IRI the parser rejects).
                if (
                    term is None
                    or (idx == 0 and term[:1] == '"')
                    or (idx == 1 and term[:1] != "<")
                    or (idx == 3 and term[:1] == '"')
                ):
                    satisfiable = False
                n3[idx] = term
        patterns.append(n3 if satisfiable else None)
    return patterns


def _probe_lookahead(store, batch_n3: Sequence[Sequence[str | None]]) -> int | None:
    """The ``lookahead`` of one batch call over these probes' quad patterns:
    ``None``, the binding's default, when every probe is a point lookup
    (``store._point_lookup``), else ``SETTINGS.scan_lookahead``."""
    if all(store._point_lookup(n3) for n3 in batch_n3):
        return None
    return SETTINGS.scan_lookahead


def _batched_probes(store, patterns) -> list[int]:
    """The indexes of the quad patterns :func:`_run_probes` answers through
    one batch call: those that scan, when there are enough of them
    (``SETTINGS.scan_batch_min``, ``SETTINGS.memory_scan_batch_min``), or all of them when
    enough cheap ones are probing a file-backed store
    (``SETTINGS.cheap_batch_min``)."""
    minimum = SETTINGS.memory_scan_batch_min if store.in_memory else SETTINGS.scan_batch_min
    if len(patterns) < minimum:
        return []
    batched = [i for i, n3 in enumerate(patterns) if not store._point_lookup(n3)]
    if not store.in_memory and len(patterns) - len(batched) >= SETTINGS.cheap_batch_min:
        return list(range(len(patterns)))
    return batched if len(batched) >= minimum else []


def _run_probes(store, probes, count: bool) -> list | Iterator:
    """Native answers to ``(n3, narrowing)`` probes (see :func:`_narrowing`),
    in order: a list of counts, or an iterator of code columns that makes
    each answer as it is pulled.

    The probes :func:`_batched_probes` picks go through one
    ``count_quads_many`` or ``match_codes_many`` call — every probe parsed
    before any runs, running as far ahead on the native runtime as
    :func:`_probe_lookahead` lets them, a file-backed store overlapping their
    reads — and the rest through plain calls. The query's cancel token is
    read once: a batch call races it, a plain call races or checks it as
    :func:`cancel_kw_for` decides, and without a token no call is given a
    keyword. The matches come from :func:`_probe_matches` under a token or
    when only some probes are batched; otherwise from a generator over the
    plain calls, or from the batch's own native iterator
    (:func:`_batch_answers`).
    """
    if not probes:
        return []
    native = tracing._native(store)
    minimum = SETTINGS.memory_scan_batch_min if store.in_memory else SETTINGS.scan_batch_min
    batched = _batched_probes(store, [n3 for n3, _ in probes]) if len(probes) >= minimum else []
    cancel = cancel_kw()
    if not count:
        if cancel or 0 < len(batched) < len(probes):
            return _probe_matches(store, native, probes, batched, cancel)
        if batched:
            return _batch_answers(store, native, probes)
        return (native.match_codes(*n3, **narrowing) for n3, narrowing in probes)
    if not batched:
        if not cancel:
            return [native.count_quads(*n3, **narrowing) for n3, narrowing in probes]
        return [
            native.count_quads(
                *n3, **narrowing, **cancel_kw_for(cancel, store, n3, narrowing.get("limit"))
            )
            for n3, narrowing in probes
        ]
    answers: list = [None] * len(probes)
    batch = [_batch_probe(*probes[i]) for i in batched]
    lookahead = _probe_lookahead(store, [probes[i][0] for i in batched])
    counts = native.count_quads_many(batch, lookahead=lookahead, **cancel)
    for i, answer in zip(batched, counts, strict=True):
        answers[i] = answer
    done = set(batched)
    for i, (n3, narrowing) in enumerate(probes):
        if i not in done:
            limit = narrowing.get("limit")
            answers[i] = native.count_quads(
                *n3, **narrowing, **cancel_kw_for(cancel, store, n3, limit)
            )
    return answers


def _batch_probe(n3, narrowing):
    """A probe as the batch calls take it: its quad pattern, or a dict of the
    pattern and its narrowing."""
    if narrowing:
        return {"s": n3[0], "p": n3[1], "o": n3[2], "g": n3[3], **narrowing}
    return tuple(n3)


#: What :func:`_probe_matches` and :func:`_batch_answers` read past a batch's
#: last answer when the batch has no more.
_NO_ANSWER = object()


def _batch_answers(store, native, probes) -> Iterator:
    """The code columns of every probe, in order, from one
    ``match_codes_many`` call the first pull makes: the native iterator's
    answers, each waited for as it is pulled, while the probes run as far
    ahead as :func:`_probe_lookahead` lets them. A batch that answers fewer
    probes than it was given raises ``ValueError`` at the first probe left
    without an answer, and one that answers more, with its last probe.
    Closing the iterator releases the batch's answers, which aborts the
    probes still in flight."""
    total = len(probes)
    answers = native.match_codes_many(
        [_batch_probe(*probe) for probe in probes],
        lookahead=_probe_lookahead(store, [n3 for n3, _ in probes]),
    )
    try:
        yield from islice(answers, total - 1)
        last = next(answers, _NO_ANSWER)
        if last is _NO_ANSWER:
            raise ValueError(f"match_codes_many answered fewer than its {total} probes")
        if next(answers, _NO_ANSWER) is not _NO_ANSWER:
            raise ValueError(f"match_codes_many answered more than its {total} probes")
        yield last
    finally:
        close = getattr(answers, "close", None)
        if close is not None:
            close()


def _probe_matches(store, native, probes, batched, cancel: dict) -> Iterator:
    """The code columns of each probe, in order, each made when it is pulled:
    the ``batched`` probes' from the one ``match_codes_many`` call the first
    pull makes, whose probes run, as far ahead as :func:`_probe_lookahead`
    lets them, while the consumer works through the answers before theirs;
    the others from a ``match_codes`` call at their position. ``cancel`` is
    the query's cancel keyword (:func:`cancel_kw`): the batch is given it,
    and a plain call races or checks the token as :func:`cancel_kw_for`
    decides. A batch that answers fewer probes than it was given raises
    ``ValueError`` at the first probe left without an answer, and one that
    answers more, with its last probe. Closing the iterator releases the
    batch's answers, which aborts the probes still in flight."""
    in_batch = set(batched)
    answers = iter(())
    if batched:
        answers = native.match_codes_many(
            [_batch_probe(*probes[i]) for i in batched],
            lookahead=_probe_lookahead(store, [probes[i][0] for i in batched]),
            **cancel,
        )
    unanswered = len(batched)
    try:
        for i, (n3, narrowing) in enumerate(probes):
            if i not in in_batch:
                limit = narrowing.get("limit")
                yield native.match_codes(
                    *n3, **narrowing, **cancel_kw_for(cancel, store, n3, limit)
                )
                continue
            try:
                answer = next(answers)
            except StopIteration:
                raise ValueError(
                    f"match_codes_many answered fewer than its {len(batched)} probes"
                ) from None
            unanswered -= 1
            if not unanswered and next(answers, _NO_ANSWER) is not _NO_ANSWER:
                raise ValueError(f"match_codes_many answered more than its {len(batched)} probes")
            yield answer
    finally:
        close = getattr(answers, "close", None)
        if close is not None:
            close()


def _batch_probes_sent(store, probes, pulled: int, count: bool) -> int:
    """How many of one batch's ``(n3, narrowing)`` probes have reached the
    native layer once ``pulled`` of their answers are taken, as
    :func:`_run_probes` sends them: every count, all made at once; of
    matches, the batched ones (:func:`_batched_probes`), sent together at the
    first pull, and the others pulled so far."""
    if count:
        return len(probes)
    if not pulled:
        return 0
    batched = set(_batched_probes(store, [n3 for n3, _ in probes]))
    return len(batched) + sum(1 for k in range(pulled) if k not in batched)


def _probe_shape(pat: Pattern, shared: Sequence) -> list:
    """``pat``'s quad pattern as each probe of a row binds it: a placeholder
    at every position a ``shared`` variable takes, where the row's code goes.
    It tells ``store._point_lookup`` whether the probes would scan."""
    shape = list(pat.n3)
    for var in shared:
        for position in pat.varpos[var]:
            shape[position] = ""
    return shape


def _probe_join(
    store,
    schema,
    rows,
    pat: Pattern,
    keep_unmatched=False,
    row_preds=(),
    max_rows: int | None = None,
):
    """Join the relation against a pattern by re-matching it per binding.

    The adaptive half of the join strategy: when the running relation is far
    smaller than the pattern's match (``SETTINGS.probe_fanout``), materializing
    thousands of pattern rows to hash-join against a handful is the wrong
    trade. Substituting each relation row's codes into the pattern and
    re-matching natively keeps the work proportional to the small side. The
    probes go to the native layer in batches (``match_codes_many``, or
    ``count_quads_many`` for an existence probe, a pattern with no free
    position): one call each, a batch's probes running as far ahead as
    :func:`_probe_lookahead` lets them. An existence probe keeps its row
    once when its triple is there: the quads are unique, and the union over
    every graph is a set, so its count stops at one row. A probe of a
    ``distinct`` pattern (the union over the graphs of a store holding
    several) keeps one row of each run of equal triples, adjacent in its
    match (:mod:`vortex_rdflib.union`). The pattern's native keep narrows each probe on the
    positions it leaves free (a code range inside the native match, a code
    column over the rows it returns), as do ``row_preds`` over the extended
    row; with ``keep_unmatched`` a row without a continuation is kept, padded
    with None (a left join). Each batch's answers are pulled as the join
    reaches them and released when the batch ends, however it ends. Once a
    probe's rows take the join past ``max_rows``, it drops the rows it made
    and the batch it is pulling, which aborts the probes still in flight,
    and raises :class:`_OverBudget` with the probes it sent.
    """
    bound = [(schema.index(v), pos) for v, pos in pat.varpos.items() if v in schema]
    free = {v: pos for v, pos in pat.varpos.items() if v not in schema}
    eq_checks = [(pos[0], later) for pos in free.values() for later in pos[1:]]
    out_positions = [pos[0] for pos in free.values()]
    needed = sorted({idx for pos in free.values() for idx in pos})
    # A code range rides every probe; a code column is tested on the rows a
    # probe returns, since the native layer would re-read it per probe.
    probe_keep, tests = {}, []
    for position, spec in (pat.nkeep or {}).items():
        if position in needed:
            if isinstance(spec, tuple):
                probe_keep[position] = spec
            else:
                tests.append((position, filters.native.keep_member(spec)))
    narrowing = matching._narrowing(probe_keep, limit=None if free else 1)
    patterns = _substituted_patterns(store, rows, pat, bound)

    pad = (None,) * len(free)
    out = []
    for start in range(0, len(rows), SETTINGS.probe_batch):
        batch = range(start, min(start + SETTINGS.probe_batch, len(rows)))
        probes = [(patterns[i], narrowing) for i in batch if patterns[i] is not None]
        answers = iter(_run_probes(store, probes, count=not free))
        try:
            for i in batch:
                row = rows[i]
                matched = False
                if patterns[i] is None:
                    pass
                elif not free:
                    # Existence probe: a count, no columns; the row stands once
                    # per quad counted.
                    count = next(answers)
                    if count and all(pred(row) for pred in row_preds):
                        out.extend([row] * count)
                        matched = True
                else:
                    cols = next(answers)
                    if cols is None:
                        raise NotImplementedError
                    views = {idx: memoryview(cols[idx]).cast("Q").tolist() for idx in needed}
                    previous = None
                    for j in range(len(views[needed[0]])):
                        if pat.distinct:
                            # The rows of one triple differ only in their
                            # graph, which no variable reads: keep the first.
                            triple = tuple(views[idx][j] for idx in needed)
                            if triple == previous:
                                continue
                            previous = triple
                        if all(views[a][j] == views[b][j] for a, b in eq_checks) and all(
                            test(views[p][j]) for p, test in tests
                        ):
                            extended = row + tuple(views[idx][j] for idx in out_positions)
                            if all(pred(extended) for pred in row_preds):
                                out.append(extended)
                                matched = True
                if keep_unmatched and not matched:
                    out.append(row + pad)
                if max_rows is not None and len(out) > max_rows:
                    out.clear()
                    sent = sum(n3 is not None for n3 in patterns[:start])
                    pulled = sum(n3 is not None for n3 in patterns[start : i + 1])
                    sent += _batch_probes_sent(store, probes, pulled, count=not free)
                    raise _OverBudget(None, sent)
        finally:
            close = getattr(answers, "close", None)
            if close is not None:
                close()
    combined_schema = schema + tuple(free.keys())
    # The deferred per-variable restrictions, over the codes the probes
    # actually reached rather than the pattern's whole column. A variable
    # the relation bound was restricted where it was bound.
    pending, pat.deferred = pat.deferred, None
    if pending and out:
        restriction_input_rows = len(out)
        for var, conjuncts in pending.items():
            if var not in free:
                continue
            position = combined_schema.index(var)
            values = {row[position] for row in out}
            started = tracing._stopwatch()
            allowed = filters.evaluate_column(store, conjuncts, values)
            elapsed_ns = started()
            out = [row for row in out if row[position] in allowed]
            tracing._trace_event(
                "bgp_restriction_complete",
                original_pattern_index=pat.index,
                variable=str(var),
                mode="probe",
                input_rows=restriction_input_rows,
                output_rows=len(out),
                **tracing._restriction_counts(conjuncts),
                distinct_binding_count=len(values),
                allowed_binding_count=len(allowed),
                elapsed_ns=elapsed_ns,
            )
            restriction_input_rows = len(out)
        pat.probe_rows = len(out)
    return combined_schema, out


def _probe_spellings(store, rows, bound) -> dict:
    """The N-Triples spelling of every code the probes substitute: one
    batch decode for dictionary codes, and for a constant outside the
    dictionary the canonical spelling :func:`_constant_codes` looked up
    (which then matches nothing), or ``None`` for an IRI the parser rejects,
    which it did not look up."""
    distinct = {row[row_idx] for row in rows for row_idx, _ in bound}
    codes = [code for code in distinct if code >= 0]
    foreign = store._terms().foreign
    n3_cache: dict[int, str | None] = {
        code: None if unparseable_iri(foreign[code]) else canonical_spelling(foreign[code])
        for code in distinct
        if code < 0
    }
    for code, term in zip(codes, store._dict.decode_many(codes), strict=True):
        if term is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        n3_cache[code] = term
    return n3_cache
