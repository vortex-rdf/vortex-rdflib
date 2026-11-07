"""A SPARQL constant vortex-rdf's parser refuses matches nothing (#26): a BGP,
an ASK, a COUNT, a join, an OPTIONAL's right side and a GRAPH name holding
one answer as rdflib's own store does, through the probe and the scan
plans alike, and so does a context binding (``initBindings``)."""

import pytest
from rdflib import Dataset, URIRef
from tests.support import run
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown
from vortex_rdflib.settings import SETTINGS

EX = "http://ex.org/"
DATA = f'<{EX}a> <{EX}p> "x" .\n<{EX}a> <{EX}q> <{EX}b> .\n<{EX}b> <{EX}p> "y" <{EX}g1> .\n'

QUERIES = {
    "bgp": "SELECT ?o WHERE { <http://x/%zz> ?p ?o }",
    "ask": "ASK { ?s ?p <http://x/%zz> }",
    "count": "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://x/a#b#c> ?o }",
    "join": f"SELECT * WHERE {{ ?s <{EX}q> ?o . ?o ?p <http://x/%zz> }}",
    "optional": f"SELECT * WHERE {{ ?s <{EX}p> ?o OPTIONAL {{ ?s ?q <http://x/%zz> }} }}",
    "graph": "SELECT * WHERE { GRAPH <http://x/%zz> { ?s ?p ?o } }",
    "lang": 'SELECT * WHERE { ?s ?p "x"@en-a }',
    "datatype": 'SELECT * WHERE { ?s ?p "x"^^<http://x/%zz> }',
    # A VALUES row rdflib substitutes into the pattern it hands the pushdown.
    "values": "SELECT * WHERE { VALUES ?s { <http://x/%zz> } ?s ?p ?o }",
    # VALUES tables and FILTERs the pushdown joins and decides itself: the
    # refused term keys no row, the valid one keeps its.
    "values-key": f"SELECT * WHERE {{ VALUES ?o {{ <http://x/%zz> <{EX}b> }} ?s <{EX}q> ?o }}",
    "values-after": f"SELECT * WHERE {{ ?s <{EX}q> ?o VALUES ?o {{ <http://x/%zz> <{EX}b> }} }}",
    "filter": "SELECT * WHERE { ?s ?p ?o FILTER(?o = <http://x/%zz>) }",
    "filter-in": f"SELECT * WHERE {{ ?s ?p ?o FILTER(?o IN (<http://x/%zz>, <{EX}b>)) }}",
}


@pytest.fixture(scope="module")
def quads_file(tmp_path_factory):
    directory = tmp_path_factory.mktemp("refused")
    source = directory / "data.nq"
    source.write_text(DATA, encoding="utf-8")
    path = directory / "data.vortex"
    serialize_rdf(str(source), str(path), layout="dictionary")
    return str(path)


@pytest.fixture(scope="module")
def memory_dataset():
    """rdflib's own store over the same quads: the answer to match."""
    dataset = Dataset(default_union=True)
    dataset.parse(data=DATA, format="nquads")
    return dataset


@pytest.mark.parametrize("fanout", [0, 10**9], ids=["probe", "scan"])
@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("sparql", QUERIES.values(), ids=QUERIES.keys())
def test_a_query_naming_a_refused_term_answers_as_rdflib(
    quads_file, memory_dataset, monkeypatch, in_memory, fanout, sparql
):
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    register_sparql_pushdown()
    dataset = Dataset(store=VortexRdflibStore(quads_file, in_memory=in_memory), default_union=True)
    assert run(dataset, sparql) == run(memory_dataset, sparql)


@pytest.mark.parametrize("term", [URIRef("http://x/%zz"), URIRef("http://x/\ud800")])
def test_init_bindings_with_a_refused_term_match_nothing(quads_file, term):
    register_sparql_pushdown()
    dataset = Dataset(store=VortexRdflibStore(quads_file), default_union=True)
    rows = dataset.query("SELECT ?p ?o WHERE { ?s ?p ?o }", initBindings={"s": term})
    assert list(rows) == []
