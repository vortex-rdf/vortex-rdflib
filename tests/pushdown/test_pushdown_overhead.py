"""What the pushdown pays per query before any native call: it reads
rdflib's algebra and expression nodes as the dicts they are, and tells
rdflib's terms apart by their exact classes.

rdflib's ``CompValue`` answers an attribute read by resolving the field
against the context of an expression being evaluated: outside one, after a
lookup of the context that misses and a ``KeyError`` caught — about a
microsecond a read. rdflib's term classes test an instance through
``abc.ABCMeta``, a few hundred nanoseconds for each class a term is not.
Over the shapes below, the pushdown's own code does neither; rdflib's does
what it does."""

import abc
import sys

import pytest
from rdflib import Dataset, term
from rdflib.plugins.sparql.parserutils import CompValue
from tests.support import both_ways, run
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore

EX = "http://ex.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"

QUADS = "".join(
    f'<{EX}s{i}> <{EX}p> "{i}"^^<{XSD}integer> <{EX}g{i % 2}> .\n'
    f'<{EX}s{i}> <{EX}label> "item {i}"@{"en" if i % 2 else "fr"} <{EX}g{i % 2}> .\n'
    f"<{EX}s{i}> <{EX}type> <{EX}T{i % 3}> .\n"
    for i in range(12)
)

SHAPES = {
    "ask-bound": f"ASK {{ <{EX}s1> <{EX}type> <{EX}T1> }}",
    "ask-var": f"ASK {{ ?s <{EX}type> ?o }}",
    "count-all": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
    "star": f"SELECT ?s ?l WHERE {{ ?s <{EX}type> <{EX}T1> . ?s <{EX}label> ?l }}",
    "optional": f"""SELECT ?s ?l ?v WHERE {{ ?s <{EX}type> <{EX}T0> .
        OPTIONAL {{ ?s <{EX}p> ?v FILTER(?v > 3) }} ?s <{EX}label> ?l }}""",
    "filter-range": f"SELECT ?s WHERE {{ ?s <{EX}p> ?v FILTER(?v >= 2 && ?v < 9) }}",
    "filter-lang": f'SELECT ?s WHERE {{ ?s <{EX}label> ?l FILTER(langMatches(lang(?l), "en")) }}',
    "filter-regex": f'SELECT ?s WHERE {{ ?s <{EX}label> ?l FILTER(REGEX(STR(?l), "^item 1")) }}',
    "minus": f"SELECT ?s WHERE {{ ?s <{EX}type> ?t MINUS {{ ?s <{EX}type> <{EX}T2> }} }}",
    "values": f"SELECT ?s ?t WHERE {{ VALUES ?t {{ <{EX}T0> <{EX}T2> }} ?s <{EX}type> ?t }}",
    "graph-var": f"SELECT ?g ?s WHERE {{ GRAPH ?g {{ ?s <{EX}p> ?v }} }}",
    "head": f"SELECT DISTINCT ?t WHERE {{ ?s <{EX}type> ?t }} ORDER BY DESC(?t) LIMIT 2",
    "group": f"SELECT ?t (COUNT(?s) AS ?n) WHERE {{ ?s <{EX}type> ?t }} GROUP BY ?t",
}


@pytest.fixture(scope="module")
def quads_file(tmp_path_factory):
    directory = tmp_path_factory.mktemp("overhead")
    source = directory / "data.nq"
    source.write_text(QUADS, encoding="utf-8")
    path = directory / "data.vortex"
    serialize_rdf(str(source), str(path), format="nquads", layout="dictionary")
    return str(path)


def _ours(frame) -> str | None:
    """The pushdown's frame making a call from ``frame`` up, past abc's own."""
    while frame is not None and "abc" in frame.f_code.co_filename.rsplit("/", 1)[-1]:
        frame = frame.f_back
    if frame is None or "vortex_rdflib" not in frame.f_code.co_filename:
        return None
    return f"{frame.f_code.co_filename.rsplit('vortex_rdflib/', 1)[-1]}:{frame.f_code.co_name}"


@pytest.fixture
def slow_calls(monkeypatch) -> list:
    """The attribute reads of rdflib nodes and the abc tests of rdflib's term
    classes that the pushdown's own code makes."""
    calls: list = []
    read, test = CompValue.__getattr__, abc.ABCMeta.__instancecheck__

    def spy_read(node, name):
        where = _ours(sys._getframe(1))
        if where is not None:
            calls.append((where, f"{node.name}.{name}"))
        return read(node, name)

    def spy_test(cls, instance):
        where = _ours(sys._getframe(1))
        if where is not None and cls.__module__ == term.__name__:
            calls.append((where, f"{type(instance).__name__} vs {cls.__name__}"))
        return test(cls, instance)

    monkeypatch.setattr(CompValue, "__getattr__", spy_read)
    monkeypatch.setattr(abc.ABCMeta, "__instancecheck__", spy_test)
    return calls


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("sparql", SHAPES.values(), ids=SHAPES.keys())
def test_a_query_reads_rdflib_nodes_and_terms_without_slow_calls(
    quads_file, slow_calls, monkeypatch, in_memory, sparql
):
    dataset = Dataset(store=VortexRdflibStore(quads_file, in_memory=in_memory), default_union=True)
    pushed, plain = both_ways(dataset, sparql)
    assert pushed == plain
    slow_calls.clear()
    # The pushdown answers the whole query: rdflib reads no pattern itself.
    monkeypatch.setattr(VortexRdflibStore, "triples", None)
    assert run(dataset, sparql) == pushed
    assert slow_calls == [], sorted(set(slow_calls))
