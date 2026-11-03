"""A query's cancel token.

``cancel_scope(token)`` gives the queries started inside it ``token``. A
query keeps it in its scope (:mod:`.query_scope`), so every step of its lazy
result runs under it. The native boundary (:mod:`.boundary`) gives each native
call the current token by one policy — a call the token can stop races it,
any other checks it as it is made — and the loops between native calls check
it per step (:func:`check_cancelled`).
"""

import contextlib
from collections.abc import Generator
from contextvars import ContextVar

from vortex_rdf import Cancelled, CancelToken

#: The cancel token of the query being evaluated: set by :func:`cancel_scope`
#: for the queries started inside it, kept in the query's scope
#: (``QueryScope.cancel``) and set again around each step of its lazy result
#: (``pushdown.hook._scoped_iterator``).
CANCEL_TOKEN: ContextVar[CancelToken | None] = ContextVar("vortex_rdflib_cancel", default=None)


@contextlib.contextmanager
def cancel_scope(token: CancelToken) -> Generator[CancelToken]:
    """Run the queries started inside the block under ``token``, which it
    yields.

    Each native call such a query makes that the token can stop — a batch, a
    chunked match, a one-shot call that scans a file-backed store — is given
    it (``cancel=``) and races it; every other one-shot call — a point
    lookup, a call capped at one row, any call on an in-memory store, which
    finishes without waiting — checks it as it is made (a join's per-row
    probes are calls of either kind), as do the loops between calls: a
    streamed join's pieces, the rows rdflib finishes, and the slices and decode
    chunks of the rows it yields. So ``token.cancel()``, from any thread, stops
    the query with ``vortex_rdf.Cancelled`` at its next call, piece, finished
    row, slice or decode chunk, while it is planned or while its rows are read;
    the rows already decoded, at most one decode chunk of 4,096, are
    delivered first. A query keeps the token it started with until its rows
    end, the rows read after the block included; a query started outside a
    block has none. A ``triples()`` iteration started inside the block gives
    its native calls the token too.
    """
    reset = CANCEL_TOKEN.set(token)
    try:
        yield token
    finally:
        CANCEL_TOKEN.reset(reset)


def check_cancelled() -> None:
    """Raise ``vortex_rdf.Cancelled`` when the current query's cancel token is
    cancelled: the check made per step of a loop that makes no native call,
    and before a one-shot native call that does not race the token
    (:mod:`.boundary`)."""
    token = CANCEL_TOKEN.get()
    if token is not None and token.cancelled:
        raise Cancelled("cancelled through its CancelToken")
