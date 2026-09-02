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
"""

import rdflib.plugins.sparql.processor as processor
from bench.queries import Query as BenchQuery
from bench.worker import MODES, make_row, measure_query, run_once
from rdflib import Dataset, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery

SPARQL = "SELECT ?o WHERE { <http://ex.org/s> <http://ex.org/p> ?o }"
QUERY = BenchQuery(name="probe", group="tests", sparql=SPARQL)


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
