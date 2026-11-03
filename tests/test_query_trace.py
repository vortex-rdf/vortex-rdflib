import contextlib
import io
import sys
import time
from typing import Any

import pytest
from rdflib import Graph
from tests.corpus import (
    FIXTURE_NT,
)
from tests.support import (
    both_ways,
    counting_native,
    is_graph_name_check,
    run,
    three_predicate_graph,
    trace_events,
)
from vortex_rdf import Cancelled, CancelToken, serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    boundary,
    query_scope,
    register_sparql_pushdown,
    settings,
    trace,
)
from vortex_rdflib.cancel import CANCEL_TOKEN, cancel_scope
from vortex_rdflib.open_file import OpenFile
from vortex_rdflib.settings import SETTINGS


@pytest.fixture
def traced_graph(tmp_path):
    source = tmp_path / "trace.nt"
    source.write_text(
        "".join(
            f'<http://ex.org/s{i}> <http://ex.org/date> "{i}"^^<http://www.w3.org/2001/XMLSchema#integer> .\n'  # noqa: E501
            f'<http://ex.org/s{i}> <http://ex.org/name> "n{i}" .\n'
            for i in range(40)
        ),
        encoding="utf-8",
    )
    output = tmp_path / "trace.vortex"
    serialize_rdf(str(source), str(output), layout="dictionary")
    return Graph(store=VortexRdflibStore(str(output), in_memory=True))


def test_trace_disabled_is_silent_and_preserves_results(traced_graph, capsys, monkeypatch):
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY", raising=False)
    register_sparql_pushdown()
    rows = list(traced_graph.query("SELECT ?s WHERE { ?s <http://ex.org/name> ?n } LIMIT 3"))
    assert len(rows) == 3
    assert trace_events(capsys.readouterr().err) == []


def test_trace_reports_bounded_order_and_decoding(traced_graph, capsys, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "q8-test")
    register_sparql_pushdown()
    query = """SELECT ?s ?d WHERE {
        ?s <http://ex.org/date> ?d .
        ?s <http://ex.org/name> ?n
    } ORDER BY DESC(?d) LIMIT 5"""
    traced = list(traced_graph.query(query))
    payloads = trace_events(capsys.readouterr().err)
    assert len(traced) == 5
    assert payloads
    assert all(item["schema"] == "vortex-rdf-query-trace-v1" for item in payloads)
    assert all(item["query_id"] == "q8-test" for item in payloads)
    assert [item["sequence"] for item in payloads] == sorted(item["sequence"] for item in payloads)
    planned = next(item for item in payloads if item["event"] == "head_planned")
    assert planned["limit"] == 5
    ordered = next(item for item in payloads if item["event"] == "order_complete")
    assert ordered["algorithm"] == "bounded_heap_nsmallest"
    assert ordered["input_rows"] == 40
    assert ordered["output_rows"] == 5
    ranked = next(item for item in payloads if item["event"] == "rank_codes_complete")
    assert ranked["unique_codes"] == 40
    yielded = next(item for item in payloads if item["event"] == "yield_rows_complete")
    assert yielded["rows_yielded"] == 5
    assert yielded["chunks"] == 1

    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "0")
    untraced = list(traced_graph.query(query))
    assert traced == untraced


def test_trace_switch_reads_as_every_switch(monkeypatch):
    """The trace switch is parsed as every on/off switch is (``settings.flag``)."""
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "yes")
    assert settings.trace_enabled()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "maybe")
    with pytest.raises(ValueError, match="VORTEX_RDF_TRACE_QUERY"):
        settings.trace_enabled()


def test_trace_reports_native_call_shape_and_rows(traced_graph, capsys, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "native-test")
    register_sparql_pushdown()
    list(traced_graph.query("SELECT ?s WHERE { ?s <http://ex.org/name> ?n } LIMIT 1"))

    native_calls = [
        event
        for event in trace_events(capsys.readouterr().err)
        if event["event"] == "native_call_complete"
    ]
    assert native_calls
    assert "match_codes" in {event["operation"] for event in native_calls}
    assert all(len(event["pattern"]) == 4 for event in native_calls)
    assert all(event["returned_rows"] is not None for event in native_calls)


_NAMES = "SELECT ?s WHERE { ?s <http://ex.org/name> ?n }"


def test_an_unread_result_leaves_no_trace(traced_graph, capsys, monkeypatch):
    """A traced query's result never read keeps its trace to itself: a later
    query, untraced, prints nothing, and nothing is traced outside a query."""
    from vortex_rdflib import trace

    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    unread = traced_graph.query(_NAMES)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "0")
    capsys.readouterr()
    assert len(list(traced_graph.query(_NAMES))) == 40
    assert trace_events(capsys.readouterr().err) == []
    assert trace.active() is None
    del unread


def test_unread_results_keep_their_own_traces(traced_graph, capsys, monkeypatch):
    """Two traced results made before either is read: each query's events
    carry its own id, and each ends with one ``query_iterator_complete``."""
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "first")
    first = traced_graph.query(_NAMES)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "second")
    second = traced_graph.query(_NAMES + " LIMIT 3")
    assert (len(list(first)), len(list(second))) == (40, 3)
    events = trace_events(capsys.readouterr().err)
    for query_id in ("first", "second"):
        own = [event["event"] for event in events if event["query_id"] == query_id]
        assert "eval_part_start" in own, query_id
        assert own.count("query_iterator_complete") == 1, query_id
    assert {event["query_id"] for event in events} == {"first", "second"}


def test_a_query_is_one_trace(traced_graph, capsys, monkeypatch):
    """rdflib evaluates a UNION itself and offers each branch to the hook:
    the query is still one trace, numbered without restarting."""
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY_ID", "union")
    capsys.readouterr()
    union = (
        "SELECT ?s WHERE { { ?s <http://ex.org/name> ?n } UNION { ?s <http://ex.org/date> ?d } }"
    )
    rows = list(traced_graph.query(union))
    assert len(rows) == 80
    events = trace_events(capsys.readouterr().err)
    assert {event["query_id"] for event in events} == {"union"}
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["event"] for event in events].count("query_iterator_complete") == 1


def test_a_result_read_in_another_thread(traced_graph, capsys, monkeypatch):
    """A traced result read in another thread completes its trace there."""
    import threading

    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    result = traced_graph.query(_NAMES)
    capsys.readouterr()
    rows, errors = [], []

    def read():
        try:
            rows.extend(result)
        except Exception as error:  # noqa: BLE001 - the test reports it
            errors.append(error)

    reader = threading.Thread(target=read)
    reader.start()
    reader.join()
    assert errors == [] and len(rows) == 40
    events = [event["event"] for event in trace_events(capsys.readouterr().err)]
    assert events.count("query_iterator_complete") == 1


def test_order_by_traces_each_measure_once(traced_graph, capsys, monkeypatch):
    """ORDER BY is traced by ``order_complete`` and, per sort key, the
    ``rank_codes_complete`` of its ranking — no start event, no second
    event timing the same ranking, and no field that repeats another."""
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    query = """SELECT ?s ?d WHERE { ?s <http://ex.org/date> ?d } ORDER BY DESC(?d) LIMIT 5"""
    assert len(list(traced_graph.query(query))) == 5
    payloads = trace_events(capsys.readouterr().err)
    names = [item["event"] for item in payloads]
    assert "order_start" not in names and "order_key_ranked" not in names
    [ordered] = [item for item in payloads if item["event"] == "order_complete"]
    assert "limit_applied_during_order" not in ordered
    [ranked] = [item for item in payloads if item["event"] == "rank_codes_complete"]
    assert "input_codes" not in ranked and ranked["unique_codes"] == 40


def test_an_untraced_query_builds_no_trace_payload(traced_graph, monkeypatch):
    """Untraced, no trace event's fields are computed: the trace helpers
    return before they build them. A row count the trace reads is one."""
    monkeypatch.delenv("VORTEX_RDF_TRACE_QUERY", raising=False)

    def traced_only(*args, **kwargs):
        raise AssertionError("a trace payload was built for an untraced query")

    monkeypatch.setattr(pd.relation, "_relation_rows", traced_only)
    register_sparql_pushdown()
    queries = [
        "SELECT ?s ?d WHERE { ?s <http://ex.org/date> ?d } ORDER BY ?d LIMIT 3",
        "SELECT ?s WHERE { ?s <http://ex.org/date> ?d FILTER(?d > 30) }",
        """SELECT ?s ?n WHERE {
            ?s <http://ex.org/date> 3
            OPTIONAL { ?s <http://ex.org/name> ?n } }""",
    ]
    for query in queries:
        assert list(traced_graph.query(query))


@pytest.fixture(scope="module")
def corpus_vortex(tmp_path_factory):
    """The corpus's triples as a dictionary file."""
    d = tmp_path_factory.mktemp("trace-corpus")
    nt = d / "fixture.nt"
    nt.write_text(FIXTURE_NT, encoding="utf-8")
    out = d / "fixture.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture
def file_graph(corpus_vortex):
    """The corpus's triples, file-backed."""
    return Graph(store=VortexRdflibStore(str(corpus_vortex), in_memory=False))


@contextlib.contextmanager
def _tracing():
    """A query trace around the native wrapper under test; the events it
    emits wait in the trace's pending lines (:func:`_pending_events`). The
    scope keeps the cancel token current as it starts, as a query's does."""
    scope = query_scope.QueryScope()
    scope.cancel = CANCEL_TOKEN.get()
    scope.trace = trace.QueryTrace(query_id=None)
    token = query_scope.enter(scope)
    try:
        yield scope.trace
    finally:
        query_scope.leave(token)


def _pending_events(trace) -> list[dict]:
    """The events a trace holds, not yet flushed to stderr."""
    return trace_events("\n".join(trace.pending))


_PRUNED_SQL = """SELECT ?c WHERE {
    ?s <http://ex.org/p> ?a .
    ?s <http://ex.org/q> ?b .
    ?s <http://ex.org/r> ?c
    FILTER(?a < ?b) }"""


def _pruned_graph(tmp_path) -> Graph:
    """`?a < ?b` holds for `s0` only, and `r` (400 rows) is large enough for
    the one surviving row to be probed into: `_PRUNED_SQL` prunes mid-join."""
    return three_predicate_graph(
        tmp_path, "s", lambda i: 1000 if i == 0 else i - 1000, r_per_subject=8
    )


def test_trace_records_every_native_call(tmp_path, monkeypatch, capsys):
    """Every native call a traced query makes is an event — a single call a
    `native_call_complete`, a batch a `native_batch_complete` carrying its
    probe count — the per-row probes included, so the events add up to the
    calls the store served: 3 counts (one batch), the `p` and `q` matches
    and 1 probe of `r`. The store's own graph-name check is traced too, and
    left out here as the counter leaves it out (`is_graph_name_check`)."""
    graph = _pruned_graph(tmp_path)
    register_sparql_pushdown()
    native = counting_native(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    run(graph, _PRUNED_SQL)
    events = trace_events(capsys.readouterr().err)
    calls = [
        e
        for e in events
        if e["event"] == "native_call_complete" and not is_graph_name_check(tuple(e["pattern"]))
    ]
    batches = [e for e in events if e["event"] == "native_batch_complete"]
    assert (native.counts, native.matches, native.batches) == (3, 3, 1)
    assert [e["operation"] for e in batches] == ["count_quads_many"]
    assert len(calls) + sum(e["probe_count"] for e in batches) == native.counts + native.matches


_STAR_SQL = """SELECT ?x ?n ?a WHERE {
    ?x <http://ex.org/name> ?n .
    ?x <http://ex.org/age> ?a }"""


_FOUR_PROBES = [
    (None, f"<http://ex.org/{name}>", None, None) for name in ("name", "knows", "age", "likes")
]


def test_traced_batch_does_not_pull_ahead(file_graph, monkeypatch, capsys):
    """A traced ``match_codes_many`` hands its caller the native iterator, not
    the rows it drained from it: the probe join pulls exactly the answers it
    consumes, and the batch events add up to the probes served."""
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    monkeypatch.setattr(SETTINGS, "probe_batch", 3)
    monkeypatch.setattr(OpenFile, "point_lookup", lambda opened, n3: True)
    monkeypatch.setattr(SETTINGS, "cheap_batch_min", 1)
    native = _PullCountingNative(file_graph.store._opened().native)
    file_graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    file_graph.store._opened().native = native
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with_pushdown, without_pushdown = both_ways(file_graph, _STAR_SQL)
    assert with_pushdown == without_pushdown
    batches = [
        e
        for e in trace_events(capsys.readouterr().err)
        if e["event"] == "native_batch_complete" and e["operation"] == "match_codes_many"
    ]
    assert len(native.answers) == len(batches) > 1
    assert [a.pulls for a in native.answers] == [len(a) for a in native.answers]
    assert [e["probe_count"] for e in batches] == [len(a) for a in native.answers]
    assert [e["returned_rows"] for e in batches] == [a.rows for a in native.answers]


def test_closing_a_traced_batch_reports_the_pulls_it_made(file_graph):
    """A batch closed early has pulled only what its caller took, and reports
    that once: its probe count is the batch's, its rows the pulled answers'."""
    native = _PullCountingNative(file_graph.store._opened().native)
    file_graph.store._opened().native = native
    with _tracing() as trace:
        traced: Any = boundary.native(file_graph.store)  # the proxy closes what it hands out
        answers = traced.match_codes_many(_FOUR_PROBES)
        assert len(answers) == len(_FOUR_PROBES)
        pulled = [next(answers), next(answers)]
        assert native.answers[0].pulls == 2 and _pending_events(trace) == []
        answers.close()
        answers.close()
        assert list(answers) == [] and native.answers[0].pulls == 2
    (event,) = _pending_events(trace)
    assert (event["event"], event["operation"]) == ("native_batch_complete", "match_codes_many")
    assert event["probe_count"] == len(_FOUR_PROBES)
    assert event["first_pattern"] == list(_FOUR_PROBES[0])
    assert event["returned_rows"] == sum(len(answer[0]) for answer in pulled)
    assert event["elapsed_ns"] > 0


def test_traced_chunks_report_the_chunks_pulled(file_graph):
    """A chunk iterator reports once, when it is exhausted: the pattern, its
    keep, the columns and the chunks and rows pulled."""
    pattern = (None, "<http://ex.org/knows>", None, None)
    keep = {"o": (0, 1_000_000)}
    rows = file_graph.store._opened().native.count_quads(*pattern)
    with _tracing() as trace:
        traced: Any = boundary.native(file_graph.store)  # the proxy closes what it hands out
        chunks = traced.match_code_chunks(*pattern, keep=keep, columns=("s", "o"), batch_rows=4)
        pulled = [next(chunks)]
        assert _pending_events(trace) == []
        pulled.extend(chunks)
        assert len(_pending_events(trace)) == 1
        chunks.close()
    (event,) = _pending_events(trace)
    assert (event["event"], event["operation"]) == ("native_chunks_complete", "match_code_chunks")
    assert (event["pattern"], event["keep"]) == (list(pattern), {"o": {"range": [0, 1_000_000]}})
    assert event["columns"] == ["s", "o"]
    assert (event["chunks"], event["returned_rows"]) == (len(pulled), rows)
    assert len(pulled) > 1 and all(len(chunk) == 2 for chunk in pulled)
    assert event["elapsed_ns"] > 0


def test_a_traced_chunk_call_without_codes_returns_none(tmp_path):
    source = tmp_path / "plain.nt"
    source.write_text(FIXTURE_NT, encoding="utf-8")
    out = tmp_path / "plain.vortex"
    serialize_rdf(str(source), str(out), layout="default")
    with _tracing():
        assert boundary.native(VortexRdflibStore(str(out))).match_code_chunks() is None


def test_the_traced_native_forwards_the_native_keywords(file_graph):
    """Traced, the boundary passes a call's keywords on and gives it the
    query's cancel token, which a scan, a chunked match and a batch race."""
    with cancel_scope(CancelToken()) as token, _tracing():
        traced: Any = boundary.native(file_graph.store)
        counts = traced.count_quads_many(_FOUR_PROBES, lookahead=1)
        answers = traced.match_codes_many(_FOUR_PROBES, lookahead=1)
        assert counts == [len(answer[0]) for answer in answers]
        for many in (traced.count_quads_many, traced.match_codes_many):
            with pytest.raises(ValueError, match="lookahead"):
                many(_FOUR_PROBES, lookahead=0)
        token.cancel()
        for single in (traced.count_quads, traced.match_codes, traced.match_code_chunks):
            with pytest.raises(Cancelled):
                single()
        for many in (traced.count_quads_many, traced.match_codes_many):
            with pytest.raises(Cancelled):
                many(_FOUR_PROBES)


def test_trace_reports_a_mid_join_prune_between_the_steps_it_links(tmp_path, monkeypatch, capsys):
    """A mid-join prune is its own trace event, so row counts reconcile step
    to step: the `p`/`q` join's 50 rows are the prune's input, and its one
    surviving row is the input of the `r` probe that follows."""
    graph = _pruned_graph(tmp_path)
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    run(graph, _PRUNED_SQL)
    events = trace_events(capsys.readouterr().err)
    steps = [e for e in events if e["event"] == "bgp_join_step_complete"]
    prunes = [e for e in events if e["event"] == "bgp_prune_complete"]
    assert len(prunes) == 1 and prunes[0]["pruned"] is True
    prune = prunes[0]
    assert (steps[0]["output_rows"], prune["input_rows"]) == (50, 50)
    assert (prune["output_rows"], steps[1]["input_rows"]) == (1, 1)


def test_trace_output_is_not_timed_as_native_work(tmp_path, monkeypatch):
    """Trace lines are written after the planning they describe, so however
    slowly stderr takes them, the native time the events report is the
    native calls' own: a 20 ms write must not show up in it."""
    graph = three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=5)
    register_sparql_pushdown()

    class _SlowStderr(io.StringIO):
        def write(self, text):
            time.sleep(0.02)
            return super().write(text)

    slow = _SlowStderr()
    monkeypatch.setattr(sys, "stderr", slow)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    run(graph, "SELECT ?s WHERE { ?s <http://ex.org/p> ?a . ?s <http://ex.org/q> ?b }")
    done = next(e for e in trace_events(slow.getvalue()) if e["event"] == "bgp_complete")
    assert done["native_match_ns"] < 20_000_000


class _PullCounter:
    """The answers a ``match_codes_many`` stand-in returns: the native
    iterator's, counting the answers pulled and the rows they hold."""

    def __init__(self, inner):
        self._inner = inner
        self.pulls = 0
        self.rows = 0

    def __iter__(self):
        return self

    def __next__(self):
        answer = next(self._inner)
        self.pulls += 1
        self.rows += 0 if answer is None else len(answer[0])
        return answer

    def __len__(self):
        return len(self._inner)


class _PullCountingNative:
    """A VortexRdfStore stand-in whose ``match_codes_many`` hands out a
    :class:`_PullCounter` per batch, kept in call order."""

    def __init__(self, inner):
        self._inner = inner
        self.answers: list[_PullCounter] = []

    def match_codes_many(self, probes, **options):
        answers = _PullCounter(self._inner.match_codes_many(probes, **options))
        self.answers.append(answers)
        return answers

    def __getattr__(self, name):
        return getattr(self._inner, name)
