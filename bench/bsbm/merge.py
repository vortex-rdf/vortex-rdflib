"""Merge the BSBM results of several CI jobs into one results file.

usage: python -m bench.bsbm.merge --out bench/results-bsbm.json PARTIAL.json...
           [--expect SLUG,SLUG,...]

Each CI job runs ``run_bench --dataset bsbm`` over one group of stores. This
joins their rows, memory, failures and answers; re-runs the answer check over
every store (each job could only check its own); and names every expected store
no job reported, so a group that timed out or crashed shows as a failure. A
missing file is skipped with a warning; none at all is an error.
"""

import argparse
import json
import sys
from pathlib import Path

from ..adapters import ADAPTERS, BY_SLUG
from . import report

#: Partial results that differ in any of these measured different things.
SAME_RUN = (
    "dataset",
    "products",
    "seed",
    "warmupMixes",
    "mixes",
    "mix",
    "tools",
    "instanceTemplates",
    "queryTimeoutS",
    "storeBudgetS",
)
STORE_KEYS = ("adapters", "skipped", "disputedRows", "templates", "stores", "answers")


def merge_payloads(payloads: list[dict], expected: list[str]) -> dict:
    first = payloads[0]["config"]
    for payload in payloads[1:]:
        for key in SAME_RUN:
            if payload["config"].get(key) != first.get(key):
                raise ValueError(
                    f"partial results disagree on {key!r}: {first.get(key)!r} vs "
                    f"{payload['config'].get(key)!r}"
                )
    order = {a.slug: n for n, a in enumerate(ADAPTERS)}
    entries: dict[str, dict] = {}
    results: list[dict] = []
    memory: list[dict] = []
    failures: list[dict] = []
    stores: dict[str, dict] = {}
    answers: dict[str, dict] = {}
    stamps: list[str] = []
    for payload in payloads:
        config = payload["config"]
        for entry in config["adapters"]:
            entries.setdefault(entry["slug"], entry)
        results += payload["results"]
        memory += payload["memory"]
        failures += payload["failures"]
        stores.update(config.get("stores", {}))
        answers.update(config.get("answers", {}))
        if payload.get("provenance") and payload["provenance"] not in stamps:
            stamps.append(payload["provenance"])
    for slug in expected:
        if slug not in entries:
            label = BY_SLUG[slug].label if slug in BY_SLUG else slug
            failures.append(
                {
                    "slug": slug,
                    "label": label,
                    "phase": "worker",
                    "error": "no results: the CI job measuring it did not finish",
                }
            )
    adapters = sorted(entries.values(), key=lambda e: order.get(e["slug"], len(order)))
    memory.sort(key=lambda m: order.get(m["slug"], len(order)))
    base = {k: v for k, v in first.items() if k not in STORE_KEYS}
    return report.assemble(
        base, adapters, results, memory, failures, stores, answers, " | ".join(stamps)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("partials", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--expect",
        default=",".join(a.slug for a in ADAPTERS),
        help="the stores some job should have reported (default: all)",
    )
    args = parser.parse_args(argv)
    payloads = []
    for path in args.partials:
        if path.is_file():
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            print(f"warning: no partial results at {path}", file=sys.stderr)
    if not payloads:
        print("no partial BSBM results to merge", file=sys.stderr)
        return 1
    try:
        merged = merge_payloads(payloads, [s for s in args.expect.split(",") if s])
    except ValueError as error:
        print(f"merge: {error}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    print(
        f"merged {len(payloads)} file(s): {len(merged['results'])} rows, "
        f"{len(merged['failures'])} failure(s) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
