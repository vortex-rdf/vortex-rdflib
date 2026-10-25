"""The memory a streamed join step holds: one piece at a time, where the
one-shot join holds every pair.

Each path runs in an interpreter of its own, so its peak RSS (``VmHWM``,
read through ``bench.procmem``) starts from that process's baseline: the
store loaded and a first query run. The join's rows are then consumed one by
one and dropped. Linux only: the figures come from ``/proc/self/status``."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support import trace_events
from vortex_rdf import serialize_rdf

ROOT = Path(__file__).resolve().parents[1]
EX = "http://ex/"

#: A hot key and 100 cold ones. The relation, `?s :a ?k`, has 1,000 rows,
#: 900 of them on the hot key; the pattern, `?k :b ?o`, has 10,000 rows,
#: 1,000 of them on the hot key and 90 on each cold one. The join makes
#: 900 * 1,000 + 100 * 90 = 909,000 pairs over about 2,100 distinct terms; the
#: pattern is the larger side, so it is the one fed.
QUERY = f"SELECT ?s ?k ?o WHERE {{ ?s <{EX}a> ?k . ?k <{EX}b> ?o }}"
PAIRS = 909_000
#: A first query, so the parser, the evaluator and the pushdown are warm
#: before the baseline is read.
WARMUP = f"SELECT ?k WHERE {{ ?k <{EX}b> ?o }} LIMIT 1"

_CHILD = """
import sys

from rdflib import Graph
from rdflib.plugins.sparql.processor import SPARQLProcessor

from bench.procmem import peak_rss_mb
from vortex_rdflib import VortexRdflibStore

path, query, warmup = sys.argv[1:]
processor = SPARQLProcessor(Graph(store=VortexRdflibStore(path, in_memory=True)))
for _ in processor.query(warmup)["bindings"]:
    pass
before = peak_rss_mb()
rows = 0
for _ in processor.query(query)["bindings"]:
    rows += 1
print(rows, peak_rss_mb() - before)
"""


def _skewed_store(tmp_path) -> Path:
    lines = [f"<{EX}s{i}> <{EX}a> <{EX}hot> ." for i in range(900)]
    lines += [f"<{EX}s{900 + c}> <{EX}a> <{EX}cold{c}> ." for c in range(100)]
    lines += [f"<{EX}hot> <{EX}b> <{EX}o{j}> ." for j in range(1000)]
    lines += [f"<{EX}cold{c}> <{EX}b> <{EX}o{j}> ." for c in range(100) for j in range(90)]
    source = tmp_path / "skewed.nt"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "skewed.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary")
    return out


def _consume(path: Path, budget: str | None) -> tuple[int, int, list[str]]:
    """The join run in a fresh interpreter with ``VORTEX_RDF_PAIR_BUDGET``
    set to ``budget`` (unset for ``None``): the rows it made, its peak RSS
    growth in MiB, and the strategy of its join step from the query trace."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("VORTEX_RDF")}
    env["VORTEX_RDF_TRACE_QUERY"] = "1"
    if budget is not None:
        env["VORTEX_RDF_PAIR_BUDGET"] = budget
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, str(path), QUERY, WARMUP],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    rows, growth = (int(field) for field in done.stdout.split())
    steps = [
        event["strategy"]
        for event in trace_events(done.stderr)
        if event["event"] == "bgp_join_step_complete"
    ]
    return rows, growth, steps


@pytest.mark.skipif(sys.platform != "linux", reason="peak RSS is read from /proc/self/status")
def test_a_streamed_join_holds_one_piece(tmp_path):
    """The one-shot join holds its index columns and its gathered columns,
    every pair at once. Under a budget of 10,000 pairs the step streams:
    the 1,000-row relation is grouped and the 10,000 pattern rows fed
    through it, a piece of at most 10,000 pairs at a time, so the peak grows
    by a fraction of the one-shot's, and by less than 16 MiB."""
    path = _skewed_store(tmp_path)
    rows, one_shot, steps = _consume(path, None)
    assert (rows, steps) == (PAIRS, ["hash_columns"])
    rows, streamed, steps = _consume(path, "10000")
    assert (rows, steps) == (PAIRS, ["stream"])
    assert streamed < one_shot / 4, (streamed, one_shot)
    assert streamed < 16, (streamed, one_shot)
