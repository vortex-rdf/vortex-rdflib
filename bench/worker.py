"""Per-adapter benchmark worker: ``python -m bench.worker <slug> <nq> <nt> <workdir> <out>``.

One process per adapter, spawned by ``run_bench.py``. Process isolation is
what makes the memory figures trustworthy: the kernel-tracked peak RSS
(``VmHWM``) covers exactly one store's lifecycle — parse/build, every query,
result materialization — with no residue from other adapters, and a crash
loses only this adapter's rows.

Timing is wall-clock ``perf_counter_ns`` around a full execute-and-consume of
each query (results are always iterated to exhaustion — lazy result setup
must not masquerade as query speed). Normal queries get one warmup run, then
up to ``BENCH_QUERY_ITERS`` samples capped by a ``BENCH_QUERY_BUDGET_S`` time
budget; ``heavy`` queries (full-scan class) get ``BENCH_HEAVY_ITERS`` fixed
samples and no warmup. The load is sampled ``BENCH_LOAD_ITERS`` times, each a
full rebuild of the store's own file from the shared source — the ``.nq`` for
a store with named graphs, the ``.nt`` for one without.

A query that names a graph is skipped for a store that does not serve them,
and reported as ``skipped`` rather than a failure: nobody could have measured
it.
"""

import gc
import json
import os
import statistics
import sys
from time import perf_counter_ns

from .adapters import BY_SLUG
from .dataset import config_from_env, moduli
from .queries import Query, build_queries

QUERY_ITERS = int(os.environ.get("BENCH_QUERY_ITERS", 10))
QUERY_MIN_ITERS = 3
QUERY_BUDGET_NS = float(os.environ.get("BENCH_QUERY_BUDGET_S", 2.0)) * 1e9
HEAVY_ITERS = int(os.environ.get("BENCH_HEAVY_ITERS", 3))
LOAD_ITERS = int(os.environ.get("BENCH_LOAD_ITERS", 3))


def _status_mb(key: str) -> int | None:
    """A memory figure from /proc/self/status (Linux; None elsewhere)."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return round(int(line.split()[1]) / 1024)
    except OSError:
        pass
    return None


def rss_mb() -> int | None:
    return _status_mb("VmRSS")


def peak_rss_mb() -> int | None:
    return _status_mb("VmHWM")


def fmt_ns(ns: float) -> str:
    if ns < 1e3:
        return f"{ns:.0f} ns"
    if ns < 1e6:
        return f"{ns / 1e3:.3g} µs"
    if ns < 1e9:
        return f"{ns / 1e6:.3g} ms"
    return f"{ns / 1e9:.3g} s"


def make_row(group: str, slug: str, samples_ns: list[float]) -> dict:
    fastest, slowest = min(samples_ns), max(samples_ns)
    median, mean = statistics.median(samples_ns), statistics.fmean(samples_ns)
    return {
        "group": group,
        "variant": slug,
        "id": f"{group}::{slug}",
        "fastest": fmt_ns(fastest),
        "slowest": fmt_ns(slowest),
        "median": fmt_ns(median),
        "mean": fmt_ns(mean),
        "fastest_ns": fastest,
        "slowest_ns": slowest,
        "median_ns": median,
        "mean_ns": mean,
        "samples": str(len(samples_ns)),
    }


def run_once(graph, query: Query, query_kwargs: dict) -> tuple[int, float]:
    """Execute and fully consume one query; returns (result rows, ns)."""
    t0 = perf_counter_ns()
    result = graph.query(query.sparql, **query_kwargs)
    n = (1 if result.askAnswer else 0) if query.is_ask else sum(1 for _ in result)
    return n, float(perf_counter_ns() - t0)


def measure_query(graph, query: Query, query_kwargs: dict) -> tuple[int, list[float]]:
    if query.heavy:
        counts_and_times = [run_once(graph, query, query_kwargs) for _ in range(HEAVY_ITERS)]
        return counts_and_times[0][0], [t for _, t in counts_and_times]
    matched, _warmup_ns = run_once(graph, query, query_kwargs)
    samples: list[float] = []
    while len(samples) < QUERY_ITERS:
        samples.append(run_once(graph, query, query_kwargs)[1])
        if len(samples) >= QUERY_MIN_ITERS and sum(samples) > QUERY_BUDGET_NS:
            break
    return matched, samples


def main() -> int:
    slug, nq_path, nt_path, work_dir, out_path = sys.argv[1:6]
    adapter = BY_SLUG[slug]
    cfg = config_from_env()
    queries = build_queries(cfg, moduli(cfg))

    rows: list[dict] = []
    matched: dict[str, int] = {}
    failures: list[dict] = []
    skipped: list[str] = []

    # Build the store LOAD_ITERS times, keeping the last for the queries.
    # Every sample is a full build from the same source file: the factories
    # rewrite their own file rather than reusing one.
    #
    # The footprint is read off the FIRST build, against the baseline taken
    # before it. A later build is no good for that: freeing a store returns
    # its pages to the allocator, not the OS, so the next build reuses them
    # and the delta collapses to near zero. Each later build still releases
    # its predecessor first, so only one store is ever live and the peak-RSS
    # figure stays a single store's lifecycle.
    gc.collect()
    baseline_mb = rss_mb()
    loaded_mb = None
    load_samples: list[float] = []
    graph = None
    for iteration in range(LOAD_ITERS):
        if graph is not None:
            graph = None
            gc.collect()
        t0 = perf_counter_ns()
        graph = adapter.make(nq_path, nt_path, work_dir)
        load_samples.append(float(perf_counter_ns() - t0))
        if iteration == 0:
            gc.collect()
            loaded_mb = rss_mb()
    rows.append(make_row("load", slug, load_samples))
    print(
        f"[{slug}] loaded in {rows[-1]['median']} ({len(load_samples)} samples)"
        f" — RSS {baseline_mb} -> {loaded_mb} MB",
        flush=True,
    )

    for query in queries:
        if query.quads and not adapter.quads:
            skipped.append(query.name)
            print(f"[{slug}] -- {query.name} skipped: no named graphs in rdflib", flush=True)
            continue
        try:
            matched[query.name], samples = measure_query(graph, query, adapter.query_kwargs)
        except Exception as error:  # noqa: BLE001 — one query must not sink the rest
            failures.append({"phase": query.name, "error": f"{type(error).__name__}: {error}"})
            print(f"[{slug}] !! {query.name} failed: {error}", flush=True)
            continue
        rows.append(make_row(query.name, slug, samples))
        print(
            f"[{slug}] {query.name}: median {rows[-1]['median']} "
            f"({len(samples)} samples, {matched[query.name]} rows)",
            flush=True,
        )

    out = {
        "rows": rows,
        "matched": matched,
        "failures": failures,
        "skipped": skipped,
        "baselineMb": baseline_mb,
        "loadedMb": loaded_mb,
        "peakRssMb": peak_rss_mb(),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
