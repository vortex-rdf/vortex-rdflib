"""Benchmark orchestrator: ``python -m bench.run_bench [--out bench/results.json]``.

Generates the synthetic dataset once, then runs one worker process per
adapter (see ``worker.py`` for why isolation matters), merges their rows into
the dashboard-shaped JSON consumed by ``scripts/render_bench_dashboard.py``,
and cross-checks that every store returned the same result count for every
query. Each worker emits two rows per query — the evaluation alone, and that
same run including the parse and algebra translation in front of it — which
the dashboard shows as a pair of columns (see ``worker.py``'s modes).

The dataset is generated twice, from one deterministic generator: as
N-Quads for the stores that serve named graphs, and as the N-Triples
flattening of the same statements for HDT and COTTAS, which do not (see
``bench.adapters``). Both hold the same triples, so every query that names no
graph is asked of every store over identical data (``bench.dataset``).

Scale knobs (env): ``BENCH_TRIPLES`` (default 250,000), ``BENCH_PREDICATES``,
``BENCH_SUBJ_RATIO``, ``BENCH_OBJ_RATIO``, ``BENCH_LITERAL_FRAC``,
``BENCH_GRAPHS`` (default 8, one of them the default graph),
``BENCH_QUERY_ITERS``, ``BENCH_HEAVY_ITERS``, ``BENCH_LOAD_ITERS``,
``BENCH_QUERY_BUDGET_S``.
A quick local run: ``BENCH_TRIPLES=20000 uv run python -m bench.run_bench``.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from importlib import metadata, util
from pathlib import Path
from shutil import which

from .adapters import ADAPTERS, Adapter
from .dataset import config_from_env, moduli, write_nquads, write_ntriples
from .queries import build_queries

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_TIMEOUT_S = int(os.environ.get("BENCH_WORKER_TIMEOUT_S", 3600))


def run_worker(
    adapter: Adapter, nq_path: Path, nt_path: Path, work_dir: Path, python: str
) -> dict | None:
    out_file = work_dir / f"worker-{adapter.slug}.json"
    cmd = [
        python,
        "-m",
        "bench.worker",
        adapter.slug,
        str(nq_path),
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


def ensure_venv(adapter: Adapter, work_dir: Path) -> str:
    """Create the isolated environment `adapter` runs in; return its python.

    The worker still runs `python -m bench.worker` from the repo root, so the
    venv only needs rdflib and the contender itself — never vortex-rdflib,
    which these adapters do not import.

    `CXXFLAGS` carries the include hdt-cpp 1.3.3 omits: its sources use
    `uint64_t` without including <cstdint>, which GCC 13+ rejects. Harmless
    for contenders that ship wheels.
    """
    venv_dir = work_dir / f"venv-{adapter.slug}"
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {**os.environ, "CXXFLAGS": "-include cstdint " + os.environ.get("CXXFLAGS", "")}
    print(f"[{adapter.slug}] building isolated env: {' '.join(adapter.venv_packages)}")
    subprocess.run(["uv", "venv", str(venv_dir)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), *adapter.venv_packages],
        check=True,
        capture_output=True,
        env=env,
    )
    return str(python)


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


def provenance(n: int, terms: int, graphs: int) -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    py = ".".join(map(str, sys.version_info[:3]))
    deps = " · ".join(
        f"{p} {version_of(p)}"
        for p in ("vortex-rdflib", "vortex-rdf", "rdflib", "oxrdflib", "pyoxigraph")
    )
    return (
        f"Measured {date} · Python {py} · {cpu_model()}, {os.cpu_count()} threads · "
        f"{n:,} quads in {graphs} graphs, {terms:,} distinct terms · {deps} · "
        f"wall-clock perf_counter · one adapter per process, isolated"
    )


def reconcile(
    counted: dict[str, dict[str, int]], adapters: list[Adapter], failures: list[dict]
) -> tuple[dict[str, int], list[str]]:
    """Agree one row count per query; report every store that dissents.

    The agreed count is the majority, not the first store to answer — the
    vortex rows run first, so first-wins would make this package's own output
    the reference it is being checked against.
    """
    label_of = {a.slug: a.label for a in adapters}
    agreed: dict[str, int] = {}
    disputed: list[str] = []
    for name, per_store in counted.items():
        tally = Counter(per_store.values())
        consensus, votes = tally.most_common(1)[0]
        agreed[name] = consensus
        if len(tally) == 1:
            continue
        disputed.append(name)
        others = f"{votes} of {len(per_store)} stores returned {consensus}"
        for slug, n in sorted(per_store.items()):
            if n == consensus:
                continue
            print(f"  !! {label_of.get(slug, slug)} returned {n} rows for '{name}'; {others}")
            failures.append(
                {
                    "slug": slug,
                    "label": label_of.get(slug, slug),
                    "phase": name,
                    "error": f"returned {n} rows; {others}",
                }
            )
    return agreed, disputed


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
    missing = sorted(
        {a.requires for a in adapters if a.requires and not util.find_spec(a.requires)}
    )
    if missing:
        parser.error(f"not installed: {', '.join(missing)} — run `uv sync --group bench` first")
    no_cli = sorted(
        {a.requires_cli for a in adapters if a.requires_cli and not which(a.requires_cli)}
    )
    if no_cli:
        parser.error(
            f"command(s) not on PATH: {', '.join(no_cli)} — the HDT builder comes from "
            "`cargo install hdt --features cli`"
        )

    cfg = config_from_env()
    m = moduli(cfg)
    queries = build_queries(cfg, m)

    work_dir = Path(tempfile.mkdtemp(prefix="vortex-rdflib-bench-"))
    nq_path, nt_path = work_dir / "data.nq", work_dir / "data.nt"
    print(
        f"Generating {cfg.n:,} quads in {m.n_graph} graphs "
        f"({m.terms:,} distinct terms) -> {nq_path}, flattened -> {nt_path}"
    )
    write_nquads(str(nq_path), cfg)
    write_ntriples(str(nt_path), cfg)

    results: list[dict] = []
    memory: list[dict] = []
    # query -> {slug: rows}. Reconciled after every worker has reported, so a
    # store that disagrees is named against what the others found, rather than
    # against whichever store happened to run first.
    counted: dict[str, dict[str, int]] = {}
    failures: list[dict] = []
    # Queries a store's format cannot express (a GRAPH clause against HDT or
    # COTTAS). Kept apart from `failures`: an empty cell there is a question
    # nobody could ask, not a store that fell over answering it.
    skipped: dict[str, list[str]] = {}

    for adapter in adapters:
        print(f"\n=== {adapter.label} ({adapter.slug}, engine: {adapter.engine}) ===")
        try:
            python = ensure_venv(adapter, work_dir) if adapter.venv_packages else sys.executable
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode(errors="replace").strip().splitlines()
            print(f"[{adapter.slug}] isolated env failed to build — skipping; others still run")
            failures.append(
                {
                    "slug": adapter.slug,
                    "label": adapter.label,
                    "phase": "venv",
                    "error": detail[-1] if detail else "uv failed",
                }
            )
            continue
        out = run_worker(adapter, nq_path, nt_path, work_dir, python)
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
        if out.get("skipped"):
            skipped[adapter.slug] = out["skipped"]
        for name, n in out.get("matched", {}).items():
            counted.setdefault(name, {})[adapter.slug] = n

    # Same query, same data -> every store must return the same number of
    # rows. A disagreement is a correctness bug in one of them, so it becomes
    # a reported failure: printing it would leave the dashboard showing a
    # single agreed count that nobody agreed on.
    matched, disputed = reconcile(counted, adapters, failures)

    config = {
        "triples": cfg.n,
        "graphs": m.n_graph,
        "disputedRows": disputed,
        "cardinality": {
            "nSubj": m.n_subj,
            "nPred": m.n_pred,
            "nObj": m.n_obj,
            "nGraph": m.n_graph,
            "terms": m.terms,
        },
        "matchedRows": matched,
        "skipped": skipped,
        "adapters": [
            {
                "slug": a.slug,
                "label": a.label,
                "engine": a.engine,
                "quads": a.quads,
                # Which measurement modes this row has: a store that answers
                # the query string itself reports only the end-to-end one.
                "modes": ["exec", "full"] if a.prepared else ["full"],
            }
            for a in adapters
        ],
        "queries": [
            {
                "name": q.name,
                "group": q.group,
                "heavy": q.heavy,
                "quads": q.quads,
                "countable": q.countable,
                "sparql": q.sparql,
            }
            for q in queries
        ],
        "queryIters": int(os.environ.get("BENCH_QUERY_ITERS", 10)),
        "heavyIters": int(os.environ.get("BENCH_HEAVY_ITERS", 3)),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provenance": provenance(cfg.n, m.terms, m.n_graph),
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
