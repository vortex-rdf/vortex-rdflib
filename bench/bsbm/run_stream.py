"""Run a BSBM stream once, in order, as a BSBM driver would.

usage: python -m bench.bsbm.run_stream <store.vortex> (--file|--in-memory)
           <warmup.json> <measured.json> <out.json>
           [--query-timeout SECONDS] [--store-budget SECONDS]

``bench.bsbm.execute`` runs the streams (``prepare``'s warmup.json and
measured.json): warm-up untimed, then every measured instance once, timed as
prepare plus evaluate-and-consume, digested outside the timing, a garbage
collection before each (outside its timing), RssAnon after every mix.
PYTHONPATH decides which vortex-rdflib and vortex-rdf are measured;
``VORTEX_RDF_NATIVE_FILTERS`` is recorded. No limit applies unless given.
"""

import argparse
import json
import os
import sys
from time import perf_counter_ns

from .cli import add_residency, read_streams
from .execute import execute_stream, limit


def run(
    store_path: str,
    in_memory: bool,
    warmup: list[dict],
    measured: list[dict],
    *,
    query_timeout_s: float | None = None,
    store_budget_s: float | None = None,
) -> dict:
    import vortex_rdf
    from rdflib import Graph

    import vortex_rdflib
    from vortex_rdflib import VortexRdflibStore

    t0 = perf_counter_ns()
    store = VortexRdflibStore(store_path, in_memory=in_memory)
    graph = Graph(store=store)
    open_ns = perf_counter_ns() - t0
    stream = execute_stream(
        graph,
        warmup,
        measured,
        query_timeout_s=query_timeout_s,
        store_budget_s=store_budget_s,
        collect_each=True,
    )
    return {
        "vortex_rdflib": getattr(vortex_rdflib, "__version__", "?"),
        "vortex_rdflib_path": os.path.dirname(vortex_rdflib.__file__ or ""),
        "vortex_rdf": getattr(vortex_rdf, "__version__", "?"),
        "native_filters": os.environ.get("VORTEX_RDF_NATIVE_FILTERS", "default"),
        "in_memory": in_memory,
        "code_path": getattr(store, "_dict", None) is not None,
        "open_ns": open_ns,
        "query_timeout_s": query_timeout_s,
        "store_budget_s": store_budget_s,
        **stream,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store")
    add_residency(parser)
    parser.add_argument("warmup")
    parser.add_argument("measured")
    parser.add_argument("out")
    parser.add_argument(
        "--query-timeout",
        type=float,
        metavar="SECONDS",
        help="abort a query after this long; 0 (or a negative or infinite value) means no "
        "limit, as with BSBM_QUERY_TIMEOUT_S",
    )
    parser.add_argument(
        "--store-budget",
        type=float,
        metavar="SECONDS",
        help="stop after the mix during which the measured mixes pass this; 0 (or a negative "
        "or infinite value) means no limit, as with BSBM_STORE_BUDGET_S",
    )
    args = parser.parse_args(argv)
    warmup, measured = read_streams(args.warmup, args.measured)
    out = run(
        args.store,
        args.in_memory,
        warmup,
        measured,
        query_timeout_s=limit(args.query_timeout),
        store_budget_s=limit(args.store_budget),
    )
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)
    results = out["results"]
    total = sum((r.get("prep_ns") or 0) + r.get("exec_ns", 0) for r in results) / 1e9
    errors = sum("error" in r for r in results)
    timeouts = sum(bool(r.get("timeout")) for r in results)
    partial = (
        f", partial {out['mixes_done']}/{out['mixes_planned']} mixes" if out["partial"] else ""
    )
    peak = out["peak_anon_mb"]
    print(
        f"{len(results)} queries in {total:.1f} s ({errors} errors, {timeouts} timeouts{partial}); "
        f"peak RssAnon {peak if peak is not None else 'n/a'} MB",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
