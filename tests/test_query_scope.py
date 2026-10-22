"""What a query learns about terms lives as long as the query: decoded
terms, literal alias classes, the codes of constants outside the dictionary
and blank-node graph names are kept in the query's scope, which covers the
lazy iteration of its results and is dropped with them; the store itself
keeps fixed-size structure only."""

import gc
import itertools
import weakref

import pytest
import rdflib.plugins.sparql.evaluate as rdflib_evaluate
from rdflib import Graph, URIRef
from rdflib.plugins.sparql import CUSTOM_EVALS
from vortex_rdf import Cancelled, CancelToken, serialize_rdf

import vortex_rdflib.pushdown as pd
from test_pushdown import _counting_term_dict, _row_key, both_ways, is_graph_name_check, run
from vortex_rdflib import VortexRdflibStore, cancel_scope, query_scope, register_sparql_pushdown

XSD = "http://www.w3.org/2001/XMLSchema#"


def _store(tmp_path, in_memory=False) -> VortexRdflibStore:
    nt = tmp_path / "scope.nt"
    nt.write_text(
        "".join(
            f'<http://ex.org/s{i}> <http://ex.org/name> "name {i}"@en .\n'
            f'<http://ex.org/s{i}> <http://ex.org/age> "0{i % 7}"^^<{XSD}integer> .\n'
            f"<http://ex.org/s{i}> <http://ex.org/knows> <http://ex.org/s{(i + 1) % 50}> .\n"
            for i in range(50)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "scope.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return VortexRdflibStore(str(out), in_memory=in_memory)


def _footprint(store) -> dict:
    """The size of every container the store holds."""
    return {
        name: len(value)
        for name, value in vars(store).items()
        if isinstance(value, (dict, set, list, tuple))
    }


def _queries(i: int) -> list[str]:
    """Fresh constants through every per-query table: decodes, a VALUES
    constant the dictionary lacks, typed literals classified for aliasing,
    a regex constant, an ORDER BY rank."""
    return [
        f'SELECT ?s ?n WHERE {{ ?s <http://ex.org/name> ?n FILTER(regex(?n, "{i}$")) }}',
        f"""SELECT ?s WHERE {{ VALUES ?s {{ <http://ex.org/absent{i}> <http://ex.org/s{i}> }}
            ?s <http://ex.org/knows> ?o }}""",
        "SELECT DISTINCT ?a WHERE { ?s <http://ex.org/age> ?a }",
        f"SELECT ?s WHERE {{ ?s <http://ex.org/knows> ?o }} ORDER BY ?o LIMIT {i % 5 + 1}",
    ]


@pytest.mark.parametrize("in_memory", [True, False], ids=["mem", "file"])
def test_the_store_does_not_grow_with_the_queries_it_answers(tmp_path, in_memory):
    store = _store(tmp_path, in_memory)
    graph = Graph(store=store)
    register_sparql_pushdown()
    for query in _queries(0):
        run(graph, query)
    before = _footprint(store)
    for i in range(1, 40):
        for query in _queries(i):
            run(graph, query)
    assert _footprint(store) == before
    assert query_scope.current() is None


def test_a_query_scope_is_released_with_its_results(tmp_path, monkeypatch):
    """The scope is current only while a step of the query runs, and is
    cleared — then collected — once the results are consumed, whether read
    to the end or dropped half-way."""
    graph = Graph(store=_store(tmp_path))
    opened = []

    class _Tracked(query_scope.QueryScope):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            opened.append(weakref.ref(self))

    monkeypatch.setattr(query_scope, "QueryScope", _Tracked)
    register_sparql_pushdown()
    assert len(run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }")) == 50
    result = graph.query("SELECT ?s ?n WHERE { ?s <http://ex.org/name> ?n }")
    rows = iter(result)
    next(rows)
    assert query_scope.current() is None  # not current between steps
    del rows, result
    gc.collect()
    assert len(opened) == 2
    assert all(ref() is None for ref in opened)


def test_a_query_scope_holds_its_token_and_drops_it(tmp_path, monkeypatch):
    """A query started inside ``cancel_scope`` keeps the block's token in its
    scope while its results are read, after the block too, and the scope
    drops it with the rest once they end; a query started outside a block
    holds none."""
    graph = Graph(store=_store(tmp_path))
    opened = []

    class _Tracked(query_scope.QueryScope):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            opened.append(self)

    monkeypatch.setattr(query_scope, "QueryScope", _Tracked)
    register_sparql_pushdown()
    query = "SELECT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }"
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(graph.query(query))
    [scope] = opened
    assert scope.cancel is token
    assert len(list(rows)) == 50
    assert scope.cancel is None
    rows = iter(graph.query(query))
    assert opened[1].cancel is None
    assert len(list(rows)) == 50


def test_each_query_has_a_scope_of_its_own(tmp_path, monkeypatch):
    """A second query run while the first is half read gets its own scope,
    and both answer in full."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    seen = []
    original = store._prime_decode_cache

    def spy(*columns):
        seen.append(query_scope.current())
        return original(*columns)

    monkeypatch.setattr(store, "_prime_decode_cache", spy)
    register_sparql_pushdown()
    first = iter(graph.query("SELECT ?s ?o WHERE { ?s <http://ex.org/knows> ?o }"))
    head = [next(first)]
    second = run(graph, "SELECT ?s ?n WHERE { ?s <http://ex.org/name> ?n }")
    head.extend(first)
    assert len(head) == 50 and len(second) == 50
    assert None not in seen and len({id(scope) for scope in seen}) == 2


def test_triples_outside_a_query_keep_their_decodes_for_the_call(tmp_path):
    graph = Graph(store=_store(tmp_path))
    list(graph.store.contexts())  # one context per graph: store state, decoded once
    counting = _counting_term_dict(graph)
    first = sorted(graph.triples((None, None, None)))
    once = counting.decoded
    second = sorted(graph.triples((None, None, None)))
    assert first == second and len(first) == 150
    assert counting.decoded == 2 * once  # nothing carried over between calls
    assert query_scope.current() is None


def test_triples_inside_a_query_share_the_query_decodes(tmp_path, monkeypatch):
    """With no node pushed down, rdflib's own evaluator calls triples() once
    per pattern and binding; inside the query they share its decodes: the 50
    subjects and the predicate are decoded once each."""
    graph = Graph(store=_store(tmp_path))
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset())
    register_sparql_pushdown()
    list(graph.store.contexts())  # one context per graph: store state, decoded once
    counting = _counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?a ?c WHERE {
        ?a <http://ex.org/knows> ?b . ?b <http://ex.org/knows> ?c }""",
    )
    assert len(rows) == 50
    assert counting.decoded == 51


def test_constants_outside_the_dictionary_do_not_outlive_their_query(tmp_path):
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    for i in range(3):
        query = f"SELECT ?x WHERE {{ VALUES ?x {{ <http://ex.org/absent{i}> }} }}"
        with_pushdown, without_pushdown = both_ways(graph, query)
        assert with_pushdown == without_pushdown == [(URIRef(f"http://ex.org/absent{i}"),)]
    assert not hasattr(store, "_foreign") and not hasattr(store, "_decode_cache")


def test_a_bare_graphs_label_is_resolved_once_per_query(tmp_path):
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    checks = []
    inner = store._store()

    class _Counting:
        def count_quads(self, *args, **kwargs):
            if is_graph_name_check(args):
                checks.append(args[3])
            return inner.count_quads(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    store._native = _Counting()  # ty: ignore[invalid-assignment]
    for _ in range(3):
        run(graph, "SELECT ?s WHERE { ?s <http://ex.org/knows> ?o . ?o <http://ex.org/knows> ?x }")
    assert len(checks) == 3


def test_graph_names_the_file_lacks_leave_no_state(tmp_path):
    """A view of a graph the file does not hold matches nothing and leaves
    nothing behind: the store keeps one context per graph of the file."""
    store = _store(tmp_path)
    before = _footprint(store)
    for i in range(20):
        view = Graph(store=store, identifier=URIRef(f"http://ex.org/absent{i}"))
        assert list(view.triples((None, None, None))) == []
    assert _footprint(store) == before


def test_later_custom_evaluators_are_offered_the_query_node(tmp_path):
    """The hook opens the query's scope at the query node and hands the node
    back to rdflib's evalPart, where it declines it: a CUSTOM_EVALS entry
    registered after the hook is offered SelectQuery and ConstructQuery once
    each, inside the scope."""
    graph = Graph(store=_store(tmp_path))
    register_sparql_pushdown()
    offered = []

    def record(ctx, part):
        offered.append((part.name, query_scope.current() is not None))
        raise NotImplementedError

    CUSTOM_EVALS["test_query_scope_recorder"] = record
    try:
        order = list(CUSTOM_EVALS)
        assert order.index(pd._EVAL_KEY) < order.index("test_query_scope_recorder")
        assert len(run(graph, "SELECT ?s WHERE { ?s <http://ex.org/knows> ?o }")) == 50
        built = graph.query(
            "CONSTRUCT { ?o <http://ex.org/known> ?s } WHERE { ?s <http://ex.org/knows> ?o }"
        )
        assert len(built.graph) == 50
    finally:
        CUSTOM_EVALS.pop("test_query_scope_recorder", None)
    names = [name for name, _ in offered]
    assert names.count("SelectQuery") == 1 and names.count("ConstructQuery") == 1
    assert all(scoped for _, scoped in offered)


@pytest.fixture
def tracked(monkeypatch) -> list:
    """Weak references to every query scope and term state the test opens."""
    refs = []

    class _Scope(query_scope.QueryScope):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            refs.append(weakref.ref(self))

    class _Terms(query_scope.QueryTerms):
        __slots__ = ("__weakref__",)

        def __init__(self):
            super().__init__()
            refs.append(weakref.ref(self))

    monkeypatch.setattr(query_scope, "QueryScope", _Scope)
    monkeypatch.setattr(query_scope, "QueryTerms", _Terms)
    return refs


def _assert_released(store, before: dict, tracked: list) -> None:
    """No scope is current, every scope and term state the test opened is
    gone, and the store holds what it held before the case: its
    store-lifetime structure."""
    gc.collect()
    assert query_scope.current() is None
    assert tracked and all(ref() is None for ref in tracked)
    assert _footprint(store) == before


class _FailingDecodes:
    """The store's TermDict, but decode_many fails from its second call on:
    a query's first chunk of rows decodes, the next one raises."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def decode_many(self, codes):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("decode_many failed part-way")
        return self._inner.decode_many(codes)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_query_failing_mid_iteration_releases_its_scope(tmp_path, tracked):
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    query = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"
    assert len(run(graph, query)) == 150
    before = _footprint(store)
    store._dict = _FailingDecodes(store._dict)  # ty: ignore[invalid-assignment]
    rows = []
    with pytest.raises(RuntimeError, match="part-way"):
        for row in graph.query(query):
            rows.append(row)
    assert len(rows) == pd._CHUNK_START
    _assert_released(store, before, tracked)


def test_a_cancelled_query_releases_its_scope(tmp_path, tracked, monkeypatch):
    """A query cancelled while its rows are read — rdflib's evaluator reads
    the store once per binding, each read given the token — raises
    ``Cancelled`` at the next read and releases its scope and its token as
    any failure does."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset())
    register_sparql_pushdown()
    query = "SELECT ?a ?c WHERE { ?a <http://ex.org/knows> ?b . ?b <http://ex.org/knows> ?c }"
    assert len(run(graph, query)) == 50
    before = _footprint(store)
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(graph.query(query))
        assert next(rows)
        token.cancel()
        with pytest.raises(Cancelled):
            next(rows)
    del rows
    assert pd._CANCEL.get() is None
    _assert_released(store, before, tracked)


def test_rdflibs_own_limit_releases_the_scope(tmp_path, tracked, monkeypatch):
    """A LIMIT the hook leaves to rdflib is an islice over the rows below it:
    the scope goes when the islice ends, those rows left unread."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset({"BGP"}))
    register_sparql_pushdown()
    sliced = []
    original = rdflib_evaluate.evalSlice

    def spy(ctx, part):
        result = original(ctx, part)
        sliced.append(type(result))
        return result

    monkeypatch.setattr(rdflib_evaluate, "evalSlice", spy)
    query = "SELECT ?s WHERE { ?s <http://ex.org/knows> ?o } LIMIT 3"
    assert len(run(graph, query)) == 3
    before = _footprint(store)
    assert len(run(graph, query)) == 3
    assert sliced == [itertools.islice, itertools.islice]
    _assert_released(store, before, tracked)


@pytest.mark.parametrize(
    ("query", "answer", "rdflib_asks"),
    [
        ("ASK { ?s <http://ex.org/knows> ?o }", True, 0),
        (
            """ASK { { ?s <http://ex.org/knows> <http://ex.org/nobody> }
                UNION { ?s <http://ex.org/name> ?n } }""",
            True,
            1,
        ),
        ("CONSTRUCT { ?o <http://ex.org/known> ?s } WHERE { ?s <http://ex.org/knows> ?o }", 50, 0),
    ],
    ids=["ask-by-the-hook", "ask-by-rdflib", "construct"],
)
def test_ask_and_construct_release_their_scope(
    tmp_path, tracked, monkeypatch, query, answer, rdflib_asks
):
    """An ASK, answered by the hook's count or by rdflib's evaluator (which
    stops at the first row and drops the rest), and a CONSTRUCT are answered
    before the query returns; the scope goes with the answer."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    asks = []
    original = rdflib_evaluate.evalAskQuery

    def counting(ctx, part):
        asks.append(part.name)
        return original(ctx, part)

    monkeypatch.setattr(rdflib_evaluate, "evalAskQuery", counting)

    def answered():
        result = graph.query(query)
        return result.askAnswer if result.type == "ASK" else len(result.graph)

    assert answered() == answer
    before = _footprint(store)
    assert answered() == answer
    assert len(asks) == 2 * rdflib_asks
    _assert_released(store, before, tracked)


#: A VALUES table crossed with a pattern it shares no variable with: every
#: row of the table, the IRIs the dictionary lacks included, is an answer.
_INTERLEAVED = [
    """SELECT ?x ?o WHERE {
        VALUES ?x { <http://ex.org/a1> <http://ex.org/a2> <http://ex.org/s1> }
        <http://ex.org/s0> <http://ex.org/knows> ?o }""",
    """SELECT ?x ?o WHERE {
        VALUES ?x { <http://ex.org/b1> <http://ex.org/b2> <http://ex.org/s2> }
        <http://ex.org/s5> <http://ex.org/knows> ?o }""",
]


def test_interleaved_queries_keep_their_own_foreign_codes(tmp_path, tracked, monkeypatch):
    """Two queries read in turns, a row at a time, whose constants outside
    the dictionary get the same negative codes: both are planned before
    either is read, and each answers as it does alone."""
    store = _store(tmp_path)
    graph = Graph(store=store)
    register_sparql_pushdown()
    alone = [run(graph, query) for query in _INTERLEAVED]
    assert [sorted(str(row[0]) for row in answer) for answer in alone] == [
        ["http://ex.org/a1", "http://ex.org/a2", "http://ex.org/s1"],
        ["http://ex.org/b1", "http://ex.org/b2", "http://ex.org/s2"],
    ]
    before = _footprint(store)
    given = []
    original = store._foreign_code

    def spy(term):
        code = original(term)
        given.append((code, term))
        return code

    monkeypatch.setattr(store, "_foreign_code", spy)
    results = [iter(graph.query(query)) for query in _INTERLEAVED]
    rows: list[list] = [[] for _ in results]
    pending = list(range(len(results)))
    while pending:
        for i in list(pending):
            row = next(results[i], None)
            if row is None:
                pending.remove(i)
            else:
                rows[i].append(tuple(row))
    del results
    assert [sorted(answer, key=_row_key) for answer in rows] == alone
    # a1 and b1 share one negative code, a2 and b2 another.
    assert len({term for _, term in given}) == 4 and len({code for code, _ in given}) == 2
    _assert_released(store, before, tracked)
