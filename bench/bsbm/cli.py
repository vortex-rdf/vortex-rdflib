"""Arguments the BSBM stream CLIs share (``run_stream``, ``record_native``,
``replay_native``)."""

import argparse
import json


def add_residency(parser: argparse.ArgumentParser) -> None:
    """``--file`` (the store memory-mapped) or ``--in-memory``: one is required."""
    residency = parser.add_mutually_exclusive_group(required=True)
    residency.add_argument("--file", dest="in_memory", action="store_false")
    residency.add_argument("--in-memory", dest="in_memory", action="store_true")


def read_streams(warmup_path: str, measured_path: str) -> tuple[list[dict], list[dict]]:
    """The warm-up and measured streams, as ``bench.bsbm.prepare`` writes them."""
    with open(warmup_path, encoding="utf-8") as f:
        warmup = json.load(f)
    with open(measured_path, encoding="utf-8") as f:
        measured = json.load(f)
    return warmup, measured
