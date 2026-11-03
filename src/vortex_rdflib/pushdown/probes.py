"""Probing a pattern per row of a relation: the native calls a probe join
makes — one at a time, or in concurrent batches when that pays — and the
join they make (:func:`_probe_join`), and the existence probes of
``FILTER (NOT) EXISTS``.
"""

from collections.abc import Generator, Iterator, Sequence
from itertools import islice

from .. import boundary, filters, trace, union
from ..settings import SETTINGS
from ..terms import canonical_spelling, is_range, unparseable_iri
from . import matching
from .matching import Pattern


def _probe_exists(store, schema, rows, pat: Pattern) -> list:
    """Whether the pattern, with each row's codes substituted, matches at
    least one quad — counts capped at one row (``count_quads_many``, in
    batches), no row materialized."""
    bound = [(schema.index(v), pos) for v, pos in pat.varpos.items() if v in schema]
    free_positions = {idx for v, pos in pat.varpos.items() if v not in schema for idx in pos}
    keep = {p: spec for p, spec in pat.nkeep.items() if p in free_positions}
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
    (``OpenFile.point_lookup``), else ``SETTINGS.scan_lookahead``."""
    point_lookup = store._opened().point_lookup
    if all(point_lookup(n3) for n3 in batch_n3):
        return None
    return SETTINGS.scan_lookahead


def _batches(store, patterns) -> bool:
    """Whether :func:`_run_probes` sends a join's probes — of one shape
    (:func:`_probe_shape`), so all or none of them scan — through one batch
    call: probes that scan, when there are enough of them
    (``SETTINGS.scan_batch_min``, ``SETTINGS.memory_scan_batch_min``), and
    point lookups, when enough of them probe a file-backed store
    (``SETTINGS.cheap_batch_min``)."""
    opened = store._opened()
    minimum = SETTINGS.memory_scan_batch_min if opened.in_memory else SETTINGS.scan_batch_min
    if len(patterns) < minimum:
        return False
    if not opened.point_lookup(patterns[0]):
        return True
    return not opened.in_memory and len(patterns) >= SETTINGS.cheap_batch_min


class _Answers:
    """A batch of probes' answers, in order (``source``, an iterator the
    join pulls), and how many of the probes have reached the native layer
    (``sent``): every count, made at once; a batch call's matches, all at
    its first pull; plain matches, one per pull. The answers count
    themselves as they are made, so a pull costs no more than the
    iterator's own."""

    __slots__ = ("source", "sent")

    def __init__(self) -> None:
        self.source: Iterator = iter(())
        self.sent = 0

    def __iter__(self) -> Iterator:
        return self.source

    def close(self) -> None:
        """Release what the native layer still holds: a batch call's probes
        still in flight."""
        close = getattr(self.source, "close", None)
        if close is not None:
            close()


def _run_probes(store, probes, count: bool) -> _Answers:
    """Native answers to ``(n3, narrowing)`` probes (see :func:`_narrowing`),
    in order: counts, all made at once, or code columns, each made as it is
    pulled.

    A join's probes go through one ``count_quads_many`` or
    ``match_codes_many`` call where :func:`_batches` says — every probe
    parsed before any runs, running as far ahead on the native runtime as
    :func:`_probe_lookahead` lets them, a file-backed store overlapping their
    reads, the matches checked as they are pulled (:func:`_batch_answers`)
    — and through plain calls otherwise. The native boundary is taken once
    (:mod:`vortex_rdflib.boundary`), so the query's cancel token is read
    once: a batch call races it, a plain call races or checks it as the
    boundary's policy says.
    """
    answers = _Answers()
    native = boundary.native(store)
    batched = bool(probes) and _batches(store, [n3 for n3, _ in probes])
    if count:
        if batched:
            counts = native.count_quads_many(
                [_batch_probe(*probe) for probe in probes],
                lookahead=_probe_lookahead(store, [n3 for n3, _ in probes]),
            )
        else:
            counts = [native.count_quads(*n3, **narrowing) for n3, narrowing in probes]
        answers.source, answers.sent = iter(counts), len(probes)
    elif batched:
        answers.source = _batch_answers(store, native, probes, answers)
    else:
        answers.source = _plain_matches(native, probes, answers)
    return answers


def _plain_matches(native, probes, answers: _Answers) -> Iterator:
    """The code columns of each probe, one ``match_codes`` call per pull,
    each counted on ``answers`` as it is sent."""
    for n3, narrowing in probes:
        answers.sent += 1
        yield native.match_codes(*n3, **narrowing)


def _batch_probe(n3, narrowing):
    """A probe as the batch calls take it: its quad pattern, or a dict of the
    pattern and its narrowing."""
    if narrowing:
        return {"s": n3[0], "p": n3[1], "o": n3[2], "g": n3[3], **narrowing}
    return tuple(n3)


class RowsOverBudget(Exception):
    """A probe join whose rows passed its ``max_rows`` (:func:`_probe_join`):
    ``calls`` are the probes it sent."""

    def __init__(self, calls: int):
        super().__init__(calls)
        self.calls = calls


#: What :func:`_checked_batch` reads past a batch's last answer when the
#: batch has no more.
_NO_ANSWER = object()


def _checked_batch(answers, total: int) -> Generator:
    """The answers of one ``match_codes_many`` call given ``total`` probes,
    each as it is pulled — all but the last through a C-level ``islice``. A
    batch that answers fewer probes raises ``ValueError`` at the first probe
    left without an answer, and one that answers more, with its last probe.
    The batch's iterator is closed however its answers end, which aborts the
    probes still in flight."""
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


def _batch_answers(store, native, probes, answers: _Answers) -> Iterator:
    """The code columns of every probe, in order, from one
    ``match_codes_many`` call the first pull makes — every probe counted on
    ``answers`` as sent — checked (:func:`_checked_batch`): each waited for
    as it is pulled, while the probes run as far ahead as
    :func:`_probe_lookahead` lets them."""
    batch = native.match_codes_many(
        [_batch_probe(*probe) for probe in probes],
        lookahead=_probe_lookahead(store, [n3 for n3, _ in probes]),
    )
    answers.sent = len(probes)
    yield from _checked_batch(batch, len(probes))


def _probe_shape(pat: Pattern, shared: Sequence) -> list:
    """``pat``'s quad pattern as each probe of a row binds it: a placeholder
    at every position a ``shared`` variable takes, where the row's code goes.
    It tells ``OpenFile.point_lookup`` whether the probes would scan."""
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
    every graph is a set, so its count stops at one row. The answer of a
    probe of a ``distinct`` pattern (the union over the graphs of a store
    where some triple is in several) is made a set of triples
    (:func:`union.distinct_rows`). The pattern's native keep narrows each probe on the
    positions it leaves free (a code range inside the native match, a code
    column over the rows it returns), as do ``row_preds`` over the extended
    row; with ``keep_unmatched`` a row without a continuation is kept, padded
    with None (a left join). Each batch's answers are pulled as the join
    reaches them and released when the batch ends, however it ends. Once a
    probe's rows take the join past ``max_rows``, it drops the rows it made
    and the batch it is pulling, which aborts the probes still in flight,
    and raises :class:`RowsOverBudget` with the probes it sent
    (:class:`_Answers`).

    A left join takes no deferred restriction: evaluated over the joined
    rows, it would drop the rows the join keeps padded.
    """
    assert not (keep_unmatched and pat.deferred), "a left join's padded rows meet no restriction"
    bound = [(schema.index(v), pos) for v, pos in pat.varpos.items() if v in schema]
    free = {v: pos for v, pos in pat.varpos.items() if v not in schema}
    eq_checks = [(pos[0], later) for pos in free.values() for later in pos[1:]]
    out_positions = [pos[0] for pos in free.values()]
    needed = sorted({idx for pos in free.values() for idx in pos})
    # A code range rides every probe; a code column is tested on the rows a
    # probe returns, since the native layer would re-read it per probe.
    probe_keep, tests = {}, []
    for position, spec in pat.nkeep.items():
        if position in needed:
            if is_range(spec):
                probe_keep[position] = spec
            else:
                tests.append((position, filters.native.keep_member(spec)))
    narrowing = matching._narrowing(probe_keep, limit=None if free else 1)
    patterns = _substituted_patterns(store, rows, pat, bound)

    pad = (None,) * len(free)
    out = []
    sent = 0  # the probes the batches before this one sent
    for start in range(0, len(rows), SETTINGS.probe_batch):
        batch = range(start, min(start + SETTINGS.probe_batch, len(rows)))
        probes = [(patterns[i], narrowing) for i in batch if patterns[i] is not None]
        answers = _run_probes(store, probes, count=not free)
        pull = answers.source.__next__
        try:
            for i in batch:
                row = rows[i]
                matched = False
                if patterns[i] is None:
                    pass
                elif not free:
                    # Existence probe: a count capped at one row, no columns;
                    # the row stands once.
                    if pull() and all(pred(row) for pred in row_preds):
                        out.append(row)
                        matched = True
                else:
                    cols = pull()
                    if cols is None:
                        raise NotImplementedError
                    if pat.distinct:
                        cols = union.distinct_rows(cols)
                    views = {idx: memoryview(cols[idx]).cast("Q").tolist() for idx in needed}
                    for j in range(len(views[needed[0]])):
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
                    raise RowsOverBudget(sent + answers.sent)
        finally:
            answers.close()
        sent += answers.sent
    combined_schema = schema + tuple(free.keys())
    # The deferred per-variable restrictions, over the codes the probes
    # actually reached rather than the pattern's whole column. A variable
    # the relation bound was restricted where it was bound.
    pending, pat.deferred = pat.deferred, {}
    if pending and out:
        restriction_input_rows = len(out)
        for var, conjuncts in pending.items():
            if var not in free:
                continue
            position = combined_schema.index(var)
            values = {row[position] for row in out}
            started = trace.stopwatch()
            allowed = filters.evaluate_column(store, conjuncts, values)
            elapsed_ns = started()
            out = [row for row in out if row[position] in allowed]
            trace.event(
                "bgp_restriction_complete",
                original_pattern_index=pat.index,
                variable=str(var),
                mode="probe",
                input_rows=restriction_input_rows,
                output_rows=len(out),
                **filters.restriction_counts(conjuncts),
                distinct_binding_count=len(values),
                allowed_binding_count=len(allowed),
                elapsed_ns=elapsed_ns,
            )
            restriction_input_rows = len(out)
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
    for code, term in zip(codes, store._opened().codes.spellings(codes), strict=True):
        n3_cache[code] = term
    return n3_cache
