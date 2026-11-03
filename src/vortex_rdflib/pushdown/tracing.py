"""What the pushdown's query trace (:mod:`vortex_rdflib.trace`) reports beyond
its events' own fields: a native keep and a batch probe as JSON, how a
variable's conjuncts are decided, and the native handle with every call an
event (:func:`_native`, while the query is traced).
"""

import abc
import time

from .. import trace


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
        trace.event(
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
        trace.event(
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
        trace.event(
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
        trace.event(
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
        trace.event(
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
    return native if trace.active() is None else _TracedNative(native)


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
