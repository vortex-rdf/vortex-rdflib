"""Replay a recorded native-call trace against the installed vortex-rdf.

usage: python -m bench.bsbm.replay_native <store.vortex> <trace.jsonl>
           (--file|--in-memory) [--skip filter_codes] [--passes 2] [--json OUT]

Opens the store with ``vortex_rdf.VortexRdfStore`` (no vortex-rdflib) and
re-issues every call in order, timing each; the first passes warm the page
cache, the last is reported per method and per template. A call the installed
API rejects (changed signature) counts under ``errors`` and is skipped. On a
vortex-rdf that accepts ``max_resident_bytes`` (0.11), file stores open with
vortex-rdflib 0.2.0's 1 GiB default.
"""

import argparse
import json
import sys
import time
from collections import defaultdict

from .cli import add_residency


def _key(key):
    """A ``keep`` names its positions by int (0-3); JSON spells the keys as strings."""
    return int(key) if isinstance(key, str) and key.isdecimal() else key


def decode_value(value):
    from vortex_rdf import U32Column

    if isinstance(value, list):
        return [decode_value(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"range"}:
            return range(*value["range"])
        if set(value) == {"tuple"}:
            return tuple(decode_value(v) for v in value["tuple"])
        if set(value) == {"codes"}:
            return U32Column(value["codes"])
        return {_key(k): decode_value(v) for k, v in value.items()}
    return value


def _open(store_path: str, in_memory: bool):
    from vortex_rdf import VortexRdfStore

    if in_memory:
        return VortexRdfStore(store_path, in_memory=True)
    try:
        return VortexRdfStore(store_path, in_memory=False, max_resident_bytes=1 << 30)
    except TypeError:  # 0.12+: memory-mapped, no residency budget
        return VortexRdfStore(store_path, in_memory=False)


def replay(
    store_path: str, trace_path: str, in_memory: bool, skip: set[str], passes: int = 2
) -> dict:
    import vortex_rdf

    store = _open(store_path, in_memory)
    targets = {"store": store, "dict": store.term_dict()}
    with open(trace_path, encoding="utf-8") as f:
        f.readline()  # header
        queries = [json.loads(line) for line in f]
    prepared = [
        (
            q["q"],
            [
                (obj, m, decode_value(a), decode_value(k))
                for obj, m, a, k, _ns in q["calls"]
                if m not in skip
            ],
        )
        for q in queries
    ]
    methods: dict = {}
    templates: dict = {}
    for _ in range(passes):
        methods = defaultdict(lambda: {"calls": 0, "ms": 0.0, "errors": 0})
        templates = defaultdict(lambda: {"queries": 0, "ms": 0.0})
        for q, calls in prepared:
            spent = 0
            for obj, method, args, kwargs in calls:
                fn = getattr(targets[obj], method, None)
                stats = methods[method]
                stats["calls"] += 1
                if fn is None:
                    stats["errors"] += 1
                    continue
                t0 = time.perf_counter_ns()
                try:
                    fn(*args, **kwargs)
                except (TypeError, ValueError):
                    stats["errors"] += 1
                    continue
                ns = time.perf_counter_ns() - t0
                stats["ms"] += ns / 1e6
                spent += ns
            templates[q]["queries"] += 1
            templates[q]["ms"] += spent / 1e6
    return {
        "vortex_rdf": getattr(vortex_rdf, "__version__", "?"),
        "methods": dict(methods),
        "templates": {k: templates[k] for k in sorted(templates)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store")
    parser.add_argument("trace")
    add_residency(parser)
    parser.add_argument("--skip", default="filter_codes")
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args(argv)
    skip = {s for s in args.skip.split(",") if s}
    out = replay(args.store, args.trace, args.in_memory, skip, args.passes)
    print(f"vortex-rdf {out['vortex_rdf']}  (skipped: {', '.join(sorted(skip)) or 'none'})")
    for name, m in sorted(out["methods"].items(), key=lambda kv: -kv[1]["ms"]):
        print(f"  {name:<20} {m['calls']:>8} calls {m['ms']:>10.1f} ms  errors {m['errors']}")
    for q, t in out["templates"].items():
        print(
            f"  Q{q:<3} {t['queries']:>5} queries {t['ms']:>10.1f} ms "
            f"({t['ms'] / max(1, t['queries']):.2f} ms/query)"
        )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
