"""Process memory readings from /proc/self/status (Linux; None elsewhere).

``VmRSS`` and ``VmHWM`` count the file pages a process maps, so a memory-mapped
store looks as large as the part of its file the kernel has paged in, pages the
kernel may drop again. ``RssAnon`` is the memory the process itself holds: the
figure to compare across stores and versions. Linux keeps no high-water mark for
it, so callers sample it.
"""

STATUS = "/proc/self/status"


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
