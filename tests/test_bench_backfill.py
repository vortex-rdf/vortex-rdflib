"""The backfill's measuring order, and the records it keeps: each commit's, corrected for the
runner's drift that the anchor commit's repeated measurements show."""

import json
import subprocess

import pytest
from bench import backfill

ANCHOR = "a" * 40
OTHER = "b" * 40
STORE = "vortex_dict_file_copy"


def measurement(
    sha: str, position: int, store_ns: dict[str, float], dropped: list[str] | None = None
) -> dict:
    """A record of one measurement: rdflib takes 100 ns on every template, so a template's
    speedup is 100 over the store's time."""
    return {
        "schema": 1,
        "commit": {"sha": sha, "short": sha[:7]},
        "run": {
            "source": "backfill",
            "measured": f"2026-10-29T{position:02d}:00:00+00:00",
            "position": position,
        },
        "reference": "rdflib_memory",
        "medians": {
            "rdflib_memory": {mode: dict.fromkeys(store_ns, 100.0) for mode in ("exec", "full")},
            STORE: {mode: dict(store_ns) for mode in ("exec", "full")},
        },
        "dropped": {STORE: dropped} if dropped else {},
    }


def by_sha(records: list[dict]) -> dict[str, dict]:
    return {r["commit"]["sha"]: r for r in records}


def test_the_anchor_opens_the_job_returns_after_every_few_others_and_closes_it():
    order = backfill.schedule(["a", "b", "c", "d", "e", "z"], anchor="z", every=2)
    assert order == ["z", "a", "b", "z", "c", "d", "z", "e", "z"]


@pytest.mark.parametrize(("anchor", "every"), [("", 4), ("a", 0)], ids=["no anchor", "every 0"])
def test_without_an_anchor_the_list_is_measured_as_given(anchor, every):
    assert backfill.schedule(["a", "b", "a"], anchor=anchor, every=every) == ["a", "b", "a"]


def test_the_anchor_listed_among_the_others_is_measured_as_the_anchor_alone():
    assert backfill.schedule(["z", "a", "z", "b", "z"], anchor="z", every=4) == ["z", "a", "b", "z"]


def test_a_commit_measured_twice_keeps_its_last_measurement():
    records = [
        measurement(OTHER, 1, {"Q1": 50.0}),
        measurement(ANCHOR, 2, {"Q1": 50.0}),
        measurement(OTHER, 3, {"Q1": 40.0}),
    ]
    kept = by_sha(backfill.collect(records, anchor=None))
    assert kept[OTHER]["run"]["position"] == 3
    assert "drift" not in kept[OTHER]


def test_a_record_between_two_anchor_measurements_takes_the_drift_interpolated_there():
    # the anchor runs 2.0x faster than rdflib at position 1, 1.6x at position 5
    records = [
        measurement(ANCHOR, 1, {"Q1": 50.0}),
        measurement(OTHER, 3, {"Q1": 40.0}),
        measurement(ANCHOR, 5, {"Q1": 62.5}),
    ]
    kept = by_sha(backfill.collect(records, anchor=ANCHOR))
    # halfway, geometrically: 2.0 * sqrt(0.8), so the factor back to 2.0 is 1 / sqrt(0.8)
    factors = kept[OTHER]["drift"]["factors"][STORE]
    assert factors == {"exec": pytest.approx(1.118034, abs=1e-4), "full": factors["exec"]}
    assert kept[OTHER]["drift"]["anchor"]["sha"] == ANCHOR


def test_the_anchor_keeps_its_first_measurement_uncorrected():
    records = [
        measurement(ANCHOR, 1, {"Q1": 50.0}),
        measurement(OTHER, 2, {"Q1": 40.0}),
        measurement(ANCHOR, 3, {"Q1": 62.5}),
    ]
    anchor = by_sha(backfill.collect(records, anchor=ANCHOR))[ANCHOR]
    assert anchor["run"]["position"] == 1
    assert "drift" not in anchor


def test_a_record_after_the_last_anchor_measurement_takes_its_drift():
    records = [
        measurement(ANCHOR, 1, {"Q1": 50.0}),
        measurement(ANCHOR, 3, {"Q1": 62.5}),
        measurement(OTHER, 4, {"Q1": 40.0}),
    ]
    factors = by_sha(backfill.collect(records, anchor=ANCHOR))[OTHER]["drift"]["factors"]
    assert factors[STORE]["exec"] == pytest.approx(1.25)


def test_one_anchor_measurement_corrects_nothing():
    records = [measurement(ANCHOR, 1, {"Q1": 50.0}), measurement(OTHER, 2, {"Q1": 40.0})]
    assert "drift" not in by_sha(backfill.collect(records, anchor=ANCHOR))[OTHER]


def test_a_store_the_anchor_never_measured_gets_no_factor():
    other = measurement(OTHER, 2, {"Q1": 40.0})
    other["medians"]["vortex_dict_mem"] = other["medians"].pop(STORE)
    records = [measurement(ANCHOR, 1, {"Q1": 50.0}), other, measurement(ANCHOR, 3, {"Q1": 62.5})]
    assert by_sha(backfill.collect(records, anchor=ANCHOR))[OTHER]["drift"]["factors"] == {}


def test_a_template_the_anchor_dropped_once_is_left_out_of_every_anchor_measurement():
    # Q2 alone reads 4x at position 1 and is dropped at position 5: over Q1 alone the anchor
    # drifts from 2.0 to 1.6, a factor of 1.25 at position 5
    records = [
        measurement(ANCHOR, 1, {"Q1": 50.0, "Q2": 25.0}),
        measurement(ANCHOR, 5, {"Q1": 62.5, "Q2": 25.0}, dropped=["Q2"]),
        measurement(OTHER, 5, {"Q1": 40.0, "Q2": 40.0}),
    ]
    factors = by_sha(backfill.collect(records, anchor=ANCHOR))[OTHER]["drift"]["factors"]
    assert factors[STORE]["exec"] == pytest.approx(1.25)


def head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def write_all(folder, records: list[dict]) -> list[str]:
    folder.mkdir()
    paths = []
    for i, record in enumerate(records):
        path = folder / f"{i}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(str(path))
    return paths


def test_collect_names_the_anchor_by_any_revision_and_writes_a_record_per_commit(tmp_path):
    head = head_sha()
    paths = write_all(
        tmp_path / "measured",
        [
            measurement(head, 1, {"Q1": 50.0}),
            measurement(OTHER, 2, {"Q1": 40.0}),
            measurement(head, 3, {"Q1": 62.5}),
        ],
    )
    out = tmp_path / "records"
    assert backfill.main(["collect", "--anchor", "HEAD", "--out", str(out), *paths]) == 0
    anchor = json.loads((out / f"{head}.json").read_text(encoding="utf-8"))
    other = json.loads((out / f"{OTHER}.json").read_text(encoding="utf-8"))
    assert anchor["run"]["position"] == 1
    assert other["drift"]["factors"][STORE]["exec"] == pytest.approx(1.118034, abs=1e-4)


def test_collect_refuses_a_record_that_does_not_know_its_place(tmp_path):
    record = measurement(OTHER, 1, {"Q1": 40.0})
    del record["run"]["position"]
    paths = write_all(tmp_path / "measured", [record])
    with pytest.raises(SystemExit) as stop:
        backfill.main(["collect", "--out", str(tmp_path / "records"), *paths])
    assert stop.value.code == 2
