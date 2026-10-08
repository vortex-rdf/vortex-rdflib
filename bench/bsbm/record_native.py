"""Record the native calls a BSBM stream makes on the store and its dictionary.

usage: python -m bench.bsbm.record_native <store.vortex> (--file|--in-memory)
           <warmup.json> <measured.json> <trace.jsonl>

vortex-rdflib talks to vortex-rdf through the store (``store._native``) and its
term dictionary (``store._dict``). Both are wrapped in recording proxies once
the warm-up has run; every measured call on them is written with its
arguments, so ``replay_native`` re-issues the same native work against another
vortex-rdf build, separating native time from Python time.

Not recorded: operations on the ``U32Column`` results the proxies return
(``join_indices``, ``take``, ``distinct``, ``value_counts``), ``len()`` on the
proxies, and calls that raise (a call is logged once it has returned). They are
negligible: about 0.5 ms of ``U32Column`` calls over the 10k-product stream.
"""

import argparse
import json
import sys
import time

from .cli import add_residency, read_streams
from .execute import run_instance


def encode_value(value):
    """JSON form of a native argument; ``replay_native.decode_value`` inverts it."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, range):
        return {"range": [value.start, value.stop]}
    if isinstance(value, tuple):
        return {"tuple": [encode_value(v) for v in value]}
    if isinstance(value, list):
        return [encode_value(v) for v in value]
    if isinstance(value, dict):
        return {k: encode_value(v) for k, v in value.items()}
    try:
        return {"codes": memoryview(value).cast("I").tolist()}
    except TypeError:
        return {"repr": repr(value)}


class _Sink:
    def __init__(self) -> None:
        self.on = False
        self.calls: list = []

    def add(self, obj, method, args, kwargs, ns) -> None:
        if self.on:
            self.calls.append([obj, method, encode_value(list(args)), encode_value(kwargs), ns])


class _Recorder:
    """Forwards every call to ``inner`` and records it while ``sink.on``."""

    def __init__(self, inner, obj: str, sink: _Sink):
        self._inner, self._obj, self._sink = inner, obj, sink

    def __len__(self):
        return len(self._inner)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name.startswith("_") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            t0 = time.perf_counter_ns()
            result = attr(*args, **kwargs)
            self._sink.add(self._obj, name, args, kwargs, time.perf_counter_ns() - t0)
            return result

        return call


def record(
    store_path: str, in_memory: bool, warmup: list[dict], measured: list[dict], out_path: str
) -> int:
    import vortex_rdf
    from rdflib import Graph

    from vortex_rdflib import VortexRdflibStore

    store = VortexRdflibStore(store_path, in_memory=in_memory)
    graph = Graph(store=store)
    init_ns = dict(graph.namespaces())

    def run(text: str) -> bool:
        """One query as ``run_stream`` runs it; whether it raised."""
        try:
            run_instance(graph, text, init_ns=init_ns)
        except Exception:  # noqa: BLE001 — record what ran; the summary counts what raised
            return True
        return False

    for q in warmup:
        run(q["text"])
    sink = _Sink()
    store._store()  # the lazy open, if any, happens before the proxies go in
    store._native = _Recorder(store._native, "store", sink)  # ty: ignore[invalid-assignment]
    if store._dict is not None:
        store._dict = _Recorder(store._dict, "dict", sink)  # ty: ignore[invalid-assignment]
    total = raised = 0
    with open(out_path, "w", encoding="utf-8") as out:
        header = {
            "trace": 1,
            "store": store_path,
            "in_memory": in_memory,
            "vortex_rdf": getattr(vortex_rdf, "__version__", "?"),
        }
        out.write(json.dumps(header) + "\n")
        for q in measured:
            sink.calls, sink.on = [], True
            raised += run(q["text"])
            sink.on = False
            total += len(sink.calls)
            out.write(json.dumps({"i": q["i"], "q": q["q"], "calls": sink.calls}) + "\n")
    print(
        f"recorded {total} native calls over {len(measured)} queries ({raised} raised)",
        file=sys.stderr,
    )
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store")
    add_residency(parser)
    parser.add_argument("warmup")
    parser.add_argument("measured")
    parser.add_argument("trace")
    args = parser.parse_args(argv)
    warmup, measured = read_streams(args.warmup, args.measured)
    record(args.store, args.in_memory, warmup, measured, args.trace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
