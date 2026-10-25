"""The query trace (``VORTEX_RDF_TRACE_QUERY=1``): one JSON line per planning
step, held until the planning it describes is over and printed to stderr
prefixed ``VORTEX_RDF_QUERY_TRACE``.

The trace is current through a context variable; :func:`_trace_event` and
:func:`_stopwatch` cost nothing without one. While a query is traced, its
native calls go through :class:`_TracedNative` (:func:`_native`), so each is
an event too.
"""

import abc
import json
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from .. import candidates

_TRACE_PREFIX = "VORTEX_RDF_QUERY_TRACE "


_TRACE_SCHEMA = "vortex-rdf-query-trace-v1"


@dataclass(slots=True)
class _QueryTrace:
    query_id: str | None
    sequence: int = 0
    depth: int = 0
    # Lines wait here until flush(), which runs once the planning (or the
    # iteration) they describe is over, so a slow stderr never lands in the
    # timed windows the events report.
    pending: list = field(default_factory=list)

    def emit(self, event: str, **fields) -> None:
        self.sequence += 1
        payload = {
            "schema": _TRACE_SCHEMA,
            "event": event,
            "sequence": self.sequence,
            "query_id": self.query_id,
            **fields,
        }
        try:
            line = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            _TRACE.set(None)
            return
        self.pending.append(_TRACE_PREFIX + line)

    def flush(self) -> None:
        if not self.pending:
            return
        lines, self.pending = self.pending, []
        try:
            print("\n".join(lines), file=sys.stderr, flush=True)
        except OSError:
            _TRACE.set(None)


_TRACE: ContextVar[_QueryTrace | None] = ContextVar("vortex_rdf_query_trace", default=None)


def _trace_event(event: str, **fields) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.emit(event, **fields)


def _no_time() -> int:
    return 0


def _stopwatch() -> Callable[[], int]:
    """A clock for a trace field: called, the nanoseconds since this call
    while the query is traced; ``0``, and no clock read, otherwise."""
    if _TRACE.get() is None:
        return _no_time
    started = time.perf_counter_ns()
    return lambda: time.perf_counter_ns() - started


def _trace_native_filter(
    kind: str, sent: int, passed: int, undecided: int, elapsed_ns: int
) -> None:
    """One ``TermDict.filter_codes`` call (:func:`candidates.evaluate`) as a
    trace event."""
    _trace_event(
        "native_filter_complete",
        kind=kind,
        candidate_codes=sent,
        passed_codes=passed,
        undecided_codes=undecided,
        elapsed_ns=elapsed_ns,
    )


candidates.TRACE = _trace_native_filter


def _keep_summary(keep) -> dict | None:
    """A native keep as JSON: per position, a code range or a set's size."""
    if not keep:
        return None
    return {
        str(position): ({"range": list(spec)} if isinstance(spec, tuple) else {"codes": len(spec)})
        for position, spec in keep.items()
    }


def _probe_pattern(probe) -> list:
    """The ``(s, p, o, g)`` of a batch probe, tuple or dict."""
    if isinstance(probe, dict):
        return [probe.get(key) for key in ("s", "p", "o", "g")]
    return list(probe)


class _TracedIterator(abc.ABC):
    """A native iterator with its pulls traced. Each ``__next__`` is exactly
    one timed pull of the native iterator, so nothing is pulled ahead of the
    caller; the rows of what was pulled are summed, and the iterator's event
    is emitted once, when it is exhausted or closed. Closing drops the native
    iterator, which aborts the work it still has in flight. The time reported
    is the native layer's: the call that made the iterator and every pull."""

    __slots__ = ("_inner", "_pulls", "_rows", "_elapsed_ns", "_emitted")

    def __init__(self, inner, elapsed_ns: int):
        self._inner = inner
        self._pulls = 0
        self._rows = 0
        self._elapsed_ns = elapsed_ns
        self._emitted = False

    def __iter__(self):
        return self

    def __next__(self):
        inner = self._inner
        if inner is None:
            raise StopIteration
        started = time.perf_counter_ns()
        try:
            item = next(inner)
        except StopIteration:
            self._elapsed_ns += time.perf_counter_ns() - started
            self.close()
            raise
        self._elapsed_ns += time.perf_counter_ns() - started
        self._pulls += 1
        self._rows += 0 if item is None else len(item[0])
        return item

    def close(self) -> None:
        """Drop the native iterator and emit the event, if it was not yet."""
        self._inner = None
        if not self._emitted:
            self._emitted = True
            self._emit()

    @abc.abstractmethod
    def _emit(self) -> None:
        """Emit the iterator's trace event."""


class _TracedAnswers(_TracedIterator):
    """The answers of a traced ``match_codes_many``; its length is the number
    of probes. Its event is ``native_batch_complete``."""

    __slots__ = ("_probe_count", "_first_pattern")

    def __init__(self, inner, probes, elapsed_ns: int):
        super().__init__(inner, elapsed_ns)
        self._probe_count = len(probes)
        self._first_pattern = _probe_pattern(probes[0]) if probes else None

    def __len__(self) -> int:
        return self._probe_count

    def _emit(self) -> None:
        _trace_event(
            "native_batch_complete",
            operation="match_codes_many",
            probe_count=self._probe_count,
            first_pattern=self._first_pattern,
            returned_rows=self._rows,
            elapsed_ns=self._elapsed_ns,
        )


class _TracedChunks(_TracedIterator):
    """The chunks of a traced ``match_code_chunks``. Its event is
    ``native_chunks_complete``."""

    __slots__ = ("_pattern", "_keep", "_columns")

    def __init__(self, inner, pattern, options: dict, elapsed_ns: int):
        super().__init__(inner, elapsed_ns)
        self._pattern = list(pattern)
        self._keep = _keep_summary(options.get("keep"))
        self._columns = list(options.get("columns", ("s", "p", "o", "g")))

    def _emit(self) -> None:
        _trace_event(
            "native_chunks_complete",
            operation="match_code_chunks",
            pattern=self._pattern,
            keep=self._keep,
            columns=self._columns,
            chunks=self._pulls,
            returned_rows=self._rows,
            elapsed_ns=self._elapsed_ns,
        )


class _TracedNative:
    """The native store with every call recorded as an event: a single call
    as ``native_call_complete`` (its operation, pattern, narrowing, rows
    returned and elapsed time), a batch of counts as ``native_batch_complete``
    (its operation, probe count, total rows and elapsed time). A batch of
    matches and a chunked match return the traced iterators
    :class:`_TracedAnswers` and :class:`_TracedChunks`, which emit their
    events as they end. The keywords of a call (``keep``, ``limit``,
    ``offset``, ``cancel``, ``lookahead``, ...) pass to the native call.
    Handed out by :func:`_native` while a query is traced, so each native
    call the pushdown makes is an event, the per-row probes included."""

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    def count_quads(self, *pattern, **options):
        started = time.perf_counter_ns()
        result = self._inner.count_quads(*pattern, **options)
        elapsed = time.perf_counter_ns() - started
        _trace_event(
            "native_call_complete",
            operation="count_quads",
            pattern=list(pattern),
            keep=_keep_summary(options.get("keep")),
            limit=options.get("limit"),
            returned_rows=result,
            elapsed_ns=elapsed,
        )
        return result

    def match_codes(self, *pattern, **options):
        started = time.perf_counter_ns()
        result = self._inner.match_codes(*pattern, **options)
        elapsed = time.perf_counter_ns() - started
        _trace_event(
            "native_call_complete",
            operation="match_codes",
            pattern=list(pattern),
            keep=_keep_summary(options.get("keep")),
            limit=options.get("limit"),
            returned_rows=None if result is None else len(result[0]),
            elapsed_ns=elapsed,
        )
        return result

    def count_quads_many(self, probes, **options):
        started = time.perf_counter_ns()
        result = self._inner.count_quads_many(probes, **options)
        elapsed = time.perf_counter_ns() - started
        _trace_event(
            "native_batch_complete",
            operation="count_quads_many",
            probe_count=len(probes),
            first_pattern=_probe_pattern(probes[0]) if probes else None,
            returned_rows=sum(result),
            elapsed_ns=elapsed,
        )
        return result

    def match_codes_many(self, probes, **options):
        started = time.perf_counter_ns()
        answers = self._inner.match_codes_many(probes, **options)
        return _TracedAnswers(answers, probes, time.perf_counter_ns() - started)

    def match_code_chunks(self, *pattern, **options):
        started = time.perf_counter_ns()
        chunks = self._inner.match_code_chunks(*pattern, **options)
        if chunks is None:
            return None
        return _TracedChunks(chunks, pattern, options, time.perf_counter_ns() - started)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _native(store):
    """The store's native handle, traced while the query is."""
    native = store._store()
    return native if _TRACE.get() is None else _TracedNative(native)


def _trace_generator(iterator, trace: _QueryTrace, token):
    _TRACE.reset(token)
    started = time.perf_counter_ns()
    rows = 0
    try:
        while True:
            inner_token = _TRACE.set(trace)
            try:
                item = next(iterator)
            except StopIteration:
                return
            finally:
                _TRACE.reset(inner_token)
            rows += 1
            yield item
    finally:
        inner_token = _TRACE.set(trace)
        try:
            _trace_event(
                "query_iterator_complete",
                rows_yielded=rows,
                elapsed_ns=time.perf_counter_ns() - started,
            )
            trace.flush()
        finally:
            _TRACE.reset(inner_token)


def _restriction_counts(conjuncts) -> dict:
    """The trace fields that say how a variable's conjuncts are decided: how
    many there are, and how many have a native predicate (sent to vortex-rdf),
    a fast route, a fast route over term kinds only, or rdflib's evaluator
    alone."""
    return {
        "predicate_count": len(conjuncts),
        "native_predicate_count": sum(c.native is not None and not c.kind_only for c in conjuncts),
        "fast_predicate_count": sum(c.fast is not None for c in conjuncts),
        "kind_only_predicate_count": sum(c.kind_only for c in conjuncts),
        "generic_only_predicate_count": sum(c.fast is None for c in conjuncts),
    }
