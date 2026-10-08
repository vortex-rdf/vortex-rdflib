#!/usr/bin/env python3
"""Render the benchmark results into the static HTML dashboard.

Usage:
    python3 scripts/render_bench_dashboard.py bench/results.json public/index.html
        [--bsbm bench/results-bsbm.json] [--history DIR] [--local DIR] [--bsbm-history DIR]

Both datasets' results go into one page, where every section has a tab per
dataset. A results file missing (or not given) disables its tabs with a note;
with neither, nothing renders. ``--history`` (``records/`` of the
``bench-history`` branch) and ``--local`` (``bench/history-local``) feed the
synthetic history chart, ``--bsbm-history`` (``records-bsbm/``) the BSBM one.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench.history import build_series, load_records  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "bench_dashboard_template.html"
#: Per-instance answers: for merges and history records, not the page.
PAGE_DROPS = ("answers",)


def history_data(history: Path | None, local: Path | None) -> dict | None:
    """The chart's series, printing every skipped record as a warning."""
    if history is None and local is None:
        return None
    main_records, warnings = load_records(history) if history else ([], [])
    local_records, local_warnings = load_records(local) if local else ([], [])
    series = build_series(main_records, local_records)
    for warning in [*warnings, *local_warnings, *(series["warnings"] if series else [])]:
        print(f"warning: {warning}", file=sys.stderr)
    return series


def load_results(path: Path | None, what: str) -> tuple[dict | None, str | None]:
    """A dataset's results, or None and why its tabs are disabled."""
    if path is None:
        return None, f"this render was given no {what} results"
    if not path.is_file():
        return None, f"{path} does not exist"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("results"):
        return None, f"{path} has no benchmark rows"
    return data, None


def page_set(data: dict, history: dict | None) -> dict:
    config = {k: v for k, v in data.get("config", {}).items() if k not in PAGE_DROPS}
    return {
        "results": data["results"],
        "memory": data.get("memory", []),
        "config": config,
        "failures": data.get("failures", []),
        "provenance": data.get("provenance", ""),
        "history": history,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("results", type=Path, help="bench/results.json (synthetic)")
    parser.add_argument("output", type=Path, help="the HTML file to write")
    parser.add_argument("--bsbm", type=Path, help="bench/results-bsbm.json")
    parser.add_argument("--history", type=Path, help="folder of main's synthetic history records")
    parser.add_argument("--local", type=Path, help="folder of local synthetic history records")
    parser.add_argument("--bsbm-history", type=Path, help="folder of main's BSBM history records")
    args = parser.parse_args()
    synthetic, synthetic_note = load_results(args.results, "synthetic")
    bsbm, bsbm_note = load_results(args.bsbm, "BSBM")
    if synthetic is None and bsbm is None:
        print(f"nothing to render: {synthetic_note}; {bsbm_note}", file=sys.stderr)
        return 1
    synthetic_history = history_data(args.history, args.local) if synthetic else None
    bsbm_history = history_data(args.bsbm_history, None) if bsbm else None
    sets = {
        "synthetic": page_set(synthetic, synthetic_history) if synthetic else None,
        "bsbm": page_set(bsbm, bsbm_history) if bsbm else None,
        "notes": {"synthetic": synthetic_note, "bsbm": bsbm_note},
    }
    # Commit subjects and query texts are free text: "</script>" or "<!--" in one
    # must not end the page's script, so every "<" goes in as <.
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATASETS__", json.dumps(sets).replace("<", "\\u003c")
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    for key, data, series, note in (
        ("synthetic", synthetic, synthetic_history, synthetic_note),
        ("bsbm", bsbm, bsbm_history, bsbm_note),
    ):
        if data is None:
            print(f"{key}: tab disabled ({note})")
            continue
        points = f", {len(series['points'])} history points" if series else ""
        memory = len(data.get("memory", []))
        print(f"{key}: {len(data['results'])} rows, {memory} memory readings{points}")
    print(f"rendered -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
