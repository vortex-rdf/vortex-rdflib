"""Per-commit history of the vortex configurations' speedup over rdflib.

A *record* is one benchmark run of one commit, reduced to what the
dashboard's history chart needs: every query's median time, exec and full,
for each vortex configuration and for the reference store, ``rdflib
(in-mem)``, plus the queries a configuration answered with a different row
count than rdflib. CI writes one per commit on ``main`` to the
``bench-history`` branch (``records/<sha>.json``), the backfill workflow
writes older commits' there, and ``scripts/refresh.sh --history`` writes
local ones to ``bench/history-local/``.

The chart plots, per record and configuration, rdflib's time divided by the
configuration's: how many times faster than rdflib, in the same run, so runs
on different machines stay roughly comparable.

    python -m bench.history adapters
    python -m bench.history record RESULTS --source ci|backfill|local
        --commit REV [--ref NAME] --out DIR
"""

import argparse
import json
import math
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .adapters import ADAPTERS
from .run_bench import cpu_model, version_of

SCHEMA = 1
REFERENCE = "rdflib_memory"
#: The configurations the chart plots, in the dashboard's row order. A
#: configuration's place here is its color slot, so its color never depends
#: on which configurations a render happens to contain.
CONFIGURATIONS = tuple(a.slug for a in ADAPTERS if a.slug.startswith("vortex_"))
MODES = ("exec", "full")
SOURCES = ("ci", "backfill", "local")


def make_record(results: dict, commit: dict, source: str) -> dict:
    """Reduce one ``run_bench`` results file to a history record."""
    config = results["config"]
    wanted = {*CONFIGURATIONS, REFERENCE}
    medians: dict[str, dict[str, dict[str, float]]] = {}
    for row in results["results"]:
        slug = row["variant"].split("::")[0]
        if slug in wanted and row["group"] != "load":
            modes = medians.setdefault(slug, {mode: {} for mode in MODES})
            modes[row.get("mode", "full")][row["group"]] = row["median_ns"]
    return {
        "schema": SCHEMA,
        "commit": commit,
        "run": {
            "source": source,
            "measured": datetime.now(UTC).isoformat(timespec="seconds"),
            "cpu": cpu_model(),
            "python": platform.python_version(),
            "versions": {package: version_of(package) for package in ("vortex-rdf", "rdflib")},
        },
        # Everything the generator was told, not only its size: a run with any
        # other knob set is another dataset, skipped beside main's line.
        "dataset": {
            "triples": config["triples"],
            "graphs": config["graphs"],
            "cardinality": config.get("cardinality"),
        },
        "reference": REFERENCE,
        "labels": {
            a["slug"]: a["label"] for a in config["adapters"] if a["slug"] in CONFIGURATIONS
        },
        "medians": medians,
        "dropped": dropped_queries(config.get("rowCounts", {}), results.get("failures", [])),
    }


def dropped_queries(
    row_counts: dict[str, dict[str, int]], failures: list[dict]
) -> dict[str, list[str]]:
    """Per configuration, the queries that failed or whose row count is not rdflib's.

    rdflib is the oracle rather than the majority: every configuration runs
    the same commit's library code, so a bug they share would outvote it. A
    query rdflib has no count for is skipped; it is never plotted. A failure
    names its query as its phase, and counts even when the worker kept its
    medians (a prepared run answering differently from the query text).
    """
    dropped: dict[str, set[str]] = {}
    for query, per_store in row_counts.items():
        expected = per_store.get(REFERENCE)
        if expected is None:
            continue
        for slug in CONFIGURATIONS:
            if slug in per_store and per_store[slug] != expected:
                dropped.setdefault(slug, set()).add(query)
    for failure in failures:
        slug, query = failure.get("slug"), failure.get("phase")
        if slug in CONFIGURATIONS and query in row_counts:
            dropped.setdefault(slug, set()).add(query)
    return {slug: sorted(queries) for slug, queries in dropped.items()}


def load_records(folder: Path) -> tuple[list[dict], list[str]]:
    """Every record in ``folder``, and a warning per file that can't be read."""
    if not folder.is_dir():
        return [], [f"no history folder at {folder}"]
    records: list[dict] = []
    warnings: list[str] = []
    for path in sorted(folder.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            warnings.append(f"skipped {path.name}: {error}")
    return records, warnings


def build_series(main: list[dict], local: list[dict]) -> dict | None:
    """The history chart's data: one point per record, one line per configuration.

    Records with an unknown schema, without reference medians, or on another
    dataset scale than the newest ``main`` one are skipped with a warning.
    Every point is a geometric mean over the same queries (those every kept
    record has reference medians for), so the lines stay comparable; a query
    a configuration lacks or answered wrongly is dropped from that point
    alone. ``None`` when there is nothing to plot.
    """
    warnings: list[str] = []
    main = sorted(
        (r for r in main if _usable(r, warnings)),
        key=lambda r: datetime.fromisoformat(r["commit"]["date"]),
    )
    local = sorted(
        (r for r in local if _usable(r, warnings)),
        key=lambda r: datetime.fromisoformat(r["run"]["measured"]),
    )
    if not main and not local:
        return None
    scale = (main or local)[-1]["dataset"]
    kept: list[dict] = []
    for record in [*main, *local]:
        if record["dataset"] == scale:
            kept.append(record)
        else:
            warnings.append(
                f"skipped {_name(record)}: dataset {record['dataset']}, plotting {scale}"
            )
    queries = sorted(
        set.intersection(*(set(r["medians"][r["reference"]][mode]) for r in kept for mode in MODES))
    )
    present = [s for s in CONFIGURATIONS if any(s in r["medians"] for r in kept)]
    labels: dict[str, str] = {}
    for record in kept:  # oldest first, so the newest label wins
        labels.update(record.get("labels", {}))
    return {
        "queries": len(queries),
        "configurations": [
            {"slug": s, "label": labels.get(s, s), "slot": CONFIGURATIONS.index(s) + 1}
            for s in present
        ],
        "points": [_point(record, present, queries) for record in kept],
        "warnings": warnings,
    }


def _point(record: dict, present: list[str], queries: list[str]) -> dict:
    reference = record["medians"][record["reference"]]
    values: dict[str, dict | None] = {}
    for slug in present:
        mine = record["medians"].get(slug)
        if mine is None:
            values[slug] = None
            continue
        wrong = set(record.get("dropped", {}).get(slug, ()))
        per_mode: dict[str, dict] = {}
        for mode in MODES:
            times = mine.get(mode, {})
            dropped = [q for q in queries if q in wrong or q not in times]
            used = [q for q in queries if q not in dropped]
            speedup = (
                math.exp(sum(math.log(reference[mode][q] / times[q]) for q in used) / len(used))
                if used
                else None
            )
            per_mode[mode] = {"speedup": speedup, "dropped": dropped}
        values[slug] = per_mode
    commit = record["commit"]
    return {
        "commit": {k: commit.get(k) for k in ("sha", "short", "date", "subject", "ref", "dirty")},
        "source": record["run"]["source"],
        "local": record["run"]["source"] == "local",
        "measured": record["run"]["measured"],
        "rdflib": record["run"].get("versions", {}).get("rdflib"),
        "values": values,
    }


def _usable(record: dict, warnings: list[str]) -> bool:
    if record.get("schema") != SCHEMA:
        warnings.append(f"skipped {_name(record)}: schema {record.get('schema')!r}, not {SCHEMA}")
        return False
    reference = record.get("medians", {}).get(record.get("reference"), {})
    if not all(reference.get(mode) for mode in MODES):
        warnings.append(f"skipped {_name(record)}: no {record.get('reference')} medians")
        return False
    return True


def _name(record: dict) -> str:
    commit = record.get("commit") or {}
    run = record.get("run") or {}
    return f"{commit.get('short', '?')} ({run.get('source', '?')})"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def commit_info(rev: str, ref: str | None, local: bool) -> dict:
    """The record's ``commit`` block, read from git."""
    sha, short, date, subject = git("log", "-1", "--format=%H%n%h%n%cI%n%s", rev).split("\n", 3)
    dirty = local and bool(git("status", "--porcelain", "--untracked-files=no").strip())
    if ref is None:
        ref = git("branch", "--show-current").strip() or short
    return {
        "sha": sha,
        "short": short,
        "date": date,
        "subject": subject.strip(),
        "ref": ref,
        "dirty": dirty,
    }


def record_path(folder: Path, record: dict) -> Path:
    """Where a record is written: by commit for main, by time for local runs."""
    commit = record["commit"]
    if record["run"]["source"] != "local":
        return folder / f"{commit['sha']}.json"
    stamp = datetime.fromisoformat(record["run"]["measured"]).strftime("%Y%m%dT%H%M%SZ")
    return folder / f"{stamp}-{commit['short']}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("adapters", help="the slugs a history run measures, for --adapters")
    rec = commands.add_parser("record", help="reduce a results file to a history record")
    rec.add_argument("results", type=Path, help="a run_bench results file")
    rec.add_argument("--source", choices=SOURCES, required=True)
    rec.add_argument("--commit", required=True, help="the commit those results measured")
    rec.add_argument("--ref", help="the branch it was measured on (default: the current one)")
    rec.add_argument("--out", type=Path, required=True, help="folder to write the record to")
    args = parser.parse_args(argv)

    if args.command == "adapters":
        print(",".join((*CONFIGURATIONS, REFERENCE)))
        return 0

    results = json.loads(args.results.read_text(encoding="utf-8"))
    commit = commit_info(args.commit, args.ref, local=args.source == "local")
    record = make_record(results, commit, args.source)
    if not record["medians"].get(REFERENCE):
        parser.error(
            f"{args.results} has no {REFERENCE} rows; measure with "
            "--adapters $(python -m bench.history adapters)"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    path = record_path(args.out, record)
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
