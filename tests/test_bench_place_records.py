"""History records placed into a folder: a commit's newest measurement stays."""

import json
import subprocess
import sys
from pathlib import Path

from bench.place_records import place

ROOT = Path(__file__).resolve().parent.parent
EARLIER = "2026-10-26T21:04:09+00:00"
LATER = "2026-10-27T00:12:03+00:00"


def write(folder: Path, measured: str, source: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    record = {"run": {"source": source, "measured": measured}}
    (folder / "c.json").write_text(json.dumps(record), encoding="utf-8")


def source_of(folder: Path) -> str:
    return json.loads((folder / "c.json").read_text(encoding="utf-8"))["run"]["source"]


def run(*args: Path) -> subprocess.CompletedProcess:
    """The module as the record and push jobs run it: on a Python without site-packages."""
    command = [sys.executable, "-S", "-m", "bench.place_records", *map(str, args)]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def test_an_earlier_measurement_leaves_a_later_one_in_place(tmp_path):
    # a re-run render job's own CI record, over a backfill that re-measured the commit since
    write(tmp_path / "history", LATER, "backfill")
    write(tmp_path / "own", EARLIER, "ci")
    place(tmp_path / "own", tmp_path / "history")
    assert source_of(tmp_path / "history") == "backfill"


def test_a_later_measurement_replaces_an_earlier_one(tmp_path):
    write(tmp_path / "history", EARLIER, "ci")
    write(tmp_path / "own", LATER, "backfill")
    place(tmp_path / "own", tmp_path / "history")
    assert source_of(tmp_path / "history") == "backfill"


def test_a_record_the_folder_lacks_is_added_to_a_folder_made_for_it(tmp_path):
    write(tmp_path / "own", EARLIER, "ci")
    place(tmp_path / "own", tmp_path / "history" / "records")
    assert source_of(tmp_path / "history" / "records") == "ci"


def test_an_unreadable_record_in_place_is_replaced(tmp_path):
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "c.json").write_text("{", encoding="utf-8")
    write(tmp_path / "own", EARLIER, "ci")
    place(tmp_path / "own", tmp_path / "history")
    assert source_of(tmp_path / "history") == "ci"


def test_a_file_that_is_no_record_replaces_nothing(tmp_path):
    write(tmp_path / "history", EARLIER, "ci")
    (tmp_path / "own").mkdir()
    (tmp_path / "own" / "c.json").write_text("{}", encoding="utf-8")
    place(tmp_path / "own", tmp_path / "history")
    assert source_of(tmp_path / "history") == "ci"


def test_the_jobs_that_install_nothing_can_run_it(tmp_path):
    write(tmp_path / "own", EARLIER, "ci")
    done = run(tmp_path / "own", tmp_path / "history")
    assert done.returncode == 0, done.stderr
    assert source_of(tmp_path / "history") == "ci"


def test_a_folder_that_was_never_downloaded_places_nothing(tmp_path):
    # a failed measuring job uploads no record: the job placing them still succeeds
    done = run(tmp_path / "none", tmp_path / "history")
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "history").exists()
