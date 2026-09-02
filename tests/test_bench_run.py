"""The orchestrator's cross-store agreement check.

Every store answers the same query over the same data, so the row counts must
match. That check is the only correctness guard the comparative bench has —
the dashboard's timings mean nothing if the stores are not answering the same
question — so it has to name a dissenter, and has to keep naming one when the
dissenter is this package's own store.
"""

from bench.adapters import Adapter
from bench.run_bench import reconcile
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
