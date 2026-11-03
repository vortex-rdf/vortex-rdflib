"""How the store reads quads for rdflib (:meth:`VortexRdflibStore.triples`):
the graph a call reads is resolved once, a ground pattern is one row, and
the union over every graph is a set by term code — as ``len()`` and the
pushdown count it."""

import pytest
from rdflib import Graph, URIRef
from tests.support import run
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

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
    calls = _Calls(store._store())
    store._native = calls  # ty: ignore[invalid-assignment]
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
    store._native = _CountsTwo(store._store())  # ty: ignore[invalid-assignment]
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
