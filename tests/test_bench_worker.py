"""What ``bench/worker.py`` assumes about rdflib, pinned.

The dashboard reports two figures per query out of a single run: the
evaluation, and that plus the ``prepareQuery`` in front of it. The second
stands in for ``graph.query(<text>)`` without paying for a separate run of
it. That is honest only while two things hold of rdflib, neither of which is
this package's to control:

1. its string path is exactly ``prepareQuery`` then evaluate, so the split
   run reconstructs it;
2. it re-parses on every string call, so the cost the ``full`` column
   attributes to preparation is really paid every time — the claim the
   dashboard makes in words.

Both are cheap to check and would otherwise fail silently on an rdflib
upgrade, leaving a plausible-looking number that means something else.

The pyoxigraph row is the other way round: rdflib has no part in it, so what
is pinned for it is that none creeps in — not in its answers, not in the
process whose peak RSS it reports.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import rdflib.plugins.sparql.processor as processor
from bench.adapters import BY_SLUG
from bench.queries import Query as BenchQuery
from bench.worker import (
    MODES,
    consume_native,
    make_row,
    measure_query,
    run_once,
    run_string_once,
)
from rdflib import Dataset, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery

SPARQL = "SELECT ?o WHERE { <http://ex.org/s> <http://ex.org/p> ?o }"
QUERY = BenchQuery(name="probe", group="tests", sparql=SPARQL)
ROOT = Path(__file__).resolve().parent.parent


def python(code: str, *args: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter, where no other test's imports linger."""
    return subprocess.run(
        [sys.executable, "-c", code, *args], cwd=ROOT, capture_output=True, text=True
    )


def dataset():
    ds = Dataset(default_union=True)
    ds.add((URIRef("http://ex.org/s"), URIRef("http://ex.org/p"), Literal("o")))
    return ds


def counting_parse(monkeypatch) -> list[str]:
    """Record every SPARQL string rdflib's processor parses."""
    seen: list[str] = []
    original = processor.parseQuery

    def spy(query_string):
        seen.append(query_string)
        return original(query_string)

    monkeypatch.setattr(processor, "parseQuery", spy)
    return seen


def test_a_string_query_is_parsed_on_every_call(monkeypatch):
    # The `full` column charges every call for the parse. rdflib memoizing it
    # would make that a first-call cost the column would keep reporting.
    graph = dataset()
    seen = counting_parse(monkeypatch)
    for _ in range(3):
        list(graph.query(SPARQL))
    assert seen == [SPARQL] * 3


def test_a_prepared_query_is_not_parsed_again(monkeypatch):
    # ...and the `exec` column charges for none of it.
    graph = dataset()
    prepared = prepareQuery(SPARQL, initNs=dict(graph.namespaces()))
    seen = counting_parse(monkeypatch)
    for _ in range(3):
        list(graph.query(prepared))
    assert seen == []


def test_the_string_path_translates_what_preparequery_translates(monkeypatch):
    """The reconstruction only holds if both paths reach the same algebra."""
    graph = dataset()
    init_ns = dict(graph.namespaces())
    evaluated = []
    original = processor.evalQuery

    def spy(g, query, init_bindings, base=None):
        evaluated.append(query)
        return original(g, query, init_bindings, base)

    monkeypatch.setattr(processor, "evalQuery", spy)
    list(graph.query(SPARQL))

    assert len(evaluated) == 1
    assert evaluated[0].algebra == prepareQuery(SPARQL, initNs=init_ns).algebra


def test_one_run_yields_a_sample_of_each_mode():
    rows, prepare_ns, evaluate_ns = run_once(dataset(), QUERY, {}, {})
    assert rows == 1
    assert prepare_ns > 0 and evaluate_ns > 0


def test_every_full_sample_is_its_own_exec_sample_plus_a_preparation():
    rows, warmed, samples = measure_query(dataset(), QUERY, {})
    assert rows == warmed == 1
    assert set(samples) == set(MODES)
    assert len(samples["full"]) == len(samples["exec"]) >= 3
    # Paired, not two independently drawn series: the same run underlies both.
    assert all(full > ex for full, ex in zip(samples["full"], samples["exec"], strict=True))


def test_a_heavy_query_is_sampled_without_the_string_warmup():
    heavy = BenchQuery(name="probe", group="tests", sparql=SPARQL, heavy=True)
    rows, warmed, samples = measure_query(dataset(), heavy, {})
    assert rows == 1
    assert warmed is None  # no warmup run, so no count to compare against
    assert len(samples["full"]) == len(samples["exec"]) == 3


def test_only_the_exec_rows_carry_a_mode_in_their_id():
    # The dashboard pairs the two columns by this id shape, and `full` keeping
    # the bare slug is what leaves the ids of every earlier run unchanged.
    assert make_row("p-scan", "vortex_dict_mem", [1.0])["id"] == "p-scan::vortex_dict_mem"
    assert (
        make_row("p-scan", "vortex_dict_mem", [1.0], "exec")["id"]
        == "p-scan::vortex_dict_mem::exec"
    )


class Solution:
    """Stands in for a pyoxigraph solution; records each time its values are read."""

    def __init__(self, reads: list[str]):
        self.reads = reads

    def __iter__(self):
        self.reads.append("read")
        return iter(("s", "o"))


class NativeStore:
    """Stands in for a pyoxigraph store: takes the query text, has no namespaces."""

    def query(self, text: str, **kwargs):
        return [Solution([])]


def test_every_value_of_a_native_solution_is_read():
    # A pyoxigraph solution builds its Python terms only when they are read,
    # while an rdflib row arrives holding them: counting solutions unread would
    # time an answer nobody looked at.
    reads: list[str] = []
    assert consume_native([Solution(reads), Solution(reads)], QUERY) == 2
    assert reads == ["read", "read"]


def test_a_native_store_is_timed_end_to_end_only():
    # No algebra for rdflib to prepare, so there is no `exec` half to split off.
    rows, warmed, samples = measure_query(NativeStore(), QUERY, {}, native=True)
    assert rows == warmed == 1
    assert set(samples) == {"full"}
    assert len(samples["full"]) >= 3


def test_the_native_path_never_imports_rdflib():
    # The pyoxigraph worker's peak RSS would carry ~20 MB of rdflib otherwise.
    code = (
        "import sys\n"
        "from bench.queries import Query\n"
        "from bench.worker import measure_query\n"
        "class Store:\n"
        "    def query(self, text, **kwargs):\n"
        "        return []\n"
        "measure_query(Store(), Query(name='q', group='g', sparql='ASK {}'), {}, native=True)\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'rdflib'))\n"
    )
    proc = python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_an_rdflib_row_counts_rdflib_in_its_baseline_not_its_store():
    # The worker reads the baseline RSS before building the store, and the
    # store's footprint is what the build adds on top. rdflib is the engine
    # every rdflib row shares, so it belongs below that line, where the
    # worker's top-level import always put it; the native row never loads it.
    code = (
        "import sys\n"
        "from bench.worker import import_engine\n"
        "import_engine(sys.argv[1])\n"
        "print('rdflib.plugins.sparql' in sys.modules)\n"
    )
    for engine, loaded in (("rdflib", "True"), ("native", "False")):
        proc = python(code, engine)
        assert proc.stdout.strip() == loaded, proc.stderr


def test_pyoxigraph_answers_what_the_rdflib_rows_answer(tmp_path):
    # Its default graph must be the union of the graphs, as the rdflib rows'
    # `Dataset(default_union=True)` has it, and `GRAPH ?g` must range over the
    # named graphs alone.
    pytest.importorskip("pyoxigraph")
    nq = tmp_path / "data.nq"
    nq.write_text(
        '<http://ex.org/s> <http://ex.org/p> "default" .\n'
        '<http://ex.org/s> <http://ex.org/p> "named" <http://ex.org/g> .\n',
        encoding="utf-8",
    )
    adapter = BY_SLUG["pyoxigraph"]
    store = adapter.make(str(nq), "", str(tmp_path))
    reference = Dataset(default_union=True)
    reference.parse(str(nq), format="nquads")

    for sparql, is_ask, expected in (
        ("SELECT ?o WHERE { ?s ?p ?o }", False, 2),
        ("SELECT ?g ?o WHERE { GRAPH ?g { ?s ?p ?o } }", False, 1),
        ('ASK { ?s ?p "named" }', True, 1),
        ('ASK { ?s ?p "absent" }', True, 0),
    ):
        query = BenchQuery(name="probe", group="tests", sparql=sparql, is_ask=is_ask)
        assert run_string_once(reference, query, {})[0] == expected, sparql
        assert run_string_once(store, query, adapter.query_kwargs, native=True)[0] == expected
