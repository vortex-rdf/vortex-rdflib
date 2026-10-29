"""The union default graph is a set of triples: a triple several graphs hold
is one solution, as in rdflib's own Dataset (SPARQL 1.1 §13). Answered with
the pushdown and without, on every index and residency, with matches read in
chunks, streamed and probed, against rdflib's in-memory union."""

import random

import pytest
from rdflib import Dataset, Graph
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore
from vortex_rdflib.settings import SETTINGS

from tests.support import both_ways, run

EX = "http://ex.org/"
PREFIX = f"PREFIX : <{EX}> "
QUERIES = {
    "all": "SELECT * WHERE { ?s ?p ?o }",
    "predicate": "SELECT ?s ?o WHERE { ?s :p0 ?o }",
    "object": "SELECT ?s ?p WHERE { ?s ?p :o1 }",
    "ground": "SELECT * WHERE { :s1 :p0 :o1 }",
    "join": "SELECT ?s ?o ?z WHERE { ?s :p0 ?o . ?o :p1 ?z }",
    "star": "SELECT ?s ?a ?b WHERE { ?s :p0 ?a . ?s :p1 ?b }",
    "optional": "SELECT ?s ?b WHERE { ?s :p0 :o1 OPTIONAL { ?s :p1 ?b } }",
    "exists": "SELECT ?s WHERE { ?s :p0 ?o FILTER EXISTS { ?s :p1 ?b } }",
    "count": "SELECT (COUNT(*) AS ?n) WHERE { ?s :p0 ?o }",
    "count-all": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
    "count-group": "SELECT ?s (COUNT(?o) AS ?n) WHERE { ?s :p0 ?o } GROUP BY ?s",
    "graph-variable": "SELECT ?g ?s ?o WHERE { GRAPH ?g { ?s :p0 ?o } }",
    "graph-join": "SELECT ?g ?s WHERE { GRAPH ?g { ?s :p0 ?o . ?o :p1 ?z } }",
}


@pytest.fixture(scope="module")
def quads():
    rng = random.Random(11)
    graphs = ["", *(f" <{EX}g{i}>" for i in range(4))]
    objects = [f"<{EX}o{i}>" for i in range(8)] + ['"x"', '"y"@en']
    lines = {
        f"<{EX}s{rng.randrange(8)}> <{EX}p{rng.randrange(2)}> {rng.choice(objects)}{rng.choice(graphs)} .\n"
        for _ in range(300)
    }
    # Objects that are subjects too, so the joins have something to join.
    lines |= {f"<{EX}o{i}> <{EX}p1> <{EX}s{i}>{g} .\n" for i in range(8) for g in graphs[: 1 + i % 3]}
    return "".join(sorted(lines))


@pytest.fixture(scope="module")
def stores(quads, tmp_path_factory):
    directory = tmp_path_factory.mktemp("union")
    source = directory / "union.nq"
    source.write_text(quads, encoding="utf-8")
    paths = {}
    for index in (None, "secondary-by-copy", "secondary-by-reference"):
        out = directory / f"union-{index}.vortex"
        options = {} if index is None else {"indexes": [index]}
        serialize_rdf(str(source), str(out), layout="dictionary", format="nquads", **options)
        paths[index] = out
    return paths


@pytest.fixture(scope="module")
def expected(quads):
    memory = Dataset(default_union=True)
    memory.parse(data=quads, format="nquads")
    return {name: run(memory, PREFIX + sparql) for name, sparql in QUERIES.items()}, len(memory)


@pytest.mark.parametrize("small", [False, True], ids=["whole", "chunked"])
@pytest.mark.parametrize("fanout", [0, 100])
@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("index", [None, "secondary-by-copy", "secondary-by-reference"])
def test_the_union_holds_each_triple_once(stores, expected, monkeypatch, index, in_memory, fanout, small):
    answers, size = expected
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    if small:
        # Matches read in chunks of a few rows, joins streamed past a tiny
        # budget: the runs of one triple cross chunk boundaries.
        for name in ("triples_chunk_rows", "piece_rows", "pair_budget", "chunk_start", "chunk_max"):
            monkeypatch.setattr(SETTINGS, name, 3)
    store = VortexRdflibStore(str(stores[index]), in_memory=in_memory)
    assert store._union_has_repeats()
    dataset = Dataset(store=store, default_union=True)
    for name, sparql in QUERIES.items():
        with_pushdown, without_pushdown = both_ways(dataset, PREFIX + sparql)
        assert with_pushdown == answers[name], name
        assert without_pushdown == answers[name], name
    assert len(store) == len(Graph(store=store)) == size
    assert len(set(dataset.quads((None, None, None, None)))) == quads_in(stores[index])


def quads_in(path) -> int:
    return len(VortexRdflibStore(str(path))._store())


def test_a_store_of_one_graph_pays_nothing_for_the_union(tmp_path):
    source = tmp_path / "one.nt"
    source.write_text(f"<{EX}s> <{EX}p> <{EX}o> .\n<{EX}s> <{EX}p> <{EX}o2> .\n", encoding="utf-8")
    out = tmp_path / "one.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out))
    assert not store._union_has_repeats()
    assert len(store) == 2
