"""The pushdown's hook into rdflib: registering it in ``CUSTOM_EVALS``, the
query scope a query runs in, and dispatching each algebra node to its
handler (:data:`_HANDLERS`) — under the query's trace when it is traced.

rdflib only catches ``NotImplementedError`` at hook-call time, so a handler
makes every capability check, and every native call that can decline,
before it hands a generator back.
"""

from rdflib.plugins.sparql import CUSTOM_EVALS
from rdflib.plugins.sparql.evaluate import evalPart
from vortex_rdf import Cancelled

from .. import query_scope, settings
from ..cancel import CANCEL_TOKEN
from ..settings import SETTINGS
from . import aggregate, blocks, head, tracing
from .tracing import _TRACE, _QueryTrace

_EVAL_KEY = "vortex_rdflib_bgp"


#: The algebra nodes the hook answers: each node's handler, as its module
#: and name. A handler is looked up when it is called, so one patched on its
#: module (a test's spy) is the one called.
_HANDLERS = {
    "BGP": (blocks, "_eval_block_node"),
    "Filter": (blocks, "_eval_block_node"),
    "Join": (blocks, "_eval_block_node"),
    "LeftJoin": (blocks, "_eval_block_node"),
    "Minus": (blocks, "_eval_block_node"),
    "ToMultiSet": (blocks, "_eval_block_node"),
    "Graph": (blocks, "_eval_block_node"),
    "Project": (head, "_eval_head"),
    "Slice": (head, "_eval_head"),
    "Distinct": (head, "_eval_head"),
    "OrderBy": (head, "_eval_head"),
    "AggregateJoin": (aggregate, "_eval_aggregate"),
    "AskQuery": (blocks, "_eval_ask"),
}
assert _HANDLERS.keys() == settings.ALL_OPS


def register_sparql_pushdown():
    """Install the hook into rdflib's CUSTOM_EVALS (idempotent), narrowed by
    the environment's switches (:func:`.settings.apply_switches`)."""
    if settings.pushdown_disabled():
        return
    settings.apply_switches()
    CUSTOM_EVALS.setdefault(_EVAL_KEY, _eval_part)


def unregister_sparql_pushdown():
    CUSTOM_EVALS.pop(_EVAL_KEY, None)


def _eval_part(ctx, part):
    if query_scope.current() is None:
        return _eval_scoped(ctx, part)
    handler = _HANDLERS.get(part.name)
    if handler is None or part.name not in SETTINGS.enabled_ops:
        raise NotImplementedError

    from ..store import VortexRdflibStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexRdflibStore) or store._dict is None:
        raise NotImplementedError
    run = getattr(*handler)
    active = _TRACE.get()
    if active is None and not settings.trace_enabled():
        return run(ctx, store, part)
    return _traced_eval(active, run, ctx, store, part)


def _traced_eval(active: _QueryTrace | None, run, ctx, store, part):
    """``run(ctx, store, part)`` as a traced step: under the query's trace
    ``active`` or, when there is none, a trace of its own, kept current
    around each step of a lazy result (:func:`_trace_generator`). The
    node's start is an event, and so is how it ends: completed, handed back
    to rdflib, cancelled or failed. The trace's lines are flushed once the
    planning is over, so nothing after it is timed."""
    token = None
    if active is None:
        active = _QueryTrace(settings.trace_query_id())
        token = _TRACE.set(active)
    elapsed = tracing._stopwatch()
    tracing._trace_event(
        "eval_part_start", algebra_node=part.name, enabled_ops=sorted(SETTINGS.enabled_ops)
    )
    try:
        result = run(ctx, store, part)
    except Exception as error:
        if isinstance(error, NotImplementedError):
            tracing._trace_event("eval_part_fallback", algebra_node=part.name)
        elif isinstance(error, Cancelled):
            tracing._trace_event("eval_part_cancelled", algebra_node=part.name)
        else:
            tracing._trace_event(
                "eval_part_failed", algebra_node=part.name, error_type=type(error).__name__
            )
        active.flush()
        if token is not None:
            _TRACE.reset(token)
        raise
    tracing._trace_event(
        "eval_part_complete", algebra_node=part.name, handled=True, elapsed_ns=elapsed()
    )
    active.flush()
    if token is None:
        return result
    if hasattr(result, "__next__"):
        return tracing._trace_generator(result, active, token)
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
    (:func:`~.cancel.cancel_scope`), which every step of a lazy result runs under. A
    graph whose store is not a VortexRdflibStore is left alone.
    """
    from ..store import VortexRdflibStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexRdflibStore):
        raise NotImplementedError
    opened = query_scope.QueryScope()
    opened.cancel = CANCEL_TOKEN.get()
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
    the query's :func:`~.cancel.cancel_scope` block."""
    cancel = opened.cancel
    enter, leave = query_scope.enter, query_scope.leave
    get_cancel, set_cancel, reset_cancel = CANCEL_TOKEN.get, CANCEL_TOKEN.set, CANCEL_TOKEN.reset
    try:
        while True:
            token = enter(opened)
            reset = set_cancel(cancel) if get_cancel() is not cancel else None
            try:
                item = next(iterator)
            except StopIteration:
                return
            finally:
                if reset is not None:
                    reset_cancel(reset)
                leave(token)
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            token = query_scope.enter(opened)
            reset = CANCEL_TOKEN.set(cancel) if CANCEL_TOKEN.get() is not cancel else None
            try:
                close()
            finally:
                if reset is not None:
                    CANCEL_TOKEN.reset(reset)
                query_scope.leave(token)
        opened.clear()
