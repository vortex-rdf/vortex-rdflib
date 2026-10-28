"""The backfill workflow's measuring order, and the records it keeps.

A long CI job runs slower as it goes, so a point measured late in it reads low.
The backfill measures one commit, the anchor, first, again after every few
others, and last. A commit's record is its last measurement and the anchor's its
first. Every other record carries ``drift``: per configuration and mode, the
factor that takes its speedups back to the anchor's first measurement of them,
read off the anchor's measurements around it (geometric interpolation, held past
the last). The history chart applies it. Records name their place in the job
(``run.position``, from ``bench.history record --position``).

    python -m bench.backfill schedule [--anchor REV --every N] COMMIT...
    python -m bench.backfill collect [--anchor REV] --out DIR RECORD...
"""

import argparse
import json
import math
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

from .history import CONFIGURATIONS, MODES, git, record_path

#: Per configuration and mode: the anchor's (position, speedup) at each measurement.
Series = dict[str, dict[str, list[tuple[int, float]]]]


def schedule(commits: list[str], anchor: str, every: int) -> list[str]:
    """The order to measure ``commits`` in: ``anchor`` first, again after every ``every``
    of the others, and last. The list as given without an anchor or with ``every`` 0."""
    if not anchor or every <= 0:
        return list(commits)
    others = [commit for commit in commits if commit != anchor]
    order = [anchor]
    for start in range(0, len(others), every):
        order += [*others[start : start + every], anchor]
    return order


def collect(records: list[dict], anchor: str | None) -> list[dict]:
    """One record per commit: its last measurement, or the anchor's first, the others
    with the drift the anchor's measurements show (none from fewer than two)."""
    by_commit: dict[str, list[dict]] = {}
    for record in sorted(records, key=lambda r: r["run"]["position"]):
        by_commit.setdefault(record["commit"]["sha"], []).append(record)
    anchors = by_commit.get(anchor, []) if anchor else []
    series = drift_series(anchors) if len(anchors) > 1 else None
    kept: list[dict] = []
    for sha, measured in by_commit.items():
        if sha == anchor:
            kept.append(measured[0])
            continue
        record = dict(measured[-1])
        if series is not None:
            position = record["run"]["position"]
            record["drift"] = {
                "anchor": {key: anchors[0]["commit"][key] for key in ("sha", "short")},
                "positions": [a["run"]["position"] for a in anchors],
                "factors": {
                    slug: {
                        mode: round(factor(points, position), 4) for mode, points in modes.items()
                    }
                    for slug, modes in series.items()
                    if slug in record["medians"]
                },
            }
        kept.append(record)
    return kept


def drift_series(anchors: list[dict]) -> Series:
    """The anchor's speedup at each measurement that has the configuration and mode, over
    the templates all of those kept. Left out where fewer than two have them."""
    series: Series = {}
    for slug in CONFIGURATIONS:
        for mode in MODES:
            answered = [(record, _answered(record, slug, mode)) for record in anchors]
            usable = [(record, templates) for record, templates in answered if templates]
            if len(usable) < 2:
                continue
            common = sorted(set.intersection(*(templates for _record, templates in usable)))
            if common:
                series.setdefault(slug, {})[mode] = [
                    (record["run"]["position"], _speedup(record, slug, mode, common))
                    for record, _templates in usable
                ]
    return series


def factor(points: list[tuple[int, float]], position: int) -> float:
    """The first point's speedup over the speedup at ``position``: geometric between the
    points around it, held past the last."""
    first = points[0][1]
    for (p0, s0), (p1, s1) in pairwise(points):
        if position <= p1:
            t = min(max((position - p0) / (p1 - p0), 0.0), 1.0)
            return first / math.exp(math.log(s0) + t * (math.log(s1) - math.log(s0)))
    return first / points[-1][1]


def _answered(record: dict, slug: str, mode: str) -> set[str]:
    reference = record["medians"][record["reference"]].get(mode, {})
    times = record["medians"].get(slug, {}).get(mode, {})
    dropped = set(record.get("dropped", {}).get(slug, ()))
    return {q for q in times if q in reference and q not in dropped}


def _speedup(record: dict, slug: str, mode: str, templates: list[str]) -> float:
    reference = record["medians"][record["reference"]][mode]
    times = record["medians"][slug][mode]
    return math.exp(sum(math.log(reference[q] / times[q]) for q in templates) / len(templates))


def _summary(record: dict) -> str:
    drift = record.get("drift")
    if drift is None:
        note = "as measured"
    else:
        values = [f for modes in drift["factors"].values() for f in modes.values()]
        note = f"drift x{min(values):.3f} to x{max(values):.3f}" if values else "no factor"
    return f"{record['run']['position']:>3}  {record['commit']['short']}  {note}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    order = commands.add_parser("schedule", help="print the measuring order, one commit a line")
    order.add_argument("--anchor", default="", help="the commit to measure again (default: none)")
    order.add_argument("--every", type=int, default=0, help="others between anchor measurements")
    order.add_argument("commits", nargs="*")
    keep = commands.add_parser("collect", help="write each commit's record, corrected for drift")
    keep.add_argument("--anchor", default="", help="the anchor, any revision (default: none)")
    keep.add_argument("--out", type=Path, required=True, help="folder to write the records to")
    keep.add_argument("records", type=Path, nargs="+", help="records written with --position")
    args = parser.parse_args(argv)

    if args.command == "schedule":
        print("\n".join(schedule(args.commits, args.anchor, args.every)))
        return 0

    records = [json.loads(path.read_text(encoding="utf-8")) for path in args.records]
    unplaced = [
        str(p) for p, r in zip(args.records, records, strict=True) if "position" not in r["run"]
    ]
    if unplaced:
        parser.error(f"no run.position in {', '.join(unplaced)}: record them with --position")
    anchor = None
    if args.anchor:
        try:
            anchor = git("rev-parse", "--verify", f"{args.anchor}^{{commit}}").strip()
        except subprocess.CalledProcessError:
            parser.error(f"--anchor {args.anchor} is not a commit")
    args.out.mkdir(parents=True, exist_ok=True)
    for record in collect(records, anchor):
        record_path(args.out, record).write_text(
            json.dumps(record, indent=1) + "\n", encoding="utf-8"
        )
        print(_summary(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
