"""Per-adapter benchmark worker: ``python -m bench.worker <slug> <nq> <nt> <workdir> <out>``.

One process per adapter, spawned by ``run_bench.py``. Process isolation is
what makes the memory figures trustworthy: the kernel-tracked peak RSS
(``VmHWM``) covers exactly one store's lifecycle — parse/build, every query,
result materialization — with no residue from other adapters, and a crash
loses only this adapter's rows.

Timing is wall-clock ``perf_counter_ns`` around a full execute-and-consume of
each query (results are always iterated to exhaustion — lazy result setup
must not masquerade as query speed).

Every query yields two **modes** from one run, by splitting that run's span
where rdflib's own string path splits:

- ``exec`` — evaluating an already-translated algebra: the part a store's own
  speed can move.
- ``full`` — that plus the ``prepareQuery`` in front of it, which is the parse
  and algebra translation rdflib repeats on *every* string query. It is what
  an application passing a query string pays, and for a selective query it
  dominates — which is why the two are reported side by side rather than one
  of them alone.

One run, not two: ``run_once`` times the preparation and the evaluation as
adjacent spans, so ``full`` is a measured elapsed time and not two medians
added together. It reconstructs ``graph.query(<text>)`` because that call
*is* these two steps — ``SPARQLProcessor.query`` translates a string with
exactly the ``translateQuery(parseQuery(...))`` that ``prepareQuery`` wraps.
``tests/test_bench_worker.py`` pins that, including the part the timing
depends on: that rdflib caches nothing between calls.

Normal queries get one warmup — a real string query, so the row count the
prepared path reports is checked against the path it stands in for — then up
to ``BENCH_QUERY_ITERS`` samples capped by ``BENCH_QUERY_BUDGET_S``; ``heavy``
queries (full-scan class) get ``BENCH_HEAVY_ITERS`` fixed samples and no
warmup, so they carry no such check. The orchestrator's cross-store count
comparison still covers them.

The load is sampled ``BENCH_LOAD_ITERS`` times, each a full rebuild of the
store's own file from the shared source — the ``.nq`` for a store with named
graphs, the ``.nt`` for one without.

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

from rdflib.plugins.sparql import prepareQuery

from .adapters import BY_SLUG
from .dataset import config_from_env, moduli
from .queries import Query, build_queries

QUERY_ITERS = int(os.environ.get("BENCH_QUERY_ITERS", 10))
QUERY_MIN_ITERS = 3
QUERY_BUDGET_NS = float(os.environ.get("BENCH_QUERY_BUDGET_S", 2.0)) * 1e9
HEAVY_ITERS = int(os.environ.get("BENCH_HEAVY_ITERS", 3))
LOAD_ITERS = int(os.environ.get("BENCH_LOAD_ITERS", 3))
#: Measurement modes, in the order the dashboard shows them (see the module
#: docstring). `full` keeps the plain slug, so the ids it has always written
#: are unchanged and the load row needs no mode at all.
MODES = ("exec", "full")


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


def make_row(group: str, slug: str, samples_ns: list[float], mode: str = "full") -> dict:
    fastest, slowest = min(samples_ns), max(samples_ns)
    median, mean = statistics.median(samples_ns), statistics.fmean(samples_ns)
    variant = slug if mode == "full" else f"{slug}::{mode}"
    return {
        "group": group,
        "variant": variant,
        "id": f"{group}::{variant}",
        "mode": mode,
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


def consume(result, query: Query) -> int:
    """Materialize a result set fully; returns its row count.

    Always to exhaustion: lazy result setup must not masquerade as query
    speed. An ASK counts as one row when it answers true.
    """
    return (1 if result.askAnswer else 0) if query.is_ask else sum(1 for _ in result)


def run_string_once(graph, query: Query, query_kwargs: dict) -> tuple[int, float]:
    """`graph.query(<text>)`, end to end; returns (result rows, ns)."""
    t0 = perf_counter_ns()
    n = consume(graph.query(query.sparql, **query_kwargs), query)
    return n, float(perf_counter_ns() - t0)


def run_once(graph, query: Query, query_kwargs: dict, init_ns: dict) -> tuple[int, float, float]:
    """One sample of both modes; returns (result rows, prepare ns, evaluate ns).

    The two spans are adjacent and cover, in order, exactly what
    `graph.query(<text>)` does: parse, translate, evaluate, materialize. So
    their sum is a measured end-to-end time, and the second half alone is the
    evaluation. `init_ns` is hoisted out because `Graph.query` builds it once
    per call and already does so inside the second span.
    """
    t0 = perf_counter_ns()
    prepared = prepareQuery(query.sparql, initNs=init_ns)
    t1 = perf_counter_ns()
    n = consume(graph.query(prepared, **query_kwargs), query)
    t2 = perf_counter_ns()
    return n, float(t1 - t0), float(t2 - t1)


def measure_query(
    graph, query: Query, query_kwargs: dict, split: bool = True
) -> tuple[int, int | None, dict[str, list[float]]]:
    """Sample one query; returns (rows, rows the warmup string query saw, ns per mode).

    `split` off is a store that answers the query string itself, below
    rdflib's evaluator: there is no algebra for rdflib to prepare, so the run
    cannot be split and only `full` — the end-to-end figure both kinds of row
    report — is measured.
    """
    init_ns = dict(graph.namespaces())
    samples: dict[str, list[float]] = {mode: [] for mode in (MODES if split else ("full",))}

    def sample() -> int:
        if not split:
            rows, elapsed_ns = run_string_once(graph, query, query_kwargs)
            samples["full"].append(elapsed_ns)
            return rows
        rows, prepare_ns, evaluate_ns = run_once(graph, query, query_kwargs, init_ns)
        samples["exec"].append(evaluate_ns)
        samples["full"].append(prepare_ns + evaluate_ns)
        return rows

    if query.heavy:
        for _ in range(HEAVY_ITERS):
            matched = sample()
        return matched, None, samples

    # The warmup is the string path itself, so where the run is split it also
    # checks that the prepared path answers the same query. Discarded as a
    # sample either way.
    warmed, _ns = run_string_once(graph, query, query_kwargs)
    spent = 0.0
    while len(samples["full"]) < QUERY_ITERS:
        matched = sample()
        spent += samples["full"][-1]
        if len(samples["full"]) >= QUERY_MIN_ITERS and spent > QUERY_BUDGET_NS:
            break
    return matched, warmed, samples


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
            rows_matched, warmed, samples = measure_query(
                graph, query, adapter.query_kwargs, adapter.prepared
            )
        except Exception as error:  # noqa: BLE001 — one query must not sink the rest
            failures.append({"phase": query.name, "error": f"{type(error).__name__}: {error}"})
            print(f"[{slug}] !! {query.name} failed: {error}", flush=True)
            continue
        matched[query.name] = rows_matched
        # The prepared algebra must answer what the query text answers; the
        # `full` figure is only the string path's time if it is its work too.
        if warmed is not None and warmed != rows_matched:
            detail = f"prepared {rows_matched} rows, the query text {warmed}"
            failures.append({"phase": query.name, "error": detail})
            print(f"[{slug}] !! {query.name}: {detail}", flush=True)
        medians = {}
        for mode, mode_samples in samples.items():
            row = make_row(query.name, slug, mode_samples, mode)
            medians[mode] = row["median"]
            rows.append(row)
        shown = " / ".join(f"{medians[mode]} {mode}" for mode in MODES if mode in medians)
        print(
            f"[{slug}] {query.name}: median {shown} "
            f"({len(samples['full'])} samples, {matched[query.name]} rows)",
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
