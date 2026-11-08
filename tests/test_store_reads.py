"""How the store reads quads for rdflib (:meth:`VortexRdflibStore.triples`):
the graph a call reads is resolved once, a ground pattern is one row, the
union over every graph is a set by term code — as ``len()`` and the pushdown
count it — and a store without term codes streams its match."""

import gc

import pytest
from rdflib import Graph, URIRef
from tests.support import native_spy, run
from vortex_rdf import Cancelled, CancelToken, serialize_rdf

from vortex_rdflib import VortexRdflibStore, cancel_scope, register_sparql_pushdown
from vortex_rdflib.settings import SETTINGS

EX = "http://ex.org/"
INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


def _quads(tmp_path, lines: list[str], layout: str = "dictionary") -> str:
    source = tmp_path / f"quads-{layout}.nq"
    source.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / f"quads-{layout}.vortex"
    serialize_rdf(str(source), str(out), layout=layout, format="nquads")
    return str(out)


class _Calls:
    """The native handle with every call it serves recorded by name."""

    def __init__(self, inner):
        self.inner = inner
        self.names: list[str] = []

    def __len__(self):
        return len(self.inner)

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            self.names.append(name)
            return attribute(*args, **kwargs)

        return call


_TWO_GRAPHS = [
    f"<{EX}s> <{EX}p> <{EX}o> <{EX}g1> .\n",
    f"<{EX}s> <{EX}p> <{EX}o> <{EX}g2> .\n",
    f"<{EX}s> <{EX}q> <{EX}o> <{EX}g1> .\n",
]


def test_triples_on_a_bare_graph_makes_two_native_calls(tmp_path):
    """A bare ``Graph`` outside a query: its blank-node identifier is
    resolved once per ``triples()`` call — one count finds it names no
    graph — and the pattern is matched once."""
    store = VortexRdflibStore(_quads(tmp_path, _TWO_GRAPHS))
    graph = Graph(store=store)
    store._union_has_repeats()  # the file's one-time bookkeeping is not a call's cost
    calls = _Calls(store._opened().native)
    store._opened().native = calls  # ty: ignore[invalid-assignment]
    for pattern in [(None, None, None), (URIRef(f"{EX}s"), None, None)]:
        calls.names.clear()
        assert list(graph.triples(pattern))
        assert calls.names == ["count_quads", "match_codes"], pattern


def test_a_ground_pattern_in_a_named_graph_is_yielded_once(tmp_path):
    """A store's quads are unique, so a ground pattern in one graph is there
    or not: the count is read as that, and the triple yielded once."""

    class _CountsTwo(_Calls):
        def count_quads(self, *args, **kwargs):
            return 2  # what no file answers

    store = VortexRdflibStore(_quads(tmp_path, _TWO_GRAPHS))
    store._opened().native = _CountsTwo(store._opened().native)  # ty: ignore[invalid-assignment]
    ground = (URIRef(f"{EX}s"), URIRef(f"{EX}p"), URIRef(f"{EX}o"))
    rows = list(store.triples(ground, Graph(store=store, identifier=URIRef(f"{EX}g1"))))
    assert [triple for triple, _ in rows] == [ground]


def test_a_ground_pattern_in_the_union_is_one_row_with_every_graph(tmp_path):
    """Under the union a ground triple is one row, carrying the context of
    each graph that holds it."""
    store = VortexRdflibStore(_quads(tmp_path, _TWO_GRAPHS))
    ground = (URIRef(f"{EX}s"), URIRef(f"{EX}p"), URIRef(f"{EX}o"))
    [(triple, contexts)] = list(store.triples(ground))
    assert triple == ground
    assert sorted(str(graph.identifier) for graph in contexts) == [f"{EX}g1", f"{EX}g2"]


def test_the_union_merges_by_code(tmp_path):
    """Two spellings of one integer are two codes, which rdflib reads as one
    term: the union is a set of code triples, so each is its own row, as
    ``len()`` and the pushdown's ``COUNT(*)`` count them."""
    lines = [
        f'<{EX}s> <{EX}p> "042"^^<{INTEGER}> <{EX}g1> .\n',
        f'<{EX}s> <{EX}p> "42"^^<{INTEGER}> <{EX}g1> .\n',
        f"<{EX}s> <{EX}q> <{EX}o> <{EX}g1> .\n",
        f"<{EX}s> <{EX}q> <{EX}o> <{EX}g2> .\n",
    ]
    store = VortexRdflibStore(_quads(tmp_path, lines))
    graph = Graph(store=store)
    register_sparql_pushdown()
    rows = list(store.triples((URIRef(f"{EX}s"), URIRef(f"{EX}p"), None)))
    assert len(rows) == 2
    assert all([str(c.identifier) for c in contexts] == [f"{EX}g1"] for _, contexts in rows)
    [(count,)] = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")
    assert len(store) == int(count) == len(list(store.triples((None, None, None)))) == 3


@pytest.mark.parametrize("layout", ["default", "typed-object"])
def test_string_layout_len_counts_distinct_triples(tmp_path, monkeypatch, layout):
    """A layout without codes counts the union's distinct triples from their
    spellings: the rows of a triple are adjacent, and no term is built."""
    store = VortexRdflibStore(_quads(tmp_path, _TWO_GRAPHS, layout))
    built: list[str] = []
    original = store._from_n3_safe
    monkeypatch.setattr(store, "_from_n3_safe", lambda s: built.append(s) or original(s))
    assert len(store) == 2
    assert built == []


# A read of a store without term codes streams its match through iter_quads
# (#27): rows are pulled as they are consumed, the term memo holds one chunk,
# a cancel stops the read at its next chunk, and an abandoned read lets the
# native iterator go.

TWENTY = [f"<{EX}s{i:02d}> <{EX}p> <{EX}o{i:02d}> .\n" for i in range(20)]


def _no_code_file(tmp_path, lines, *, quads=False) -> str:
    """A store file without term codes (the default layout)."""
    source = tmp_path / ("data.nq" if quads else "data.nt")
    source.write_text("".join(lines), encoding="utf-8")
    path = tmp_path / "data.vortex"
    serialize_rdf(str(source), str(path), layout="default")
    return str(path)


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_a_read_without_codes_pulls_rows_as_they_are_consumed(tmp_path, monkeypatch, in_memory):
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 4)
    graph = Graph(store=VortexRdflibStore(_no_code_file(tmp_path, TWENTY), in_memory=in_memory))
    spy = native_spy(graph)
    first = next(iter(graph.triples((None, None, None))))
    assert first[1] == URIRef(EX + "p")
    assert spy.names() == ["iter_quads"]
    assert spy.rows == [1]


def test_a_read_without_codes_parses_a_term_once_per_chunk(tmp_path, monkeypatch):
    """The term memo starts over every chunk, so it holds one chunk's terms
    at most, but keeps the last row's: <A>, on the even rows, is parsed once
    per chunk of four; <B>, on the odd rows and so on every chunk's last
    row, once."""
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 4)
    lines = [f"<{EX}s{i:02d}> <{EX}p> <{EX}{'AB'[i % 2]}> .\n" for i in range(20)]
    graph = Graph(store=VortexRdflibStore(_no_code_file(tmp_path, lines)))
    parsed, original = [], VortexRdflibStore._from_n3_safe
    monkeypatch.setattr(
        VortexRdflibStore,
        "_from_n3_safe",
        staticmethod(lambda raw: parsed.append(raw) or original(raw)),
    )
    assert len(list(graph.triples((None, None, None)))) == 20
    assert parsed.count(f"<{EX}A>") == 5
    assert parsed.count(f"<{EX}B>") == 1


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_a_cancel_stops_a_read_without_codes_at_its_next_chunk(tmp_path, monkeypatch, in_memory):
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 4)
    graph = Graph(store=VortexRdflibStore(_no_code_file(tmp_path, TWENTY), in_memory=in_memory))
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(graph.triples((None, None, None)))
        next(rows)
        token.cancel()
        with pytest.raises(Cancelled):
            for _ in range(4):
                next(rows)


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_an_abandoned_read_without_codes_releases_its_native_iterator(
    tmp_path, monkeypatch, in_memory
):
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 4)
    store = VortexRdflibStore(_no_code_file(tmp_path, TWENTY), in_memory=in_memory)
    graph = Graph(store=store)
    spy = native_spy(graph)
    rows = iter(graph.triples((None, None, None)))
    next(rows)
    assert spy.feeds
    del rows
    gc.collect()
    assert all(feed() is None for feed in spy.feeds)


def test_the_union_without_codes_counts_a_triple_straddling_chunks_once(tmp_path, monkeypatch):
    """A triple held by two graphs whose rows a chunk boundary splits is one
    triple of the union (a guard: it passes before streaming, and fails if a
    run of one triple restarts at a chunk boundary)."""
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 3)
    lines = [f"<{EX}s{i}> <{EX}p> <{EX}o> <{EX}g{g}> .\n" for i in range(3) for g in (1, 2)]
    store = VortexRdflibStore(_no_code_file(tmp_path, lines, quads=True))
    assert len(store) == 3
    graphs = store.contexts((URIRef(EX + "s1"), URIRef(EX + "p"), URIRef(EX + "o")))
    assert sorted(str(graph.identifier) for graph in graphs) == [EX + "g1", EX + "g2"]


def test_the_union_without_codes_yields_a_triple_straddling_chunks_once(tmp_path, monkeypatch):
    """The union's rows of one triple are told apart by identity
    (``union.one_per_triple``): a triple whose two graphs' rows a chunk
    boundary splits keeps its term objects across the boundary, and is
    yielded once with both graphs."""
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 3)
    lines = [f"<{EX}s{i}> <{EX}p> <{EX}o> <{EX}g{g}> .\n" for i in range(3) for g in (1, 2)]
    graph = Graph(store=VortexRdflibStore(_no_code_file(tmp_path, lines, quads=True)))
    rows = list(graph.store.triples((None, None, None)))
    assert [str(triple[0]) for triple, _ in rows] == [EX + "s0", EX + "s1", EX + "s2"]
    assert all(len(list(contexts)) == 2 for _, contexts in rows)
