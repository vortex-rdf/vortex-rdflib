"""Per-adapter benchmark worker:
``python -m bench.worker <slug> <nq> <nt> <workdir> <out> [--phase all|load|query] [--bsbm DIR]``.

Each adapter runs in worker processes of its own, spawned by ``run_bench.py``.
Process isolation is what makes the memory figures trustworthy: the
kernel-tracked peak RSS (``VmHWM``) covers one store's process with no residue
from other adapters, and a crash loses only this adapter's rows. ``--phase all`` (the default) loads
the store and answers the queries in one process. A store built into a file
(``Adapter.reopen``) runs as two: ``--phase load`` builds it, timed as the load
row, and ``--phase query`` opens the built file in a fresh process and answers
the queries, as an application opens a built store. The build's allocations
set glibc's dynamic trim and mmap thresholds for the rest of a process, so a
query run after an in-process build can fault its freed buffers in again where
a user's process does not, or the other way round.

Timing is wall-clock ``perf_counter_ns`` around a full execute-and-consume of
each query (results are always iterated to exhaustion — lazy result setup
must not masquerade as query speed). Every sample also counts the process's
minor page faults over the same span (``procmem.minor_faults``); a row carries
the median per sample as ``minflt``.

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

The native adapter (pyoxigraph) has neither half to split: its store takes
the query text and returns its own results, which ``consume_native`` reads to
the last value, so it reports ``full`` alone. rdflib is imported only for the
rdflib rows — before their baseline RSS is read, as it always was, so it
counts toward no store's footprint — and the native worker's process never
loads it: its peak RSS would otherwise carry a library it does not use.

Normal queries get one warmup — a real string query, so the row count the
prepared path reports is checked against the path it stands in for — then up
to ``BENCH_QUERY_ITERS`` samples capped by ``BENCH_QUERY_BUDGET_S``; ``heavy``
queries (full-scan class) get ``BENCH_HEAVY_ITERS`` samples and no warmup, so
they carry no such check. The orchestrator's cross-store count comparison
still covers them. ``BENCH_HEAVY_BUDGET_S`` caps a heavy query's samples by
their time as well, at no fewer than three: a store that answers it in
milliseconds takes every sample, one that takes seconds stops early.

With ``BENCH_FRESH_CONSTANTS=1``, a query with a FILTER constant asks a new
constant in every sample (its ``Query.fresh`` variants) instead of repeating
its text, so work a store memoizes per constant is paid in every sample, as in
a BSBM run (see ``measure_query``). The orchestrator marks such results
(``config.freshConstants``): their history record is another dataset.

The load is sampled ``BENCH_LOAD_ITERS`` times, each a full rebuild of the
store's own file from the shared source — the ``.nq`` for a store with named
graphs, the ``.nt`` for one without — and its open.

A query that names a graph is skipped for a store that does not serve them,
and reported as ``skipped`` rather than a failure: nobody could have measured
it. ``BENCH_QUERIES`` (comma-separated names) measures those queries alone, as
a confirmation of a few cells does (``bench.compare``).

With ``--bsbm DIR`` (``run_bench --dataset bsbm``), the worker builds the store
from DIR's BSBM ``dataset.nt`` and runs DIR's streams instead of the synthetic
query set: see ``main_bsbm``.
"""

import argparse
import gc
import importlib
import json
import os
import statistics
import sys
from dataclasses import replace
from time import perf_counter_ns

from .adapters import BY_SLUG, Adapter
from .dataset import config_from_env, moduli
from .procmem import minor_faults, peak_rss_mb, rss_anon_mb, rss_mb
from .queries import Query, build_queries

QUERY_ITERS = int(os.environ.get("BENCH_QUERY_ITERS", 10))
QUERY_MIN_ITERS = 3
QUERY_BUDGET_NS = float(os.environ.get("BENCH_QUERY_BUDGET_S", 2.0)) * 1e9
HEAVY_ITERS = int(os.environ.get("BENCH_HEAVY_ITERS", 3))
HEAVY_BUDGET_NS = float(os.environ.get("BENCH_HEAVY_BUDGET_S", "inf")) * 1e9
LOAD_ITERS = int(os.environ.get("BENCH_LOAD_ITERS", 3))
#: ``BENCH_QUERIES``: the names of the queries to measure; every query when unset.
ONLY_QUERIES = frozenset(q for q in os.environ.get("BENCH_QUERIES", "").split(",") if q)
FRESH_CONSTANTS_ENV = "BENCH_FRESH_CONSTANTS"
#: Sample a query's fresh-constant variants (``Query.fresh``) instead of
#: repeating its text, so per-constant costs cannot hide behind warm caches.
FRESH_CONSTANTS = os.environ.get(FRESH_CONSTANTS_ENV) == "1"
#: Measurement modes, in the order the dashboard shows them (see the module
#: docstring). `full` keeps the plain slug, so the ids it has always written
#: are unchanged and the load row needs no mode at all.
MODES = ("exec", "full")


def fmt_ns(ns: float) -> str:
    if ns < 1e3:
        return f"{ns:.0f} ns"
    if ns < 1e6:
        return f"{ns / 1e3:.3g} µs"
    if ns < 1e9:
        return f"{ns / 1e6:.3g} ms"
    return f"{ns / 1e9:.3g} s"


def make_row(
    group: str,
    slug: str,
    samples_ns: list[float],
    mode: str = "full",
    faults: list[int] | None = None,
) -> dict:
    """One results row; with ``faults`` (minor page faults per sample), their
    median as ``minflt``."""
    fastest, slowest = min(samples_ns), max(samples_ns)
    median, mean = statistics.median(samples_ns), statistics.fmean(samples_ns)
    variant = slug if mode == "full" else f"{slug}::{mode}"
    row = {
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
    if faults:
        row["minflt"] = round(statistics.median(faults))
    return row


def _faults_since(start: int | None) -> int | None:
    now = minor_faults()
    return None if start is None or now is None else now - start


def consume(result, query: Query) -> int:
    """Materialize a result set fully; returns its row count.

    Always to exhaustion: lazy result setup must not masquerade as query
    speed. An ASK counts as one row when it answers true.
    """
    return (1 if result.askAnswer else 0) if query.is_ask else sum(1 for _ in result)


def consume_native(result, query: Query) -> int:
    """`consume` for a pyoxigraph result, which rdflib never touches.

    An rdflib row arrives holding its terms; a pyoxigraph solution keeps its
    values on the Rust side and builds each Python term only when it is read.
    So every value is read: the row ends where the rdflib rows end, with the
    answer's terms in Python, instead of timing solutions nobody looked at.
    """
    if query.is_ask:
        return 1 if result else 0
    rows = 0
    for solution in result:
        tuple(solution)
        rows += 1
    return rows


def run_string_once(
    graph, query: Query, query_kwargs: dict, native: bool = False
) -> tuple[int, float, int | None]:
    """`graph.query(<text>)`, end to end; returns (result rows, ns, minor faults).

    `native` is a pyoxigraph store in place of an rdflib graph: it takes the
    query text the same way, and returns results of its own.
    """
    count = consume_native if native else consume
    f0 = minor_faults()
    t0 = perf_counter_ns()
    n = count(graph.query(query.sparql, **query_kwargs), query)
    elapsed = float(perf_counter_ns() - t0)
    return n, elapsed, _faults_since(f0)


def run_once(
    graph, query: Query, query_kwargs: dict, init_ns: dict
) -> tuple[int, float, float, int | None, int | None]:
    """One sample of both modes; returns (result rows, prepare ns, evaluate ns,
    prepare faults, evaluate faults).

    The two spans are adjacent and cover, in order, exactly what
    `graph.query(<text>)` does: parse, translate, evaluate, materialize. So
    their sum is a measured end-to-end time, and the second half alone is the
    evaluation. `init_ns` is hoisted out because `Graph.query` builds it once
    per call and already does so inside the second span. The minor page faults
    are counted over the same two spans; each count is read outside both timed
    spans.
    """
    # Here rather than at the top, so the native worker never loads rdflib.
    from rdflib.plugins.sparql import prepareQuery

    f0 = minor_faults()
    t0 = perf_counter_ns()
    prepared = prepareQuery(query.sparql, initNs=init_ns)
    t1 = perf_counter_ns()
    f1 = minor_faults()
    t2 = perf_counter_ns()
    n = consume(graph.query(prepared, **query_kwargs), query)
    t3 = perf_counter_ns()
    prepare_faults = None if f0 is None or f1 is None else f1 - f0
    return n, float(t1 - t0), float(t3 - t2), prepare_faults, _faults_since(f1)


def measure_query(
    graph, query: Query, query_kwargs: dict, native: bool = False
) -> tuple[int, int | None, dict[str, list[float]], dict[str, list[int]]]:
    """Sample one query; returns (rows, rows the warmup string query saw, ns per
    mode, minor faults per mode).

    `native` is a store that answers the query text itself, with no rdflib
    involved: there is no algebra for rdflib to prepare, so the run cannot be
    split and only `full` — the end-to-end figure both kinds of row report —
    is measured.

    With `FRESH_CONSTANTS`, a query that has variants asks a new one in every
    sample, so no answer repeats to be checked: the second item is None, and
    the rows are the warmup's (a heavy query has no warmup: its last sample's,
    the same variant for every store).

    The fault lists are parallel to the samples, and empty where the platform
    counts none.
    """
    init_ns = {} if native else dict(graph.namespaces())
    modes = ("full",) if native else MODES
    samples: dict[str, list[float]] = {mode: [] for mode in modes}
    faults: dict[str, list[int]] = {mode: [] for mode in modes}
    # Fresh-constants mode: sample k asks the query's k-th constant (k = 1, 2, ...).
    fresh = query.fresh if FRESH_CONSTANTS else None

    def variant(k: int) -> Query:
        return query if fresh is None else replace(query, sparql=fresh(k))

    def sample() -> int:
        target = variant(len(samples["full"]) + 1)
        if native:
            rows, elapsed_ns, run_faults = run_string_once(graph, target, query_kwargs, native=True)
            samples["full"].append(elapsed_ns)
            if run_faults is not None:
                faults["full"].append(run_faults)
            return rows
        rows, prepare_ns, evaluate_ns, prepare_faults, evaluate_faults = run_once(
            graph, target, query_kwargs, init_ns
        )
        samples["exec"].append(evaluate_ns)
        samples["full"].append(prepare_ns + evaluate_ns)
        if prepare_faults is not None and evaluate_faults is not None:
            faults["exec"].append(evaluate_faults)
            faults["full"].append(prepare_faults + evaluate_faults)
        return rows

    if query.heavy:
        spent = 0.0
        for _ in range(HEAVY_ITERS):
            matched = sample()
            spent += samples["full"][-1]
            if len(samples["full"]) >= QUERY_MIN_ITERS and spent > HEAVY_BUDGET_NS:
                break
        return matched, None, samples, faults

    # The warmup is the string path itself, so where the run is split it also
    # checks that the prepared path answers the same query. Discarded as a
    # sample either way. In fresh mode it asks the original text (k = 0) and
    # every sample its own variant, with its own answer: nothing to check. The
    # rows reported are then the warmup's: the time budget ends each store's
    # samples at a different k, and only k = 0 is asked of every store.
    warmed, _ns, _faults = run_string_once(graph, variant(0), query_kwargs, native)
    spent = 0.0
    while len(samples["full"]) < QUERY_ITERS:
        matched = sample()
        spent += samples["full"][-1]
        if len(samples["full"]) >= QUERY_MIN_ITERS and spent > QUERY_BUDGET_NS:
            break
    if fresh is not None:
        return warmed, None, samples, faults
    return matched, warmed, samples, faults


def import_engine(engine: str) -> None:
    """Load what an adapter's engine needs before the baseline RSS is read.

    rdflib is the engine every rdflib row shares, so it belongs in the
    baseline rather than in any one store's footprint — where it sat while
    this module imported it at the top. The native row never loads it.
    """
    if engine == "rdflib":
        importlib.import_module("rdflib.plugins.sparql")


def load_store(
    adapter: Adapter, nq_path: str, nt_path: str, work_dir: str
) -> tuple[object, dict, int | None, int | None]:
    """Build ``adapter``'s store LOAD_ITERS times, keeping the last; returns
    (store, load row, baseline RSS, RSS after the first build).

    Every sample is a full build from the same source file. The footprint is
    read off the FIRST build: freeing a store returns its pages to the
    allocator, not the OS, so a later build's delta collapses to near zero.
    Each later build releases its predecessor first, so only one store is ever
    live and the peak-RSS figure stays a single store's lifecycle.
    """
    import_engine(adapter.engine)
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
    row = make_row("load", adapter.slug, load_samples)
    print(
        f"[{adapter.slug}] loaded in {row['median']} ({len(load_samples)} samples)"
        f" — RSS {baseline_mb} -> {loaded_mb} MB",
        flush=True,
    )
    return graph, row, baseline_mb, loaded_mb


def open_built(adapter: Adapter, work_dir: str) -> tuple[object, int | None, int | None]:
    """Open the file a ``--phase load`` process built (``Adapter.reopen``) in
    this fresh process; returns (store, baseline RSS, RSS after the open)."""
    if adapter.reopen is None:
        raise SystemExit(f"{adapter.slug} has no built file to open: run it with --phase all")
    import_engine(adapter.engine)
    gc.collect()
    baseline_mb = rss_mb()
    t0 = perf_counter_ns()
    graph = adapter.reopen(work_dir)
    opened_ns = perf_counter_ns() - t0
    gc.collect()
    loaded_mb = rss_mb()
    print(
        f"[{adapter.slug}] opened the built store in {fmt_ns(opened_ns)}"
        f" — RSS {baseline_mb} -> {loaded_mb} MB",
        flush=True,
    )
    return graph, baseline_mb, loaded_mb


def main_bsbm(
    adapter: Adapter,
    graph,
    rows: list[dict],
    baseline_mb: int | None,
    loaded_mb: int | None,
    out_path: str,
    bsbm_dir: str,
) -> int:
    """BSBM mode, over the store loaded or opened: run the warm-up stream, then
    every measured instance once (``bench.bsbm.execute``), with
    ``BSBM_QUERY_TIMEOUT_S`` (default 5 s) and ``BSBM_STORE_BUDGET_S`` (default
    300 s); 0 turns either off. ``rows`` holds the load row, when this process
    loaded the store."""
    from .bsbm import report
    from .bsbm.execute import (
        DEFAULT_QUERY_TIMEOUT_S,
        DEFAULT_STORE_BUDGET_S,
        QUERY_TIMEOUT_ENV,
        STORE_BUDGET_ENV,
        execute_stream,
        limit_from_env,
    )
    from .bsbm.streams import load_streams

    warmup, measured = load_streams(bsbm_dir)
    native = adapter.engine == "native"
    anon = [mb for mb in (rss_anon_mb(),) if mb is not None]
    run = execute_stream(
        graph,
        warmup,
        measured,
        native=native,
        query_kwargs=adapter.query_kwargs,
        query_timeout_s=limit_from_env(QUERY_TIMEOUT_ENV, DEFAULT_QUERY_TIMEOUT_S),
        store_budget_s=limit_from_env(STORE_BUDGET_ENV, DEFAULT_STORE_BUDGET_S),
    )
    store = report.store_report(adapter.slug, run, native)
    anon += [mb for mb in run["rss_anon_per_mix_mb"] if mb is not None]
    out = {
        "rows": [*rows, *store.pop("rows")],
        "bsbm": store,
        "failures": report.error_failures(store),
        "baselineMb": baseline_mb,
        "loadedMb": loaded_mb,
        "peakRssMb": peak_rss_mb(),
        "peakAnonMb": max(anon) if anon else None,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    qmph = f"{store['qmph']:,.0f}" if store["qmph"] else "n/a"
    print(
        f"[{adapter.slug}] BSBM: {store['mixes']}/{store['plannedMixes']} mixes, QMpH {qmph}, "
        f"{sum(store['timeouts'].values())} timeouts" + (" — partial" if store["partial"] else ""),
        flush=True,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bench.worker")
    parser.add_argument("slug", choices=sorted(BY_SLUG))
    parser.add_argument("nq_path")
    parser.add_argument("nt_path")
    parser.add_argument("work_dir")
    parser.add_argument("out_path")
    parser.add_argument(
        "--phase",
        choices=("all", "load", "query"),
        default="all",
        help="all: load and query in this process; load: build the store's file and time it; "
        "query: open the file a load process built, then answer the queries",
    )
    parser.add_argument("--bsbm", metavar="DIR", help="run DIR's BSBM streams (bench.bsbm)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    adapter = BY_SLUG[args.slug]
    if args.phase == "load":
        _graph, load_row, baseline_mb, loaded_mb = load_store(
            adapter, args.nq_path, args.nt_path, args.work_dir
        )
        out = {
            "rows": [load_row],
            "failures": [],
            "baselineMb": baseline_mb,
            "loadedMb": loaded_mb,
            "buildPeakRssMb": peak_rss_mb(),
        }
        with open(args.out_path, "w", encoding="utf-8") as f:
            json.dump(out, f)
        return 0

    rows: list[dict] = []
    if args.phase == "query":
        graph, baseline_mb, loaded_mb = open_built(adapter, args.work_dir)
    else:
        graph, load_row, baseline_mb, loaded_mb = load_store(
            adapter, args.nq_path, args.nt_path, args.work_dir
        )
        rows.append(load_row)
    if args.bsbm:
        return main_bsbm(adapter, graph, rows, baseline_mb, loaded_mb, args.out_path, args.bsbm)

    slug = adapter.slug
    cfg = config_from_env()
    queries = build_queries(cfg, moduli(cfg))
    matched: dict[str, int] = {}
    failures: list[dict] = []
    skipped: list[str] = []
    # Linux keeps no high-water mark for RssAnon: sampled after the load (or
    # the open) and after every query, its maximum is the store's own peak.
    anon_samples: list[int] = []

    def sample_anon() -> None:
        mb = rss_anon_mb()
        if mb is not None:
            anon_samples.append(mb)

    sample_anon()

    for query in queries:
        if ONLY_QUERIES and query.name not in ONLY_QUERIES:
            continue
        if query.quads and not adapter.quads:
            skipped.append(query.name)
            print(f"[{slug}] -- {query.name} skipped: no named graphs in rdflib", flush=True)
            continue
        try:
            rows_matched, warmed, samples, faults = measure_query(
                graph, query, adapter.query_kwargs, native=adapter.engine == "native"
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
            row = make_row(query.name, slug, mode_samples, mode, faults[mode])
            medians[mode] = row["median"]
            rows.append(row)
        shown = " / ".join(f"{medians[mode]} {mode}" for mode in MODES if mode in medians)
        faulted = f", {rows[-1]['minflt']:,} faults" if "minflt" in rows[-1] else ""
        print(
            f"[{slug}] {query.name}: median {shown} "
            f"({len(samples['full'])} samples, {matched[query.name]} rows{faulted})",
            flush=True,
        )
        sample_anon()

    out = {
        "rows": rows,
        "matched": matched,
        "failures": failures,
        "skipped": skipped,
        "baselineMb": baseline_mb,
        "loadedMb": loaded_mb,
        "peakRssMb": peak_rss_mb(),
        "peakAnonMb": max(anon_samples) if anon_samples else None,
    }
    with open(args.out_path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
