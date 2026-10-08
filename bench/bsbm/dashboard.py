"""``run_bench --dataset bsbm``: every store over the official BSBM streams.

The synthetic bench's adapters and worker processes, on a directory
``bench.bsbm.prepare`` wrote: every store builds from ``dataset.nt``, given as
both its N-Quads and N-Triples source (N-Triples is valid N-Quads), runs the
warm-up, then each measured instance once (``bench.worker ... --bsbm DIR``).
The results file is rewritten after every store, so a cut-short run keeps every
store that finished. The work dir (isolated venvs, built stores: about 1 GB at
10K products) is removed at the end. Env: ``BSBM_QUERY_TIMEOUT_S`` (5),
``BSBM_STORE_BUDGET_S`` (300; 0 turns either off), ``BSBM_LOAD_ITERS`` (1: a
rebuild at BSBM scale costs minutes for the stores that parse).
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..adapters import Adapter
from ..run_bench import (
    adapter_entries,
    cpu_model,
    dependency_versions,
    measure_adapter,
    memory_entry,
)
from . import report, streams
from .execute import (
    DEFAULT_QUERY_TIMEOUT_S,
    DEFAULT_STORE_BUDGET_S,
    QUERY_TIMEOUT_ENV,
    STORE_BUDGET_ENV,
    limit_from_env,
)


def limits() -> dict:
    return {
        "queryTimeoutS": limit_from_env(QUERY_TIMEOUT_ENV, DEFAULT_QUERY_TIMEOUT_S),
        "storeBudgetS": limit_from_env(STORE_BUDGET_ENV, DEFAULT_STORE_BUDGET_S),
    }


def provenance(meta: dict, applied: dict) -> str:
    def limit(s: float | None) -> str:
        return "none" if s is None else f"{s:g} s"

    py = ".".join(map(str, sys.version_info[:3]))
    return " · ".join(
        [
            f"Measured {datetime.now(UTC):%Y-%m-%d}",
            f"Python {py}",
            f"{cpu_model()}, {os.cpu_count()} threads",
            f"BSBM {meta['products']:,} products, {meta['dataset']['triples']:,} triples "
            f"(official tools {meta['tools']['commit'][:7]})",
            f"{meta['warmup_mixes']} warm-up + {meta['mixes']} measured mixes, seed {meta['seed']}",
            f"per-query timeout {limit(applied['queryTimeoutS'])}, "
            f"store budget {limit(applied['storeBudgetS'])}",
            dependency_versions(),
            "one adapter per process, isolated",
        ]
    )


def run(adapters: list[Adapter], prepared: Path, out_path: Path) -> int:
    meta = streams.read_meta(prepared)
    dataset = streams.dataset_path(prepared, meta).resolve()  # the worker's cwd is the repo root
    _warmup, measured = streams.load_streams(prepared)
    applied = limits()
    base = report.base_config(meta, applied, [q["q"] for q in measured])
    entries = adapter_entries(adapters)
    stamp = provenance(meta, applied)
    work_dir = Path(tempfile.mkdtemp(prefix="vortex-rdflib-bsbm-"))
    print(
        f"BSBM {meta['products']:,} products, {meta['warmup_mixes']} + {meta['mixes']} mixes "
        f"from {prepared} (work dir {work_dir})"
    )
    results: list[dict] = []
    memory: list[dict] = []
    failures: list[dict] = []
    stores: dict[str, dict] = {}
    answers: dict[str, dict] = {}

    def write() -> dict:
        payload = report.assemble(base, entries, results, memory, failures, stores, answers, stamp)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        staging = out_path.with_name(out_path.name + ".tmp")
        staging.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        staging.replace(out_path)
        return payload

    load_iters = os.environ.get("BSBM_LOAD_ITERS", "1")
    try:
        for adapter in adapters:
            print(f"\n=== {adapter.label} ({adapter.slug}, BSBM) ===")
            out = measure_adapter(
                adapter,
                dataset,
                dataset,
                work_dir,
                failures,
                extra_args=("--bsbm", str(prepared.resolve())),
                extra_env={"BENCH_LOAD_ITERS": load_iters},
            )
            if out is None:
                write()
                continue
            results.extend(out["rows"])
            memory.append(memory_entry(adapter, out))
            answers[adapter.slug] = out["bsbm"].pop("answers")
            stores[adapter.slug] = out["bsbm"]
            write()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    found = write()["failures"]
    print(
        f"\nWrote {len(results)} BSBM rows, {len(memory)} memory readings"
        + (f", {len(found)} failure(s)" if found else "")
        + f" -> {out_path}"
    )
    for f in found:
        print(f"  missing: {f['label']} / {f['phase']}: {f['error']}")
    return 0
