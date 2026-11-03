"""The query trace (``VORTEX_RDF_TRACE_QUERY=1``): one JSON line per planning
step, printed to stderr prefixed ``VORTEX_RDF_QUERY_TRACE``, held until the
planning it describes is over.

A query's trace lives on its scope (``QueryScope.trace``,
:mod:`vortex_rdflib.query_scope`): the pushdown hook makes it when the query
starts, if tracing is on then, so it is current wherever the query is
evaluated — its planning, and each step of its result, in whatever thread
reads it — and nowhere else. :func:`event` and :func:`stopwatch` cost next
to nothing outside a traced query.
"""

import json
import sys
import time
from collections.abc import Callable

from . import query_scope
from .terms import is_range

PREFIX = "VORTEX_RDF_QUERY_TRACE "


SCHEMA = "vortex-rdf-query-trace-v1"


class QueryTrace:
    """One query's events, numbered from 1 and labelled with its id
    (``VORTEX_RDF_TRACE_QUERY_ID``)."""

    __slots__ = ("query_id", "sequence", "depth", "pending")

    def __init__(self, query_id: str | None) -> None:
        self.query_id = query_id
        self.sequence = 0
        #: How deep the block being solved is nested (``blocks._solve_block``).
        self.depth = 0
        # Lines wait here until flush(), which runs once the planning (or the
        # iteration) they describe is over, so a slow stderr never lands in
        # the timed windows the events report.
        self.pending: list[str] = []

    def emit(self, event: str, **fields) -> None:
        """Queue one event. A field JSON cannot hold raises ``TypeError`` or
        ``ValueError``."""
        self.sequence += 1
        payload = {
            "schema": SCHEMA,
            "event": event,
            "sequence": self.sequence,
            "query_id": self.query_id,
            **fields,
        }
        self.pending.append(PREFIX + json.dumps(payload, separators=(",", ":"), allow_nan=False))

    def flush(self) -> None:
        """Print the queued lines; a stderr that refuses them raises
        ``OSError``."""
        if not self.pending:
            return
        lines, self.pending = self.pending, []
        print("\n".join(lines), file=sys.stderr, flush=True)


def active() -> QueryTrace | None:
    """The trace of the query being evaluated, or ``None``."""
    scope = query_scope.current()
    return None if scope is None else scope.trace


def event(name: str, **fields) -> None:
    """An event of the query being evaluated, when it is traced. A trace
    that cannot write an event is dropped: the query is traced no further."""
    scope = query_scope.current()
    if scope is None or scope.trace is None:
        return
    try:
        scope.trace.emit(name, **fields)
    except (TypeError, ValueError):
        scope.trace = None


def flush() -> None:
    """Print the lines the query's trace holds; a trace stderr refuses is
    dropped."""
    scope = query_scope.current()
    if scope is None or scope.trace is None:
        return
    try:
        scope.trace.flush()
    except OSError:
        scope.trace = None


def _no_time() -> int:
    return 0


def stopwatch() -> Callable[[], int]:
    """A clock for a trace field: called, the nanoseconds since this call
    while the query is traced; ``0``, and no clock read, otherwise."""
    if active() is None:
        return _no_time
    started = time.perf_counter_ns()
    return lambda: time.perf_counter_ns() - started


def keep_summary(keep) -> dict | None:
    """A native keep as an event's JSON: per position, a code range or a code
    set's size."""
    if not keep:
        return None
    return {
        str(position): ({"range": list(spec)} if is_range(spec) else {"codes": len(spec)})
        for position, spec in keep.items()
    }


def probe_pattern(probe) -> list:
    """The ``(s, p, o, g)`` of a batch probe, tuple or dict, as an event's
    JSON."""
    if isinstance(probe, dict):
        return [probe.get(key) for key in ("s", "p", "o", "g")]
    return list(probe)
