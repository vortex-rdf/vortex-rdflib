"""SPARQL pushdown for :class:`~vortex_rdflib.store.VortexRdflibStore`.

rdflib evaluates a query as a tree of algebra operators and offers every node
to the ``CUSTOM_EVALS`` hooks before running its own evaluator. This module's
hook answers the nodes it understands in **code space** — over the ``u64``
term codes of a Dictionary-layout store — and hands everything else back to
rdflib by raising ``NotImplementedError``; rdflib then evaluates that node
itself and re-enters the hook for the nodes below it, so every supported
subtree of a query still runs here.

What runs in code space:

- a basic graph pattern is solved in one pass: its point patterns are
  *counted* natively first, one at a time, the scans after them in one
  concurrent batch (a count is a fraction of a match's cost and moves no
  columns); a pattern sharing a variable with one that counts at most
  ``_PROBE_FANOUT`` rows is not counted until its join step, and then only as
  far as the probe-or-match decision needs; the smallest counted pattern is
  matched and seeds the relation, and each further pattern — smallest first
  among those sharing a variable with the relation — is joined per row of
  the relation when the relation is far smaller than the pattern's count
  (re-probed natively where each probe is a point lookup, ``_probe_join``;
  matched once, narrowed to the codes the relation binds, where each would
  scan, ``_keep_scan``) or matched once and hash-joined by the native
  ``join_indices``/``take`` kernels; a pattern that counts to nothing empties
  the block before anything is matched, and the join itself never decodes a
  term. A join step whose pairs would pass ``_PAIR_BUDGET``
  (``VORTEX_RDF_PAIR_BUDGET``) is not built whole: a hash step has its pairs
  counted natively once its sides' product passes the budget, a probe step
  stops once its rows pass a quarter of it, and a keep-scan once its match
  passes it. When it is the last step and shares one variable it streams
  instead (tier 1): the smaller side is grouped, the other is fed through the
  groups, and the pairs are gathered a piece at a time as the rows are read,
  so the join holds one piece. Any other step is put back and a pattern that
  provably fits joins first (tier 2); when none does, rdflib finishes the
  patterns left per row of the relation so far (tier 3) where the relation
  goes straight to the emitter, and elsewhere the node goes back to rdflib,
  which offers the BGP below it alone, where tier 3 applies;
- a ``Filter`` over a block is split into conjuncts (:mod:`.filters`):
  those over one variable that are a kind test or an IRI equality (``=``,
  ``sameTerm``, ``IN``) become a ``keep`` on the match of every pattern
  binding the variable, so the rows that fail never leave it (a probed
  pattern tests a code set on the rows each probe returns); the others over
  one variable are evaluated once per distinct code of the variable, on the
  pattern that binds it — over the pattern's whole column when it is
  matched, over the codes the probes reach when it is probed — by
  vortex-rdf when they have a native shape
  (``TermDict.filter_codes`` over exactly those codes, :mod:`.candidates`),
  and the match is compacted natively to the rows that pass; the rest per
  distinct code tuple, after the join (mid-join when that lets a later
  pattern be probed, and as a test of one variable when the relation binds
  all but one of its variables to single values); a whitelist of expression
  shapes runs over the stored spellings, anything else (and any value
  vortex-rdf or the whitelist leaves undecided) goes to rdflib's own
  evaluator, per distinct value, not per row;
- group joins (``Join``), ``OPTIONAL`` (``LeftJoin``), ``MINUS`` and
  ``FILTER (NOT) EXISTS`` over blocks run as hash joins, left joins, anti-
  and semi-joins over code tuples, with a one-pattern inner side counted
  first and re-probed per outer row when the outer relation is small —
  instead of rdflib re-entering the store once per outer solution, and
  without the inner side's complete match; an inline ``VALUES`` table
  is a code-space relation too, its constants encoded in one batch (a
  constant the dictionary does not hold gets a private negative code and
  joins nothing);
- ``Project``, ``Distinct``, ``OrderBy`` (on variables) and ``Slice``
  (LIMIT/OFFSET) heads are applied to the code-space relation — a LIMIT over
  one pattern stops the native match itself — and an ``AskQuery`` over one
  pattern is a count capped at one row, so only the projected variables of
  the rows that are actually consumed are ever decoded — a ``LIMIT 10``
  decodes a few dozen codes, an ASK none, a ``DISTINCT ?p`` over the whole
  store only its distinct predicates (found by the native ``distinct``); an
  ORDER BY ranks each distinct code once (blank nodes by code, as the
  spelling ``_:label`` sorts as the label; IRIs by their spelling without
  the brackets, the string rdflib compares, with no term built; literals
  decoded once per query and ranked by rdflib's comparison) and sorts the
  code rows, a top-k when a LIMIT follows;
- ``COUNT`` aggregates (``COUNT(*)``, ``COUNT(?v)``, ``COUNT(DISTINCT ?v)``,
  with or without ``GROUP BY`` variables) are computed over the code
  columns — a column at a time by the native ``value_counts`` and
  ``distinct`` where the body is columnar — decoding only the group keys,
  and a ``COUNT(*)`` over one pattern (under a FILTER the native match
  decides, too) is answered by ``count_quads`` without matching a row;
- solutions are decoded lazily in growing chunks, each distinct code once
  per query through the query's term cache (:mod:`.query_scope`), and built
  directly as ``FrozenBindings``, the row shape every rdflib operator above
  expects.

Named graphs: a ``Graph`` node sets the scope of the block below it rather
than matching anything itself. A bound name restricts every pattern to that
graph (as does the graph the context is active in, for a block under no
``GRAPH`` at all); an unbound ``GRAPH ?g`` binds the graph from the match's
fourth column instead, so ``?g`` is an ordinary variable of the relation and
one match answers what rdflib would walk the store's graphs for. A FILTER in
the block still reads ``?g`` unbound unless a pattern of its group binds it:
rdflib joins the graph in after the group is solved. Where no pattern of the
group is matched in that scope, or an OPTIONAL or MINUS has one on its right
side only, or an OPTIONAL binds ``?g`` on its right side only, or a MINUS does
and its two sides share a binding, the ``Graph`` node is left to rdflib, which
solves the group once per named graph. The rows of the default graph are
dropped from a variable's match, which is what makes ``GRAPH`` range over the
named graphs only.

Cancellation: ``cancel_scope(token)`` gives the queries started inside it a
``vortex_rdf.CancelToken``. A query keeps it in its scope
(:mod:`.query_scope`), so every step of its lazy result runs under it, the
steps after the block included. A native call the token can stop — a batch,
a chunked match, a one-shot call that scans a file-backed store — is given
it (``cancel=``) and races it; the other one-shot calls — a point lookup, a
call capped at one row, any call on an in-memory store — check it as they
are made, as do the loops between calls: the pieces of a streamed join, the
rows rdflib finishes, the probes of a join. ``token.cancel()``, from any
thread, stops the query at the next of these with ``vortex_rdf.Cancelled``,
which reaches the caller and is never a hand-back. A query started outside a
block has no token, and without one no native call gains a keyword.

rdflib only catches ``NotImplementedError`` at hook-call time, so every
capability check and every native call that can decline happens before a
generator is handed back; the generators decode, pull iterators created
while planning, run kernels and, where a plan leaves patterns to rdflib
(tier 3), run rdflib's own evaluation of them, which makes new
``triples()`` calls. The hook applies when the active graph's
store is a VortexRdflibStore with the code path available (Dictionary layout,
its dictionary memory-mapped from the file or loaded with the store);
behaviour is identical to rdflib's default evaluation, only faster, and the
equivalence tests compare both paths on every query shape.

Registration happens automatically when the first ``VortexRdflibStore`` is
constructed. ``VORTEX_RDF_DISABLE_PUSHDOWN=1`` keeps rdflib's evaluator
entirely (the equivalence tests' oracle); ``VORTEX_RDF_PUSHDOWN_OPS`` narrows
the intercepted algebra nodes to a comma-separated list (``bgp`` = basic
graph patterns only), ``VORTEX_RDF_NATIVE_FILTERS=0`` keeps the conjuncts
the native layer decides on the Python fast route and
``VORTEX_RDF_FILTER_FAST=0`` routes every FILTER value through rdflib's
evaluator, for bisecting and A/B measurements.
``VORTEX_RDF_TRACE_QUERY=1`` prints one JSON line per planning step to
stderr (prefixed ``VORTEX_RDF_QUERY_TRACE``): what was counted, matched,
probed, restricted and joined, with row counts and timings.
"""

import abc
import contextlib
import heapq
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import islice
from operator import itemgetter
from typing import Any

import rdflib.term
from rdflib.plugins.sparql import CUSTOM_EVALS
from rdflib.plugins.sparql.evaluate import evalBGP, evalPart
from rdflib.plugins.sparql.evalutils import _val
from rdflib.plugins.sparql.sparql import FrozenBindings
from rdflib.term import BNode, Literal, URIRef, Variable
from vortex_rdf import Cancelled, CancelToken, CodeGroups, U64Column

from . import candidates, filters, query_scope
from .terms import CANONICAL_INTEGER, INTEGER_SUFFIX, canonical_spelling, unparseable_iri

_EVAL_KEY = "vortex_rdflib_bgp"

# Query bnodes act as variables, exactly as rdflib's evalBGP treats them.
_VAR_LIKE = (Variable, BNode)

# Algebra nodes this module answers, by handler name. Handlers are resolved
# from the module namespace at call time so tests can spy on them.
_HANDLER_NAMES = {
    "BGP": "_eval_block_node",
    "Filter": "_eval_block_node",
    "Join": "_eval_block_node",
    "LeftJoin": "_eval_block_node",
    "Minus": "_eval_block_node",
    "ToMultiSet": "_eval_block_node",
    "Graph": "_eval_block_node",
    "Project": "_eval_head",
    "Slice": "_eval_head",
    "Distinct": "_eval_head",
    "OrderBy": "_eval_head",
    "AggregateJoin": "_eval_aggregate",
    "AskQuery": "_eval_ask",
}
_ALL_OPS = frozenset(_HANDLER_NAMES)
_ENABLED_OPS = _ALL_OPS

# Solutions decode in chunks that grow geometrically: the first chunk keeps a
# LIMIT (or any consumer that stops early) from decoding more than a few
# dozen rows, the cap keeps a full scan to a handful of decode_many calls.
_CHUNK_START = 64
_CHUNK_MAX = 4096

_TRACE_PREFIX = "VORTEX_RDF_QUERY_TRACE "
_TRACE_SCHEMA = "vortex-rdf-query-trace-v1"
_TRACE_ENV = "VORTEX_RDF_TRACE_QUERY"
_TRACE_ID_ENV = "VORTEX_RDF_TRACE_QUERY_ID"


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

#: The cancel token of the query being evaluated: set by :func:`cancel_scope`
#: for the queries started inside it, kept in the query's scope
#: (``QueryScope.cancel``) and set again around each step of its lazy result
#: (:func:`_scoped_iterator`).
_CANCEL: ContextVar[CancelToken | None] = ContextVar("vortex_rdflib_cancel", default=None)


@contextlib.contextmanager
def cancel_scope(token: CancelToken) -> Iterator[CancelToken]:
    """Run the queries started inside the block under ``token``, which it
    yields.

    Each native call such a query makes that the token can stop — a batch, a
    chunked match, a one-shot call that scans a file-backed store — is given
    it (``cancel=``) and races it; every other one-shot call — a point
    lookup, a call capped at one row, any call on an in-memory store, which
    finishes without waiting — checks it as it is made, as do the loops
    between calls: a streamed join's pieces, the rows rdflib finishes, the
    per-row probes. So ``token.cancel()``, from any thread, stops the query
    with ``vortex_rdf.Cancelled`` at its next call, piece, row or probe,
    while it is planned or while its rows are read. A
    query keeps the token it started with until its rows end, the rows read
    after the block included; a query started outside a block has none. A
    ``triples()`` iteration started inside the block gives its native calls
    the token too.
    """
    reset = _CANCEL.set(token)
    try:
        yield token
    finally:
        _CANCEL.reset(reset)


def _cancel_kw() -> dict:
    """The keyword that gives a native call the current query's cancel token:
    ``{"cancel": token}``, or none without a token."""
    token = _CANCEL.get()
    return {} if token is None else {"cancel": token}


def _check_cancelled() -> None:
    """Raise ``vortex_rdf.Cancelled`` when the current query's cancel token is
    cancelled: the check made per step of a loop that makes no native call,
    and before a one-shot native call that does not race the token
    (:func:`_cancel_kw_for`)."""
    token = _CANCEL.get()
    if token is not None and token.cancelled:
        raise Cancelled("cancelled through its CancelToken")


def _cancel_kw_for(cancel: dict, store, n3, limit: int | None = None) -> dict:
    """The cancel keyword of one one-shot native call (``match_codes``,
    ``count_quads``) on ``store`` over the quad pattern ``n3``, given the
    query's (``cancel``, :func:`_cancel_kw`): ``cancel`` for a call the token
    can stop — one that can scan a file-backed store — which races it; none
    for any other — any call on an in-memory store, which finishes without
    waiting, a point lookup (:func:`_cheap_probe`) or a call capped at one
    row (``limit``) — which checks the token as it is made
    (:func:`_check_cancelled`)."""
    if cancel and (
        store.in_memory or (limit is not None and limit <= 1) or _cheap_probe(store, n3)
    ):
        _check_cancelled()
        return {}
    return cancel


def _trace_enabled() -> bool:
    value = os.environ.get(_TRACE_ENV)
    if value is None or value == "0":
        return False
    if value != "1":
        raise ValueError(f"{_TRACE_ENV} must be 0 or 1, got {value!r}")
    return True


def _trace_event(event: str, **fields) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.emit(event, **fields)


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


def _relation_rows(rel: "Relation") -> int | None:
    """The relation's row count where it is known without reading the
    rows: ``None`` for a columnar body still to be filtered, for a streamed
    one, and for one rdflib still finishes (``rest``)."""
    if rel.rest:
        return None
    if rel.rows is not None:
        return len(rel.rows)
    if rel.cols is not None and not rel.preds:
        return rel.nrows
    return None


def register_sparql_pushdown():
    """Install the hook into rdflib's CUSTOM_EVALS (idempotent)."""
    global _ENABLED_OPS
    if os.environ.get("VORTEX_RDF_DISABLE_PUSHDOWN") == "1":
        return
    spec = os.environ.get("VORTEX_RDF_PUSHDOWN_OPS")
    if spec is not None:
        _ENABLED_OPS = _parse_ops(spec)
    if os.environ.get("VORTEX_RDF_FILTER_FAST") == "0":
        filters._FAST_ENABLED = False
    if os.environ.get("VORTEX_RDF_NATIVE_FILTERS") == "0":
        filters._NATIVE_ENABLED = False
    CUSTOM_EVALS.setdefault(_EVAL_KEY, _eval_part)


def unregister_sparql_pushdown():
    CUSTOM_EVALS.pop(_EVAL_KEY, None)


def _parse_ops(spec: str) -> frozenset[str]:
    """``VORTEX_RDF_PUSHDOWN_OPS``: ``bgp`` or a comma-separated node list."""
    if spec.strip().lower() == "bgp":
        return frozenset({"BGP"})
    names = frozenset(name.strip() for name in spec.split(",") if name.strip())
    unknown = names - _ALL_OPS
    if unknown:
        raise ValueError(
            f"VORTEX_RDF_PUSHDOWN_OPS names unknown algebra nodes {sorted(unknown)}; "
            f"known: {sorted(_ALL_OPS)}"
        )
    return names


def _eval_part(ctx, part):
    if query_scope.current() is None:
        return _eval_scoped(ctx, part)
    handler = _HANDLER_NAMES.get(part.name)
    if handler is None or part.name not in _ENABLED_OPS:
        raise NotImplementedError

    from .store import VortexRdflibStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexRdflibStore) or store._dict is None:
        raise NotImplementedError
    active = _TRACE.get()
    token = None
    if active is None and _trace_enabled():
        active = _QueryTrace(os.environ.get(_TRACE_ID_ENV))
        token = _TRACE.set(active)
    started = time.perf_counter_ns() if active is not None else 0
    _trace_event("eval_part_start", algebra_node=part.name, enabled_ops=sorted(_ENABLED_OPS))
    try:
        result = globals()[handler](ctx, store, part)
    except NotImplementedError:
        _trace_event("eval_part_fallback", algebra_node=part.name)
        if active is not None:
            active.flush()
        if token is not None:
            _TRACE.reset(token)
        raise
    except Cancelled:
        _trace_event("eval_part_cancelled", algebra_node=part.name)
        if active is not None:
            active.flush()
        if token is not None:
            _TRACE.reset(token)
        raise
    except Exception as error:
        _trace_event("eval_part_failed", algebra_node=part.name, error_type=type(error).__name__)
        if active is not None:
            active.flush()
        if token is not None:
            _TRACE.reset(token)
        raise
    _trace_event(
        "eval_part_complete",
        algebra_node=part.name,
        handled=True,
        elapsed_ns=time.perf_counter_ns() - started if active is not None else 0,
    )
    if active is not None:
        active.flush()  # the planning is over: nothing below is timed
    if token is not None:
        # Generators keep the trace object explicitly through _yield_rows.
        if hasattr(result, "__next__"):
            assert active is not None  # a token only exists for a trace we created
            result = _trace_generator(result, active, token)
        else:
            _TRACE.reset(token)
    return result


def _eval_scoped(ctx, part):
    """Evaluate ``part`` in a query scope of its own (:mod:`.query_scope`).

    That is the top of a query — rdflib offers the query node first — or a
    node reached outside any query (a SPARQL Update's WHERE clause). Inside
    the scope the node goes back to rdflib's ``evalPart``, which offers it to
    every ``CUSTOM_EVALS`` entry again — with a scope current, this hook
    answers it only through a handler for it (``AskQuery`` has one,
    ``SelectQuery`` none) — and evaluates it itself when no entry answers,
    so every node below runs in the scope; a lazy result keeps it current
    around each step and clears it at the end (:func:`_scoped_iterator`). The
    scope holds the cancel token current when the query starts
    (:func:`cancel_scope`), which every step of a lazy result runs under. A
    graph whose store is not a VortexRdflibStore is left alone.
    """
    from .store import VortexRdflibStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexRdflibStore):
        raise NotImplementedError
    opened = query_scope.QueryScope()
    opened.cancel = _CANCEL.get()
    token = query_scope.enter(opened)
    try:
        result = evalPart(ctx, part)
    finally:
        query_scope.leave(token)
    if hasattr(result, "__next__"):
        return _scoped_iterator(result, opened)
    bindings = result.get("bindings") if isinstance(result, dict) else None
    if hasattr(bindings, "__next__"):
        result = {**result, "bindings": _scoped_iterator(bindings, opened)}
    else:
        opened.clear()
    return result


def _scoped_iterator(iterator, opened):
    """``iterator`` with the query's scope current around each step, and the
    query's cancel token (``opened.cancel``) the current one; the scope is
    cleared when the iterator ends, fails or is closed. The token is set
    only when the current one is another, as it is for a step pulled after
    the query's :func:`cancel_scope` block."""
    cancel = opened.cancel
    try:
        while True:
            token = query_scope.enter(opened)
            reset = _CANCEL.set(cancel) if _CANCEL.get() is not cancel else None
            try:
                item = next(iterator)
            except StopIteration:
                return
            finally:
                if reset is not None:
                    _CANCEL.reset(reset)
                query_scope.leave(token)
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            token = query_scope.enter(opened)
            reset = _CANCEL.set(cancel) if _CANCEL.get() is not cancel else None
            try:
                close()
            finally:
                if reset is not None:
                    _CANCEL.reset(reset)
                query_scope.leave(token)
        opened.clear()


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


@dataclass(frozen=True, slots=True)
class _GraphScope:
    """How the block below sees the graph column of a match.

    ``n3`` is the graph the patterns are restricted to — ``None`` is the
    wildcard over every graph, ``""`` the default graph, a name its own graph.
    ``var`` is the variable a ``GRAPH ?g`` binds *from* the column instead, in
    which case ``n3`` is the wildcard and the default graph's rows are dropped
    (a ``GRAPH`` clause ranges over the named graphs only).
    """

    n3: str | None = None
    var: Variable | None = None


def _active_scope(ctx, store) -> _GraphScope:
    """The scope of a block that is not under a ``GRAPH`` node of its own: the
    graph the context has made active (the union default graph, a dataset's
    default graph, or the graph rdflib pushed for a ``GRAPH`` it evaluated
    itself)."""
    return _GraphScope(store._graph_n3(getattr(ctx, "graph", None)))


def _graph_scope(ctx, node) -> _GraphScope:
    """The scope a ``Graph`` node establishes, mirroring rdflib's ``evalGraph``:
    a bound term names one graph, an unbound variable ranges over the named
    graphs. A term that cannot name a graph is left to rdflib rather than
    guessed at: one other than an IRI or a blank node, and an IRI vortex-rdf's
    parser rejects (:func:`~vortex_rdflib.terms.unparseable_iri`), which the
    store answers with nothing."""
    term = node.term
    value = ctx[term]
    if value is None:
        return _GraphScope(None, term)
    if not isinstance(value, (URIRef, BNode)) or unparseable_iri(value):
        raise NotImplementedError
    return _GraphScope(canonical_spelling(value), None)


def _peel_graph(ctx, node, scope) -> tuple:
    """Look through ``GRAPH`` nodes that name one graph, so the single-pattern
    fast paths below still apply to a block inside a named graph."""
    while getattr(node, "name", None) == "Graph":
        inner = _graph_scope(ctx, node)
        if inner.var is not None:
            break
        node, scope = node.p, inner
    return node, scope


@dataclass(slots=True)
class Relation:
    """A code-space relation.

    ``schema`` names the variable-like terms. The body is exactly one of
    ``u64`` columns (``cols``, one per schema entry: zero-copy views over
    native ``U64Column`` buffers — a single matched pattern's, or a hash
    join's gathered columns), materialized ``rows`` of ``int`` codes, or
    ``pieces``: an iterator of column tuples in schema order, a join step
    gathered a piece at a time as it is pulled (:func:`_streamed_step`),
    consumed once. ``None`` in a row is an unbound variable and
    ``nullable`` lists the variables that may hold it; ``foreign`` maps
    negative codes to terms outside the dictionary; ``preds`` are row
    predicates still to be applied while a columnar or streamed body streams
    (``nrows`` counts a columnar body's rows before them).

    ``rest`` holds the triple patterns of a BGP that rdflib's ``evalBGP``
    still has to join into each row (tier 3, :func:`_finish_with_rdflib`):
    the body is then the rows to finish, not the block's solutions, and the
    schema lacks the variables only those patterns bind. Such a relation
    goes to :func:`_yield_solutions` alone.
    """

    schema: tuple
    cols: tuple | None  # a u64 memoryview per entry (slice, tolist, iterate)
    rows: list[tuple] | None
    nrows: int
    nullable: frozenset = frozenset()
    foreign: dict = field(default_factory=dict)
    preds: tuple[Callable[[tuple], bool], ...] = ()
    pieces: Iterator[tuple] | None = None
    rest: tuple = ()

    @classmethod
    def from_rows(cls, schema, rows):
        return cls(schema, None, rows, len(rows))


@dataclass(slots=True)
class _Head:
    block: object
    pv: tuple | None
    start: int
    stop: int | None
    distinct: bool
    order: list  # (variable, descending) per ORDER BY condition


def _plan_head(part) -> _Head:
    """Recognize a ``Slice? -> Distinct? -> Project -> OrderBy? -> block``
    head (or a bare ``OrderBy -> block``), eagerly.

    Anything else is rdflib's: its evaluator runs the node and re-enters the
    hook for the nodes below.
    """
    node, start, stop, distinct, order = part, 0, None, False, []
    if node.name == "Slice":
        start = int(node.start or 0)
        # The key is absent without LIMIT; CompValue then answers None.
        length = node.length
        stop = None if length is None else start + int(length)
        node = node.p
    if node.name == "Distinct":
        distinct = True
        node = node.p
    pv = None
    if node.name == "Project":
        pv = node.PV
        pv = (pv,) if isinstance(pv, Variable) else tuple(pv)
        node = node.p
    elif distinct or start or stop is not None:
        raise NotImplementedError
    if node.name == "OrderBy":
        for condition in node.expr:
            if not isinstance(condition.expr, Variable):
                raise NotImplementedError
            order.append((condition.expr, condition.order == "DESC"))
        node = node.p
    if pv is None and not order:
        raise NotImplementedError
    _check_block(node)
    return _Head(node, pv, start, stop, distinct, order)


def _check_block(node) -> None:
    """Eagerly reject anything the block solver does not handle.

    A *block* is a subtree solved into one code-space relation; this walk and
    `_solve_block`'s dispatch are the two statements of what that subtree may
    contain.
    """
    name = getattr(node, "name", None)
    if name == "BGP":
        return
    if name in ("Filter", "Graph"):
        _check_block(node.p)
        return
    if name in ("Join", "LeftJoin", "Minus"):
        _check_block(node.p1)
        _check_block(node.p2)
        return
    if name == "ToMultiSet" and getattr(node.p, "name", None) == "values":
        return
    raise NotImplementedError


def _node_vars(node, out: list | None = None, *, bound: bool = False) -> list:
    """Every variable-like term a block mentions, in first-seen order
    (static: what rdflib's ``_vars`` is made of). With ``bound``, only those
    its solutions can bind: a MINUS's right side removes rows, it binds
    nothing."""
    if out is None:
        out = []
    name = node.name
    if name == "BGP":
        for triple in node.triples:
            for term in triple:
                if isinstance(term, _VAR_LIKE) and term not in out:
                    out.append(term)
    elif name == "Filter":
        _node_vars(node.p, out, bound=bound)
    elif name == "ToMultiSet":
        for row in node.p.res:
            for term in row:
                if term not in out:
                    out.append(term)
    elif name == "Graph":
        # The graph term binds in every row of the block below it, and comes
        # last, as it does in a matched pattern's schema.
        _node_vars(node.p, out, bound=bound)
        if isinstance(node.term, _VAR_LIKE) and node.term not in out:
            out.append(node.term)
    else:
        _node_vars(node.p1, out, bound=bound)
        if not (bound and name == "Minus"):
            _node_vars(node.p2, out, bound=bound)
    return out


def _scoped_vars(node, scope: _GraphScope) -> list:
    """A block's variable-like terms, plus the graph variable an enclosing
    ``GRAPH ?g`` binds in every row of it."""
    out = _node_vars(node)
    if scope.var is not None and scope.var not in out:
        out.append(scope.var)
    return out


def _block_vars(ctx, node, scope: _GraphScope) -> list:
    """The variables a block binds: its variable-like terms the context has
    not bound, in first-seen order (static, no matching)."""
    return [term for term in _scoped_vars(node, scope) if ctx[term] is None]


def _group_vars(ctx, node) -> list:
    """The variables a block's own solutions bind in rdflib's evaluation: its
    variable-like terms the context has not bound, none from a MINUS's right
    side, and an enclosing ``GRAPH ?g``'s variable only where the block binds
    it itself — rdflib's ``evalGraph`` joins the graph in after the group is
    solved."""
    return [var for var in _node_vars(node, bound=True) if ctx[var] is None]


def _carries_graph(node, var) -> bool:
    """Whether the rows of a block under ``GRAPH var`` carry its graph
    column: a pattern of the block is matched in that scope, so the column
    names the graph each row was solved in.

    A part without the column (an empty group, a VALUES table, a ``GRAPH`` of
    another graph) is the same in every named graph. A join with a part that
    has it repeats it per graph, as rdflib does; an OPTIONAL or a MINUS whose
    right side alone has it cannot, and raises ``NotImplementedError``.
    """
    name = node.name
    if name == "BGP":
        return bool(node.triples)
    if name == "Filter":
        return _carries_graph(node.p, var)
    if name == "Graph":
        return node.term == var and _carries_graph(node.p, var)
    if name in ("Join", "LeftJoin", "Minus"):
        left, right = _carries_graph(node.p1, var), _carries_graph(node.p2, var)
        if name == "Join":
            return left or right
        if right and not left:
            raise NotImplementedError
        return left
    return False


def _block_nullable(node) -> frozenset:
    """The variables a block may leave unbound: those an OPTIONAL side
    introduces, every one its left side does not bind.

    A ``GRAPH ?g``'s variable is among them only where an OPTIONAL's right
    side names it and its left side does not bind it, or a VALUES table
    leaves it UNDEF; the solver leaves both to rdflib (see
    :func:`_solve_left_join`, :func:`_solve_minus` and :func:`_shared_key_ok`).
    Otherwise the graph column is bound in every row below the ``GRAPH``,
    and when both sides of an OPTIONAL are under the same one, the join on it
    keeps them in the same graph.
    """
    name = node.name
    if name == "BGP":
        return frozenset()
    if name == "Graph":
        return _block_nullable(node.p)
    if name == "ToMultiSet":
        rows = node.p.res
        return frozenset(
            v for v in _node_vars(node) if any(row.get(v, "UNDEF") == "UNDEF" for row in rows)
        )
    if name == "Filter":
        return _block_nullable(node.p)
    if name == "Minus":
        return _block_nullable(node.p1)
    nullable = _block_nullable(node.p1) | _block_nullable(node.p2)
    if name == "LeftJoin":
        nullable |= frozenset(_node_vars(node.p2)) - frozenset(_node_vars(node.p1, bound=True))
    return nullable


def _eval_head(ctx, store, part):
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    head = _plan_head(part)
    _trace_event(
        "head_planned",
        entry_node=part.name,
        project_variable_count=0 if head.pv is None else len(head.pv),
        order_key_count=len(head.order),
        descending_key_count=sum(descending for _, descending in head.order),
        distinct=head.distinct,
        offset=head.start,
        limit=None if head.stop is None else head.stop - head.start,
    )
    # Without an ORDER BY or a DISTINCT between them, a LIMIT bounds the
    # block's rows themselves; a block that is one native match takes it.
    # The relation then goes straight to the emitter (``stream``).
    stream = not head.order and not head.distinct
    limit = head.stop if stream else None
    rel = _solve_block(ctx, store, head.block, limit=limit, stream=stream)
    _trace_event(
        "block_solve_complete",
        rows=_relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        foreign_terms=len(rel.foreign),
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
    )
    if head.order:
        # DISTINCT narrows the rows after the sort, so only a plain LIMIT
        # can stop the sort at the top k.
        rows = _order_rows(ctx, store, rel, head.order, None if head.distinct else head.stop)
        rel = Relation(rel.schema, None, rows, len(rows), rel.nullable, rel.foreign)
    if head.distinct:
        return _eval_distinct(ctx, store, rel, head)
    return _yield_solutions(ctx, store, rel, head.pv, head.start, head.stop)


def _order_rows(ctx, store, rel: Relation, order, stop) -> list:
    """The relation's rows in ORDER BY order.

    Each sort variable's distinct codes are ranked once (``_rank_codes``);
    a row's key is the tuple of its ranks, negated for DESC, so one stable
    sort reproduces rdflib's chain of stable sorts, and a following LIMIT
    keeps only the top ``stop`` rows. A variable the block does not bind
    ties every row, as it does for rdflib.
    """
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    rows = _rows_of(rel)
    _trace_event(
        "order_start",
        input_rows=len(rows),
        order_key_count=len(order),
        descending_key_count=sum(descending for _, descending in order),
        limit=stop,
    )
    keys = []
    for key_index, (var, descending) in enumerate(order):
        if var not in rel.schema:
            continue
        index = rel.schema.index(var)
        distinct_codes = {row[index] for row in rows}
        key_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        ranks = _rank_codes(store, rel.foreign, distinct_codes)
        _trace_event(
            "order_key_ranked",
            key_index=key_index,
            descending=descending,
            input_rows=len(rows),
            distinct_codes=len(distinct_codes),
            elapsed_ns=time.perf_counter_ns() - key_started if key_started else 0,
        )
        keys.append((index, ranks, -1 if descending else 1))
    if not keys:
        return rows

    def sort_key(row):
        return tuple(sign * ranks[row[i]] for i, ranks, sign in keys)

    if stop is not None and stop < len(rows):
        output = heapq.nsmallest(stop, rows, key=sort_key)
        algorithm = "bounded_heap_nsmallest"
    else:
        output = sorted(rows, key=sort_key)
        algorithm = "full_stable_sort"
    _trace_event(
        "order_complete",
        algorithm=algorithm,
        input_rows=len(rows),
        output_rows=len(output),
        limit=stop,
        limit_applied_during_order=algorithm == "bounded_heap_nsmallest",
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
    )
    return output


def _rank_codes(store, foreign: dict, codes) -> dict:
    """Ranks reproducing rdflib's ORDER BY comparison (``_val``): unbound
    first, then blank nodes, IRIs and literals.

    Blank nodes rank by code: the spelling ``_:label`` sorts as the label.
    IRIs rank by their spelling without the brackets, the string rdflib
    compares, read in one ``decode_many`` with no term built. Their codes
    are not in that order: the closing ``>`` of ``<iri>`` sorts an IRI after
    the IRIs it is a prefix of whenever the next character is below ``>``
    (``<…/a>`` after ``<…/a/b>``). Literals are decoded once per query, in
    one batch through the query's term cache, and ranked by rdflib's own
    comparator. Adjacent keys that compare equal share a rank, so the stable
    sort keeps their input order. With a query constant outside the
    dictionary among the codes, every code ranks as the literals do."""
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    codes = set(codes)
    cache_before = set(store._terms().decode) if _TRACE.get() is not None else set()
    ranks = {None: -1}
    literal_lo, iri_lo, blank_lo = store._term_kind_bounds()
    blanks, iris, others = [], [], []
    by_kind = all(code is None or code >= 0 for code in codes)
    for code in codes:
        if code is None:
            continue
        if by_kind and code >= blank_lo:
            blanks.append(code)
        elif by_kind and code >= iri_lo:
            iris.append(code)
        else:
            others.append(code)
    rank = 0
    for code in sorted(blanks):
        ranks[code] = rank
        rank += 1
    if iris:
        # ``<iri>`` decodes to ``URIRef(iri)``, so the string rdflib compares
        # is the spelling without its brackets.
        spellings = store._dict.decode_many(iris)
        if None in spellings:
            code = iris[spellings.index(None)]
            raise ValueError(f"term code {code} is not in the store dictionary")
        keyed = [(spelling[1:-1], code) for spelling, code in zip(spellings, iris, strict=True)]
        rank = _rank_keyed(ranks, keyed, rank)
    local = [code for code in others if code >= 0]
    if others:
        cached = store._prime_decode_cache(local)
        keyed = [(_val(cached[code] if code >= 0 else foreign[code]), code) for code in others]
        _rank_keyed(ranks, keyed, rank)
    if _TRACE.get() is not None:
        decoded = set(local)
        _trace_event(
            "rank_codes_complete",
            input_codes=len(codes),
            unique_codes=len(codes),
            null_codes=int(None in codes),
            blank_codes=len(blanks),
            iri_codes=len(iris),
            other_codes=len(others),
            foreign_codes=sum(code < 0 for code in others),
            decoded_codes=len(decoded),
            decode_cache_hits=len(decoded & cache_before),
            decode_cache_misses=len(decoded - cache_before),
            elapsed_ns=time.perf_counter_ns() - started,
        )
    return ranks


def _rank_keyed(ranks: dict, keyed: list, rank: int) -> int:
    """Rank the codes of ``keyed``, ``(key, code)`` pairs, in key order from
    ``rank`` on, adjacent keys that compare equal sharing a rank. Returns one
    more than the last rank given, or ``rank + 1`` for an empty ``keyed``: a
    gap, which leaves the order of the ranks unchanged."""
    keyed.sort(key=itemgetter(0))
    previous = None
    for key, code in keyed:
        if previous is not None and previous < key:
            rank += 1
        ranks[code] = rank
        previous = key
    return rank + 1


def _eval_distinct(ctx, store, rel: Relation, head: _Head):
    """DISTINCT over the projected variables, without decoding the
    duplicates: code-distinct tuples first, then a term-level pass over the
    survivors (rdflib compares decoded terms, and two spellings of a typed
    literal can be one term), then LIMIT/OFFSET."""
    pv = head.pv or ()
    keys = tuple(v for v in pv if v in rel.schema)
    idx = [rel.schema.index(v) for v in keys]
    rows = _term_distinct(store, _code_distinct(rel, idx), rel.foreign)
    return _yield_rows(ctx, store, keys, rows, rel.foreign, pv, head.start, head.stop)


def _code_distinct(rel: Relation, idx: list) -> Iterator[tuple]:
    """The relation's rows projected to ``idx``, one per distinct code
    tuple, in first-seen order."""
    if rel.cols is not None and not rel.preds:
        if len(idx) == 1:
            # The native kernel keeps first-seen order, as dict.fromkeys did.
            return ((code,) for code in _distinct_codes(rel.cols[idx[0]]))
        columns = [rel.cols[i].tolist() for i in idx]
        return iter(dict.fromkeys(zip(*columns, strict=True)))

    def rows():
        seen: set = set()
        for row in _code_rows(rel):
            key = tuple(row[i] for i in idx)
            if key not in seen:
                seen.add(key)
                yield key

    return rows()


def _term_distinct(store, tuples: Iterator[tuple], foreign: dict) -> Iterator[tuple]:
    """Drop the code tuples whose decoded terms equal an earlier tuple's."""
    seen: set = set()
    aliases: dict = {}
    size = _CHUNK_START
    while True:
        chunk = list(islice(tuples, size))
        if not chunk:
            return
        _alias_codes(store, {c for row in chunk for c in row}, foreign, aliases)
        for row in chunk:
            key = tuple(aliases.get(c, c) for c in row) if aliases else row
            if key not in seen:
                seen.add(key)
                yield row
        size = min(size * 4, _CHUNK_MAX)


def _alias_codes(store, codes, foreign: dict, aliases: dict) -> None:
    """Record in ``aliases`` each code of ``codes`` whose decoded term another
    code represents, mapped to that code — rdflib compares decoded terms,
    and two spellings of a typed literal can be one term
    (``"042"^^xsd:integer`` and ``"42"^^xsd:integer`` are both ``42``).

    IRIs, blank nodes and untyped literals are one term per spelling, so
    only typed literals can alias, and only through rdflib's normalization:
    a typed spelling rdflib keeps as written is its term's canonical one
    (``canonical_spelling``), and every other spelling of that term is one
    rdflib rewrites. So only the typed spellings rdflib may rewrite are
    built (`_rewritable`), and each term built is represented by its
    canonical spelling's code when the dictionary holds it
    (`_canonical_twins`), else by the first code that carried it. Whether a
    code aliases is a property of the store, not of the query: each literal
    code is classified once per query, and later calls in the query only
    look their codes up. A query constant outside the dictionary (a negative
    code, stable for the query too) takes part through its own term; its
    canonical spelling is not in the dictionary, which is why its code is
    negative (`_constant_codes`), so no twin is looked up for it. For most
    stores no code aliases another and ``aliases`` stays empty.
    """
    literal_lo, iri_lo, _ = store._term_kind_bounds()
    terms = store._terms()
    classified = terms.alias_classified
    pending = [
        c
        for c in codes
        if c is not None and (c < 0 or literal_lo <= c < iri_lo) and c not in classified
    ]
    if pending:
        typed = []
        local = [c for c in pending if c >= 0]
        if local:
            cache = terms.decode
            for code, spelling in _rewritable(local, store._dict.decode_many(local)):
                term = cache.get(code)
                if term is None:
                    term = cache[code] = store._from_n3_safe(spelling)
                typed.append((code, term, spelling))
        for code in pending:
            if code < 0:
                term = foreign[code]
                if isinstance(term, Literal) and term.datatype is not None:
                    typed.append((code, term, None))
        firsts, reps = terms.alias_firsts, terms.alias_reps
        twins = _canonical_twins(
            store, [(t, s) for _, t, s in typed if s is not None and t not in firsts]
        )
        for code, term, _ in typed:
            representative = firsts.get(term)
            if representative is None:
                representative = firsts[term] = twins.get(term, code)
            if representative != code:
                reps[code] = representative
        classified.update(pending)
    reps = terms.alias_reps
    if reps:
        for code in codes:
            representative = reps.get(code)
            if representative is not None:
                aliases[code] = representative


#: rdflib's datatype converters (``Literal`` normalizes the lexical forms of
#: their datatypes); ``None`` in an rdflib release without them, and then the
#: alias pass builds every typed literal.
_toPythonMapping = getattr(rdflib.term, "_toPythonMapping", None)

#: A canonical xsd:integer's whole spelling, which rdflib keeps as written.
_CANONICAL_INTEGER_SPELLING = re.compile(
    f'"(?:{CANONICAL_INTEGER.pattern})"{re.escape(INTEGER_SUFFIX)}'
)


def _rewritable(codes, spellings) -> Iterator[tuple[int, str]]:
    """The ``(code, spelling)`` pairs whose typed literal rdflib's
    normalization may rewrite: every typed spelling but a canonical
    xsd:integer and one of a datatype rdflib has no converter for, which
    ``Literal`` keeps as written. Without rdflib's converter table every
    typed spelling. Assumes rdflib's own xsd:integer binding, as the native
    filters do."""
    mapping = _toPythonMapping
    if mapping is None:
        for code, spelling in zip(codes, spellings, strict=True):
            if spelling is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            if spelling.endswith(">"):
                yield code, spelling
        return
    canonical = _CANONICAL_INTEGER_SPELLING.fullmatch
    converted = None
    for code, spelling in zip(codes, spellings, strict=True):
        if spelling is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        if not spelling.endswith(">") or canonical(spelling) is not None:
            continue
        if converted is None:
            # Read per call, so a datatype bound at runtime counts.
            converted = {str(datatype) for datatype in mapping if datatype is not None}
        cut = spelling.rfind('"^^<')
        if cut > 0 and spelling[cut + 4 : -1] not in converted:
            continue
        yield code, spelling


def _canonical_twins(store, read: list) -> dict:
    """The code of each term's canonical spelling, for the ``(term,
    spelling)`` pairs of ``read`` whose term was read from another spelling
    and whose canonical spelling the dictionary holds: one ``encode_many``
    call, and the codes it finds decoded to confirm the term."""
    wanted: dict = {}
    for term, spelling in read:
        if term not in wanted:
            canonical = canonical_spelling(term)
            if canonical != spelling:
                wanted[term] = canonical
    if not wanted:
        return {}
    spellings = list(wanted.values())
    try:
        found = store._dict.encode_many(spellings)
    except ValueError:
        # A spelling the native parser rejects fails the whole batch; alone,
        # it names no term of the file.
        found = []
        for spelling in spellings:
            try:
                found.append(store._dict.encode(spelling))
            except ValueError:
                found.append(None)
    hits = [code for code in found if code is not None]
    if not hits:
        return {}
    # The lookup canonicalizes a spelling as the native parser does, so a
    # code is a twin only if it decodes to the same term.
    cache = store._prime_decode_cache(hits)
    return {
        term: code
        for term, code in zip(wanted, found, strict=True)
        if code is not None and cache[code] == term
    }


def _eval_aggregate(ctx, store, part):
    """``AggregateJoin`` over a ``Group`` of a block, for COUNT aggregates.

    Counts are taken over the code columns; only the group keys are decoded.
    Groups whose decoded keys are equal terms are merged, as rdflib groups by
    decoded term. ``Aggregate_Sample`` of a GROUP BY variable is the key
    itself (that is how rdflib carries the group variable into the row);
    every other aggregate is rdflib's.
    """
    group = part.p
    if getattr(group, "name", None) != "Group":
        raise NotImplementedError
    block = group.p
    _check_block(block)
    block, scope = _peel_graph(ctx, block, _active_scope(ctx, store))
    group_vars = None if group.expr is None else list(group.expr)
    if group_vars is not None and not all(isinstance(v, Variable) for v in group_vars):
        raise NotImplementedError
    counts, samples = [], []
    for agg in part.A:
        if agg.name == "Aggregate_Count":
            target = agg.vars
            if target != "*" and not isinstance(target, _VAR_LIKE):
                raise NotImplementedError
            counts.append((agg.res, target, bool(agg.distinct)))
        elif agg.name == "Aggregate_Sample":
            if group_vars is None or agg.vars not in group_vars:
                raise NotImplementedError
            samples.append((agg.res, agg.vars))
        else:
            raise NotImplementedError

    # One pattern — under a FILTER the native match decides entirely, or
    # none — no grouping, plain counts: the row selection's size.
    pat = _single_pattern(ctx, store, block, scope) if group_vars is None else None
    if (
        pat is not None
        and all(len(pos) == 1 for pos in pat["varpos"].values())
        and all(
            not distinct and (target == "*" or target in pat["varpos"] or ctx[target] is not None)
            for _, target, distinct in counts
        )
    ):
        n = _count_pattern(store, pat, {}, exact=True)
        return iter([FrozenBindings(ctx, {res: Literal(n) for res, _, _ in counts})])

    rel = _solve_block(ctx, store, block, scope=scope)
    return _aggregate_rows(ctx, store, rel, group_vars or [], counts, samples, group.expr is None)


@dataclass(slots=True)
class _CountSpec:
    """One COUNT aggregate over the block: ``rows`` (``COUNT(*)``), a
    column (``COUNT(?v)`` for a block variable), ``const`` (a variable the
    context bound, present in every row) or ``none`` (bound nowhere)."""

    res: Any
    kind: str
    col: int = -1
    distinct: bool = False


@dataclass(slots=True)
class _Group:
    rows: int
    counts: list  # per spec: bound values seen (non-distinct column counts)
    sets: list  # per spec: the codes (or rows) seen, for distinct counts


def _aggregate_rows(ctx, store, rel: Relation, group_vars, counts, samples, implicit_group):
    schema = rel.schema
    col = {v: i for i, v in enumerate(schema)}
    # A group key part is a column; a context-bound variable does not split
    # groups and an unbound one contributes a None key part (as rdflib's).
    key_cols = [col[v] for v in group_vars if v in col]
    specs = []
    for res, target, distinct in counts:
        if target == "*":
            specs.append(_CountSpec(res, "rows", distinct=distinct))
        elif target in col:
            specs.append(_CountSpec(res, "col", col[target], distinct))
        elif ctx[target] is not None:
            specs.append(_CountSpec(res, "const", distinct=distinct))
        else:
            specs.append(_CountSpec(res, "none"))
    nspecs = len(specs)

    def new_group(rows=0):
        return _Group(rows, [0] * nspecs, [set() for _ in specs])

    groups: dict[tuple, _Group] = {}
    if rel.cols is not None and not rel.preds and len(key_cols) <= 1:
        groups = _columnar_groups(rel, key_cols, specs, new_group)
    else:
        for row in _code_rows(rel):
            key = tuple(row[i] for i in key_cols)
            group = groups.get(key)
            if group is None:
                group = groups[key] = new_group()
            group.rows += 1
            for j, spec in enumerate(specs):
                if spec.kind == "col":
                    code = row[spec.col]
                    if code is not None:
                        if spec.distinct:
                            group.sets[j].add(code)
                        else:
                            group.counts[j] += 1
                elif spec.distinct:  # rows, const
                    group.sets[j].add(row if spec.kind == "rows" else ())

    if not groups:
        if implicit_group:
            return iter([FrozenBindings(ctx, {res: Literal(0) for res, _, _ in counts})])
        return iter([FrozenBindings(ctx)])

    # Merge groups whose decoded keys are equal terms; distinct counts likewise
    # count equal terms once (see `_alias_codes`).
    codes = {c for key in groups for c in key if c is not None}
    for group in groups.values():
        for j, spec in enumerate(specs):
            if spec.distinct and spec.kind == "col":
                codes.update(group.sets[j])
            elif spec.distinct and spec.kind == "rows":
                codes.update(c for row in group.sets[j] for c in row if c is not None)
    foreign = rel.foreign
    aliases: dict = {}
    _alias_codes(store, codes, foreign, aliases)

    def distinct_count(values) -> int:
        if not aliases:
            return len(values)
        return len({aliases.get(c, c) for c in values})

    merged: dict[tuple, tuple[tuple, _Group]] = {}
    for key, group in groups.items():
        tkey = tuple(aliases.get(c, c) for c in key)
        first = merged.get(tkey)
        if first is None:
            merged[tkey] = (key, group)
            continue
        base = first[1]
        base.rows += group.rows
        for j in range(nspecs):
            base.counts[j] += group.counts[j]
            base.sets[j] |= group.sets[j]

    key_index = {v: i for i, v in enumerate(v for v in group_vars if v in col)}
    cached = store._prime_decode_cache(
        [c for key, _ in merged.values() for c in key if c is not None and c >= 0]
    )

    def solutions():
        for key, group in merged.values():
            row: dict = {}
            for j, spec in enumerate(specs):
                if spec.kind == "rows":
                    if not spec.distinct:
                        n = group.rows
                    elif aliases:
                        n = len({tuple(aliases.get(c, c) for c in r) for r in group.sets[j]})
                    else:
                        n = len(group.sets[j])
                elif spec.kind == "col":
                    n = distinct_count(group.sets[j]) if spec.distinct else group.counts[j]
                elif spec.kind == "const":
                    n = 1 if spec.distinct else group.rows
                else:
                    n = 0
                row[spec.res] = Literal(n)
            for res, var in samples:
                if var in key_index:
                    code = key[key_index[var]]
                    row[res] = (
                        None if code is None else (cached[code] if code >= 0 else foreign[code])
                    )
                else:
                    row[res] = ctx[var]
            yield FrozenBindings(ctx, row)

    return solutions()


#: Groups up to which COUNT(DISTINCT) gathers each group's rows natively
#: (one ``join_indices`` over the key column per group); past it, one pass
#: over the distinct (key, value) pairs.
_NATIVE_GROUPS_MAX = 64


def _columnar_groups(rel: Relation, key_cols, specs, new_group) -> dict:
    """The groups of a columnar body by at most one key column, counted a
    column at a time.

    A columnar body holds no unbound value, so a group's ``COUNT(?v)`` is
    its row count, which the key column's native ``value_counts`` gives. A
    ``COUNT(DISTINCT ?v)`` takes each group's distinct codes of ``?v`` — for
    a handful of groups natively, gathering a group's rows by its key
    (``join_indices``, ``take``, ``distinct``), otherwise from the distinct
    (key, value) pairs.
    """
    cols = rel.cols
    assert cols is not None  # the caller's columnar case
    groups: dict = {}
    key_col = None
    if key_cols:
        key_col = _native_column(cols[key_cols[0]])
        values, tallies = key_col.value_counts()
        for code, n in zip(
            memoryview(values).cast("Q").tolist(),
            memoryview(tallies).cast("Q").tolist(),
            strict=True,
        ):
            groups[(code,)] = new_group(n)
    elif rel.nrows:
        groups[()] = new_group(rel.nrows)
    if not groups:
        return groups
    members = keys = None
    for j, spec in enumerate(specs):
        if spec.kind == "col" and not spec.distinct:
            for group in groups.values():
                group.counts[j] = group.rows
        elif spec.kind == "const" and spec.distinct:
            for group in groups.values():
                group.sets[j].add(())
        elif spec.kind == "col":
            column = _native_column(cols[spec.col])
            if key_col is None:
                groups[()].sets[j] = set(_distinct_codes(column))
            elif len(groups) <= _NATIVE_GROUPS_MAX:
                if members is None:
                    members = {key: key_col.join_indices(U64Column([key[0]]))[0] for key in groups}
                for key, rows in members.items():
                    groups[key].sets[j] = set(_distinct_codes(column.take(rows)))
            else:
                if keys is None:
                    keys = memoryview(key_col).cast("Q").tolist()
                for key, code in set(zip(keys, memoryview(column).cast("Q").tolist(), strict=True)):
                    groups[(key,)].sets[j].add(code)
        elif spec.kind == "rows" and spec.distinct:
            rows = set(zip(*(col.tolist() for col in cols), strict=True))
            for row in rows:
                groups[tuple(row[i] for i in key_cols)].sets[j].add(row)
    return groups


def _eval_block_node(ctx, store, part):
    _check_block(part)
    return _yield_solutions(ctx, store, _solve_block(ctx, store, part, stream=True))


def _eval_ask(ctx, store, part):
    project = part.p
    if getattr(project, "name", None) != "Project":
        raise NotImplementedError
    _check_block(project.p)
    return {"type_": "ASK", "askAnswer": _block_nonempty(ctx, store, project.p)}


def _block_nonempty(ctx, store, block) -> bool:
    block, scope = _peel_graph(ctx, block, _active_scope(ctx, store))
    pat = _single_pattern(ctx, store, block, scope)
    if pat is not None:
        if pat["unsatisfiable"]:
            return False
        if all(len(pos) == 1 for pos in pat["varpos"].values()):
            # Existence needs no rows: a count capped at one, which stops a
            # file-backed scan at its first match.
            narrowing = _narrowing(pat.get("nkeep"), limit=1)
            _check_cancelled()
            return _native(store).count_quads(*pat["n3"], **narrowing) > 0
    rel = _solve_block(ctx, store, block, scope=scope, limit=1)
    # One row is pulled, and the rows are closed after it: a streamed join
    # gathers one piece and releases its feed.
    rows = _code_rows(rel)
    try:
        return next(rows, None) is not None
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


def _single_pattern(ctx, store, block, scope) -> dict | None:
    """``block`` as one triple pattern the native layer answers whole: a
    ``BGP`` of one pattern, or a ``FILTER`` over one whose conjuncts all push
    into the native match as keeps. The resolved pattern, ready to count, or
    ``None``."""
    var_preds = None
    if block.name == "Filter":
        inner = block.p
        if inner.name != "BGP" or len(inner.triples) != 1:
            return None
        plan = filters.analyze_filter(block, _block_vars(ctx, inner, scope), ctx)
        if plan.constant or plan.tuples or plan.exists:
            return None
        var_preds, block = plan.per_var, inner
    elif block.name != "BGP" or len(block.triples) != 1:
        return None
    pat = _pattern_terms(ctx, store, *block.triples[0], scope=scope)
    if var_preds and _push_native_restrictions(store, pat, var_preds):
        return None  # a conjunct the native match cannot decide
    return pat


class _Pushed(dict):
    """The filter conjuncts a FILTER hands down into its block. As a dict,
    each variable's single-variable conjuncts, for the scans and VALUES
    tables that bind it;
    ``tuples``, the fast conjuncts over several variables, which a BGP that
    binds all of theirs may prune with mid-join (a pre-filter: the FILTER
    still applies them); ``views``, the terms parsed for those, shared with
    the FILTER so none is decoded twice; ``applied``, the ids of the tuple
    conjuncts a BGP turned into restrictions of one of its patterns (see
    :func:`_specialize_tuple_conjuncts`), which every row the block yields
    therefore passes. They travel together wherever the block solvers pass
    them — a group join's sides, an OPTIONAL's or a MINUS's left side, a
    GRAPH block — and never an OPTIONAL's or a MINUS's right side, where
    dropping a row would change the answer. Empty, it is falsy like
    ``None``."""

    __slots__ = ("tuples", "views", "applied")

    def __init__(self, per_var=(), tuples=(), views=None, applied=None):
        super().__init__(per_var)
        self.tuples = tuple(tuples)
        self.views = {} if views is None else views
        self.applied = set() if applied is None else applied


def _solve_block(
    ctx, store, node, env=frozenset(), var_preds=None, scope=None, limit=None, stream=False
) -> Relation:
    """Solve a block into a relation.

    ``env`` is the set of variables an enclosing lazy join or OPTIONAL binds
    row by row in rdflib's evaluation — here the sides are solved
    independently and joined, which is only exact while nothing inside
    depends on those values (a FILTER that could see them falls back).
    ``var_preds`` (a :class:`_Pushed`) are the filter conjuncts a FILTER
    above pushed down: single-variable ones to the pattern scans and VALUES
    tables binding the variable, tuple ones to the BGPs binding all of
    theirs. ``scope`` is the graph every pattern below is matched in
    (:class:`_GraphScope`); ``None`` means the graph the context has made
    active. ``limit`` says only that many rows will be read: a block that is
    one native match fetches no more (see :func:`_solve_bgp`); any other
    ignores it. ``stream`` says the relation goes straight to
    :func:`_yield_solutions`: it reaches a BGP through FILTER and GRAPH nodes
    only, where rdflib may finish the patterns a join over the pair budget
    leaves (:func:`_join_incremental_bgp`).
    """
    if scope is None:
        scope = _active_scope(ctx, store)
    name = node.name
    trace = _TRACE.get()
    started = time.perf_counter_ns() if trace is not None else 0
    if trace is not None:
        trace.depth += 1
    try:
        if name == "BGP":
            rel = _solve_bgp(ctx, store, node.triples, scope, var_preds, limit, stream)
        elif name == "Filter":
            rel = _solve_filter(ctx, store, node, env, var_preds, scope, limit, stream)
        elif name == "Join":
            rel = _solve_join(ctx, store, node, env, var_preds, scope)
        elif name == "LeftJoin":
            rel = _solve_left_join(ctx, store, node, env, var_preds, scope)
        elif name == "Minus":
            rel = _solve_minus(ctx, store, node, env, var_preds, scope)
        elif name == "ToMultiSet":
            rel = _solve_values(ctx, store, node, var_preds)
        elif name == "Graph":
            inner = _graph_scope(ctx, node)
            if inner.var is not None and not _carries_graph(node.p, inner.var):
                raise NotImplementedError
            rel = _solve_block(ctx, store, node.p, env, var_preds, inner, limit, stream)
        else:
            raise NotImplementedError
    finally:
        if trace is not None:
            trace.depth -= 1
    _trace_event(
        "block_operator_complete",
        operator=name,
        depth=0 if trace is None else trace.depth,
        rows=_relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
        pattern_count=len(node.triples) if name == "BGP" else None,
    )
    return rel


def _solve_values(ctx, store, node, var_preds=None) -> Relation:
    """An inline VALUES table as a relation: its constants looked up in one
    batch (:func:`_constant_codes`; a term the dictionary does not hold gets
    a negative code, so it joins nothing but is still yielded verbatim),
    UNDEF unbound. A row that contradicts a context binding is dropped, as
    rdflib's ``evalValues`` skips it on AlreadyBound, and so is a row whose
    term fails a conjunct a FILTER above pushed down (``var_preds``; the
    variables they name are bound in every row), tested on the table's own
    terms (:func:`filters.constant_predicate`)."""
    variables = _node_vars(node)
    schema = tuple(v for v in variables if ctx[v] is None)
    bound = {v: ctx[v] for v in variables if ctx[v] is not None}
    rows_in = [
        row
        for row in node.p.res
        if not any(row.get(v, "UNDEF") != "UNDEF" and row[v] != term for v, term in bound.items())
    ]
    terms = list(
        dict.fromkeys(row[v] for row in rows_in for v in schema if row.get(v, "UNDEF") != "UNDEF")
    )
    codes = dict(zip(terms, _constant_codes(store, terms), strict=True))
    if var_preds:
        tests = [(v, filters.constant_predicate(c)) for v in schema for c in var_preds.get(v, ())]
        rows_in = [row for row in rows_in if all(test(row[v]) for v, test in tests)]
    rows = [
        tuple(None if row.get(v, "UNDEF") == "UNDEF" else codes[row[v]] for v in schema)
        for row in rows_in
    ]
    nullable = frozenset(v for i, v in enumerate(schema) if any(row[i] is None for row in rows))
    return Relation(schema, None, rows, len(rows), nullable, store._terms().foreign)


def _constant_codes(store, terms: list) -> list:
    """The dictionary codes of query constants, private negative ones for the
    terms it does not hold.

    One ``encode_many`` looks every spelling up, tolerant of spelling: the
    native layer canonicalizes a term the way its pattern parser does, so a
    miss means no match of any pattern could reach the term either. An IRI
    the parser rejects (:func:`~vortex_rdflib.terms.unparseable_iri`) is no
    term of the file and is not looked up; any other term the native parser
    rejects leaves the table to rdflib.
    """
    if not terms:
        return []
    rejected = [unparseable_iri(term) for term in terms]
    spellings = [canonical_spelling(t) for t, out in zip(terms, rejected, strict=True) if not out]
    try:
        encoded = iter(store._dict.encode_many(spellings) if spellings else ())
    except ValueError:
        raise NotImplementedError from None
    found = [None if out else next(encoded) for out in rejected]
    store._prime_decode_cache([code for code in found if code is not None])
    codes = []
    for term, code in zip(terms, found, strict=True):
        if code is None:
            codes.append(store._foreign_code(term))
            continue
        # rdflib would carry the query's own object into the solutions; when
        # that object is not the decoded dictionary term (a query literal
        # rdflib's parser left unnormalized, "042"^^xsd:integer), the two are
        # observably different rows, so the query is left to rdflib.
        if store._decode_term(code) != term:
            raise NotImplementedError
        codes.append(code)
    return codes


def _solve_filter(ctx, store, node, env, var_preds, scope, limit=None, stream=False) -> Relation:
    """A ``Filter`` over a block: constant conjuncts decide the whole block
    before anything is matched, single-variable conjuncts over variables the
    block always binds restrict the pattern scans and VALUES tables that bind
    them, the rest filters the solved rows. A ``limit`` reaches the block
    only when nothing is left to filter above it, and ``stream`` when the
    filter holds no EXISTS. Where rdflib still finishes the block's rows
    (``rest``), a conjunct over the variables those rows bind tests them
    before it does; one over a variable only the rest binds cannot, and the
    FILTER goes back to rdflib.

    The expression's block variables are those its group binds
    (:func:`_group_vars`; see :func:`filters.analyze_filter` for the rest of
    its scope)."""
    inner = node.p
    # The columns of the empty relation a false constant conjunct returns.
    block_vars = _block_vars(ctx, inner, scope)
    plan = filters.analyze_filter(node, _group_vars(ctx, inner), ctx)
    if plan.exists and scope.var is not None:
        # rdflib evaluates an EXISTS body against the *active* graph. Under a
        # graph variable there is no single active graph — the row's graph is
        # a column here — so the body would be answered over the union. Hand
        # the whole block back and let rdflib walk the graphs itself.
        raise NotImplementedError
    decided = plan.constant + plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    _reject_graph_exists(ctx, store, decided, scope)
    if not getattr(node, "no_isolated_scope", False):
        kept = set(node._vars or ()) | set(ctx.initBindings or ())
        _reject_forgotten_exists(ctx, decided + [e.conjunct for e in plan.exists], kept)
    if env:
        _reject_env_references(node, plan, env, ctx)
    for conjunct in plan.constant:
        if not filters.evaluate_constant(conjunct):
            return Relation.from_rows(tuple(block_vars), [])
    nullable = _block_nullable(inner)
    outer = var_preds if var_preds is not None else _Pushed()
    fast_tuples = tuple(conjunct for conjunct in plan.tuples if conjunct.fast is not None)
    pushed = _Pushed(outer, outer.tuples + fast_tuples, outer.views, outer.applied)
    residual = list(plan.tuples)
    for var, conjuncts in plan.per_var.items():
        if var in nullable:
            residual.extend(conjuncts)
        else:
            pushed[var] = pushed.get(var, []) + conjuncts
    limit = None if residual or plan.exists else limit
    stream = stream and not plan.exists
    rel = _solve_block(ctx, store, inner, env, pushed, scope, limit, stream)
    residual = [conjunct for conjunct in residual if id(conjunct) not in pushed.applied]
    if residual:
        if rel.rest and any(v not in rel.schema for c in residual for v in c.vars):
            raise NotImplementedError
        positions = [
            [rel.schema.index(variable) for variable in conjunct.vars] for conjunct in residual
        ]
        views = pushed.views
        if rel.rows is not None:
            # Only the fast route reads parsed terms; a generic-only conjunct
            # decodes through the query's term cache.
            columns = {
                index
                for conjunct, indexes in zip(residual, positions, strict=True)
                if conjunct.fast is not None
                for index in indexes
            }
            filters.prime_tuple_views(store, rel.rows, columns, views)
        preds = tuple(
            filters.tuple_predicate(store, conjunct, indexes, views)
            for conjunct, indexes in zip(residual, positions, strict=True)
        )
        rel = _filter_relation(rel, preds)
    for exists in plan.exists:
        rel = _apply_exists(ctx, store, rel, exists, scope)
    return rel


def _apply_exists(ctx, store, rel: Relation, exists, scope) -> Relation:
    """``FILTER (NOT) EXISTS { body }`` as a semi- or anti-join on the
    variables the body shares with the block: the body is solved once (or,
    for a small block and a one-pattern body, probed per row with counts
    capped at one row, in native batches) instead of once per row. Shapes
    rdflib would evaluate differently on an independently solved body — a
    shared variable the block or the body's solutions may leave unbound, a
    body the solver cannot take, a body FILTER that sees the block's bindings
    — take the generic route, rdflib's own evaluator per distinct tuple of
    the variables the EXISTS names. The FILTER is left to rdflib where that
    route would differ: under a ``GRAPH`` scoping the block to a graph other
    than the context's (see :func:`_reject_graph_exists`), and where the
    body holds a MINUS and the EXISTS names no variable of the block, or one
    the block may leave unbound."""
    body, negate = _unwrap_empty_joins(exists.body), exists.negate
    rows = _rows_of(rel)
    if not rows:
        return rel

    def generic():
        conjunct = exists.conjunct
        _reject_graph_exists(ctx, store, [conjunct], scope)
        if filters.has_minus(body) and (
            not conjunct.vars or any(v in rel.nullable for v in conjunct.vars)
        ):
            # rdflib binds the whole row in the body, so a MINUS there always
            # shares a binding; this route binds only the variables the EXISTS
            # names, so it is not taken where it names none of the block's,
            # or one the block may leave unbound.
            raise NotImplementedError
        positions = [rel.schema.index(v) for v in conjunct.vars]
        return _filter_relation(rel, (filters.tuple_predicate(store, conjunct, positions),))

    try:
        _check_block(body)
    except NotImplementedError:
        return generic()
    shared = [v for v in _block_vars(ctx, body, scope) if v in rel.schema]
    if any(v in rel.nullable for v in shared):
        return generic()
    env = frozenset(rel.schema)
    try:
        if not shared:
            found = (
                next(_code_rows(_solve_block(ctx, store, body, env, None, scope, 1)), None)
                is not None
            )
            if found != negate:
                return rel
            return Relation(rel.schema, None, [], 0, rel.nullable, rel.foreign)
        if body.name == "BGP" and len(body.triples) == 1:
            # Counted before it is matched: probing needs no match at all.
            pat = _pattern_terms(ctx, store, *body.triples[0], scope=scope)
            repeated_free = any(
                len(pos) > 1 for v, pos in pat["varpos"].items() if v not in rel.schema
            )
            if not repeated_free and len(rows) * _PROBE_FANOUT < _count_pattern(
                store, pat, {}, limit=len(rows) * _PROBE_FANOUT + 1
            ):
                flags = _probe_exists(store, rel.schema, rows, pat)
                kept = [row for row, found in zip(rows, flags, strict=True) if found != negate]
                return Relation(rel.schema, None, kept, len(kept), rel.nullable, rel.foreign)
            _match_resolved_pattern(store, pat)
            inner = _rel_from_pattern(pat)
        else:
            inner = _solve_block(ctx, store, body, env, None, scope)
    except NotImplementedError:
        return generic()
    if any(v not in inner.schema or v in inner.nullable for v in shared):
        # Where the body's solutions leave a key unbound, rdflib's body sees
        # the row's own binding of it, which a semi-join cannot.
        return generic()
    ia = [rel.schema.index(v) for v in shared]
    ib = [inner.schema.index(v) for v in shared]
    keys = {tuple(row[i] for i in ib) for row in _code_rows(inner)}
    kept = [row for row in rows if (tuple(row[i] for i in ia) in keys) != negate]
    return Relation(rel.schema, None, kept, len(kept), rel.nullable, rel.foreign)


def _unwrap_empty_joins(node):
    """rdflib never simplifies an EXISTS body, so a group translates to a
    Join with an empty BGP on one side; look through those."""
    while getattr(node, "name", None) == "Join":
        p1, p2 = node.p1, node.p2
        if getattr(p1, "name", None) == "BGP" and not p1.triples:
            node = p2
        elif getattr(p2, "name", None) == "BGP" and not p2.triples:
            node = p1
        else:
            break
    return node


def _probe_exists(store, schema, rows, pat) -> list:
    """Whether the pattern, with each row's codes substituted, matches at
    least one quad — counts capped at one row (``count_quads_many``, in
    batches), no row materialized."""
    bound = [(schema.index(v), pos) for v, pos in pat["varpos"].items() if v in schema]
    free_positions = {idx for v, pos in pat["varpos"].items() if v not in schema for idx in pos}
    keep = {p: spec for p, spec in (pat.get("nkeep") or {}).items() if p in free_positions}
    narrowing = _narrowing(keep, limit=1)
    patterns = _substituted_patterns(store, rows, pat, bound)
    out = []
    for start in range(0, len(rows), _PROBE_BATCH):
        batch = patterns[start : start + _PROBE_BATCH]
        probes = [(n3, narrowing) for n3 in batch if n3 is not None]
        answers = iter(_run_probes(store, probes, count=True))
        out.extend(n3 is not None and next(answers) > 0 for n3 in batch)
    return out


def _substituted_patterns(store, rows, pat, bound) -> list:
    """Per row, the pattern's quad with the row's codes substituted at the
    ``bound`` positions — or ``None`` when no quad can match it."""
    # One GIL-released batch decode covers every code the probes will bind.
    n3_cache = _probe_spellings(store, rows, bound)
    patterns = []
    for row in rows:
        n3 = list(pat["n3"])
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


def _reject_env_references(node, plan, env, ctx) -> None:
    """rdflib evaluates a filter on a lazy join's or an OPTIONAL's right side,
    or in an EXISTS body, once per row of the left side or of the block the
    EXISTS filters, whose variables are ``env``. Its expression sees the
    row's binding of such a variable only where the node keeps it: in its
    ``_vars`` or the query's ``initBindings``, or anywhere directly inside an
    EXISTS body. On the independently solved block a conjunct sees the
    variable bound only where the block binds it too; ``_vars`` leaves out a
    VALUES table's variables, and a filter in a group of an EXISTS body has
    none. Where the two differ, the conjunct is left to rdflib."""
    everything = getattr(node, "no_isolated_scope", False)
    allowed = set(node._vars or ()) | set(ctx.initBindings or ())
    conjuncts = plan.constant + plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    conjuncts += [exists.conjunct for exists in plan.exists]
    for conjunct in conjuncts:
        for var in filters.expr_vars(conjunct.expr):
            if var in env and var not in conjunct.consts:
                if (var in conjunct.vars) != (everything or var in allowed):
                    raise NotImplementedError


def _reject_graph_exists(ctx, store, conjuncts, scope) -> None:
    """rdflib's evaluator matches an EXISTS body in the graph the context has
    made active: a conjunct it decides that holds one cannot be evaluated
    under a ``GRAPH`` that scopes the block to another graph."""
    if scope != _active_scope(ctx, store) and any(filters.has_exists(c.expr) for c in conjuncts):
        raise NotImplementedError


def _reject_forgotten_exists(ctx, conjuncts, kept) -> None:
    """rdflib evaluates a FILTER, and an OPTIONAL's condition, with the
    context's bindings outside ``kept`` forgotten (see
    :func:`filters.analyze_filter`; a condition keeps the ``initBindings``
    alone). An EXISTS here would see them: its body is solved through the
    context, and rdflib's evaluator, handed no binding, thaws the whole
    context. A conjunct holding an EXISTS that names one is left to rdflib."""
    for conjunct in conjuncts:
        if filters.has_exists(conjunct.expr) and any(
            ctx[var] is not None and var not in kept for var in filters.expr_vars(conjunct.expr)
        ):
            raise NotImplementedError


def _rows_of(rel: Relation) -> list:
    """The relation's rows, materialized. A streamed body is drained into
    rows, its ``preds`` applied, and the relation keeps them as its body, so
    a second call returns the same list."""
    assert not rel.rest, "rdflib finishes this relation's rows: only the emitter reads them"
    if rel.rows is not None:
        return rel.rows
    rows = list(_code_rows(rel))
    if rel.pieces is not None:
        rel.rows, rel.nrows, rel.preds, rel.pieces = rows, len(rows), (), None
    return rows


def _shared_key_ok(left: Relation, right: Relation, shared) -> None:
    # A join key that may be unbound needs rdflib's compatibility semantics
    # (unbound matches anything); a hash join cannot give them.
    if any(v in left.nullable or v in right.nullable for v in shared):
        raise NotImplementedError


def _solve_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """A group join: both sides solved, then a hash join on the shared
    variables (a probe of the right pattern when the left side is small).
    rdflib deduplicates the right side of a non-lazy join."""
    lazy = bool(node.lazy)
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    left_rows = _rows_of(left)
    if not left_rows:
        schema = left.schema + tuple(
            v for v in _block_vars(ctx, node.p2, scope) if v not in left.schema
        )
        return Relation(schema, None, [], 0, left.nullable | _block_nullable(node.p2), left.foreign)
    right_env = env | frozenset(_scoped_vars(node.p1, scope)) if lazy else env
    p2 = node.p2
    if lazy and p2.name == "BGP" and len(p2.triples) == 1:
        pat, estimate, probe = _plan_pattern_side(
            ctx, store, p2.triples[0], scope, left, left_rows, var_preds
        )
        if probe:
            schema, rows = _probe_join(store, left.schema, left_rows, pat)
            _trace_event(
                "join_plan_complete",
                strategy="probe",
                input_rows=len(left_rows),
                estimated_right_rows=estimate,
                native_probe_call_count=len(left_rows),
                output_rows=len(rows),
            )
            return Relation(schema, None, rows, len(rows), left.nullable, left.foreign)
        _trace_event(
            "join_plan_complete",
            strategy="hash",
            input_rows=len(left_rows),
            estimated_right_rows=estimate,
            right_rows=pat["nrows"],
        )
        right = _rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, right_env, var_preds, scope)
    right_rows = _rows_of(right)
    if not lazy:
        right_rows = list(dict.fromkeys(right_rows))
    shared = [v for v in right.schema if v in left.schema]
    _shared_key_ok(left, right, shared)
    schema, rows = _join(left.schema, left_rows, right.schema, right_rows)
    foreign = left.foreign or right.foreign
    return Relation(schema, None, rows, len(rows), left.nullable | right.nullable, foreign)


def _solve_left_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """An OPTIONAL: every left row, extended by the matching right rows that
    pass the hoisted inner FILTER, or padded with unbound variables."""
    p1, p2, expr = node.p1, node.p2, node.expr
    vars1, vars2 = _scoped_vars(p1, scope), _scoped_vars(p2, scope)
    condition = expr if getattr(expr, "name", None) != "TrueFilter" else None
    if (
        scope.var is not None
        and scope.var in _group_vars(ctx, p2)
        and scope.var not in _group_vars(ctx, p1)
    ):
        # rdflib matches p2's ?g against any term and joins the graph in after
        # the OPTIONAL: a left row whose matches all name another graph is
        # dropped there, where the graph column would pad it.
        raise NotImplementedError
    # rdflib re-evaluates an unmatched OPTIONAL with only p1's `_vars` bound
    # (its "cheated scope" check). That differs from the first pass when a
    # variable that pass drops — bound by the context, by an enclosing join,
    # or by p1 outside its `_vars` (a VALUES table) — reaches p2, or when a
    # variable it keeps was invisible to the condition before; those shapes
    # are left to rdflib.
    outer = dict(ctx.bindings.items())
    init = frozenset(ctx.initBindings or ())
    p1_vars = p1._vars
    if p1_vars is not None:
        p1_vars = frozenset(p1_vars)
        dropped = (env | frozenset(outer) | frozenset(vars1)) - init - p1_vars
        # A graph variable is not a binding that flows from p1 into p2: rdflib
        # evaluates the whole OPTIONAL with the graph *active* and labels the
        # result with it afterwards, so p2 is in that graph however p1 went.
        # Here both sides carry it as a column and the join on it says the
        # same thing — including for a padded row, whose graph comes from p1.
        dropped -= {scope.var}
        if dropped & frozenset(vars2):
            raise NotImplementedError
        cheated = (env | frozenset(outer)) - init
        if condition is not None and frozenset(filters.expr_vars(condition)) & p1_vars & cheated:
            raise NotImplementedError

    left = _solve_block(ctx, store, p1, env, var_preds, scope)
    left_rows = _rows_of(left)
    extra = tuple(v for v in _block_vars(ctx, p2, scope) if v not in left.schema)
    nullable = left.nullable | _block_nullable(p2) | frozenset(extra)
    if not left_rows:
        return Relation(left.schema + extra, None, [], 0, nullable, left.foreign)

    def condition_preds(schema):
        if condition is None:
            return ()
        init = set(ctx.initBindings or ())
        visible = {v: term for v, term in outer.items() if v in init}
        sides = set(_group_vars(ctx, node))
        plan = filters.analyze_expr(condition, [v for v in schema if v in sides], ctx, visible)
        if plan.exists:
            # An OPTIONAL's condition can carry (NOT) EXISTS; a semi-join over
            # the padded rows is not what rdflib computes, so the whole
            # LeftJoin goes back to it.
            raise NotImplementedError
        conjuncts = plan.tuples + [c for cs in plan.per_var.values() for c in cs]
        _reject_graph_exists(ctx, store, plan.constant + conjuncts, scope)
        _reject_forgotten_exists(ctx, plan.constant + conjuncts, init)
        for conjunct in plan.constant:
            if not filters.evaluate_constant(conjunct):
                return None  # no pair can pass: every left row is unmatched
        return tuple(
            filters.tuple_predicate(store, c, [schema.index(v) for v in c.vars]) for c in conjuncts
        )

    if p2.name == "BGP" and len(p2.triples) == 1:
        # Counted before it is matched: a selective left relation drives the
        # probes without the complete right match the probes exist to avoid.
        plan_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        pat, estimate, probe = _plan_pattern_side(
            ctx, store, p2.triples[0], scope, left, left_rows, None
        )
        if probe:
            schema = left.schema + tuple(v for v in pat["varpos"] if v not in left.schema)
            preds = condition_preds(schema)
            if preds is None:
                rows = [row + (None,) * len(extra) for row in left_rows]
                schema, unmatched = left.schema + extra, len(left_rows)
            else:
                schema, rows = _probe_join(
                    store, left.schema, left_rows, pat, keep_unmatched=True, row_preds=preds
                )
                free_count = len(schema) - len(left.schema)
                unmatched = sum(row[-1] is None for row in rows) if free_count else 0
            _trace_event(
                "left_join_plan_complete",
                strategy="constant_false" if preds is None else "probe",
                input_rows=len(left_rows),
                estimated_right_rows=estimate,
                native_initial_match_count=0,
                native_probe_call_count=0 if preds is None else len(left_rows),
                matched_left_rows=len(left_rows) - unmatched,
                unmatched_left_rows=unmatched,
                output_rows=len(rows),
                elapsed_ns=time.perf_counter_ns() - plan_started if plan_started else 0,
            )
            return Relation(schema, None, rows, len(rows), nullable, left.foreign)
        _trace_event(
            "left_join_plan_complete",
            strategy="hash",
            input_rows=len(left_rows),
            estimated_right_rows=estimate,
            right_rows=pat["nrows"],
            native_initial_match_count=int(not pat["unsatisfiable"]),
            native_probe_call_count=0,
            elapsed_ns=time.perf_counter_ns() - plan_started if plan_started else 0,
        )
        right = _rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, env | frozenset(vars1), None, scope)
    shared = [v for v in right.schema if v in left.schema]
    _shared_key_ok(left, right, shared)
    schema = left.schema + tuple(v for v in right.schema if v not in left.schema)
    foreign = left.foreign or right.foreign
    preds = condition_preds(schema)
    if preds is None:
        rows = [row + (None,) * (len(schema) - len(left.schema)) for row in left_rows]
        return Relation(schema, None, rows, len(rows), nullable, foreign)
    rows = _left_join_rows(left.schema, left_rows, right.schema, _rows_of(right), preds)
    return Relation(schema, None, rows, len(rows), nullable, foreign)


def _left_join_rows(schema_a, rows_a, schema_b, rows_b, preds) -> list:
    """Hash left join: each left row extended by every compatible right row
    that passes ``preds`` (over the combined row), or padded with None."""
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    ia = [schema_a.index(v) for v in shared]
    ib = [schema_b.index(v) for v in shared]
    table: dict = {}
    for rb in rows_b:
        table.setdefault(tuple(rb[i] for i in ib), []).append(tuple(rb[i] for i in keep_b))
    pad = (None,) * len(keep_b)
    out = []
    for ra in rows_a:
        matched = False
        for tail in table.get(tuple(ra[i] for i in ia), ()):
            row = ra + tail
            if all(pred(row) for pred in preds):
                out.append(row)
                matched = True
        if not matched:
            out.append(ra + pad)
    return out


def _solve_minus(ctx, store, node, env, var_preds, scope) -> Relation:
    """MINUS: an anti-join on the shared variables.

    rdflib removes a left row only where a compatible right row shares a
    binding with it. Both sides' rows carry the context's bindings and, where
    rdflib solves the MINUS once per row of an enclosing lazy join, OPTIONAL
    or EXISTS (``env``), that row's, which the right side is also matched
    with. With no variable both sides bind, nothing is removed at top level,
    and everything is under a context binding when the right side is
    non-empty. Under ``GRAPH ?g`` rdflib solves both sides in each named
    graph with ``?g`` unbound: the graph column keeps the anti-join in one
    graph but is a shared binding only where both sides bind ``?g``, and a
    right side's own ``?g`` matches any term. The shapes that turn on the
    enclosing row, or on that ``?g``, are left to rdflib."""
    left_vars, right_vars = _group_vars(ctx, node.p1), _group_vars(ctx, node.p2)
    common = any(v in left_vars for v in right_vars)
    context = bool(dict(ctx.bindings.items()))
    if env and not (common or context):
        raise NotImplementedError  # whether the enclosing row binds anything decides
    if any(v in env and v not in left_vars for v in right_vars):
        raise NotImplementedError  # the right side reads the enclosing row's value
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    if not (common or context):
        return left
    left_rows = _rows_of(left)
    if not left_rows:
        return left
    if scope.var in right_vars and scope.var not in left_vars:
        raise NotImplementedError
    right = _solve_block(ctx, store, node.p2, env, None, scope)
    shared = [v for v in right.schema if v in left.schema]
    if not shared:
        if next(_code_rows(right), None) is not None:
            return Relation(left.schema, None, [], 0, left.nullable, left.foreign)
        return Relation(left.schema, None, left_rows, len(left_rows), left.nullable, left.foreign)
    _shared_key_ok(left, right, shared)
    ia = [left.schema.index(v) for v in shared]
    ib = [right.schema.index(v) for v in shared]
    keys = {tuple(rb[i] for i in ib) for rb in _code_rows(right)}
    rows = [ra for ra in left_rows if tuple(ra[i] for i in ia) not in keys]
    return Relation(left.schema, None, rows, len(rows), left.nullable, left.foreign)


def _filter_relation(rel: Relation, preds) -> Relation:
    """Apply row predicates: streamed over a columnar or streamed body,
    eagerly over rows. The patterns rdflib still finishes the rows with
    (``rest``) stay with them."""
    if rel.rows is None:
        return Relation(
            rel.schema,
            rel.cols,
            None,
            rel.nrows,
            rel.nullable,
            rel.foreign,
            rel.preds + tuple(preds),
            rel.pieces,
            rel.rest,
        )
    rows = [row for row in rel.rows if all(pred(row) for pred in preds)]
    return Relation(rel.schema, None, rows, len(rows), rel.nullable, rel.foreign, rest=rel.rest)


# Probing beats hash-joining when the running relation is at least this many
# times smaller than the next pattern's match. The value trades the fixed cost
# of a native match, which does not depend on how selective it is, against the
# cost of materializing a matched row into Python: one probe is worth skipping
# that for this many rows.
_PROBE_FANOUT = 100

_PAIR_BUDGET_ENV = "VORTEX_RDF_PAIR_BUDGET"


def _pair_budget() -> int:
    """``VORTEX_RDF_PAIR_BUDGET``, an integer of at least 0, or ``1 << 24``
    when it is unset."""
    value = os.environ.get(_PAIR_BUDGET_ENV)
    if value is None:
        return 1 << 24
    try:
        budget = int(value)
    except ValueError:
        budget = None
    if budget is None or budget < 0:
        raise ValueError(f"{_PAIR_BUDGET_ENV} must be an integer >= 0, got {value!r}")
    return budget


#: The most row pairs one join step of a BGP may make, set when the module is
#: imported (:func:`_pair_budget`). A step whose two sides' product is within
#: it is built uncounted; past that, its pairs are counted natively first
#: (:func:`_pair_count`), and a step that counts more is not built
#: (:class:`_OverBudget`). A probe step, which has no pairs to count, stops
#: once its rows pass a quarter of the budget; a keep-scan, once its match
#: passes the budget.
_PAIR_BUDGET = _pair_budget()


class _OverBudget(Exception):
    """A join step of a BGP over :data:`_PAIR_BUDGET`, raised before the step
    is built. ``pairs`` is the step's pair count, ``None`` for a probe step
    or a keep-scan, which stop on their rows. ``calls`` are the native calls
    the step made before it stopped — a probe step's probes, a keep-scan's
    match — and ``native_ns`` the keep-scan's time when tracing, for the
    step's statistics. :func:`_join_incremental_bgp` catches it."""

    def __init__(self, pairs: int | None, calls: int = 0, native_ns: int = 0):
        super().__init__(pairs)
        self.pairs = pairs
        self.calls = calls
        self.native_ns = native_ns


#: The most rows of its fed side a streamed join step (:func:`_streamed_step`)
#: takes at a time: a slice of a side it holds, or a chunk of a match it
#: reads in chunks. A piece over :data:`_PAIR_BUDGET` is halved before it is
#: gathered.
_PIECE_ROWS = 65_536


def _pattern_trace_shape(ctx, triple) -> dict:
    resolved = [ctx[term] for term in triple]
    bound = [value is not None for value in resolved]
    variables = [term for term, value in zip(triple, resolved, strict=True) if value is None]
    return {
        "bound_subject": bound[0],
        "bound_predicate": bound[1],
        "bound_object": bound[2],
        "variable_count": len(set(variables)),
        "repeated_variable_count": len(variables) - len(set(variables)),
        "pattern_shape": "".join("B" if item else "V" for item in bound),
    }


def _solve_bgp(ctx, store, triples, scope, var_preds=None, limit=None, stream=False) -> Relation:
    """Evaluate a BGP by matching one seed and probing connected patterns.

    The filter conjuncts that are native keeps (see
    :func:`filters.native_restriction`) become keeps on each pattern's native
    match before anything is counted (see :func:`_push_native_restrictions`);
    the others are evaluated over the codes the pattern brings
    (:func:`filters.evaluate_column`): vortex-rdf decides those with a native
    shape first, and only the codes it leaves undecided go to the Python
    routes. A single pattern with no such residual conjunct takes ``limit``
    into its match as well (see :func:`_limit_reaches_match`).

    With ``stream`` (the relation goes straight to the emitter) and the
    block in the graph the context has made active — the graph rdflib's
    ``evalBGP`` reads — the planner may leave the patterns past a join over
    the pair budget to rdflib: the relation then carries their triples as
    ``rest``.
    """
    trace = _TRACE.get()
    traced = trace is not None
    started = time.perf_counter_ns() if traced else 0
    _trace_event("bgp_start", pattern_count=len(triples), initial_bound_variable_count=0)
    if not triples:
        rel = Relation.from_rows((), [()])
        _trace_event(
            "bgp_complete",
            pattern_count=0,
            output_rows=1,
            output_columns=0,
            native_call_count=0,
            native_initial_match_count=0,
            native_probe_call_count=0,
            native_match_ns=0,
            restriction_ns=0,
            join_ns=0,
            other_ns=0,
            elapsed_ns=0,
            timing_reconciled=True,
        )
        return rel

    if len(triples) == 1:
        shape = _pattern_trace_shape(ctx, triples[0]) if traced else None
        _trace_event("bgp_pattern_start", original_pattern_index=0, **(shape or {}))
        pat = _pattern_terms(ctx, store, *triples[0], scope=scope)
        pat["index"] = 0
        residual = _push_native_restrictions(store, pat, var_preds) if var_preds else {}
        if not _limit_reaches_match(store, pat, residual):
            limit = None
        then = time.perf_counter_ns() if traced else 0
        _match_resolved_pattern(store, pat, limit=limit)
        native_ns = time.perf_counter_ns() - then if then else 0
        _trace_event(
            "bgp_pattern_complete",
            original_pattern_index=0,
            cardinality_source="matched",
            native_call_count=1,
            matched_rows=pat["nrows"],
            native_match_ns=native_ns,
            restriction_ns=0,
            relation_extend_ns=0,
            other_ns=0,
            elapsed_ns=native_ns,
            timing_reconciled=True,
            **(shape or {}),
        )
        then = time.perf_counter_ns() if traced else 0
        if residual:
            _restrict_pattern(store, pat, residual)
        restriction_ns = time.perf_counter_ns() - then if then else 0
        then = time.perf_counter_ns() if traced else 0
        rel = _rel_from_pattern(pat)
        join_ns = time.perf_counter_ns() - then if then else 0
        elapsed = time.perf_counter_ns() - started if started else 0
        known = native_ns + restriction_ns + join_ns
        other_ns = max(0, elapsed - known)
        _trace_event(
            "bgp_complete",
            pattern_count=1,
            output_rows=_relation_rows(rel),
            output_columns=len(rel.schema),
            native_call_count=1,
            native_initial_match_count=1,
            native_probe_call_count=0,
            native_match_ns=native_ns,
            restriction_ns=restriction_ns,
            join_ns=join_ns,
            other_ns=other_ns,
            elapsed_ns=known + other_ns,
            timing_reconciled=True,
        )
        return rel

    patterns = []
    for index, triple in enumerate(triples):
        shape = _pattern_trace_shape(ctx, triple) if traced else None
        _trace_event("bgp_pattern_start", original_pattern_index=index, **(shape or {}))
        pat = _pattern_terms(ctx, store, *triple, scope=scope)
        pat["index"] = index
        if var_preds:
            pending = {v: var_preds[v] for v in pat["varpos"] if v in var_preds}
            if pending:
                pending = _push_native_restrictions(store, pat, pending)
            if pending:
                pat["deferred_var_preds"] = pending
        patterns.append(pat)
        _trace_event(
            "bgp_pattern_complete",
            original_pattern_index=index,
            cardinality_source="unmatched",
            native_call_count=0,
            matched_rows=None,
            native_match_ns=0,
            restriction_ns=0,
            relation_extend_ns=0,
            other_ns=0,
            elapsed_ns=0,
            timing_reconciled=True,
            **(shape or {}),
        )

    then = time.perf_counter_ns() if traced else 0
    stats = {}
    finishable = stream and scope == _active_scope(ctx, store)
    rel = _join_patterns(store, patterns, stats, var_preds, finishable)
    if stats["rest"]:
        rel.rest = tuple(triples[index] for index in stats["rest"])
    total_join_ns = time.perf_counter_ns() - then if then else 0
    native_ns = stats["native_ns"]
    restriction_ns = stats["restriction_ns"]
    join_ns = max(0, total_join_ns - native_ns - restriction_ns)
    elapsed = time.perf_counter_ns() - started if started else 0
    known = native_ns + restriction_ns + join_ns
    other_ns = max(0, elapsed - known)
    _trace_event(
        "bgp_complete",
        pattern_count=len(patterns),
        output_rows=_relation_rows(rel),
        output_columns=len(rel.schema),
        native_call_count=stats["initial_calls"] + stats["probe_calls"],
        native_initial_match_count=stats["initial_calls"],
        native_probe_call_count=stats["probe_calls"],
        estimate_calls=stats["estimate_calls"],
        native_match_ns=native_ns,
        restriction_ns=restriction_ns,
        join_ns=join_ns,
        other_ns=other_ns,
        elapsed_ns=known + other_ns,
        timing_reconciled=True,
    )
    return rel


def _pattern_static_rank(pat) -> tuple:
    """A pattern's likely selectivity from its shape alone — more bound
    positions first, then repeated variables, then query order. It breaks
    ties between equal counts; the counts themselves decide the plan."""
    n3 = pat["n3"]
    repeated = sum(len(pos) - 1 for pos in pat["varpos"].values())
    return (-sum(value is not None for value in n3), -repeated, pat.get("index", 0))


def _pattern_key(pat) -> tuple:
    """What a pattern's native count and match are memoized by: the resolved
    quad pattern and its native keep (two triples resolving to the same quad
    share both only when they are narrowed alike). A code column stands in
    the key itself, compared by identity: the key holds it, so no later
    column can take its place while the memo keeps the entry."""
    nkeep = pat.get("nkeep")
    if not nkeep:
        return tuple(pat["n3"])
    narrowing = tuple(
        sorted(
            (position, ("range", *keep) if isinstance(keep, tuple) else ("codes", keep))
            for position, keep in nkeep.items()
        )
    )
    return (*pat["n3"], narrowing)


def _count_pattern(store, pat, counts: dict, exact: bool = False, limit: int | None = None) -> int:
    """The number of quads a resolved pattern selects, from ``count_quads``
    — the row selection's size, a fraction of a match's cost, and no
    columns. Memoized across the patterns of one block, like the matches.
    Zero for an unsatisfiable pattern.

    A planning count (the default) reads the quad pattern alone: narrowing
    it by the pattern's native keep costs the native layer a pass over the
    selected rows, more than an estimate is worth, and it decides nothing a
    narrowed match would not repair. An ``exact`` count — an answer, or a
    size a join decision hinges on — narrows by the keep. With ``limit`` the
    count stops there: an answer that reaches it is a lower bound, which is
    never memoized as the count. A count already memoized is returned whole.
    """
    if pat["unsatisfiable"]:
        return 0
    nkeep = pat.get("nkeep") if exact else None
    key = _pattern_key(pat) if nkeep else tuple(pat["n3"])
    n = counts.get(key)
    if n is None:
        cancel = _cancel_kw_for(_cancel_kw(), store, pat["n3"], limit)
        n = _native(store).count_quads(*pat["n3"], **_narrowing(nkeep, limit), **cancel)
        if limit is None or n < limit:
            counts[key] = n
    return n


def _count_patterns(store, patterns, counts: dict) -> int:
    """Set the patterns' ``"estimate"`` — their planning counts (see
    :func:`_count_pattern`) — counting the quad patterns ``counts`` does not
    hold yet, in static rank order, and stop at the first zero, which
    empties the block: the patterns left uncounted get no ``"estimate"``.

    Once a pattern counts at most ``_PROBE_FANOUT`` rows, every pattern not
    yet counted that shares a variable with it is marked ``"probe"`` and is
    not counted at all: joined after it, it is probed unless it is small,
    which a count capped at the decision's threshold settles at its join
    step (:func:`_join_incremental_bgp`).

    The patterns are counted one at a time, except the scans: enough of them
    (``_SCAN_BATCH_MIN``, ``_MEMORY_SCAN_BATCH_MIN``) are counted last, in
    one ``count_quads_many`` batch that runs them concurrently, one GIL
    release for all — after the point counts (:func:`_cheap_probe`: those
    the native layer answers without a scan), any of which may still end the
    block first. Returns the counts made."""
    before = len(counts)
    ordered = sorted(patterns, key=_pattern_static_rank)
    minimum = _MEMORY_SCAN_BATCH_MIN if store.in_memory else _SCAN_BATCH_MIN
    scans: list = []
    if len(patterns) >= minimum:
        scans = [
            pat
            for pat in ordered
            if not pat["unsatisfiable"]
            and tuple(pat["n3"]) not in counts
            and not _cheap_probe(store, pat["n3"])
        ]
        if len({tuple(pat["n3"]) for pat in scans}) < minimum:
            scans = []
    batched = {id(pat) for pat in scans}
    small: set = set()  # the variables of the patterns counted at most _PROBE_FANOUT rows
    for pat in ordered:
        if id(pat) in batched:
            continue
        if small.intersection(pat["varpos"]):
            pat["probe"] = True
            continue
        pat["estimate"] = _count_pattern(store, pat, counts)
        if pat["estimate"] == 0:
            return len(counts) - before
        if pat["estimate"] <= _PROBE_FANOUT:
            small.update(pat["varpos"])
    rest = []
    for pat in scans:
        if small.intersection(pat["varpos"]):
            pat["probe"] = True
        else:
            rest.append(pat)
    if len({tuple(pat["n3"]) for pat in rest}) >= minimum:
        keys = list(dict.fromkeys(tuple(pat["n3"]) for pat in rest))
        counts.update(zip(keys, _native(store).count_quads_many(keys, **_cancel_kw()), strict=True))
        for pat in rest:
            pat["estimate"] = counts[tuple(pat["n3"])]
    else:
        for pat in rest:
            pat["estimate"] = _count_pattern(store, pat, counts)
            if pat["estimate"] == 0:
                return len(counts) - before
    return len(counts) - before


def _narrowing(keep=None, limit=None) -> dict:
    """The keyword narrowing of a native count or match: a non-empty keep,
    a limit — only what is set."""
    narrowing: dict = {}
    if keep:
        narrowing["keep"] = keep
    if limit is not None:
        narrowing["limit"] = limit
    return narrowing


#: A batch call (``count_quads_many``/``match_codes_many``) pays a fixed cost
#: of native task scheduling before its probes run concurrently. That pays
#: off for probes that scan the store, but not for a handful of cheap point
#: probes, which plain calls in a loop answer in less time than the batch
#: costs. A probe is cheap when its subject is bound (a binary search in the
#: subject-ordered base) or a secondary index serves its predicate or object.
#: Cheap probes are batched only on a file-backed store and from this many on,
#: where overlapping their reads wins.
_CHEAP_BATCH_MIN = 256

#: Scanning probes batch from two on a file-backed store, whose scans
#: overlap their reads. An in-memory scan is pure CPU, so overlapping two of
#: them gains less than the batch costs; they batch from three on.
_SCAN_BATCH_MIN = 2
_MEMORY_SCAN_BATCH_MIN = 3

#: The ``lookahead`` of a batch call whose probes scan the store
#: (:func:`_probe_lookahead`): a scan per core, eight at most. The native
#: layer runs at most twice that many probes ahead of the caller's pulls.
_SCAN_LOOKAHEAD = min(os.cpu_count() or 1, 8)


def _cheap_probe(store, n3) -> bool:
    """Whether the native layer answers a probe without a scan."""
    return n3[0] is not None or bool(store._indexes) and (n3[1] is not None or n3[2] is not None)


def _probe_lookahead(store, batch_n3: Sequence[Sequence[str | None]]) -> int:
    """The ``lookahead`` of one batch call over these probes' quad patterns:
    the whole batch when every probe is a point lookup
    (:func:`_cheap_probe`), so all of them run at once, else
    :data:`_SCAN_LOOKAHEAD`."""
    if all(_cheap_probe(store, n3) for n3 in batch_n3):
        return len(batch_n3)
    return _SCAN_LOOKAHEAD


def _batched_probes(store, patterns) -> list[int]:
    """The indexes of the quad patterns :func:`_run_probes` answers through
    one batch call: those that scan, when there are enough of them
    (``_SCAN_BATCH_MIN``, ``_MEMORY_SCAN_BATCH_MIN``), or all of them when
    enough cheap ones are probing a file-backed store
    (``_CHEAP_BATCH_MIN``)."""
    minimum = _MEMORY_SCAN_BATCH_MIN if store.in_memory else _SCAN_BATCH_MIN
    if len(patterns) < minimum:
        return []
    batched = [i for i, n3 in enumerate(patterns) if not _cheap_probe(store, n3)]
    if not store.in_memory and len(patterns) - len(batched) >= _CHEAP_BATCH_MIN:
        return list(range(len(patterns)))
    return batched if len(batched) >= minimum else []


def _run_probes(store, probes, count: bool) -> list | Iterator:
    """Native answers to ``(n3, narrowing)`` probes (see :func:`_narrowing`),
    in order: a list of counts, or an iterator of code columns that makes
    each answer as it is pulled.

    The probes :func:`_batched_probes` picks go through one
    ``count_quads_many`` or ``match_codes_many`` call — every probe parsed
    before any runs, as many running at once on the native runtime as
    :func:`_probe_lookahead` names, a file-backed store overlapping their
    reads — and the rest through plain calls. The query's cancel token is
    read once: a batch call races it, a plain call races or checks it as
    :func:`_cancel_kw_for` decides, and without a token no call is given a
    keyword. The matches come from :func:`_probe_matches` under a token or
    when only some probes are batched; otherwise from a generator over the
    plain calls, or from the batch's own native iterator
    (:func:`_batch_answers`).
    """
    if not probes:
        return []
    native = _native(store)
    minimum = _MEMORY_SCAN_BATCH_MIN if store.in_memory else _SCAN_BATCH_MIN
    batched = _batched_probes(store, [n3 for n3, _ in probes]) if len(probes) >= minimum else []
    cancel = _cancel_kw()
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
                *n3, **narrowing, **_cancel_kw_for(cancel, store, n3, narrowing.get("limit"))
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
                *n3, **narrowing, **_cancel_kw_for(cancel, store, n3, limit)
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
    the query's cancel keyword (:func:`_cancel_kw`): the batch is given it,
    and a plain call races or checks the token as :func:`_cancel_kw_for`
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
                    *n3, **narrowing, **_cancel_kw_for(cancel, store, n3, limit)
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


def _match_resolved_pattern(store, pat, memo=None, limit=None) -> tuple[int, int]:
    """One native match for a resolved pattern, narrowed by its native keep:
    adds ``"cols"`` (the raw code columns) and ``"nrows"``. Returns the
    native calls made (0 when ``memo`` — keyed by :func:`_pattern_key` —
    already held the columns, or the pattern is unsatisfiable) and the time
    they took when tracing. ``limit`` takes only the first rows of the
    match, in base order — the order an unlimited match returns them where
    no index serves it (see :func:`_limit_reaches_match`); a limited match
    neither reads nor fills the memo.

    Only the columns are shared through the memo, as read-only views;
    ``varpos`` stays per pattern, since two triples matching the same rows
    still bind different variables.
    """
    then = time.perf_counter_ns() if _TRACE.get() is not None else 0
    if pat["unsatisfiable"]:
        pat["cols"], pat["nrows"] = None, 0
        return 0, 0
    nkeep = pat.get("nkeep")
    key = _pattern_key(pat) if nkeep else tuple(pat["n3"])
    cols = memo.get(key) if memo is not None and limit is None else None
    calls = 0
    if cols is None:
        native = _native(store)
        cancel = _cancel_kw_for(_cancel_kw(), store, pat["n3"], limit)
        if nkeep or limit is not None:
            cols = native.match_codes(*pat["n3"], **_narrowing(nkeep, limit), **cancel)
        else:
            cols = native.match_codes(*pat["n3"], **cancel)
        if cols is None:
            raise NotImplementedError
        if memo is not None and limit is None:
            memo[key] = cols
        calls = 1
    pat["cols"], pat["nrows"] = cols, len(cols[0])
    return calls, time.perf_counter_ns() - then if then else 0


def _limit_reaches_match(store, pat, residual) -> bool:
    """Whether a single pattern's rows are its native match's rows one for
    one, in the match's own order — so a LIMIT may stop the match itself.

    Nothing may be left to drop in Python (a residual conjunct, a repeated
    variable's equality), and the store must hold no secondary index: the
    native layer windows a match in base order, which is the order an
    unlimited match returns only when no index serves it — and rdflib, which
    reads the pattern through ``triples()``, sees that order.
    """
    return (
        not residual
        and not store._indexes
        and all(len(positions) == 1 for positions in pat["varpos"].values())
    )


def _join_incremental_bgp(store, patterns, pushed=None, stream=False) -> tuple[Relation, dict]:
    """Seed with the smallest pattern, then extend one connected pattern at
    a time.

    The patterns are counted before anything is matched, in static rank
    order up to the first zero — the scans together, in one concurrent
    native batch, when there are enough of them (:func:`_count_patterns`) —
    so a counted pattern that selects nothing ends the block before any
    match. A pattern sharing a variable with one of at most ``_PROBE_FANOUT``
    rows is left uncounted until its step, where it is counted only up to
    ``running * _PROBE_FANOUT + 1`` rows: a count that reaches the cap is
    large, and probes. The smallest counted pattern is matched and is the
    seed. Each further step takes the smallest pattern sharing a variable
    with the relation so far (the smallest of all for a cross product; an
    uncounted pattern ranks last). When the relation is at least
    ``_PROBE_FANOUT`` times smaller than the pattern's count, the step joins
    the pattern per row of the relation: it re-probes it natively per row
    (``_probe_join``) where each probe is a point lookup
    (:func:`_cheap_probe`), and where each would scan the store it matches
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

    A step over :data:`_PAIR_BUDGET` — a step whose pairs, counted once its
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
    cannot be (:func:`_chunkable`). Any other step over the budget puts its
    pattern back as it was planned (:func:`_put_back`) — its match left in
    the memo, a keep-scan's dropped with the keep that narrowed it — and the
    next step takes the first other pattern sharing a variable with the
    relation that provably fits (:func:`_fits`), which records the patterns
    it was reordered around; those are tried again after it. Once none
    fits, the patterns left are rdflib's to finish per row of the relation
    so far (``stats["rest"]``, :func:`_finish_with_rdflib`) where its
    ``evalBGP`` evaluates them as the block would: the relation goes
    straight to the emitter (``stream``, which the caller grants only in
    the active graph) and no FILTER conjunct was pushed into the patterns,
    which it would not apply. Otherwise the block goes back to rdflib
    (``NotImplementedError``).
    """
    counts: dict[tuple, int] = {}
    memo: dict[tuple, tuple] = {}
    stats = {
        "initial_calls": 0,
        "probe_calls": 0,
        "estimate_calls": 0,
        "native_ns": 0,
        "restriction_ns": 0,
        "rest": [],
    }

    tuples = pushed.tuples if pushed is not None else ()
    views = pushed.views if pushed is not None else {}

    traced = _TRACE.get() is not None
    then = time.perf_counter_ns() if traced else 0
    if any(pat["unsatisfiable"] for pat in patterns):
        # A literal bound into subject or predicate position, or a keep that
        # is an empty code column (IRIs the dictionary lacks, an empty ``IN``
        # list, lists with no IRI in common): the block is empty whatever the
        # other patterns count.
        for pat in patterns:
            pat["estimate"] = 0
    else:
        stats["estimate_calls"] += _count_patterns(store, patterns, counts)
    stats["native_ns"] += time.perf_counter_ns() - then if then else 0
    remaining = sorted(
        (pat for pat in patterns if "estimate" in pat or pat.get("probe")),
        key=lambda pat: (pat.get("estimate", math.inf), _pattern_static_rank(pat)),
    )
    # An empty block still names every variable of the patterns.
    if not remaining or remaining[0].get("estimate") == 0:
        schema: tuple = ()
        for pat in patterns:
            schema += tuple(v for v in pat["varpos"] if v not in schema)
        _trace_event(
            "bgp_plan_complete",
            pattern_count=len(patterns),
            execution_order=[],
            seed_pattern_index=None,
            seed_selection="empty_count",
            estimate_calls=stats["estimate_calls"],
            native_initial_match_count=0,
            native_probe_call_count=0,
            rest_pattern_indices=None,
        )
        return Relation.from_rows(schema, []), stats

    seed = remaining.pop(0)
    calls, native_ns = _match_resolved_pattern(store, seed, memo)
    stats["initial_calls"] += calls
    stats["native_ns"] += native_ns
    then = time.perf_counter_ns() if traced else 0
    _materialize_eager_restrictions(store, seed, ())
    stats["restriction_ns"] += time.perf_counter_ns() - then if then else 0
    schema, cols, rows = _pattern_body(seed)
    order = [seed.get("index")]
    _trace_event(
        "bgp_seed_complete",
        original_pattern_index=order[0],
        cardinality_source="matched",
        estimated_rows=seed["estimate"],
        matched_rows=seed["nrows"],
        native_call_count=calls,
        native_match_ns=native_ns,
    )
    cols, rows, tuples = _prune_early(store, schema, cols, rows, tuples, remaining, views)

    execution_index = 1
    # The patterns put back over the budget against the relation as it
    # stands: until a step completes, the next one takes another that fits.
    put_back: list[dict] = []
    pieces = None
    while remaining and (len(cols[0]) if cols is not None else len(rows)):
        running = len(cols[0]) if cols is not None else len(rows)
        connected = (pat for pat in remaining if any(v in schema for v in pat["varpos"]))
        if put_back:
            pat = next(
                (
                    pat
                    for pat in connected
                    if all(pat is not back for back in put_back)
                    and _fits(running, schema, cols, rows, pat, memo, store)
                ),
                None,
            )
            if pat is None:
                break
        else:
            pat = next(connected, remaining[0])
        position = next(i for i, other in enumerate(remaining) if other is pat)
        del remaining[position]
        shared = [v for v in pat["varpos"] if v in schema]
        step_then = time.perf_counter_ns() if traced else 0
        probe_calls = probe_ns = 0
        specialized = ()
        if tuples and pushed is not None:
            specialized = _specialize_tuple_conjuncts(
                store, pat, schema, cols, rows, tuples, pushed
            )
            tuples = tuple(c for c in tuples if id(c) not in pushed.applied)
        count_limit = None
        if "estimate" not in pat:
            # Next to a pattern of at most _PROBE_FANOUT rows, so not counted
            # when planned: counted here, only as far as the probe-or-match
            # decision needs; a capped answer means probe.
            count_limit = running * _PROBE_FANOUT + 1
            then = time.perf_counter_ns() if traced else 0
            pat["estimate"] = _count_pattern(store, pat, counts, limit=count_limit)
            # An answer at the cap is a lower bound of the count (_fits).
            pat["estimate_capped"] = pat["estimate"] >= count_limit
            stats["native_ns"] += time.perf_counter_ns() - then if then else 0
            stats["estimate_calls"] += 1
        estimate = pat["estimate"]
        deferred = pat.get("deferred_var_preds")
        nkeep = pat.get("nkeep")

        # Tier 1: the last step, when it shares one variable, streams
        # (_streamed_step) rather than going back to rdflib over the budget.
        streams = not remaining and len(shared) == 1
        pair_count = feed = None
        if shared and running * _PROBE_FANOUT < estimate:
            # Joined per row of the relation: by probes where each is a point
            # lookup, else by one keep-scan rather than a scan per row.
            cheap = _cheap_probe(store, _probe_shape(pat, shared))
            strategy = "probe" if cheap else "keep_scan"
        elif streams and _read_in_chunks(pat, running):
            strategy = "stream"
        else:
            strategy = "hash_columns"
        try:
            if strategy == "probe":
                source = "probed"
                probe_calls = running
                then = time.perf_counter_ns() if traced else 0
                try:
                    probed = _probe_join(
                        store,
                        schema,
                        _cols_to_rows(cols) if cols is not None else rows,
                        pat,
                        max_rows=_PAIR_BUDGET // 4,
                    )
                except _OverBudget as over:
                    # Only the probes sent before the tally passed count.
                    probed, probe_calls = None, over.calls
                probe_ns = time.perf_counter_ns() - then if then else 0
                stats["probe_calls"] += probe_calls
                stats["native_ns"] += probe_ns
                if probed is None:
                    # Over its tally: the step is planned again, as a keep-scan.
                    _trace_over_budget(execution_index, pat, strategy, None, running, estimate)
                    strategy = "keep_scan"
                else:
                    (schema, rows), cols = probed, None
                    match_rows = estimate
                    restricted_rows = pat.pop("_trace_probe_restricted_rows", len(rows))
            if strategy == "stream":
                source = "chunked"
                match_rows = restricted_rows = None
            elif strategy != "probe":
                source = "matched"
                if strategy == "keep_scan":
                    calls, native_ns = _keep_scan(store, pat, schema, cols, rows, memo)
                else:
                    calls, native_ns = _match_resolved_pattern(store, pat, memo)
                stats["initial_calls"] += calls
                stats["native_ns"] += native_ns
                then = time.perf_counter_ns() if traced else 0
                _materialize_eager_restrictions(store, pat, schema)
                stats["restriction_ns"] += time.perf_counter_ns() - then if then else 0
                match_rows = pat["_trace_match_rows"]
                restricted_rows = pat["nrows"]
                schema_b, cols_b, rows_b = _pattern_body(pat)
                joined = (
                    _join_columns(schema, cols, schema_b, cols_b)
                    if cols is not None and cols_b is not None
                    else None
                )
                if joined is not None:
                    schema, cols = joined
                    if running * restricted_rows > _PAIR_BUDGET:
                        # Past the product bound the join counted its pairs
                        # before building them, one joined row per pair.
                        pair_count = len(cols[0])
                else:
                    if strategy == "hash_columns":
                        strategy = "hash_rows"
                    rows_a = _cols_to_rows(cols) if cols is not None else rows
                    if rows_b is None:
                        rows_b = _cols_to_rows(cols_b)
                    pair_count = _row_step_pairs(schema, rows_a, schema_b, rows_b)
                    if pair_count is not None and pair_count > _PAIR_BUDGET:
                        raise _OverBudget(pair_count)
                    cols = None
                    schema, rows = _join(schema, rows_a, schema_b, rows_b)
        except _OverBudget as over:
            _trace_over_budget(execution_index, pat, strategy, over.pairs, running, estimate)
            stats["initial_calls"] += over.calls
            stats["native_ns"] += over.native_ns
            # A keep-scan that came back over the budget left its pattern
            # unread, so it streams from chunks, which only the native keep
            # narrows: a pattern that needs more goes back too.
            if not streams or "cols" not in pat and not _chunkable(pat):
                # Back where it was planned, as it was before this step: its
                # match left in the memo, its own restrictions and keep back,
                # and the tuple conjuncts the step specialized onto it
                # pending again.
                tuples = _put_back(pat, deferred, specialized, pushed, tuples, nkeep, memo)
                remaining.insert(position, pat)
                put_back.append(pat)
                continue
            strategy, pair_count = "stream", over.pairs

        if strategy == "stream":
            if cols is None:
                cols = tuple(U64Column([row[i] for row in rows]) for i in range(len(schema)))
            schema, pieces = _streamed_step(store, schema, cols, pat, memo)
            # _streamed_step reads an unread pattern out of the memo when the
            # memo holds its match: "cols" now says how the pattern is fed.
            if "cols" not in pat:
                feed, source = "chunks", "chunked"
                match_rows = restricted_rows = None
                stats["initial_calls"] += 1
            else:
                feed = "slices"
                if source == "chunked":
                    # The memo held the pattern's match: it was read whole.
                    source, match_rows = "matched", pat["nrows"]
                    restricted_rows = match_rows
        _trace_event(
            "bgp_join_step_complete",
            execution_index=execution_index,
            original_pattern_index=pat.get("index"),
            strategy=strategy,
            feed=feed,
            reordered_around=[back.get("index") for back in put_back] or None,
            cardinality_source=source,
            input_rows=running,
            estimated_rows=estimate,
            estimate_limit=count_limit,
            pair_count=pair_count,
            pattern_match_rows=match_rows,
            pattern_restricted_rows=restricted_rows,
            native_initial_match_count=int(source != "probed"),
            native_probe_call_count=probe_calls,
            native_probe_ns=probe_ns,
            output_rows=(
                None if pieces is not None else len(cols[0]) if cols is not None else len(rows)
            ),
            output_columns=len(schema),
            shared_variable_count=len(shared),
            elapsed_ns=time.perf_counter_ns() - step_then if step_then else 0,
        )
        put_back.clear()
        order.append(pat.get("index"))
        if pieces is not None:
            break
        cols, rows, tuples = _prune_early(store, schema, cols, rows, tuples, remaining, views)
        execution_index += 1

    if put_back:
        # No pattern left fits. rdflib's evalBGP finishes them per row as the
        # block would only where the rows go straight to the emitter and no
        # FILTER conjunct was pushed into the patterns: it would not apply it.
        if not stream or (pushed is not None and (pushed or pushed.tuples)):
            raise NotImplementedError
        stats["rest"] = [pat.get("index") for pat in remaining]
    else:
        # The relation ran empty before these patterns were joined: the
        # schema names their variables, which no column carries.
        for pat in remaining:
            schema += tuple(v for v in pat["varpos"] if v not in schema)
    _trace_event(
        "bgp_plan_complete",
        pattern_count=len(patterns),
        execution_order=order + [pat.get("index") for pat in remaining],
        seed_pattern_index=seed.get("index"),
        seed_selection="smallest_count",
        estimate_calls=stats["estimate_calls"],
        native_initial_match_count=stats["initial_calls"],
        native_probe_call_count=stats["probe_calls"],
        rest_pattern_indices=stats["rest"] or None,
    )
    if remaining and not put_back:
        return Relation.from_rows(schema, []), stats
    if pieces is not None:
        return Relation(schema, None, None, 0, pieces=pieces), stats
    if cols is not None:
        return Relation(schema, cols, None, len(cols[0])), stats
    return Relation.from_rows(schema, rows), stats


def _specialize_tuple_conjuncts(store, pat, schema, cols, rows, tuples, pushed) -> list:
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
    conjunct, which is marked applied: neither the mid-join prune nor the
    FILTER above evaluates it again.

    Returns what it specialized, ``(conjunct, variable, per-variable
    conjunct)`` each: a step that goes back over the budget undoes it
    (:func:`_put_back`).
    """
    specialized: list = []
    if not (filters._FAST_ENABLED and filters._NATIVE_ENABLED):
        return specialized
    single: dict = {}
    for conjunct in tuples:
        if id(conjunct) in pushed.applied:
            continue
        free = [v for v in conjunct.vars if v not in schema]
        if len(free) != 1 or free[0] not in pat["varpos"]:
            continue
        var = free[0]
        consts = dict(conjunct.consts)
        for other in conjunct.vars:
            if other == var:
                continue
            if other not in single:
                single[other] = _single_code(cols, rows, schema.index(other))
            if single[other] is None:
                break
            consts[other] = store._decode_term(single[other])
        else:
            proxy = filters.specialize(conjunct, var, consts)
            if proxy is None or proxy.native is None:
                continue
            deferred = pat.setdefault("deferred_var_preds", {})
            deferred[var] = [*deferred.get(var, ()), proxy]
            pushed.applied.add(id(conjunct))
            specialized.append((conjunct, var, proxy))
            if _TRACE.get() is not None:
                _trace_event(
                    "bgp_tuple_specialized",
                    original_pattern_index=pat.get("index"),
                    variable=str(var),
                    native_predicate=proxy.native.kind,
                )
    return specialized


def _single_code(cols, rows, index: int) -> int | None:
    """The one code a non-empty relation holds in column ``index`` in every
    row, or ``None`` when it holds several."""
    if cols is not None:
        distinct = _native_column(cols[index]).distinct()
        return memoryview(distinct).cast("Q")[0] if len(distinct) == 1 else None
    first = rows[0][index]
    return first if all(row[index] == first for row in rows) else None


def _prune_early(store, schema, cols, rows, pending, remaining, views):
    """Prune a BGP's running relation with the pending tuple conjuncts its
    schema covers, when that pays. Evaluating a conjunct costs more per row
    than a join step spends on the row, so pruning pays only by letting a
    remaining pattern be probed instead of matched whole — the planner's own
    test, `survivors * _PROBE_FANOUT < estimate`. The rows stream, evaluation
    stops once too many survive for that, and the conjunct is left to the
    FILTER above the BGP, which streams it over a columnar body, so a LIMIT
    stops it early.

    ``views`` is the parsed-term cache it shares with that FILTER. Returns
    ``(cols, rows, pending)``: the body, pruned or as it was, and the
    conjuncts not yet evaluated.
    """
    ready, waiting = [], []
    for conjunct in pending:
        (ready if all(v in schema for v in conjunct.vars) else waiting).append(conjunct)
    if not ready:
        return cols, rows, pending
    probeable = max(
        (
            pat["estimate"]
            for pat in remaining
            if "estimate" in pat and any(v in schema for v in pat["varpos"])
        ),
        default=0,
    )
    if _PROBE_FANOUT >= probeable:  # not even one survivor would be probed
        return cols, rows, pending
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    predicates = tuple(
        filters.tuple_fast_reject_predicate(
            store, conjunct, [schema.index(variable) for variable in conjunct.vars], views
        )
        for conjunct in ready
    )
    input_rows = len(rows) if cols is None else len(cols[0])
    kept = []
    evaluated = 0
    deferred = False
    for row in rows if cols is None else _column_rows(cols, input_rows):
        evaluated += 1
        if all(predicate(row) for predicate in predicates):
            kept.append(row)
            if len(kept) * _PROBE_FANOUT >= probeable:
                deferred = True
                break
    _trace_event(
        "bgp_prune_complete",
        conjunct_count=len(ready),
        probe_estimate=probeable,
        input_rows=input_rows,
        evaluated_rows=evaluated,
        output_rows=input_rows if deferred else len(kept),
        pruned=not deferred,
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
    )
    if deferred:
        return cols, rows, tuple(waiting)
    return None, kept, tuple(waiting)


def _plan_pattern_side(ctx, store, triple, scope, left: Relation, left_rows, var_preds):
    """The one-pattern right side of a lazy join or an OPTIONAL, counted
    before it is matched.

    Returns ``(pat, estimate, probe)``. The count stops at
    ``len(left_rows) * _PROBE_FANOUT + 1``, as far as the decision needs:
    ``estimate`` is exact below that and a lower bound at it. When the
    pattern shares a variable with the left relation and the relation is at
    least ``_PROBE_FANOUT`` times smaller than the pattern's count, ``probe``
    is true and ``pat`` is left unmatched — the caller re-probes it per left
    row, and the per-variable restrictions over its free variables travel
    with it. Otherwise ``pat`` is matched, restricted on its free variables,
    and ready for a hash join. A variable the left side binds was restricted
    there.
    """
    pat = _pattern_terms(ctx, store, *triple, scope=scope)
    shared = [v for v in pat["varpos"] if v in left.schema]
    free_preds = (
        {v: var_preds[v] for v in pat["varpos"] if v in var_preds and v not in left.schema}
        if var_preds
        else None
    )
    if free_preds:
        free_preds = _push_native_restrictions(store, pat, free_preds)
    estimate = _count_pattern(store, pat, {}, limit=len(left_rows) * _PROBE_FANOUT + 1)
    if shared and len(left_rows) * _PROBE_FANOUT < estimate:
        _shared_key_ok(left, Relation((), None, [], 0), shared)
        if free_preds:
            pat["deferred_var_preds"] = free_preds
        return pat, estimate, True
    _match_resolved_pattern(store, pat)
    if free_preds:
        _restrict_pattern(store, pat, free_preds)
    return pat, estimate, False


def _pattern_terms(ctx, store, s, p, o, scope) -> dict:
    """Resolve one triple pattern against the context, without matching.

    Returns ``{"n3", "varpos", "unsatisfiable"}``: the positions as
    N-Triples strings (``None`` where variable), a map from each
    variable-like term to the position(s) it occupies, and whether the
    pattern can match at all.

    ``n3`` is a *quad* pattern: its fourth position comes from ``scope`` —
    the graph the context has made active, the graph a ``GRAPH <iri>`` names,
    or the wildcard when a ``GRAPH ?g`` binds the graph instead. In that last
    case the graph variable takes position 3 in ``varpos``, so it is an
    ordinary variable of the relation, bound from the match's fourth column.
    """
    for term in (s, p, o):
        # RDF-star quoted triples (or anything else exotic) in a pattern
        # position: not supported here, use the default evaluator.
        if not isinstance(term, (*_VAR_LIKE, URIRef, Literal)):
            raise NotImplementedError
    rs, rp, ro = ctx[s], ctx[p], ctx[o]

    # rdflib joins can propagate a literal into subject or predicate
    # position; that pattern is unsatisfiable, not an error. So is one that
    # holds an IRI vortex-rdf's parser rejects, which is no term of the file.
    unsatisfiable = (
        (rs is not None and not isinstance(rs, (URIRef, BNode)))
        or (rp is not None and not isinstance(rp, URIRef))
        or unparseable_iri(rs)
        or unparseable_iri(rp)
        or unparseable_iri(ro)
    )

    varpos: dict = {}
    for idx, (term, value) in enumerate(zip((s, p, o), (rs, rp, ro), strict=True)):
        if value is None:
            varpos.setdefault(term, []).append(idx)

    if scope.var is not None:
        varpos.setdefault(scope.var, []).append(3)

    n3 = [store._node_to_n3(v) for v in (rs, rp, ro)]
    n3.append(scope.n3)
    pat = {"n3": n3, "varpos": varpos, "unsatisfiable": unsatisfiable}
    if scope.var is not None:
        _exclude_default_graph(store, pat)
    return pat


def _exclude_default_graph(store, pat) -> None:
    """Drop the default graph's rows from a pattern a ``GRAPH ?g`` scopes,
    inside its native match.

    A ``GRAPH`` clause ranges over the *named* graphs, and there is no "any
    named graph" pattern — but the default graph's empty name sorts before
    every term, so its code is 0 and the named graphs are the code range
    above it: a keep on the graph column, which matches, probes and exact
    counts apply. A file with no default-graph rows has no such code and
    needs no restriction at all.
    """
    default = store._default_graph_code()
    if default is None:
        return
    if default != 0:
        raise NotImplementedError  # not a dictionary in byte order
    _set_native_keep(pat, 3, (1, len(store._dict)))


def _set_native_keep(pat, position: int, keep) -> None:
    """Narrow the pattern's native match at ``position`` by ``keep`` (a code
    range or an ascending code column), on top of any keep already there.
    The pattern gets a new keep mapping, so one taken before stays whole."""
    nkeep = dict(pat.get("nkeep") or {})
    if position in nkeep:
        keep = filters.intersect_keeps([nkeep[position], keep])
    nkeep[position] = keep
    pat["nkeep"] = nkeep


def _push_native_restrictions(store, pat, var_preds) -> dict:
    """Move the conjuncts vortex-rdf answers with a keep — kind tests and IRI
    equality (:func:`filters.native_restriction`) — into the pattern's native
    keep, at every position their variable takes; returns the residual
    conjuncts per variable, evaluated over the codes the pattern brings
    (:func:`_restrict_pattern`, :func:`_probe_join`).

    The keep is exact, so it may narrow a variable the relation will already
    bind when the pattern joins: the rows it drops could not have joined. A
    code column left empty (IRIs the dictionary lacks, an empty ``IN`` list,
    lists with no IRI in common) makes the pattern unsatisfiable, so nothing
    is counted or matched for it.
    """
    residual: dict = {}
    for var, positions in pat["varpos"].items():
        conjuncts = var_preds.get(var)
        if not conjuncts:
            continue
        keep, rest = filters.native_restriction(store, conjuncts)
        if keep is not None:
            for position in positions:
                _set_native_keep(pat, position, keep)
            if _TRACE.get() is not None:
                _trace_event(
                    "bgp_native_restriction",
                    original_pattern_index=pat.get("index"),
                    variable=str(var),
                    pushed_predicate_count=len(conjuncts) - len(rest),
                    residual_predicate_count=len(rest),
                    keep=_keep_summary({positions[0]: keep}),
                )
        if rest:
            residual[var] = rest
    if any(not isinstance(k, tuple) and not len(k) for k in (pat.get("nkeep") or {}).values()):
        pat["unsatisfiable"] = True
    return residual


def _native_column(col) -> U64Column:
    """A relation column as the native column behind it: a view over a
    ``U64Column`` is that column, anything else (an ``array('Q')``) is
    copied into one."""
    if isinstance(col, U64Column):
        return col
    owner = getattr(col, "obj", None)
    if isinstance(owner, U64Column) and len(owner) == len(col):
        return owner
    return U64Column(col)


def _distinct_codes(col) -> list:
    """A column's distinct codes in first-seen order, by the native kernel."""
    return memoryview(_native_column(col).distinct()).cast("Q").tolist()


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


def _restrict_pattern(store, pat, var_preds) -> None:
    """Evaluate per-variable conjuncts over the distinct codes of a matched
    pattern's columns (:func:`filters.evaluate_column`: vortex-rdf answers the
    ones with a native shape over exactly those codes) and compact the match
    to the rows that pass (:func:`_compact_pattern`), so it stays columnar and
    its real size is what a join sees."""
    if pat["nrows"] == 0:
        return
    traced = _TRACE.get() is not None
    keep: dict = {}
    input_rows = pat["nrows"]
    records = []
    for var, positions in pat["varpos"].items():
        conjuncts = var_preds.get(var)
        if not conjuncts:
            continue
        started = time.perf_counter_ns() if traced else 0
        values = set(_distinct_codes(pat["cols"][positions[0]]))
        distinct_binding_count = len(values)
        allowed = filters.evaluate_column(store, conjuncts, values)
        if len(allowed) < distinct_binding_count:
            keep[var] = allowed
        if traced:
            records.append(
                {
                    "variable": str(var),
                    "mode": "match",
                    **_restriction_counts(conjuncts),
                    "distinct_binding_count": distinct_binding_count,
                    "allowed_binding_count": len(allowed),
                    "elapsed_ns": time.perf_counter_ns() - started,
                }
            )
    if keep:
        _compact_pattern(pat, keep)
    for record in records:
        _trace_event(
            "bgp_restriction_complete",
            original_pattern_index=pat.get("index"),
            input_rows=input_rows,
            output_rows=pat["nrows"],
            **record,
        )


def _compact_pattern(pat, keep: dict[Variable, set[int]]) -> None:
    """Keep only the matched rows whose code at each variable of ``keep`` is
    in that variable's allowed set, with the native kernels: ``join_indices``
    pairs the variable's column with the allowed codes, an ascending column of
    unique codes, so a row appears at most once and the rows come in the
    nested-loop order of the join, which is row order; ``take`` then gathers
    every column a variable reads by those rows. A repeated variable is tested
    at its first position, as ``_materialize`` reads it. The columns of
    constant positions are dropped (``None``): nothing reads them."""
    varpos = pat["varpos"]
    positions = sorted({idx for pos in varpos.values() for idx in pos})
    if not positions:
        return
    columns = {idx: _native_column(pat["cols"][idx]) for idx in positions}
    for var, allowed in keep.items():
        if not allowed:
            columns = {idx: U64Column([]) for idx in positions}
            break
        rows, _ = columns[varpos[var][0]].join_indices(U64Column(sorted(allowed)))
        if len(rows) != len(columns[positions[0]]):
            columns = {idx: column.take(rows) for idx, column in columns.items()}
    pat["cols"] = tuple(columns.get(idx) for idx in range(4))
    pat["nrows"] = len(columns[positions[0]])


def _rel_from_pattern(pat) -> Relation:
    """A single matched pattern as a relation — columnar when it can be.

    The zero-copy views are kept as the body; nothing is copied into Python
    objects until rows are consumed. Repeated variables (``?x :p ?x``) and
    ground patterns go through ``_materialize`` instead.
    """
    varpos = pat["varpos"]
    if pat["nrows"] == 0 or not varpos or any(len(pos) > 1 for pos in varpos.values()):
        return Relation.from_rows(*_materialize(pat))
    schema = tuple(varpos)
    cols = tuple(memoryview(pat["cols"][pos[0]]).cast("Q") for pos in varpos.values())
    return Relation(schema, cols, None, pat["nrows"])


def _materialize(pat):
    """A matched pattern as ``(schema, rows)`` of u64 code tuples.

    Same-variable repeats (e.g. ``?x :p ?x``) become a row filter; a repeated
    variable keeps its first position. An empty match still names its
    variables, so the operators above can pad or project by schema.
    """
    varpos = pat["varpos"]
    schema = tuple(varpos)
    if pat["nrows"] == 0:
        return schema, []
    if not schema:
        # Fully ground pattern: no bindings, one (empty) row per match, so a
        # non-matching ground pattern still eliminates all solutions.
        return (), [()] * pat["nrows"]

    needed = sorted({idx for pos in varpos.values() for idx in pos})
    views = {idx: memoryview(pat["cols"][idx]).cast("Q").tolist() for idx in needed}
    eq_checks = [(pos[0], later) for pos in varpos.values() for later in pos[1:]]
    if eq_checks:
        keep = [
            i for i in range(pat["nrows"]) if all(views[a][i] == views[b][i] for a, b in eq_checks)
        ]
        views = {idx: [column[i] for i in keep] for idx, column in views.items()}
    return schema, list(zip(*(views[pos[0]] for pos in varpos.values()), strict=True))


def _pattern_body(pat) -> tuple:
    """The matched pattern as ``(schema, cols, rows)`` — columnar when it
    can be: single-position variables (``_materialize`` turns repeats into a
    row filter)."""
    varpos = pat["varpos"]
    if pat["nrows"] and varpos and all(len(pos) == 1 for pos in varpos.values()):
        cols = tuple(memoryview(pat["cols"][pos[0]]).cast("Q") for pos in varpos.values())
        return tuple(varpos), cols, None
    schema, rows = _materialize(pat)
    return schema, None, rows


def _cols_to_rows(cols) -> list:
    """Columnar body to row tuples, in one zip."""
    return list(zip(*(col.tolist() for col in cols), strict=True))


def _pair_count(left: U64Column, right: U64Column) -> int | None:
    """The row pairs a join of the two key columns makes, counted natively
    (``join_count``, no pair built) — or ``None``, uncounted, when the
    columns' product is within :data:`_PAIR_BUDGET`."""
    if len(left) * len(right) <= _PAIR_BUDGET:
        return None
    return left.join_count(right)


def _row_step_pairs(schema_a, rows_a, schema_b, rows_b) -> int | None:
    """The pairs :func:`_join` makes of two row relations, as
    :func:`_pair_count` counts them: ``None`` while their product is within
    :data:`_PAIR_BUDGET`; past it, the pairs of the first shared variable's
    codes, copied into native columns (with several shared variables an
    upper bound of the joined rows), or the product when none is shared."""
    product = len(rows_a) * len(rows_b)
    if product <= _PAIR_BUDGET:
        return None
    shared = [v for v in schema_b if v in schema_a]
    if not shared:
        return product
    ia, ib = schema_a.index(shared[0]), schema_b.index(shared[0])
    return _pair_count(
        U64Column([row[ia] for row in rows_a]), U64Column([row[ib] for row in rows_b])
    )


def _join_columns(schema_a, cols_a, schema_b, cols_b):
    """Hash join of two columnar relations sharing exactly one variable,
    columns out — the rows exist only when a consumer materializes them.
    ``None`` when the shape is not its case (several or no shared
    variables); the row join handles those.

    Native kernels, GIL released: ``join_indices`` pairs the rows of the two
    key columns in nested-loop order (the left rows in order, each with its
    right matches in theirs) and ``take`` gathers every output column by
    those indices. Past the product bound the pairs are counted first
    (:func:`_pair_count`): a join over :data:`_PAIR_BUDGET` raises
    :class:`_OverBudget` with its count before a pair is built, and one
    within it is built with the budget as ``join_indices``' cap.
    """
    shared = [v for v in schema_b if v in schema_a]
    if len(shared) != 1:
        return None
    ia = schema_a.index(shared[0])
    ib = schema_b.index(shared[0])
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)
    left = [_native_column(col) for col in cols_a]
    key = _native_column(cols_b[ib])
    pairs = _pair_count(left[ia], key)
    if pairs is None:
        a_idx, b_idx = left[ia].join_indices(key)
    elif pairs <= _PAIR_BUDGET:
        a_idx, b_idx = left[ia].join_indices(key, max_pairs=_PAIR_BUDGET)
    else:
        raise _OverBudget(pairs)
    out = [col.take(a_idx) for col in left]
    out += [_native_column(cols_b[i]).take(b_idx) for i in keep_b]
    return schema, tuple(memoryview(col).cast("Q") for col in out)


def _slices(cols: Sequence[U64Column], nrows: int) -> Iterator[tuple[U64Column, ...]]:
    """The columns in consecutive zero-copy slices (``U64Column.slice``) of
    at most ``_PIECE_ROWS`` rows."""
    for start in range(0, nrows, _PIECE_ROWS):
        stop = min(start + _PIECE_ROWS, nrows)
        yield tuple(col.slice(start, stop) for col in cols)


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
    part counts more than :data:`_PAIR_BUDGET` pairs and holds more than one
    row it is split in halves; a single row is gathered whatever its count,
    its pairs at most the grouped rows, which are held already. A part that
    counts no pair is skipped. A part is gathered by ``CodeGroups.gather``:
    per pair, in nested-loop order, its other columns at the fed row, then
    ``grouped`` at the grouped row — permuted by ``order`` into the step's
    schema. Each part checks the query's cancel token before it is counted
    (:func:`_check_cancelled`), so a cancel stops the pieces at the next
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
                _check_cancelled()
                part = parts.pop()
                column = part[key]
                length = len(column)
                pairs = groups.count(column)
                if pairs > _PAIR_BUDGET and length > 1:
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
    finally:
        if resumed:
            elapsed_ns += time.perf_counter_ns() - resumed
        close = getattr(fed, "close", None)
        if close is not None:
            close()
        _trace_event(
            "bgp_stream_complete", pieces=pieces, output_rows=output_rows, elapsed_ns=elapsed_ns
        )


def _streamed_step(
    store, schema: tuple, cols: tuple, pat: dict, memo: dict
) -> tuple[tuple, Iterator[tuple[memoryview, ...]]]:
    """The join of a relation (``schema``, a column per entry) with ``pat``,
    sharing one variable, as pieces gathered while they are pulled: tier 1,
    the last step of a BGP over :data:`_PAIR_BUDGET` or with a pattern too
    large to read whole (:func:`_read_in_chunks`).

    A pattern whose match is read — by this step, or into ``memo`` by
    another — and the relation are two held sides: the one with fewer rows
    is grouped on its key column (``U64Column.groups``), and the other is fed
    through the groups in slices (:func:`_slices`). A pattern left unread is
    fed through the relation's groups in chunks of at most ``_PIECE_ROWS``
    rows of ``match_code_chunks`` narrowed by its native keep, opened here,
    so a store that cannot serve them raises ``NotImplementedError`` while
    the query is planned. Returns the step's schema — the relation's, then
    the pattern's new variables, as :func:`_join_columns` has it — and the
    pieces (:func:`_gather_pieces`)."""
    varpos = pat["varpos"]
    key_var = next(v for v in varpos if v in schema)
    step_schema = schema + tuple(v for v in varpos if v not in schema)
    relation = [_native_column(col) for col in cols]
    if "cols" not in pat and _pattern_key(pat) in memo:
        _match_resolved_pattern(store, pat, memo)
    pattern_schema = tuple(varpos)
    pattern = None
    if "cols" in pat:
        _, pattern_cols, pattern_rows = _pattern_body(pat)
        if pattern_cols is not None:
            pattern = [_native_column(col) for col in pattern_cols]
        else:
            pattern = [
                U64Column([row[i] for row in pattern_rows]) for i in range(len(pattern_schema))
            ]
    if pattern is not None and len(pattern[0]) < len(relation[0]):
        grouped, grouped_schema = pattern, pattern_schema
        fed, fed_schema = _slices(relation, len(relation[0])), schema
    else:
        grouped, grouped_schema, fed_schema = relation, schema, pattern_schema
        if pattern is not None:
            fed = _slices(pattern, len(pattern[0]))
        else:
            fed = _native(store).match_code_chunks(
                *pat["n3"],
                columns=[("s", "p", "o", "g")[varpos[v][0]] for v in pattern_schema],
                batch_rows=_PIECE_ROWS,
                **_narrowing(pat.get("nkeep")),
                **_cancel_kw(),
            )
            if fed is None:
                raise NotImplementedError
    groups = grouped[grouped_schema.index(key_var)].groups()
    gathered_schema = [v for v in fed_schema if v != key_var] + list(grouped_schema)
    order = [gathered_schema.index(v) for v in step_schema]
    pieces = _gather_pieces(groups, fed, fed_schema.index(key_var), grouped, order)
    return step_schema, pieces


def _read_in_chunks(pat, running: int) -> bool:
    """Whether a last hash step streams ``pat`` from chunks instead of
    matching it whole: its count is more than :data:`_PAIR_BUDGET` rows and
    more than the relation's ``running`` rows, and its chunks are enough to
    join it (:func:`_chunkable`)."""
    return pat["estimate"] > max(_PAIR_BUDGET, running) and _chunkable(pat)


def _chunkable(pat) -> bool:
    """Whether a step can join ``pat`` from chunks of its match: nothing needs
    its whole columns — no deferred restriction (:func:`_restrict_pattern`) —
    and no repeated variable asks for a test across a row's columns, which a
    chunk does not make (:func:`_materialize`)."""
    return not pat.get("deferred_var_preds") and all(
        len(positions) == 1 for positions in pat["varpos"].values()
    )


def _fits(
    running: int, schema: tuple, cols: tuple | None, rows: list | None, pat: dict, memo: dict, store
) -> bool:
    """Whether joining ``pat`` into the relation — ``running`` rows over
    ``schema``, its body ``cols`` or ``rows`` — provably stays within
    :data:`_PAIR_BUDGET`, from what is held already: nothing is counted or
    matched here.

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
    estimate = pat.get("estimate")
    if estimate is None:
        return False
    if not pat.get("estimate_capped") and running * estimate <= _PAIR_BUDGET:
        return True
    if running * _PROBE_FANOUT < estimate:
        return False
    match = memo.get(_pattern_key(pat))
    if match is None:
        return False
    var = next(v for v in pat["varpos"] if v in schema)
    index = schema.index(var)
    if cols is not None:
        key = _native_column(cols[index])
    else:
        assert rows is not None
        key = U64Column([row[index] for row in rows])
    return key.join_count(match[pat["varpos"][var][0]]) <= _PAIR_BUDGET


def _put_back(
    pat: dict,
    deferred: dict | None,
    specialized: Sequence[tuple],
    pushed,
    tuples: tuple,
    nkeep: dict | None,
    memo: dict,
) -> tuple:
    """Undo what a join step did to ``pat`` before it went over the budget,
    so a later step takes it as planned. It is unread again, with its match
    left in the memo, and its native keep is ``nkeep``, the keep it was
    planned with: a keep-scan's narrower keep (:func:`_keep_scan`) goes, and
    the match memoized under it leaves the memo with it — no step reads it
    again, and it would hold the match's columns until the plan ends. Its
    deferred restrictions — ``deferred``, which reading it consumed — are
    back for the step that reads it, less the per-variable conjuncts this
    step specialized onto it from tuple conjuncts (``specialized``,
    :func:`_specialize_tuple_conjuncts`). Those
    tuple conjuncts are no longer applied: they are pending again among
    ``tuples``, in the FILTER's order, for the next step whose pattern binds
    their variable, or for the FILTER above. Returns the pending tuple
    conjuncts."""
    if pat.get("nkeep") is not nkeep:
        memo.pop(_pattern_key(pat), None)
        if nkeep is None:
            del pat["nkeep"]
        else:
            pat["nkeep"] = nkeep
    for key in ("cols", "nrows", "_trace_match_rows"):
        pat.pop(key, None)
    if specialized:
        assert deferred is not None and pushed is not None
        for conjunct, var, proxy in specialized:
            pushed.applied.discard(id(conjunct))
            kept = [other for other in deferred[var] if other is not proxy]
            if kept:
                deferred[var] = kept
            else:
                del deferred[var]
        pending = {id(conjunct) for conjunct in tuples}
        pending.update(id(conjunct) for conjunct, _, _ in specialized)
        tuples = tuple(conjunct for conjunct in pushed.tuples if id(conjunct) in pending)
    if deferred:
        pat["deferred_var_preds"] = deferred
    else:
        pat.pop("deferred_var_preds", None)
    return tuples


def _probe_shape(pat: dict, shared: Sequence) -> list:
    """``pat``'s quad pattern as each probe of a row binds it: a placeholder
    at every position a ``shared`` variable takes, where the row's code goes.
    It tells :func:`_cheap_probe` whether the probes would scan."""
    shape = list(pat["n3"])
    for var in shared:
        for position in pat["varpos"][var]:
            shape[position] = ""
    return shape


def _keep_scan(
    store, pat: dict, schema: tuple, cols: tuple | None, rows: list | None, memo: dict
) -> tuple[int, int]:
    """Match ``pat`` once, narrowed to the codes the relation — ``schema``,
    its body ``cols`` or ``rows`` — binds its shared variables to: a step
    whose probes would each scan the store scans it once, and joins the
    match as a hash step does.

    Each shared variable's distinct codes, ascending, become the native keep
    at every position it takes, on top of any keep already there
    (:func:`_set_native_keep`): the match leaves out only rows that no row of
    the relation joins, and the join pairs the others, quad by quad, as the
    probes would have. The match goes through :func:`_match_resolved_pattern`
    and is memoized under that keep. A pattern whose count may pass
    :data:`_PAIR_BUDGET` — one over it, or a count stopped at its cap, a
    lower bound — is matched only up to one row past the budget: a match
    that reaches it is over the budget and is dropped, the pattern left
    unread, raising :class:`_OverBudget` with the call it made. Returns the
    native calls made and their time, as :func:`_match_resolved_pattern`
    does."""
    for var, positions in pat["varpos"].items():
        if var not in schema:
            continue
        index = schema.index(var)
        if cols is not None:
            codes = _distinct_codes(cols[index])
        else:
            assert rows is not None
            codes = {row[index] for row in rows}
        keep = U64Column(sorted(codes))
        for position in positions:
            _set_native_keep(pat, position, keep)
    if not pat.get("estimate_capped") and pat["estimate"] <= _PAIR_BUDGET:
        return _match_resolved_pattern(store, pat, memo)
    limit = _PAIR_BUDGET + 1
    calls, native_ns = _match_resolved_pattern(store, pat, memo, limit=limit)
    if pat["nrows"] == limit:
        del pat["cols"], pat["nrows"]
        raise _OverBudget(None, calls, native_ns)
    return calls, native_ns


def _trace_over_budget(
    execution_index: int, pat: dict, strategy: str, pairs: int | None, running: int, estimate: int
) -> None:
    """Trace a join step over :data:`_PAIR_BUDGET` as ``bgp_over_budget``:
    its place in the plan, its strategy, its pair count (``None`` where it
    stopped on its rows), the relation's rows and the pattern's count."""
    _trace_event(
        "bgp_over_budget",
        execution_index=execution_index,
        original_pattern_index=pat.get("index"),
        strategy=strategy,
        pair_count=pairs,
        input_rows=running,
        estimated_rows=estimate,
    )


def _materialize_eager_restrictions(store, pat, bound) -> None:
    """Apply a matched pattern's deferred per-variable restrictions — over
    the variables not in ``bound``, the relation it is about to join —
    compacting it natively to the rows that pass, so its real size, not the
    match's, is what the join sees."""
    input_rows = pat["nrows"]
    pending = pat.pop("deferred_var_preds", None)
    if pending:
        pending = {v: conjuncts for v, conjuncts in pending.items() if v not in bound}
    if pending:
        _restrict_pattern(store, pat, pending)
    pat["_trace_match_rows"] = input_rows


def _join_patterns(store, patterns, stats=None, pushed=None, stream=False) -> Relation:
    """Join a BGP through the incremental planner.

    Keep this function as the observable BGP join boundary: tests and
    diagnostics wrap it. _solve_bgp also passes an optional dictionary to
    receive physical native-call and timing counters, the filters a
    FILTER above pushed down (:class:`_Pushed`), whose tuple conjuncts may
    prune the relation mid-join, and ``stream``, which lets rdflib finish
    the patterns past a join over the pair budget (``stats["rest"]``).
    """
    rel, measured = _join_incremental_bgp(store, patterns, pushed, stream)
    if stats is not None:
        stats.update(measured)
    return rel


#: Probes per native batch call: bounds the columns one batch holds at once.
_PROBE_BATCH = 4096


def _probe_join(
    store, schema, rows, pat, keep_unmatched=False, row_preds=(), max_rows: int | None = None
):
    """Join the relation against a pattern by re-matching it per binding.

    The adaptive half of the join strategy: when the running relation is far
    smaller than the pattern's match (``_PROBE_FANOUT``), materializing
    thousands of pattern rows to hash-join against a handful is the wrong
    trade. Substituting each relation row's codes into the pattern and
    re-matching natively keeps the work proportional to the small side. The
    probes go to the native layer in batches (``match_codes_many``, or
    ``count_quads_many`` for an existence probe, a pattern with no free
    position): one call each, every probe of a batch running concurrently — a
    file-backed store overlaps their reads. An existence probe keeps its row
    once per quad it matches: a fixed graph (a named graph, the default graph
    alone, a bound ``GRAPH ?g``) holds at most one, so the count stops at one
    row; the union default graph holds a triple once per graph that has it,
    so the count is whole. The pattern's native keep narrows each probe on the
    positions it leaves free (a code range inside the native match, a code
    column over the rows it returns), as do ``row_preds`` over the extended
    row; with ``keep_unmatched`` a row without a continuation is kept, padded
    with None (a left join). Each batch's answers are pulled as the join
    reaches them and released when the batch ends, however it ends. Once a
    probe's rows take the join past ``max_rows``, it drops the rows it made
    and the batch it is pulling, which aborts the probes still in flight,
    and raises :class:`_OverBudget` with the probes it sent.
    """
    bound = [(schema.index(v), pos) for v, pos in pat["varpos"].items() if v in schema]
    free = {v: pos for v, pos in pat["varpos"].items() if v not in schema}
    eq_checks = [(pos[0], later) for pos in free.values() for later in pos[1:]]
    out_positions = [pos[0] for pos in free.values()]
    needed = sorted({idx for pos in free.values() for idx in pos})
    # A code range rides every probe; a code column is tested on the rows a
    # probe returns, since the native layer would re-read it per probe.
    probe_keep, tests = {}, []
    for position, spec in (pat.get("nkeep") or {}).items():
        if position in needed:
            if isinstance(spec, tuple):
                probe_keep[position] = spec
            else:
                tests.append((position, filters.keep_member(spec)))
    narrowing = _narrowing(probe_keep, limit=None if free else 1)
    whole = _narrowing(probe_keep)
    patterns = _substituted_patterns(store, rows, pat, bound)

    pad = (None,) * len(free)
    out = []
    for start in range(0, len(rows), _PROBE_BATCH):
        batch = range(start, min(start + _PROBE_BATCH, len(rows)))
        # An existence probe counts whole where its graph position is open
        # (the union default graph), capped where it is fixed — a `GRAPH ?g`
        # the row binds is substituted, so it is fixed.
        probes = [
            (patterns[i], whole if patterns[i][3] is None else narrowing)
            for i in batch
            if patterns[i] is not None
        ]
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
    pending = pat.pop("deferred_var_preds", None)
    if pending and out:
        restriction_input_rows = len(out)
        for var, conjuncts in pending.items():
            if var not in free:
                continue
            position = combined_schema.index(var)
            values = {row[position] for row in out}
            started = time.perf_counter_ns() if _TRACE.get() is not None else 0
            allowed = filters.evaluate_column(store, conjuncts, values)
            elapsed_ns = time.perf_counter_ns() - started if started else 0
            out = [row for row in out if row[position] in allowed]
            _trace_event(
                "bgp_restriction_complete",
                original_pattern_index=pat.get("index"),
                variable=str(var),
                mode="probe",
                input_rows=restriction_input_rows,
                output_rows=len(out),
                **_restriction_counts(conjuncts),
                distinct_binding_count=len(values),
                allowed_binding_count=len(allowed),
                elapsed_ns=elapsed_ns,
            )
            restriction_input_rows = len(out)
        pat["_trace_probe_restricted_rows"] = len(out)
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


def _join(schema_a, rows_a, schema_b, rows_b):
    """Hash join of two code-space relations on their shared variables.

    Keys come from :func:`~operator.itemgetter`: a bare ``int`` code for one
    shared variable (the common case — no per-row key tuple), a tuple for
    several.
    """
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)

    if not shared:
        return schema, [ra + rb for ra in rows_a for rb in rows_b]

    key_a = itemgetter(*(schema_a.index(v) for v in shared))
    key_b = itemgetter(*(schema_b.index(v) for v in shared))
    # The build-side encoding of _join_columns, inlined once per tail shape.
    table: dict = {}
    if not keep_b:
        for rb in rows_b:
            key = key_b(rb)
            previous = table.get(key)
            if previous is None:
                table[key] = ()
            elif isinstance(previous, list):
                previous.append(())
            else:
                table[key] = [previous, ()]
    elif len(keep_b) == 1:
        tail_of = itemgetter(keep_b[0])
        for rb in rows_b:
            key = key_b(rb)
            tail = (tail_of(rb),)
            previous = table.get(key)
            if previous is None:
                table[key] = tail
            elif isinstance(previous, list):
                previous.append(tail)
            else:
                table[key] = [previous, tail]
    else:
        tail_of = itemgetter(*keep_b)
        for rb in rows_b:
            key = key_b(rb)
            tail = tail_of(rb)
            previous = table.get(key)
            if previous is None:
                table[key] = tail
            elif isinstance(previous, list):
                previous.append(tail)
            else:
                table[key] = [previous, tail]
    out: list = []
    extend = out.extend
    get = table.get
    for ra in rows_a:
        tails = get(key_a(ra))
        if tails is not None:
            if isinstance(tails, list):
                extend(ra + tail for tail in tails)
            else:
                out.append(ra + tails)
    return schema, out


def _code_rows(rel: Relation) -> Iterator[tuple]:
    """The relation's rows as code tuples, lazily for a columnar or streamed
    body."""
    assert not rel.rest, "rdflib finishes this relation's rows: only the emitter reads them"
    return _body_rows(rel)


def _body_rows(rel: Relation) -> Iterator[tuple]:
    """The code tuples of the relation's body, its ``preds`` applied: its
    rows, or the rows rdflib still finishes when it has a ``rest``."""
    if rel.rows is not None:
        rows = iter(rel.rows)
    elif rel.pieces is not None:
        rows = _piece_rows(rel.pieces)
    else:
        rows = _column_rows(rel.cols, rel.nrows)
    if rel.preds:
        preds = rel.preds
        rows = (row for row in rows if all(pred(row) for pred in preds))
    return rows


def _piece_rows(pieces) -> Iterator[tuple]:
    """The rows of a streamed body, each piece's through
    :func:`_column_rows`, a piece let go before the next is pulled. The
    pieces are closed when the rows end, fail or are closed, which releases
    what feeds them."""
    try:
        for piece in pieces:
            yield from _column_rows(piece, len(piece[0]))
            del piece
    finally:
        close = getattr(pieces, "close", None)
        if close is not None:
            close()


def _column_rows(cols, nrows) -> Iterator[tuple]:
    """Zip column views into row tuples a slice at a time, so a consumer that
    stops early never converts the whole match into Python objects."""
    i, size = 0, _CHUNK_START
    while i < nrows:
        j = min(i + size, nrows)
        yield from zip(*(view[i:j].tolist() for view in cols), strict=True)
        i, size = j, min(size * 4, _CHUNK_MAX)


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
            ctx, store, rel.schema, _code_rows(rel), rel.foreign, project, start, stop
        )
    rows = _yield_rows(ctx, store, rel.schema, _body_rows(rel), rel.foreign, None, 0, None)
    return _project_window(_finish_with_rdflib(ctx, rows, rel.rest), project, start, stop)


def _finish_with_rdflib(ctx, solutions: Iterator, rest: tuple) -> Iterator[FrozenBindings]:
    """The solutions rdflib's ``evalBGP`` makes of each solution over the
    triple patterns ``rest`` (tier 3): each is thawed into a context of its
    own, where the patterns run in the order rdflib's ``evalPart`` gives a
    BGP (:func:`_most_bound_first`). ``evalBGP`` is called directly, so the
    hook is not offered the patterns again; it reads them through the
    store's ``triples()``. Each solution checks the query's cancel token
    before it is extended (:func:`_check_cancelled`). ``solutions`` is
    closed when these end, fail or are closed."""
    try:
        for solution in solutions:
            _check_cancelled()
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
    ``_yield_solutions``); ``start``/``stop`` slice the rows first."""
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
    size = _CHUNK_START
    traced = _TRACE.get() is not None
    started = time.perf_counter_ns() if traced else 0
    yielded = chunks = submitted = 0
    try:
        while True:
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
            size = min(size * 4, _CHUNK_MAX)
    finally:
        if traced:
            _trace_event(
                "yield_rows_complete",
                offset=start,
                stop=stop,
                rows_yielded=yielded,
                chunks=chunks,
                projected_columns=len(idx),
                codes_submitted_for_decode=submitted,
                elapsed_ns=time.perf_counter_ns() - started,
            )
