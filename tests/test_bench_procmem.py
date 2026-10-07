"""Process memory readings (``bench/procmem.py``), the source of every memory figure."""

import sys

import pytest
from bench import procmem, worker


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux-only")
def test_anonymous_memory_is_read_on_linux():
    anon, rss = procmem.rss_anon_mb(), procmem.rss_mb()
    assert isinstance(anon, int) and isinstance(rss, int)
    assert 0 < anon <= rss


def test_memory_readings_are_none_without_proc(tmp_path):
    assert procmem.status_mb("RssAnon", path=str(tmp_path / "no-such-status")) is None


def test_a_reading_is_rounded_to_mib(tmp_path):
    status = tmp_path / "status"
    status.write_text("VmRSS:\t  3072 kB\nRssAnon:\t  1536 kB\n", encoding="ascii")
    assert procmem.status_mb("VmRSS", path=str(status)) == 3
    assert procmem.status_mb("RssAnon", path=str(status)) == 2
    assert procmem.status_mb("VmHWM", path=str(status)) is None


def test_the_worker_reexports_the_readings():
    assert worker.rss_anon_mb is procmem.rss_anon_mb
    assert worker.rss_mb is procmem.rss_mb and worker.peak_rss_mb is procmem.peak_rss_mb
