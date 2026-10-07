"""The orchestrator's cross-store agreement check.

Every store answers the same query over the same data, so the row counts must
match. That check is the only correctness guard the comparative bench has —
the dashboard's timings mean nothing if the stores are not answering the same
question — so it has to name a dissenter, and has to keep naming one when the
dissenter is this package's own store.
"""

import subprocess
from dataclasses import replace

from bench import run_bench
from bench.adapters import Adapter
from bench.run_bench import memory_entry, reconcile
from rdflib import Graph


def adapters(*slugs) -> list[Adapter]:
    """Stores that exist only to be named in a report; none is ever built."""

    def unused(nq_path: str, nt_path: str, work_dir: str) -> Graph:
        raise AssertionError("reconcile reads labels, never builds a store")

    return [Adapter(slug, f"label {slug}", "rdflib", unused) for slug in slugs]


def test_stores_that_agree_produce_no_failures():
    failures: list[dict] = []
    agreed, disputed = reconcile(
        {"p-scan": {"vortex": 607, "rdflib": 607, "oxrdflib": 607}},
        adapters("vortex", "rdflib", "oxrdflib"),
        failures,
    )
    assert agreed == {"p-scan": 607}
    assert disputed == [] and failures == []


def test_the_dissenting_store_is_named_against_the_majority():
    failures: list[dict] = []
    agreed, disputed = reconcile(
        {"p-scan": {"vortex": 606, "rdflib": 607, "oxrdflib": 607}},
        adapters("vortex", "rdflib", "oxrdflib"),
        failures,
    )
    assert agreed == {"p-scan": 607}  # the majority, not the first to answer
    assert disputed == ["p-scan"]
    assert len(failures) == 1
    assert failures[0]["slug"] == "vortex" and failures[0]["phase"] == "p-scan"
    assert "returned 606 rows" in failures[0]["error"]
    assert "2 of 3 stores returned 607" in failures[0]["error"]


def test_the_first_store_to_answer_is_not_the_reference():
    """The vortex rows run first. First-wins would have made this package's
    own output the thing every other store is checked against — so a bug here
    would have been reported as eight other stores being wrong, or, with two
    contenders skipped, as a majority."""
    failures: list[dict] = []
    agreed, _ = reconcile(
        {"graph-var": {"vortex": 99, "rdflib": 532, "oxrdflib": 532}},
        adapters("vortex", "rdflib", "oxrdflib"),
        failures,
    )
    assert agreed == {"graph-var": 532}
    assert [f["slug"] for f in failures] == ["vortex"]


def test_every_store_outside_the_majority_is_reported():
    failures: list[dict] = []
    _agreed, disputed = reconcile(
        {"minus": {"a": 1, "b": 2, "c": 3, "d": 3}},
        adapters("a", "b", "c", "d"),
        failures,
    )
    assert disputed == ["minus"]
    assert sorted(f["slug"] for f in failures) == ["a", "b"]


def test_a_store_that_was_never_asked_is_not_a_dissenter():
    # HDT and COTTAS are not asked the `graphs` group at all, so they report
    # no count for it — absence must not read as disagreement.
    failures: list[dict] = []
    agreed, disputed = reconcile(
        {"graph-scan": {"vortex": 76, "rdflib": 76}},
        adapters("vortex", "rdflib", "hdt", "pycottas"),
        failures,
    )
    assert agreed == {"graph-scan": 76}
    assert disputed == [] and failures == []


def test_a_memory_entry_carries_peak_anon():
    entry = memory_entry(
        adapters("vortex")[0],
        {"peakRssMb": 300, "peakAnonMb": 120, "baselineMb": 40, "loadedMb": 160},
    )
    assert entry == {
        "slug": "vortex",
        "label": "label vortex",
        "engine": "rdflib",
        "peakRssMb": 300,
        "peakAnonMb": 120,
        "baselineMb": 40,
        "loadedMb": 160,
        "storeMb": 120,
    }


def test_an_older_worker_without_anon_reads_as_none():
    entry = memory_entry(adapters("vortex")[0], {"peakRssMb": 300})
    assert entry["peakAnonMb"] is None and entry["storeMb"] is None


def test_a_store_whose_env_fails_to_build_is_a_venv_failure(monkeypatch, tmp_path):
    adapter = replace(adapters("pycottas")[0], venv_packages=("x",))

    def broken(*_args):
        raise subprocess.CalledProcessError(1, ["uv"], stderr=b"first\nno such package")

    monkeypatch.setattr("bench.run_bench.ensure_venv", broken)
    failures: list[dict] = []
    out = run_bench.measure_adapter(
        adapter, tmp_path / "a.nq", tmp_path / "a.nt", tmp_path, failures
    )
    assert out is None
    assert failures == [
        {"slug": "pycottas", "label": "label pycottas", "phase": "venv", "error": "no such package"}
    ]


def test_a_worker_that_fails_is_a_worker_failure_and_its_own_failures_are_labelled(
    monkeypatch, tmp_path
):
    adapter = adapters("vortex")[0]
    nq, nt = tmp_path / "a.nq", tmp_path / "a.nt"
    failures: list[dict] = []

    monkeypatch.setattr("bench.run_bench.run_worker", lambda *args, **kwargs: None)
    assert run_bench.measure_adapter(adapter, nq, nt, tmp_path, failures) is None
    assert failures == [
        {
            "slug": "vortex",
            "label": "label vortex",
            "phase": "worker",
            "error": "worker process failed or timed out",
        }
    ]

    answered = {"rows": [], "failures": [{"phase": "Q1", "error": "boom"}]}
    monkeypatch.setattr("bench.run_bench.run_worker", lambda *args, **kwargs: answered)
    failures.clear()
    assert run_bench.measure_adapter(adapter, nq, nt, tmp_path, failures) is answered
    assert failures == [{"slug": "vortex", "label": "label vortex", "phase": "Q1", "error": "boom"}]
