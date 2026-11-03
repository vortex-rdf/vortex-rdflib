"""Work counters (``VORTEX_RDF_COUNTERS=1``, ``SETTINGS.counters``; off by default, and then
nothing is counted).

When on, every call through the native boundary (:mod:`vortex_rdflib.boundary`) is counted: native
calls, count calls, rows the native layer returned to Python, decode calls and terms decoded,
encode calls. :func:`snapshot` adds the process's I/O (``/proc/self/io``: bytes read
from storage, and bytes asked of the kernel) and page faults (major ones are pages read in for the
memory map). The counts are cumulative; a caller takes the difference around a query.
"""

import threading

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
        from vortex_rdf._native import engine_counters
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
