"""Tier 1 of the pair budget: the last join step of a BGP, when it shares one
variable, streamed instead of built whole (:func:`_streamed_step`) — the
smaller side grouped, the other fed through the groups a slice or chunk at
a time, the pairs gathered a piece at a time as the rows are read.
"""

import time
from collections.abc import Iterator, Sequence

from vortex_rdf import CodeGroups, U64Column

from ..cancel import cancel_kw, check_cancelled
from ..settings import SETTINGS
from . import matching, relation, tracing
from .matching import Pattern
from .relation import Relation
from .tracing import _TRACE


def _gather_pieces(
    groups: CodeGroups,
    fed: Iterator[tuple[U64Column, ...]],
    key: int,
    grouped: Sequence[U64Column],
    order: Sequence[int],
) -> Iterator[tuple[memoryview, ...]]:
    """The pieces of a streamed join step, each gathered when it is pulled.

    Each column tuple ``fed`` yields is joined on its column ``key`` with the
    grouped rows (``groups``, over the key column of ``grouped``). While a
    part counts more than ``SETTINGS.pair_budget`` pairs and holds more than one
    row it is split in halves; a single row is gathered whatever its count,
    its pairs at most the grouped rows, which are held already. A part that
    counts no pair is skipped. A part is gathered by ``CodeGroups.gather``:
    per pair, in nested-loop order, its other columns at the fed row, then
    ``grouped`` at the grouped row — permuted by ``order`` into the step's
    schema. Each part checks the query's cancel token before it is counted
    (:func:`check_cancelled`), so a cancel stops the pieces at the next
    one. The feed is closed when the pieces end, fail or are closed, and
    ``bgp_stream_complete`` is traced then: the pieces gathered, their rows,
    and the time spent here (pulling the feed, counting, gathering), not in
    the consumer."""
    traced = _TRACE.get() is not None
    pieces = output_rows = elapsed_ns = 0
    resumed = time.perf_counter_ns() if traced else 0
    try:
        for part in fed:
            parts = [part]
            while parts:
                check_cancelled()
                part = parts.pop()
                column = part[key]
                length = len(column)
                pairs = groups.count(column)
                if pairs > SETTINGS.pair_budget and length > 1:
                    half = length // 2
                    parts.append(tuple(col.slice(half, length) for col in part))
                    parts.append(tuple(col.slice(0, half) for col in part))
                    continue
                if not pairs:
                    continue
                gathered = groups.gather(
                    column, [col for i, col in enumerate(part) if i != key], grouped
                )
                piece = tuple(memoryview(gathered[i]).cast("Q") for i in order)
                pieces += 1
                output_rows += pairs
                if traced:
                    elapsed_ns += time.perf_counter_ns() - resumed
                    resumed = 0
                yield piece
                # Dropped before the next gather: one piece is held at a time.
                del gathered, piece
                if traced:
                    resumed = time.perf_counter_ns()
            # Dropped before the next pull, so the feed's previous chunk is
            # not held while the next one is read.
            del part, column
    finally:
        if resumed:
            elapsed_ns += time.perf_counter_ns() - resumed
        close = getattr(fed, "close", None)
        if close is not None:
            close()
        tracing._trace_event(
            "bgp_stream_complete", pieces=pieces, output_rows=output_rows, elapsed_ns=elapsed_ns
        )


def _streamed_step(
    store, rel: Relation, pat: Pattern, memo: dict
) -> tuple[tuple, Iterator[tuple[memoryview, ...]]]:
    """The join of a relation (a columnar or row body) with ``pat``, sharing
    one variable, as pieces gathered while they are pulled: tier 1,
    the last step of a BGP over ``SETTINGS.pair_budget`` or with a pattern too
    large to read whole (:func:`_read_in_chunks`).

    A pattern whose match is read — by this step, or into ``memo`` by
    another — and the relation are two held sides: the one with fewer rows
    is grouped on its key column (``U64Column.groups``), and the other is fed
    through the groups in slices (:func:`_slices`). A pattern left unread is
    fed through the relation's groups in chunks of at most ``SETTINGS.piece_rows``
    rows of ``match_code_chunks`` narrowed by its native keep, opened here,
    so a store that cannot serve them raises ``NotImplementedError`` while
    the query is planned. Returns the step's schema — the relation's, then
    the pattern's new variables, as :func:`_join_columns` has it — and the
    pieces (:func:`_gather_pieces`)."""
    varpos = pat.varpos
    schema = rel.schema
    key_var = next(v for v in varpos if v in schema)
    step_schema = schema + tuple(v for v in varpos if v not in schema)
    held = [rel.native_column(i) for i in range(len(schema))]
    if not pat.matched and matching._pattern_key(pat) in memo:
        matching._match_resolved_pattern(store, pat, memo)
    pattern_schema = tuple(varpos)
    pattern = None
    if pat.matched:
        body = matching._rel_from_pattern(pat)
        pattern = [body.native_column(i) for i in range(len(pattern_schema))]
    if pattern is not None and len(pattern[0]) < len(held[0]):
        grouped, grouped_schema = pattern, pattern_schema
        fed, fed_schema = relation._slices(held, len(held[0])), schema
    else:
        grouped, grouped_schema, fed_schema = held, schema, pattern_schema
        if pattern is not None:
            fed = relation._slices(pattern, len(pattern[0]))
        else:
            fed = tracing._native(store).match_code_chunks(
                *pat.n3,
                columns=[("s", "p", "o", "g")[varpos[v][0]] for v in pattern_schema],
                batch_rows=SETTINGS.piece_rows,
                **matching._narrowing(pat.nkeep),
                **cancel_kw(),
            )
            if fed is None:
                raise NotImplementedError
    groups = grouped[grouped_schema.index(key_var)].groups()
    gathered_schema = [v for v in fed_schema if v != key_var] + list(grouped_schema)
    order = [gathered_schema.index(v) for v in step_schema]
    pieces = _gather_pieces(groups, fed, fed_schema.index(key_var), grouped, order)
    return step_schema, pieces


def _read_in_chunks(pat: Pattern, running: int) -> bool:
    """Whether a last hash step streams ``pat`` from chunks instead of
    matching it whole: its count is more than ``SETTINGS.pair_budget`` rows and
    more than the relation's ``running`` rows, and its chunks are enough to
    join it (:func:`_chunkable`)."""
    estimate = pat.estimate
    assert estimate is not None, "a step counts its pattern before it decides to stream it"
    return estimate > max(SETTINGS.pair_budget, running) and _chunkable(pat)


def _chunkable(pat: Pattern) -> bool:
    """Whether a step can join ``pat`` from chunks of its match: nothing needs
    its whole columns — no deferred restriction (:func:`_restrict_pattern`) —
    and no repeated variable asks for a test across a row's columns, which a
    chunk does not make (:func:`_materialize`)."""
    return not pat.deferred and all(len(positions) == 1 for positions in pat.varpos.values())
