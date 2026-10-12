"""ORDER BY sorts IRIs by their bare string and blank nodes by their label,
as rdflib does.

A stored IRI is spelled ``<iri>``, so in code order an IRI follows the IRIs
it is a prefix of whenever the next character sorts below ``>``: ``<…/a>``
after ``<…/a#c>``, ``<…/s1>`` after ``<…/s10>``. rdflib compares the bare
strings and puts the prefix first. A blank node is spelled ``_:label``, which
sorts as its label.
"""

import pytest
from rdflib import XSD, BNode, Dataset, Graph, Literal, URIRef
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown

EX = "http://ex.org/"
P = URIRef(f"{EX}p")


def _iri(name: str) -> URIRef:
    return URIRef(EX + name)


#: (subject, object) pairs over ``P``. ``<…/a>`` is a prefix of every other
#: IRI object, followed by a character below ``>`` in all but ``<…/ab>``;
#: ``<…/s1>`` is a prefix of ``<…/s10>``, ``<…/s11>`` and ``<…/s12>``. Both
#: blank nodes share a subject, so their rows read the same in either order.
DATA = [
    ("s1", _iri("a")),
    ("s10", _iri("a")),
    ("s10", _iri("a1")),
    ("s11", _iri("a#c")),
    ("s11", Literal("9", datatype=XSD.integer)),
    ("s11", Literal("abc", lang="en")),
    ("s12", _iri("a.d")),
    ("s12", _iri("a/b")),
    ("s12", Literal("10", datatype=XSD.integer)),
    ("s12", BNode("x1")),
    ("s12", BNode("x2")),
    ("s2", _iri("a")),
    ("s2", _iri("a/b")),
    ("s2", _iri("ab")),
    ("s2", Literal("abc")),
]

#: Each answer is a single sequence: ``(?o, ?s)`` differs between rows, and
#: the queries sorting on ``?s`` alone project ``?s`` alone.
QUERIES = [
    f"SELECT ?s ?o WHERE {{ ?s <{P}> ?o }} ORDER BY ?o ?s",
    f"SELECT ?s ?o WHERE {{ ?s <{P}> ?o }} ORDER BY DESC(?o) ?s",
    f"SELECT ?s ?o WHERE {{ ?s <{P}> ?o }} ORDER BY ?o ?s LIMIT 4",
    f"SELECT ?s WHERE {{ ?s <{P}> ?o }} ORDER BY ?s",
    f"SELECT ?s WHERE {{ ?s <{P}> ?o }} ORDER BY ?s LIMIT 3",
]

#: The secondary index and the storage mode of each store configuration.
CONFIGURATIONS = {
    "copy-file": ("secondary-by-copy", False),
    "copy-mem": ("secondary-by-copy", True),
    "reference-file": ("secondary-by-reference", False),
    "reference-mem": ("secondary-by-reference", True),
}


def _graph(pairs) -> Graph:
    graph = Graph()
    for subject, obj in pairs:
        graph.add((_iri(subject), P, obj))
    return graph


def _vortex(directory, graph: Graph, name: str, **options):
    """``graph`` serialized as N-Triples and built into a ``.vortex`` file."""
    nt = directory / f"{name}.nt"
    nt.write_text(graph.serialize(format="nt"), encoding="utf-8")
    out = directory / f"{name}.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary", **options)
    return out


@pytest.fixture(scope="module")
def order_files(tmp_path_factory):
    """``DATA`` as a ``.vortex`` file per secondary index."""
    directory = tmp_path_factory.mktemp("order-by")
    graph = _graph(DATA)
    return {
        index: _vortex(directory, graph, index, indexes=[index])
        for index in ("secondary-by-copy", "secondary-by-reference")
    }


@pytest.fixture(params=sorted(CONFIGURATIONS))
def dataset(order_files, request):
    index, in_memory = CONFIGURATIONS[request.param]
    store = VortexRdflibStore(str(order_files[index]), in_memory=in_memory)
    assert store._dict is not None and store._dict.file_backed is not in_memory
    return Dataset(store=store, default_union=True)


@pytest.fixture
def ranked(monkeypatch) -> list:
    """Registers the pushdown and records each code set its ORDER BY ranks."""
    register_sparql_pushdown()
    calls = []
    rank_codes = pd._rank_codes

    def spy(store, foreign, codes):
        calls.append(codes)
        return rank_codes(store, foreign, codes)

    monkeypatch.setattr(pd, "_rank_codes", spy)
    return calls


def _rows(graph, sparql) -> list[tuple]:
    """The solution rows in order, each blank node replaced by its kind: the
    position of a blank node is compared, its label is the store's."""
    return [
        tuple(BNode if isinstance(term, BNode) else term for term in row)
        for row in graph.query(sparql)
    ]


@pytest.mark.parametrize("sparql", QUERIES)
def test_order_by_equals_rdflib(dataset, ranked, sparql):
    assert _rows(dataset, sparql) == _rows(_graph(DATA), sparql)
    assert ranked and all(ranked)


def test_blank_nodes_order_by_label(tmp_path, ranked):
    """Blank-node codes are in label order (``_:b1``, ``_:b10``, ``_:b2``), so
    they rank as their codes."""
    graph = _graph([("s", BNode(label)) for label in ("b2", "b10", "b1")])
    store = VortexRdflibStore(str(_vortex(tmp_path, graph, "blank")))
    assert store._dict is not None
    b1, b10, b2 = (store._dict.encode(f"_:{label}") for label in ("b1", "b10", "b2"))
    assert b1 is not None and b10 is not None and b2 is not None
    assert b1 < b10 < b2

    sparql = f"SELECT ?o WHERE {{ ?s <{P}> ?o }} ORDER BY ?o"
    expected = [(BNode("b1"),), (BNode("b10"),), (BNode("b2"),)]
    assert [tuple(row) for row in graph.query(sparql)] == expected
    stored = Dataset(store=store, default_union=True)
    assert [tuple(row) for row in stored.query(sparql)] == expected
    assert ranked
