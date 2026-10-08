"""The dashboard's BSBM mode: a store's report, the cross-store answer check, the
merge of CI groups, and the worker and orchestrator end to end."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from bench import run_bench
from bench.bsbm import merge, report
from tests.bsbm_tiny import write_prepared

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = [
    {
        "slug": "vortex_dict_mem",
        "label": "vortex",
        "engine": "rdflib",
        "quads": True,
        "modes": ["exec", "full"],
    },
    {
        "slug": "rdflib_memory",
        "label": "rdflib",
        "engine": "rdflib",
        "quads": True,
        "modes": ["exec", "full"],
    },
    {
        "slug": "oxrdflib",
        "label": "oxrdflib",
        "engine": "rdflib",
        "quads": True,
        "modes": ["exec", "full"],
    },
    {
        "slug": "pyoxigraph",
        "label": "pyoxigraph",
        "engine": "native",
        "quads": True,
        "modes": ["full"],
    },
]
META = {
    "products": 6,
    "seed": 808080,
    "warmup_mixes": 1,
    "mixes": 1,
    "only_query": None,
    "mix": [1, 2],
    "dataset": {"triples": 61},
    "tools": {"repo": "Tpt/bsbm-tools", "commit": "c"},
    "templates": {"1": "SELECT 1 WHERE {}", "2": "SELECT 2 WHERE {}"},
}
LIMITS = {"queryTimeoutS": 5.0, "storeBudgetS": 300.0}


def partial(answers: dict[str, dict]) -> dict:
    base = report.base_config(META, LIMITS, [1, 2])
    entries = [e for e in ENTRIES if e["slug"] in answers]
    stores = {s: {"mixes": 1, "plannedMixes": 1, "partial": False, "timeouts": {}} for s in answers}
    return report.assemble(
        base, entries, [], [], [], stores, answers, "run of " + ",".join(answers)
    )


JOB = ["vortex_dict_mem", "rdflib_memory", "oxrdflib"]


def mid_run(failures: list[dict]) -> dict:
    """What ``dashboard.run`` has written after two of the three stores of ``JOB``: all
    three in ``config.adapters``, a report only for the two that finished."""
    done = {
        "vortex_dict_mem": {"0": [2, "a"], "1": [1, "b"]},
        "rdflib_memory": {"0": [2, "a"], "1": [1, "b"]},
    }
    stores = {s: {"mixes": 1, "plannedMixes": 1, "partial": False, "timeouts": {}} for s in done}
    base = report.base_config(META, LIMITS, [1, 2])
    entries = [e for e in ENTRIES if e["slug"] in JOB]
    return report.assemble(base, entries, [], [], failures, stores, done, "cut short")


def test_a_store_report_has_template_rows_answers_and_qmph():
    run = {
        "results": [
            {"q": 1, "i": 0, "mix": 0, "prep_ns": 10, "exec_ns": 90, "rows": 3, "digest": "a"},
            {"q": 2, "i": 1, "mix": 0, "timeout": True, "elapsed_ns": 5},
            {"q": 1, "i": 2, "mix": 1, "prep_ns": 10, "exec_ns": 190, "rows": 1, "digest": "b"},
            {"q": 2, "i": 3, "mix": 1, "error": "TypeError: boom", "elapsed_ns": 5},
        ],
        "mix_ns": [1000, 3000],
        "mixes_done": 2,
        "mixes_planned": 2,
        "partial": False,
        "warmup_ns": 5,
        "warmup_timeouts": 0,
        "warmup_errors": 0,
    }
    out = report.store_report("vortex_dict_mem", run, native=False)
    assert [r["id"] for r in out["rows"]] == ["Q1::vortex_dict_mem::exec", "Q1::vortex_dict_mem"]
    assert out["rows"][1]["mean_ns"] == 150 and out["rows"][1]["samples"] == "2"
    assert out["answers"] == {"0": [3, "a"], "1": None, "2": [1, "b"], "3": None}
    assert out["timeouts"] == {"Q2": 1} and out["qmph"] == pytest.approx(3600e9 / 2000)
    assert report.error_failures(out) == [
        {"phase": "Q2", "error": "1 instance failed; first: TypeError: boom"}
    ]


def test_a_native_store_reports_the_full_mode_only():
    run = {
        "results": [
            {"q": 1, "i": 0, "mix": 0, "prep_ns": None, "exec_ns": 50, "rows": 2, "digest": None}
        ],
        "mix_ns": [50],
        "mixes_done": 1,
        "mixes_planned": 1,
        "partial": False,
        "warmup_ns": 1,
        "warmup_timeouts": 0,
        "warmup_errors": 0,
    }
    out = report.store_report("pyoxigraph", run, native=True)
    assert [r["id"] for r in out["rows"]] == ["Q1::pyoxigraph"]
    assert out["answers"] == {"0": [2, None]}


def test_the_majority_answer_wins_and_the_dissenter_is_named():
    answers = {
        "vortex_dict_mem": {"0": [2, "a"], "1": [1, "b"]},
        "rdflib_memory": {"0": [2, "a"], "1": [1, "b"]},
        "oxrdflib": {"0": [2, "z"], "1": [1, "b"]},
        "pyoxigraph": {"0": [2, None], "1": [1, None]},
    }
    failures, templates = report.reconcile_answers(answers, ENTRIES, [1, 2])
    assert [(f["slug"], f["phase"], f["check"]) for f in failures] == [
        ("oxrdflib", "Q1", "answers")
    ]
    assert "same 2 rows, different terms" in failures[0]["error"]
    assert templates == {
        "Q1": {"instances": 1, "minRows": 2, "maxRows": 2, "empty": 0},
        "Q2": {"instances": 1, "minRows": 1, "maxRows": 1, "empty": 0},
    }


def test_rdflib_breaks_a_tie():
    failures, _ = report.reconcile_answers(
        {"vortex_dict_mem": {"0": [3, "x"]}, "rdflib_memory": {"0": [2, "a"]}}, ENTRIES, [1]
    )
    assert [f["slug"] for f in failures] == ["vortex_dict_mem"]
    assert "3 rows against 2" in failures[0]["error"]


def test_pyoxigraph_is_held_to_the_agreed_row_count_only():
    answers = {
        "rdflib_memory": {"0": [2, "a"], "1": [0, "e"]},
        "pyoxigraph": {"0": [2, None], "1": [1, None]},
    }
    failures, _ = report.reconcile_answers(answers, ENTRIES, [1, 2])
    assert [(f["slug"], f["phase"]) for f in failures] == [("pyoxigraph", "Q2")]


def test_timeouts_are_not_compared():
    answers = {
        "rdflib_memory": {"0": [2, "a"]},
        "vortex_dict_mem": {"0": None},
        "pyoxigraph": {"0": None},
    }
    failures, templates = report.reconcile_answers(answers, ENTRIES, [1])
    assert failures == [] and templates["Q1"]["instances"] == 1


def test_a_merge_checks_answers_across_every_job():
    a = partial({"vortex_dict_mem": {"0": [5, "v"], "1": [1, "b"]}})
    b = partial(
        {
            "rdflib_memory": {"0": [2, "a"], "1": [1, "b"]},
            "oxrdflib": {"0": [2, "a"], "1": [1, "b"]},
        }
    )
    b["failures"].append(
        {
            "slug": "oxrdflib",
            "label": "oxrdflib",
            "phase": "Q2",
            "check": "answers",
            "error": "stale",
        }
    )
    assert a["failures"] == []
    merged = merge.merge_payloads([a, b], expected=["vortex_dict_mem", "rdflib_memory", "oxrdflib"])
    assert [(f["slug"], f["phase"]) for f in merged["failures"]] == [("vortex_dict_mem", "Q1")]
    assert [e["slug"] for e in merged["config"]["adapters"]] == [
        "vortex_dict_mem",
        "rdflib_memory",
        "oxrdflib",
    ]


def test_a_store_no_job_reported_is_named():
    merged = merge.merge_payloads(
        [partial({"rdflib_memory": {"0": [2, "a"]}})], expected=["rdflib_memory", "rdflib_hdt"]
    )
    assert [(f["slug"], f["phase"]) for f in merged["failures"]] == [("rdflib_hdt", "worker")]
    assert "did not finish" in merged["failures"][0]["error"]


def test_a_store_a_cut_short_job_never_reached_is_named():
    merged = merge.merge_payloads([mid_run([])], expected=JOB)
    assert [(f["slug"], f["phase"]) for f in merged["failures"]] == [("oxrdflib", "worker")]
    assert "did not finish" in merged["failures"][0]["error"]
    assert [e["slug"] for e in merged["config"]["adapters"]] == JOB


def test_a_store_whose_job_recorded_its_failure_is_not_named_again():
    venv = {"slug": "oxrdflib", "label": "oxrdflib", "phase": "venv", "error": "no such package"}
    merged = merge.merge_payloads([mid_run([venv])], expected=JOB)
    assert merged["failures"] == [venv]


def test_partials_of_different_runs_are_refused():
    a, b = partial({"rdflib_memory": {}}), partial({"oxrdflib": {}})
    b["config"]["seed"] = 1
    with pytest.raises(ValueError, match="seed"):
        merge.merge_payloads([a, b], expected=[])


def test_merge_main_skips_missing_files_and_fails_with_none(tmp_path):
    part = tmp_path / "a.json"
    part.write_text(json.dumps(partial({"rdflib_memory": {"0": [2, "a"]}})))
    out, gone = tmp_path / "merged.json", tmp_path / "gone.json"
    assert merge.main(["--out", str(out), "--expect", "rdflib_memory", str(part), str(gone)]) == 0
    assert json.loads(out.read_text())["config"]["dataset"] == "bsbm"
    assert merge.main(["--out", str(out), str(gone)]) == 1


def test_the_worker_runs_every_measured_instance_once(tmp_path):
    prepared = write_prepared(tmp_path / "bsbm", warmup_mixes=1, mixes=2)
    out, data = tmp_path / "w.json", str(prepared / "dataset.nt")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.worker",
            "vortex_dict_mem",
            data,
            data,
            str(tmp_path),
            str(out),
            "--bsbm",
            str(prepared),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "BENCH_LOAD_ITERS": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    report_ = json.loads(out.read_text())
    ids = {r["id"] for r in report_["rows"]}
    assert {"load::vortex_dict_mem", "Q1::vortex_dict_mem::exec", "Q12::vortex_dict_mem"} <= ids
    assert next(r for r in report_["rows"] if r["id"] == "Q5::vortex_dict_mem")["samples"] == "4"
    bsbm = report_["bsbm"]
    assert len(bsbm["answers"]) == 50 and all(a is not None for a in bsbm["answers"].values())
    assert bsbm["mixes"] == 2 and bsbm["partial"] is False and report_["failures"] == []


def test_run_bench_bsbm_reconciles_the_stores_answers(tmp_path, monkeypatch):
    # dashboard.run's work dir is a mkdtemp: under tmp_path, to see that the run removes it
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    prepared = write_prepared(tmp_path / "bsbm", warmup_mixes=1, mixes=1)
    out = tmp_path / "results-bsbm.json"
    args = ["--dataset", "bsbm", "--bsbm-dir", str(prepared), "--out", str(out)]
    assert run_bench.main([*args, "--adapters", "vortex_dict_mem,rdflib_memory"]) == 0
    assert list(tmp_path.glob("vortex-rdflib-bsbm-*")) == []
    payload = json.loads(out.read_text())
    config = payload["config"]
    assert config["dataset"] == "bsbm" and config["products"] == 6
    assert set(config["stores"]) == set(config["answers"]) == {"vortex_dict_mem", "rdflib_memory"}
    assert payload["failures"] == []
    assert [q["name"] for q in config["queries"]] == [
        f"Q{q}" for q in (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)
    ]
    assert config["templates"]["Q5"]["instances"] == 2


def test_pyoxigraph_agrees_on_the_row_counts(tmp_path, monkeypatch):
    pytest.importorskip("pyoxigraph")
    # dashboard.run's work dir is a mkdtemp: under tmp_path, should a failed run leave it
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    prepared = write_prepared(tmp_path / "bsbm", warmup_mixes=0, mixes=1)
    out = tmp_path / "results-bsbm.json"
    args = ["--dataset", "bsbm", "--bsbm-dir", str(prepared), "--out", str(out)]
    assert run_bench.main([*args, "--adapters", "rdflib_memory,pyoxigraph"]) == 0
    payload = json.loads(out.read_text())
    assert payload["failures"] == []
    ids = {r["id"] for r in payload["results"]}
    assert "Q1::pyoxigraph" in ids and "Q1::pyoxigraph::exec" not in ids
