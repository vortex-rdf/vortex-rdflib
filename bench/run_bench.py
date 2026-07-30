"""Benchmark orchestrator: ``python -m bench.run_bench [--out bench/results.json]``.

Generates the synthetic dataset once, then runs one worker process per
adapter (see ``worker.py`` for why isolation matters), merges their rows into
the dashboard-shaped JSON consumed by ``scripts/render_bench_dashboard.py``,
and cross-checks that every store returned the same result count for every
query.

Scale knobs (env): ``BENCH_TRIPLES`` (default 250,000), ``BENCH_PREDICATES``,
``BENCH_SUBJ_RATIO``, ``BENCH_OBJ_RATIO``, ``BENCH_LITERAL_FRAC``,
``BENCH_QUERY_ITERS``, ``BENCH_HEAVY_ITERS``, ``BENCH_QUERY_BUDGET_S``.
A quick local run: ``BENCH_TRIPLES=20000 uv run python -m bench.run_bench``.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from .adapters import ADAPTERS, Adapter
from .dataset import config_from_env, moduli, write_ntriples
from .queries import build_queries

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_TIMEOUT_S = int(os.environ.get("BENCH_WORKER_TIMEOUT_S", 3600))


def run_worker(adapter: Adapter, nt_path: Path, work_dir: Path) -> dict | None:
    out_file = work_dir / f"worker-{adapter.slug}.json"
    cmd = [
        sys.executable,
        "-m",
        "bench.worker",
        adapter.slug,
        str(nt_path),
        str(work_dir),
        str(out_file),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, env={**os.environ, **adapter.env}, timeout=WORKER_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        print(f"[{adapter.slug}] worker timed out after {WORKER_TIMEOUT_S}s — skipping")
        return None
    if proc.returncode != 0:
        print(f"[{adapter.slug}] worker exited {proc.returncode} — skipping; others still run")
        return None
    with open(out_file, encoding="utf-8") as f:
        return json.load(f)


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown CPU"


def version_of(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "?"


def provenance(n: int, terms: int) -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    py = ".".join(map(str, sys.version_info[:3]))
    deps = " · ".join(
        f"{p} {version_of(p)}"
        for p in ("vortex-rdflib", "vortex-rdf", "rdflib", "oxrdflib", "pyoxigraph")
    )
    return (
        f"Measured {date} · Python {py} · {cpu_model()}, {os.cpu_count()} threads · "
        f"{n:,} triples, {terms:,} distinct terms · {deps} · "
        f"wall-clock perf_counter · one adapter per process, isolated"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="bench/results.json", help="output JSON path")
    parser.add_argument("--adapters", default=None, help="comma-separated slugs (default: all)")
    args = parser.parse_args()

    adapters = ADAPTERS
    if args.adapters:
        wanted = args.adapters.split(",")
        unknown = set(wanted) - {a.slug for a in ADAPTERS}
        if unknown:
            parser.error(f"unknown adapter slug(s): {', '.join(sorted(unknown))}")
        adapters = [a for a in ADAPTERS if a.slug in wanted]

    # Fail fast with the fix, not per-worker skips: a plain `uv sync` prunes
    # the bench group, silently uninstalling the contenders.
    if any(a.slug.startswith("oxrdflib") for a in adapters):
        try:
            import oxrdflib  # noqa: F401
        except ImportError:
            parser.error("oxrdflib is not installed — run `uv sync --group bench` first")

    cfg = config_from_env()
    m = moduli(cfg)
    queries = build_queries(cfg, m)

    work_dir = Path(tempfile.mkdtemp(prefix="vortex-rdflib-bench-"))
    nt_path = work_dir / "data.nt"
    print(f"Generating {cfg.n:,} triples ({m.terms:,} distinct terms) -> {nt_path}")
    write_ntriples(str(nt_path), cfg)

    results: list[dict] = []
    memory: list[dict] = []
    matched: dict[str, int] = {}
    failures: list[dict] = []

    for adapter in adapters:
        print(f"\n=== {adapter.label} ({adapter.slug}, engine: {adapter.engine}) ===")
        out = run_worker(adapter, nt_path, work_dir)
        if out is None:
            failures.append(
                {
                    "slug": adapter.slug,
                    "label": adapter.label,
                    "phase": "worker",
                    "error": "worker process failed or timed out",
                }
            )
            continue
        results.extend(out["rows"])
        loaded, baseline = out.get("loadedMb"), out.get("baselineMb")
        store_mb = (
            loaded - baseline if isinstance(loaded, int) and isinstance(baseline, int) else None
        )
        memory.append(
            {
                "slug": adapter.slug,
                "label": adapter.label,
                "engine": adapter.engine,
                "peakRssMb": out.get("peakRssMb"),
                "baselineMb": baseline,
                "loadedMb": loaded,
                "storeMb": store_mb,
            }
        )
        for f in out.get("failures", []):
            failures.append({"slug": adapter.slug, "label": adapter.label, **f})
        # Same query, same data -> the counts must agree across stores. A
        # mismatch is a correctness bug in one of them; surface it loudly
        # rather than keeping whichever store reported last.
        for name, n in out.get("matched", {}).items():
            if name not in matched:
                matched[name] = n
            elif matched[name] != n:
                print(
                    f"  !! {adapter.label} returned {n} rows for '{name}', "
                    f"but an earlier store returned {matched[name]}"
                )

    config = {
        "triples": cfg.n,
        "cardinality": {"nSubj": m.n_subj, "nPred": m.n_pred, "nObj": m.n_obj, "terms": m.terms},
        "matchedRows": matched,
        "adapters": [{"slug": a.slug, "label": a.label, "engine": a.engine} for a in adapters],
        "queries": [
            {"name": q.name, "group": q.group, "heavy": q.heavy, "sparql": q.sparql}
            for q in queries
        ],
        "queryIters": int(os.environ.get("BENCH_QUERY_ITERS", 10)),
        "heavyIters": int(os.environ.get("BENCH_HEAVY_ITERS", 3)),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provenance": provenance(cfg.n, m.terms),
        "results": results,
        "memory": memory,
        "config": config,
        "failures": failures,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"\nWrote {len(results)} benchmark rows, {len(memory)} memory readings"
        + (f", {len(failures)} failure(s)" if failures else "")
        + f" -> {out_path}"
    )
    for f in failures:
        print(f"  missing: {f['label']} / {f['phase']}: {f['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
