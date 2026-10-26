"""The perf gate's BSBM cell (``bench.bsbm.cell``).

The cell runs the official Explore streams on the flagship configuration, a
store with the by-copy index. A template is flagged only when every pair of
rounds flags it the same way, and any answer that differs between runs is a
mismatch.
"""

import json

import pytest
from bench.bsbm import cell
from bench.bsbm.run_stream import run
from bench.bsbm.streams import load_streams
from tests.bsbm_tiny import write_prepared
from vortex_rdf import VortexRdfStore


def test_the_store_carries_the_by_copy_index(tmp_path):
    prepared = write_prepared(tmp_path / "bsbm", warmup_mixes=1, mixes=1)
    store = tmp_path / "store.vortex"
    cell.prepare(str(prepared), str(store))
    opened = VortexRdfStore(str(store))
    assert opened.layout() == "dictionary" and opened.indexes() == ["secondary-by-copy"]


def test_runs_over_the_store_compare_cleanly_with_themselves(tmp_path):
    prepared = write_prepared(tmp_path / "bsbm", warmup_mixes=1, mixes=2)
    store = str(tmp_path / "store.vortex")
    cell.prepare(str(prepared), store)
    warmup, measured = load_streams(prepared)
    runs = [run(store, in_memory, warmup, measured) for in_memory in (False, True)]
    assert all("minflt" in r for one in runs for r in one["results"])
    out = cell.compare(runs, runs)
    templates = {q["q"] for q in measured}
    assert {(c["residency"], c["template"], c["mode"]) for c in out["cells"]} == {
        (where, q, mode)
        for where in ("file", "in-memory")
        for q in templates
        for mode in cell.MODES
    }
    assert out["mismatches"] == [] and not cell.flagged(out)


def stream_run(in_memory: bool, ms: float, *, digest: str = "d", faults: int = 1000) -> dict:
    """A run_stream output: two templates, five answered instances each, ``ms`` per instance."""
    results = [
        {
            "q": q,
            "i": 10 * q + k,
            "mix": 0,
            "prep_ns": 0,
            "exec_ns": ms * 1e6,
            "rows": 1,
            "digest": digest,
            "minflt": faults,
        }
        for q in (1, 2)
        for k in range(5)
    ]
    return {"in_memory": in_memory, "results": results, "partial": False, "mixes_done": 1}


def verdicts(out: dict) -> set[str | None]:
    return {c["verdict"] for c in out["cells"]}


def test_a_template_is_flagged_only_when_every_pair_of_rounds_flags_it():
    base = [stream_run(False, 10.0) for _ in range(4)]
    slower = [stream_run(False, 12.0) for _ in range(4)]
    assert verdicts(cell.compare(base, slower)) == {"REGRESSION"}
    # Slower in the first pair of rounds only, as a run's position can make it.
    once = [stream_run(False, ms) for ms in (12.0, 12.0, 10.0, 10.0)]
    assert verdicts(cell.compare(base, once)) == {"within"}
    out = cell.compare(base, slower)
    assert cell.flagged(out)


def test_page_faults_must_shift_in_every_pair():
    base = [stream_run(True, 10.0, faults=1_300) for _ in range(4)]
    more = [stream_run(True, 10.0, faults=4_700) for _ in range(4)]
    out = cell.compare(base, more)
    assert {c["faults"] for c in out["cells"]} == {"MORE FAULTS"} and cell.flagged(out)
    half = [stream_run(True, 10.0, faults=f) for f in (4_700, 4_700, 1_300, 1_300)]
    assert {c["faults"] for c in cell.compare(base, half)["cells"]} == {"within"}


def test_an_answer_that_differs_or_a_timeout_is_a_mismatch():
    base = [stream_run(False, 10.0)]
    other = stream_run(False, 10.0)
    other["results"][3]["digest"] = "e"
    assert cell.compare(base, [other])["mismatches"] == ["instance 13 answered 2 ways"]
    late = stream_run(False, 10.0)
    late["results"][0] = {"q": 1, "i": 10, "mix": 0, "timeout": True, "elapsed_ns": 3e10}
    out = cell.compare(base, [late])
    assert out["mismatches"] == ["instance 10 (Q1) timed out, file"] and cell.flagged(out)


def test_the_sides_pair_round_for_round_per_residency():
    with pytest.raises(ValueError, match="pair by round"):
        cell.compare([stream_run(False, 10.0)] * 2, [stream_run(False, 10.0)])


def test_check_fails_only_on_something_flagged(tmp_path):
    def write(name: str, payload: dict) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    a = ",".join(write(f"a{r}.json", stream_run(False, 10.0)) for r in range(4))
    same = ",".join(write(f"s{r}.json", stream_run(False, 10.2)) for r in range(4))
    slower = ",".join(write(f"b{r}.json", stream_run(False, 13.0)) for r in range(4))
    assert cell.main(["compare", a, same, "--check"]) == 0
    assert cell.main(["compare", a, slower, "--check"]) == 1
    assert cell.main(["compare", a, slower]) == 0  # a report, unless asked to gate
