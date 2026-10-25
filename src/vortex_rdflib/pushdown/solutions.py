"""A relation's rows decoded into rdflib solutions, lazily and a chunk at a
time (:func:`_yield_solutions`), and the rows rdflib finishes over the
patterns a join over the pair budget left to it (tier 3).
"""

from collections.abc import Iterator
from itertools import islice

from rdflib.plugins.sparql.evaluate import evalBGP
from rdflib.plugins.sparql.sparql import FrozenBindings

from ..cancel import check_cancelled
from ..settings import SETTINGS
from . import relation, tracing
from .relation import Relation
from .tracing import _TRACE


class _Solution(FrozenBindings):
    """The ``FrozenBindings`` the pushdown yields, with a direct ``get``.

    rdflib's ``ResultRow`` reads every projected variable of every solution
    through ``get``, which ``Mapping`` routes through ``__getitem__`` and its
    type checks. A solution here only ever binds variable-like keys to
    terms, never to ``None``, so a hit in its own bindings is the answer
    ``__getitem__`` would give; anything else takes rdflib's own path.
    """

    def get(self, key, default=None):
        value = self._d.get(key)
        if value is not None:
            return value
        return FrozenBindings.get(self, key, default)


def _solution(ctx, bindings: dict) -> _Solution:
    """A solution over ``bindings``, which it takes as its own — what
    ``FrozenBindings.__init__`` sets up, without copying the dict."""
    solution = object.__new__(_Solution)
    solution._d = bindings
    solution._hash = None
    solution.ctx = ctx
    return solution


def _yield_solutions(ctx, store, rel: Relation, project=None, start=0, stop=None):
    """Decode a relation's rows into rdflib solutions, lazily.

    Rows are decoded in growing chunks, one ``decode_many`` per chunk for the
    codes the query's term cache does not hold yet. Without ``project`` a row
    carries every schema variable plus the context's own bindings (what
    rdflib's BGP rows contain); with it only the projected variables, the
    way ``Project`` would have narrowed them.

    A relation rdflib still finishes (``rest``) is decoded whole, without
    projection or window; rdflib extends each row over the rest
    (:func:`_finish_with_rdflib`), and the solutions it makes are what is
    projected and windowed.
    """
    if not rel.rest:
        return _yield_rows(
            ctx, store, rel.schema, relation._code_rows(rel), rel.foreign, project, start, stop
        )
    rows = _yield_rows(ctx, store, rel.schema, relation._body_rows(rel), rel.foreign, None, 0, None)
    return _project_window(_finish_with_rdflib(ctx, rows, rel.rest), project, start, stop)


def _finish_with_rdflib(ctx, solutions: Iterator, rest: tuple) -> Iterator[FrozenBindings]:
    """The solutions rdflib's ``evalBGP`` makes of each solution over the
    triple patterns ``rest`` (tier 3): each is thawed into a context of its
    own, where the patterns run in the order rdflib's ``evalPart`` gives a
    BGP (:func:`_most_bound_first`). ``evalBGP`` is called directly, so the
    hook is not offered the patterns again; it reads them through the
    store's ``triples()``. Each solution checks the query's cancel token
    before it is extended (:func:`check_cancelled`). ``solutions`` is
    closed when these end, fail or are closed."""
    try:
        for solution in solutions:
            check_cancelled()
            context = ctx.thaw(solution)
            yield from evalBGP(context, _most_bound_first(context, rest))
    finally:
        close = getattr(solutions, "close", None)
        if close is not None:
            close()


def _most_bound_first(ctx, triples) -> list:
    """``triples`` as rdflib's ``evalPart`` orders a BGP in ``ctx``: the
    fewest terms the context leaves unbound first, ties in their order."""
    return sorted(triples, key=lambda triple: sum(ctx[term] is None for term in triple))


def _project_window(solutions: Iterator, project, start, stop) -> Iterator:
    """``solutions`` from ``start`` up to ``stop``, each narrowed to the
    ``project`` variables as rdflib's ``Project`` narrows it (all of it
    without ``project``); ``solutions`` is closed when these end, fail or
    are closed."""
    try:
        for solution in islice(solutions, start, stop):
            yield solution if project is None else solution.project(project)
    finally:
        close = getattr(solutions, "close", None)
        if close is not None:
            close()


def _pushed_graph(ctx) -> bool:
    """Whether rdflib has made a graph other than the dataset itself active.

    That is what a ``Graph`` node rdflib evaluates *itself* does — and when it
    yields, it writes ``solution.ctx.graph`` back to the graph it replaced.
    A solution's context is therefore not read-only to the caller, so the rows
    below cannot all share one: the write would reach the rows still to come,
    and any pattern rdflib re-evaluates from them (an OPTIONAL's right side, an
    EXISTS body) would run against the wrong graph.
    """
    dataset = getattr(ctx, "_dataset", None)
    return dataset is not None and ctx.graph is not dataset


def _yield_rows(ctx, store, schema, rows: Iterator[tuple], foreign, project, start, stop):
    """Decode code rows over ``schema`` into solutions (see
    ``_yield_solutions``); ``start``/``stop`` slice the rows first. Each
    decode chunk checks the query's cancel token before its rows are pulled
    (:func:`check_cancelled`), so a cancel stops the solutions within one
    chunk."""
    outer = dict(ctx.bindings.items())
    # Normally every solution carries the caller's own context — no per-row
    # scope, unlike rdflib's `evalBGP`. Under a graph rdflib pushed, each row
    # needs its own (see `_pushed_graph`), which is what `evalBGP` does too.
    own_context = _pushed_graph(ctx)
    if project is None:
        keys, idx, base = schema, list(range(len(schema))), outer
    else:
        keys = tuple(v for v in project if v in schema)
        idx = [schema.index(v) for v in keys]
        base = {v: outer[v] for v in project if v not in schema and v in outer}
    rows = islice(rows, start, stop)
    size = SETTINGS.chunk_start
    traced = _TRACE.get() is not None
    elapsed = tracing._stopwatch()
    yielded = chunks = submitted = 0
    try:
        while True:
            check_cancelled()
            chunk = list(islice(rows, size))
            if not chunk:
                return
            codes = [c for row in chunk for c in (row[i] for i in idx) if c is not None and c >= 0]
            if traced:
                chunks += 1
                submitted += len(codes)
            cached = store._prime_decode_cache(codes)
            for row in chunk:
                solution = dict(base)
                for v, i in zip(keys, idx, strict=True):
                    c = row[i]
                    if c is not None:
                        solution[v] = cached[c] if c >= 0 else foreign[c]
                yielded += 1
                yield _solution(ctx.push() if own_context else ctx, solution)
            size = min(size * 4, SETTINGS.chunk_max)
    finally:
        if traced:
            tracing._trace_event(
                "yield_rows_complete",
                offset=start,
                stop=stop,
                rows_yielded=yielded,
                chunks=chunks,
                projected_columns=len(idx),
                codes_submitted_for_decode=submitted,
                elapsed_ns=elapsed(),
            )
