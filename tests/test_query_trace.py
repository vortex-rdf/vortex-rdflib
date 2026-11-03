import pytest
from rdflib import Graph
from tests.support import trace_events
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown, settings


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
