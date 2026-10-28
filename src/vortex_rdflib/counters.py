"""Work counters (``VORTEX_RDF_COUNTERS=1``, read once at import; off by default, and then nothing
is wrapped).

When on, the store wraps its native handle and term dictionary at open, and every call through them
is counted: native calls, count calls, rows the native layer returned to Python, decode calls and
terms decoded, encode calls. :func:`snapshot` adds the process's I/O (``/proc/self/io``: bytes read
from storage, and bytes asked of the kernel) and page faults (major ones are pages read in for the
memory map). The counts are cumulative; a caller takes the difference around a query.
"""

import threading
from typing import TypeVar, cast

from .ablation import flag

ON = flag("VORTEX_RDF_COUNTERS", False)

_NAMES = (
    "native_calls",
    "count_calls",
    "rows_returned",
    "decode_calls",
    "terms_decoded",
    "encode_calls",
)
_lock = threading.Lock()
_counts = dict.fromkeys(_NAMES, 0)


def add(**deltas) -> None:
    with _lock:
        for name, n in deltas.items():
            _counts[name] += n


def snapshot() -> dict:
    with _lock:
        out = dict(_counts)
    try:
        with open("/proc/self/io") as f:
            io = dict(line.split(": ") for line in f.read().splitlines())
        out["io_read_bytes"] = int(io["read_bytes"])
        out["io_rchar"] = int(io["rchar"])
    except (OSError, KeyError, ValueError):
        pass
    # vortex-rdf's encoded-search counters: vortex-rdf builds with them only (same switch)
    try:
        from vortex_rdf._native import engine_counters  # ty: ignore[unresolved-import]
    except ImportError:
        pass
    else:
        out.update({f"native_{k}": v for k, v in engine_counters().items()})
    try:
        import resource  # POSIX only: no page-fault counts on Windows
    except ImportError:
        return out
    usage = resource.getrusage(resource.RUSAGE_SELF)
    out["major_faults"], out["minor_faults"] = usage.ru_majflt, usage.ru_minflt
    return out


def _rows(result) -> int:
    return 0 if result is None else len(result[0]) if result else 0


class _CountedIterator:
    """A native answer iterator (``ProbeAnswers``, ``CodeChunks``) whose pulled rows are counted."""

    def __init__(self, inner, rows):
        self._inner, self._it, self._rows = inner, iter(inner), rows

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self._it)
        add(rows_returned=self._rows(item))
        return item

    def __len__(self):  # special methods bypass __getattr__
        return len(self._inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class CountingNative:
    """The native store with its calls counted."""

    def __init__(self, inner):
        self._inner = inner

    def match_codes(self, *pattern, **options):
        result = self._inner.match_codes(*pattern, **options)
        add(native_calls=1, rows_returned=_rows(result))
        return result

    def count_quads(self, *pattern, **options):
        add(native_calls=1, count_calls=1)
        return self._inner.count_quads(*pattern, **options)

    def count_quads_many(self, probes, **options):
        add(native_calls=1, count_calls=len(probes))
        return self._inner.count_quads_many(probes, **options)

    def match_codes_many(self, probes, **options):
        add(native_calls=1)
        return _CountedIterator(self._inner.match_codes_many(probes, **options), _rows)

    def match_code_chunks(self, *pattern, **options):
        add(native_calls=1)
        return _CountedIterator(self._inner.match_code_chunks(*pattern, **options), _rows)

    def __len__(self):  # special methods bypass __getattr__
        return len(self._inner)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if callable(attr):

            def counted(*args, **kwargs):
                add(native_calls=1)
                return attr(*args, **kwargs)

            return counted
        return attr


class CountingDict:
    """The term dictionary with its decodes and encodes counted."""

    def __init__(self, inner):
        self._inner = inner

    def decode(self, code):
        add(decode_calls=1, terms_decoded=1)
        return self._inner.decode(code)

    def decode_many(self, codes):
        out = self._inner.decode_many(codes)
        add(decode_calls=1, terms_decoded=len(out))
        return out

    def encode(self, term):
        add(encode_calls=1)
        return self._inner.encode(term)

    def encode_many(self, terms):
        add(encode_calls=1)
        return self._inner.encode_many(terms)

    def __len__(self):  # special methods bypass __getattr__
        return len(self._inner)

    def __getattr__(self, name):
        return getattr(self._inner, name)


N = TypeVar("N")
D = TypeVar("D")


def wrap(native: N, term_dict: D) -> tuple[N, D]:
    """``(native, term_dict)``, counted when the counters are on. Typed as what it was given: the
    proxies forward every call, so callers keep the native types."""
    if not ON:
        return native, term_dict
    counted_dict = None if term_dict is None else CountingDict(term_dict)
    return cast(N, CountingNative(native)), cast(D, counted_dict)
