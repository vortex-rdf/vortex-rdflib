#!/usr/bin/env python3
"""Run a deterministic BSBM Explore Q05 sample against Vortex-RDFLib."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from rdflib import Graph

from vortex_rdflib import VortexRdflibStore, __version__


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_queries(path: Path, limit: int | None) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = [
        item for item in payload["queries"] if item["bsbm_template_id"] == "5"
    ]
    queries.sort(key=lambda item: item["query_id"])
    return queries[:limit] if limit is not None else queries


def _fingerprint(rows: list[tuple[Any, ...]]) -> str:
    canonical = "\n".join(
        "\x1f".join(term.n3() for term in row) for row in rows
    )
    return _sha256(canonical)


def _query(graph: Graph, text: str) -> tuple[int, str]:
    rows = [tuple(row) for row in graph.query(text)]
    return len(rows), _fingerprint(rows)


def _measure(
    graph: Graph, query: dict[str, Any], repetitions: int
) -> dict[str, Any]:
    text = query["query"]
    warmup_started = time.perf_counter_ns()
    warmup_count, warmup_fingerprint = _query(graph, text)
    warmup_ns = time.perf_counter_ns() - warmup_started
    samples: list[int] = []
    result_count = warmup_count
    result_fingerprint = warmup_fingerprint
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        result_count, result_fingerprint = _query(graph, text)
        samples.append(time.perf_counter_ns() - started)
    return {
        "query_id": query["query_id"],
        "query_sha256": query["query_sha256"],
        "warmup_ns": warmup_ns,
        "elapsed_ns": samples,
        "median_ns": statistics.median(samples),
        "result_count": result_count,
        "result_fingerprint": result_fingerprint,
        "warmup_matches": result_count == warmup_count
        and result_fingerprint == warmup_fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--in-memory", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 1 or args.repetitions < 1:
        parser.error("--limit and --repetitions must be positive")

    queries = _load_queries(args.manifest, args.limit)
    query_ids = [query["query_id"] for query in queries]
    started = time.perf_counter_ns()
    graph = Graph(
        store=VortexRdflibStore(str(args.artifact), in_memory=args.in_memory)
    )
    load_ns = time.perf_counter_ns() - started
    measurements = [
        _measure(graph, query, args.repetitions) for query in queries
    ]
    output = {
        "schema": "vortex-rdflib-q05-sample-v1",
        "manifest": str(args.manifest),
        "artifact": str(args.artifact),
        "in_memory": args.in_memory,
        "query_ids": query_ids,
        "query_ids_sha256": _sha256("\n".join(query_ids)),
        "query_count": len(measurements),
        "repetitions": args.repetitions,
        "load_ns": load_ns,
        "measurements": measurements,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "vortex_rdflib": __version__,
            "vortex_rdflib_path": str(Path(__file__).resolve().parents[1]),
            "rdflib": importlib.metadata.version("rdflib"),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "query_count": len(measurements),
                "query_ids_sha256": output["query_ids_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
