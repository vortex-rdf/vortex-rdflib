"""Place history records into a folder, keeping each commit's newest measurement.

    python3 -m bench.place_records SRC DST

Copies every ``SRC/*.json`` record into ``DST`` unless ``DST`` already holds a
record of that name measured later (``run.measured``). A record in ``DST`` that
cannot be read is replaced; a file in ``SRC`` that is no record is skipped. A
missing ``SRC`` places nothing. Stdlib only: the jobs that push to
``bench-history`` run it on the runner's own Python, with nothing installed.
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def measured(path: Path) -> datetime | None:
    """When the record at ``path`` was measured; None if it is unreadable or no record."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(record["run"]["measured"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def place(source: Path, target: Path) -> list[str]:
    """Copy each record of ``source`` into ``target`` unless ``target`` holds a later
    measurement of it. One note per file not copied."""
    notes: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.json")):
        new = measured(path)
        if new is None:
            notes.append(f"skipped {path}: not a record")
            continue
        old = measured(target / path.name)
        if old is not None and old > new:
            notes.append(f"kept {target / path.name}: measured later, at {old.isoformat()}")
            continue
        shutil.copyfile(path, target / path.name)
    return notes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python3 -m bench.place_records SRC DST", file=sys.stderr)
        return 2
    source, target = Path(args[0]), Path(args[1])
    if not source.is_dir():
        print(f"no records in {source}")
        return 0
    for note in place(source, target):
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
