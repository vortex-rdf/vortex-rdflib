#!/usr/bin/env python3
"""Render bench/results.json into the static HTML dashboard.

Usage:
    uv run python -m bench.run_bench --out bench/results.json
    python3 scripts/render_bench_dashboard.py bench/results.json public/index.html
        [--history DIR] [--local DIR]

Unlike vortex-rdf's renderer (which parses divan's text tables), the Python
bench emits dashboard-shaped JSON directly, so this is pure template
substitution. ``--history`` (the ``records/`` folder of the ``bench-history``
branch) and ``--local`` (``bench/history-local``) add the history chart's
series (``bench.history.build_series``); without either, the chart stays
hidden.
"""

import argparse
import json
import sys
from pathlib import Path

# The history series come from the bench package; this script runs as a file,
# so the repo root is not on sys.path by itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench.history import build_series, load_records  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "bench_dashboard_template.html"


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("results", type=Path, help="bench/results.json")
    parser.add_argument("output", type=Path, help="the HTML file to write")
    parser.add_argument("--history", type=Path, help="folder of main's history records")
    parser.add_argument("--local", type=Path, help="folder of local history records")
    args = parser.parse_args()

    data = json.loads(args.results.read_text(encoding="utf-8"))
    if not data.get("results"):
        print("results.json has no benchmark rows — did the bench run?", file=sys.stderr)
        return 1
    history = history_data(args.history, args.local)

    html = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__BENCH_DATA__", json.dumps(data["results"]))
        .replace("__MEMORY_DATA__", json.dumps(data.get("memory", [])))
        .replace("__CONFIG_DATA__", json.dumps(data.get("config", {})))
        .replace("__FAILURES_DATA__", json.dumps(data.get("failures", [])))
        .replace("__PROVENANCE__", json.dumps(data.get("provenance", "")))
        # Commit subjects are free text: "</script>" in one must not end the
        # page's script. "<\/" is the same string to JSON and to JavaScript.
        .replace("__HISTORY_DATA__", json.dumps(history).replace("</", "<\\/"))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    points = f", {len(history['points'])} history points" if history else ""
    print(
        f"rendered {len(data['results'])} rows, {len(data.get('memory', []))} memory readings"
        f"{points} -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
