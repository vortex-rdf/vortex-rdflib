"""Process memory readings from /proc/self/status (Linux; None elsewhere).

``VmRSS`` and ``VmHWM`` count the file pages a process maps, so a memory-mapped
store looks as large as the part of its file the kernel has paged in, pages the
kernel may drop again. ``RssAnon`` is the memory the process itself holds: the
figure to compare across stores and versions. Linux keeps no high-water mark for
it, so callers sample it.

``minor_faults`` counts the pages the process has faulted in without I/O, from
``getrusage`` (Unix; None elsewhere). A query whose freed buffers the allocator
returns to the kernel faults them in again on the next query: a cost no timer
breakdown attributes, visible in this count.
"""

try:
    import resource
except ImportError:  # Windows
    resource = None

STATUS = "/proc/self/status"


def minor_faults() -> int | None:
    """The process's minor page faults so far, every thread's (``RUSAGE_SELF``)."""
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_minflt


def status_mb(key: str, path: str = STATUS) -> int | None:
    """One ``kB`` figure of ``path`` in MiB, rounded; None if unreadable."""
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return round(int(line.split()[1]) / 1024)
    except OSError:
        pass
    return None


def rss_mb() -> int | None:
    return status_mb("VmRSS")


def peak_rss_mb() -> int | None:
    return status_mb("VmHWM")


def rss_anon_mb() -> int | None:
    return status_mb("RssAnon")
