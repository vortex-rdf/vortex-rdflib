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
        "dataset": {"triples": config["triples"], "graphs": config["graphs"]},
        "reference": REFERENCE,
        "labels": {
            a["slug"]: a["label"] for a in config["adapters"] if a["slug"] in CONFIGURATIONS
        },
        "medians": medians,
        "dropped": dropped_queries(config.get("rowCounts", {})),
    }


def dropped_queries(row_counts: dict[str, dict[str, int]]) -> dict[str, list[str]]:
    """Per configuration, the queries whose row count is not rdflib's.

    rdflib is the oracle rather than the majority: every configuration runs
    the same commit's library code, so a bug they share would outvote it. A
    query rdflib has no count for is skipped; it is never plotted.
    """
    dropped: dict[str, list[str]] = {}
    for query, per_store in row_counts.items():
        expected = per_store.get(REFERENCE)
        if expected is None:
            continue
        for slug in CONFIGURATIONS:
            if slug in per_store and per_store[slug] != expected:
                dropped.setdefault(slug, []).append(query)
    return {slug: sorted(queries) for slug, queries in dropped.items()}


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
