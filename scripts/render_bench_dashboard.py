#!/usr/bin/env python3
"""Render bench/results.json into the static HTML dashboard.

Usage:
    uv run python -m bench.run_bench --out bench/results.json
    python3 scripts/render_bench_dashboard.py bench/results.json public/index.html

Unlike vortex-rdf's renderer (which parses divan's text tables), the Python
bench emits dashboard-shaped JSON directly, so this is pure template
substitution.
"""

import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "bench_dashboard_template.html"


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <results.json> <output.html>", file=sys.stderr)
        return 1
    in_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])

    data = json.loads(in_path.read_text(encoding="utf-8"))
    if not data.get("results"):
        print("results.json has no benchmark rows — did the bench run?", file=sys.stderr)
        return 1

    html = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__BENCH_DATA__", json.dumps(data["results"]))
        .replace("__MEMORY_DATA__", json.dumps(data.get("memory", [])))
        .replace("__CONFIG_DATA__", json.dumps(data.get("config", {})))
        .replace("__FAILURES_DATA__", json.dumps(data.get("failures", [])))
        .replace("__PROVENANCE__", json.dumps(data.get("provenance", "")))
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(
        f"rendered {len(data['results'])} rows, {len(data.get('memory', []))} memory readings"
        f" -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
