"""The one boundary between vortex-rdflib and vortex-rdf: every call into a
store's native handle goes through :func:`native`, and every call into its
term dictionary through :func:`term_dict`.

While something observes the call they hand out a proxy, one per call to
them, which gives the call the query's cancel token by the policy below,
emits the query trace's native events (:mod:`vortex_rdflib.trace`) and
tallies the work counters (:mod:`vortex_rdflib.counters`), and wraps an
iterator the call returns once. With nothing to observe — no cancel token, no
traced query, the counters off — they hand back the store's own handles, so
a call pays nothing for the boundary.

The cancel policy, for the token of the query being evaluated
(:data:`~vortex_rdflib.cancel.CANCEL_TOKEN`):

- ``count_quads`` and ``match_codes`` race the token (``cancel=``) when the
  call can scan a file-backed store: not on an in-memory store, which
  finishes without waiting, not capped at one row (``limit``), and not a
  point lookup (``VortexRdflibStore._point_lookup``). Any other checks the
  token before the call (:func:`~vortex_rdflib.cancel.check_cancelled`).
- ``match_columns`` and ``get_quads`` check the token before the call.
- The batches (``count_quads_many``, ``match_codes_many``) and the chunked
  match (``match_code_chunks``) always race it.

No caller passes ``cancel=`` itself.
"""

import time

from vortex_rdf import CancelToken, VortexRdfStore

from . import counters, trace
from .cancel import CANCEL_TOKEN, check_cancelled
from .pushdown.tracing import _keep_summary, _probe_pattern
from .settings import SETTINGS


def native(store) -> VortexRdfStore:
    """The store's native handle (opening the store if it is not yet), as
    every native call should reach it: the handle itself while nothing
    observes the call, else a proxy for this call."""
    handle = store._store()
    token = CANCEL_TOKEN.get()
    traced = trace.active() is not None
    if token is None and not traced and not SETTINGS.counters:
        return handle
    return _Native(store, handle, token, traced, SETTINGS.counters)  # ty: ignore[invalid-return-type]


def term_dict(store):
    """The store's term dictionary, ``None`` without one: itself while the
    counters are off, else a proxy that counts its decodes and encodes."""
    dictionary = store._dict
    if dictionary is None or not SETTINGS.counters:
        return dictionary
    return _TermDict(dictionary)


def _quad(pattern: tuple) -> tuple:
    """A call's positional pattern as the four quad positions it stands for:
    the ones it leaves out are unbound."""
    return pattern if len(pattern) == 4 else (*pattern, None, None, None, None)[:4]


def _rows(result) -> int:
    """The rows of a match's columns, ``0`` for none."""
    return 0 if not result else len(result[0])


class _Native:
    """A store's native handle as one call reaches it while it is observed
    (:func:`native`): the cancel policy applied, the call traced and counted.
    Any method not named here passes through, counted."""

    __slots__ = ("_store", "_inner", "_token", "_traced", "_counted")

    def __init__(self, store, inner, token: CancelToken | None, traced: bool, counted: bool):
        self._store = store
        self._inner = inner
        self._token = token
        self._traced = traced
        self._counted = counted

    def _one_shot(self, n3: tuple, options: dict) -> dict:
        """The keywords of a one-shot call over the quad pattern ``n3``:
        ``options`` with the token when the call races it; after a check of
        the token when it does not."""
        token = self._token
        if token is None:
            return options
        store = self._store
        limit = options.get("limit")
        if store.in_memory or (limit is not None and limit <= 1) or store._point_lookup(n3):
            check_cancelled()
            return options
        return {**options, "cancel": token}

    def _racing(self, options: dict) -> dict:
        """The keywords of a batch or a chunked match: ``options`` with the
        token."""
        return options if self._token is None else {**options, "cancel": self._token}

    def count_quads(self, *pattern, **options):
        n3 = _quad(pattern)
        options = self._one_shot(n3, options)
        started = time.perf_counter_ns() if self._traced else 0
        result = self._inner.count_quads(*pattern, **options)
        if self._counted:
            counters.add(native_calls=1, count_calls=1)
        if self._traced:
            trace.event(
                "native_call_complete",
                operation="count_quads",
                pattern=list(n3),
                keep=_keep_summary(options.get("keep")),
                limit=options.get("limit"),
                returned_rows=result,
                elapsed_ns=time.perf_counter_ns() - started,
            )
        return result

    def match_codes(self, *pattern, **options):
        n3 = _quad(pattern)
        options = self._one_shot(n3, options)
        started = time.perf_counter_ns() if self._traced else 0
        result = self._inner.match_codes(*pattern, **options)
        if self._counted:
            counters.add(native_calls=1, rows_returned=_rows(result))
        if self._traced:
            trace.event(
                "native_call_complete",
                operation="match_codes",
                pattern=list(n3),
                keep=_keep_summary(options.get("keep")),
                limit=options.get("limit"),
                returned_rows=None if result is None else len(result[0]),
                elapsed_ns=time.perf_counter_ns() - started,
            )
        return result

    def count_quads_many(self, probes, **options):
        started = time.perf_counter_ns() if self._traced else 0
        result = self._inner.count_quads_many(probes, **self._racing(options))
        if self._counted:
            counters.add(native_calls=1, count_calls=len(probes))
        if self._traced:
            trace.event(
                "native_batch_complete",
                operation="count_quads_many",
                probe_count=len(probes),
                first_pattern=_probe_pattern(probes[0]) if probes else None,
                returned_rows=sum(result),
                elapsed_ns=time.perf_counter_ns() - started,
            )
        return result

    def match_codes_many(self, probes, **options):
        started = time.perf_counter_ns() if self._traced else 0
        answers = self._inner.match_codes_many(probes, **self._racing(options))
        if self._counted:
            counters.add(native_calls=1)
        event = None
        if self._traced:
            event = {
                "operation": "match_codes_many",
                "probe_count": len(probes),
                "first_pattern": _probe_pattern(probes[0]) if probes else None,
            }
        return _Pulled(answers, self._counted, "native_batch_complete", event, started)

    def match_code_chunks(self, *pattern, **options):
        started = time.perf_counter_ns() if self._traced else 0
        chunks = self._inner.match_code_chunks(*pattern, **self._racing(options))
        if self._counted:
            counters.add(native_calls=1)
        if chunks is None:
            return None
        event = None
        if self._traced:
            event = {
                "operation": "match_code_chunks",
                "pattern": list(_quad(pattern)),
                "keep": _keep_summary(options.get("keep")),
                "columns": list(options.get("columns", ("s", "p", "o", "g"))),
            }
        return _Pulled(chunks, self._counted, "native_chunks_complete", event, started)

    def match_columns(self, *pattern, **options):
        check_cancelled()
        if self._counted:
            counters.add(native_calls=1)
        return self._inner.match_columns(*pattern, **options)

    def get_quads(self, *pattern, **options):
        check_cancelled()
        if self._counted:
            counters.add(native_calls=1)
        return self._inner.get_quads(*pattern, **options)

    def __len__(self):  # special methods bypass __getattr__
        return len(self._inner)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not self._counted or not callable(attr):
            return attr

        def counted(*args, **kwargs):
            counters.add(native_calls=1)
            return attr(*args, **kwargs)

        return counted


class _Pulled:
    """A native iterator a call returned (the answers of a batch, the chunks
    of a match), wrapped once: each ``__next__`` is exactly one pull of it,
    its rows counted while the counters are on and, while the query is
    traced, its pulls and rows summed into one event (``event``'s fields),
    emitted when it is exhausted or closed. Closing drops the native
    iterator, which aborts the work it still has in flight. The time an
    event reports is the native layer's: the call that made the iterator
    and every pull. Its length and any other attribute are the native
    iterator's (a batch's length is its number of probes)."""

    __slots__ = (
        "_source",
        "_inner",
        "_counted",
        "_name",
        "_event",
        "_elapsed_ns",
        "_pulls",
        "_rows",
    )

    def __init__(self, source, counted, name, event, started):
        self._source = source
        self._inner = iter(source)
        self._counted = counted
        self._name = name
        self._event = event
        self._elapsed_ns = time.perf_counter_ns() - started if event is not None else 0
        self._pulls = 0
        self._rows = 0

    def __iter__(self):
        return self

    def __len__(self) -> int:  # special methods bypass __getattr__
        return len(self._source)

    def __getattr__(self, name):
        return getattr(self._source, name)

    def __next__(self):
        inner = self._inner
        if inner is None:
            raise StopIteration
        started = time.perf_counter_ns() if self._event is not None else 0
        try:
            item = next(inner)
        except StopIteration:
            if self._event is not None:
                self._elapsed_ns += time.perf_counter_ns() - started
            self.close()
            raise
        rows = 0 if item is None else len(item[0])
        if self._counted:
            counters.add(rows_returned=rows)
        if self._event is not None:
            self._elapsed_ns += time.perf_counter_ns() - started
            self._pulls += 1
            self._rows += rows
        return item

    def close(self) -> None:
        """Drop the native iterator and emit the event, if it was not yet."""
        self._inner = None
        event, self._event = self._event, None
        if event is None:
            return
        if self._name == "native_chunks_complete":
            event = {**event, "chunks": self._pulls}
        trace.event(self._name, **event, returned_rows=self._rows, elapsed_ns=self._elapsed_ns)


class _TermDict:
    """A term dictionary with its decodes and encodes counted (the counters
    on). Any other method passes through."""

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    def decode(self, code):
        counters.add(decode_calls=1, terms_decoded=1)
        return self._inner.decode(code)

    def decode_many(self, codes):
        out = self._inner.decode_many(codes)
        counters.add(decode_calls=1, terms_decoded=len(out))
        return out

    def encode(self, term):
        counters.add(encode_calls=1)
        return self._inner.encode(term)

    def encode_many(self, terms):
        counters.add(encode_calls=1)
        return self._inner.encode_many(terms)

    def __len__(self):  # special methods bypass __getattr__
        return len(self._inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)
