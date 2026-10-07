"""History records of a BSBM run: template medians, and the templates a point leaves out."""

import json

import pytest
from bench import history

COMMIT = {
    "sha": "a" * 40,
    "short": "aaaaaaa",
    "date": "2026-10-07T10:00:00+00:00",
    "subject": "s",
    "ref": "main",
    "dirty": False,
}
AGREED = {"0": [3, "a"], "1": [1, "b"], "2": [0, "c"], "3": [2, "d"]}


def row(group: str, slug: str, mode: str, median_ns: float) -> dict:
    variant = slug if mode == "full" else f"{slug}::{mode}"
    return {
        "group": group,
        "variant": variant,
        "id": f"{group}::{variant}",
        "mode": mode,
        "median_ns": median_ns,
    }


def bsbm_results(answers: dict, failures: list | None = None) -> dict:
    rows = [
        row(g, s, m, base * (2 if m == "full" else 1))
        for s, base in (("rdflib_memory", 100.0), ("vortex_dict_mem", 10.0))
        for g in ("Q1", "Q2")
        for m in ("exec", "full")
    ]
    return {
        "results": [*rows, row("load", "vortex_dict_mem", "full", 5.0)],
        "failures": failures or [],
        "config": {
            "dataset": "bsbm",
            "products": 10000,
            "triples": 3534773,
            "seed": 808080,
            "warmupMixes": 5,
            "mixes": 20,
            "tools": {"repo": "Tpt/bsbm-tools", "commit": "c"},
            "queries": [{"name": "Q1"}, {"name": "Q2"}],
            "instanceTemplates": [1, 2, 1, 2],
            "adapters": [{"slug": "vortex_dict_mem", "label": "vortex"}],
            "answers": answers,
        },
    }


def test_a_bsbm_record_keeps_template_medians_and_its_dataset():
    record = history.make_record(
        bsbm_results({"rdflib_memory": AGREED, "vortex_dict_mem": AGREED}), COMMIT, "ci"
    )
    assert record["medians"]["vortex_dict_mem"]["exec"] == {"Q1": 10.0, "Q2": 10.0}
    assert (
        record["dataset"]["name"] == "bsbm"
        and record["dataset"]["products"] == 10000
        and "onlyQuery" in record["dataset"]
    )
    assert record["dropped"] == {}


def test_a_different_answer_a_timeout_or_a_failure_drops_its_template():
    for mine, failures, dropped in (
        ({**AGREED, "1": [1, "x"]}, None, ["Q2"]),
        ({**AGREED, "0": None}, None, ["Q1"]),
        (
            AGREED,
            [{"slug": "vortex_dict_mem", "phase": "Q2", "error": "1 instance failed"}],
            ["Q2"],
        ),
    ):
        results = bsbm_results({"rdflib_memory": AGREED, "vortex_dict_mem": mine}, failures)
        assert history.make_record(results, COMMIT, "ci")["dropped"] == {"vortex_dict_mem": dropped}


def test_instances_rdflib_never_reached_are_not_compared():
    partial = {"0": [3, "a"], "1": [1, "b"]}
    record = history.make_record(
        bsbm_results({"rdflib_memory": partial, "vortex_dict_mem": AGREED}), COMMIT, "ci"
    )
    assert record["dropped"] == {}


def test_bsbm_records_make_a_series_of_templates():
    record = history.make_record(
        bsbm_results({"rdflib_memory": AGREED, "vortex_dict_mem": AGREED}), COMMIT, "ci"
    )
    series = history.build_series([record], [])
    assert series is not None and series["queries"] == 2
    assert series["points"][0]["values"]["vortex_dict_mem"]["exec"]["speedup"] == pytest.approx(
        10.0
    )


def test_record_refuses_results_of_the_other_dataset(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps(bsbm_results({})))
    with pytest.raises(SystemExit) as stop:
        history.main(
            [
                "record",
                str(path),
                "--source",
                "local",
                "--commit",
                "HEAD",
                "--out",
                str(tmp_path / "o"),
            ]
        )
    assert stop.value.code == 2


def test_a_configuration_timeout_when_rdflib_also_times_out_drops_the_template():
    """A vortex timeout (None) drops its template even when rdflib also timed out."""
    mine = {**AGREED, "1": None}  # Q2, instance 1 times out
    rdflib = {**AGREED, "1": None}  # rdflib also times out on instance 1
    results = bsbm_results({"rdflib_memory": rdflib, "vortex_dict_mem": mine})
    assert history.make_record(results, COMMIT, "ci")["dropped"] == {"vortex_dict_mem": ["Q2"]}


def test_a_configuration_timeout_when_rdflib_never_reached_drops_the_template():
    """A vortex timeout (None) drops its template even when rdflib never reached that instance."""
    mine = {**AGREED, "1": None}  # Q2, instance 1 times out
    rdflib = {"0": [3, "a"], "2": [0, "c"], "3": [2, "d"]}  # rdflib never reached instance 1
    results = bsbm_results({"rdflib_memory": rdflib, "vortex_dict_mem": mine})
    assert history.make_record(results, COMMIT, "ci")["dropped"] == {"vortex_dict_mem": ["Q2"]}


def test_a_configuration_with_answers_check_failure_is_not_dropped():
    """A configuration's answers matching rdflib is not dropped for answers-check failures."""
    results = bsbm_results(
        {"rdflib_memory": AGREED, "vortex_dict_mem": AGREED},
        failures=[
            {
                "slug": "vortex_dict_mem",
                "phase": "Q2",
                "check": "answers",
                "error": "outvoted",
            }
        ],
    )
    assert history.make_record(results, COMMIT, "ci")["dropped"] == {}


def test_record_without_dataset_on_synthetic_results(tmp_path, monkeypatch):
    """A synthetic results file (no dataset key) works without --dataset."""
    results = {
        "results": [
            row("Q1", "rdflib_memory", "exec", 100.0),
            row("Q1", "rdflib_memory", "full", 200.0),
        ],
        "config": {
            "triples": 1000,
            "graphs": 1,
            "cardinality": None,
            "rowCounts": {},
            "adapters": [{"slug": "rdflib_memory", "label": "rdflib"}],
        },
        "failures": [],
    }

    path = tmp_path / "results.json"
    path.write_text(json.dumps(results))
    out = tmp_path / "out"

    monkeypatch.setattr("bench.history.commit_info", lambda *a, **kw: COMMIT)
    exit_code = history.main(
        [
            "record",
            str(path),
            "--source",
            "local",
            "--commit",
            "HEAD",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    records = list(out.glob("*.json"))
    assert records
    record = json.loads(records[0].read_text())
    assert "triples" in record["dataset"]
    assert "graphs" in record["dataset"]
    assert "cardinality" in record["dataset"]
