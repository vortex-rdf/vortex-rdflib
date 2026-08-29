"""The pushdown must be observationally identical to rdflib's default
evaluator — every query shape is run both ways and compared exactly, on a
file-backed and on an in-memory store."""

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql.sparql import QueryContext
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexStore, register_sparql_pushdown, unregister_sparql_pushdown

FIXTURE_NT = """\
<http://ex.org/alice> <http://ex.org/name> "Alice" .
<http://ex.org/alice> <http://ex.org/knows> <http://ex.org/bob> .
<http://ex.org/alice> <http://ex.org/knows> <http://ex.org/carol> .
<http://ex.org/bob> <http://ex.org/name> "Bob"@en .
<http://ex.org/bob> <http://ex.org/knows> <http://ex.org/carol> .
<http://ex.org/bob> <http://ex.org/age> "42"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/carol> <http://ex.org/name> "Carol" .
<http://ex.org/carol> <http://ex.org/likes> <http://ex.org/carol> .
_:b0 <http://ex.org/name> "Anon" .
_:b0 <http://ex.org/knows> <http://ex.org/alice> .
"""

QUERIES = [
    # single patterns
    "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT ?p ?o WHERE { <http://ex.org/bob> ?p ?o }",
    "SELECT * WHERE { ?s ?p ?o }",
    # chain join (object of one TP is subject of the next)
    """SELECT ?n WHERE {
        <http://ex.org/alice> <http://ex.org/knows> ?x .
        ?x <http://ex.org/name> ?n }""",
    # star join
    """SELECT ?x ?n ?a WHERE {
        ?x <http://ex.org/name> ?n .
        ?x <http://ex.org/age> ?a }""",
    # three-pattern chain
    """SELECT ?a ?c WHERE {
        ?a <http://ex.org/knows> ?b .
        ?b <http://ex.org/knows> ?c .
        ?c <http://ex.org/name> ?n }""",
    # repeated variable inside one pattern (s == o)
    "SELECT ?x WHERE { ?x <http://ex.org/likes> ?x }",
    # ground pattern acting as a filter alongside a variable pattern
    """SELECT ?n WHERE {
        <http://ex.org/bob> <http://ex.org/knows> <http://ex.org/carol> .
        <http://ex.org/carol> <http://ex.org/name> ?n }""",
    # ground pattern that does NOT match: must kill all solutions
    """SELECT ?n WHERE {
        <http://ex.org/carol> <http://ex.org/knows> <http://ex.org/bob> .
        ?s <http://ex.org/name> ?n }""",
    # cross product (no shared variables)
    """SELECT ?a ?b WHERE {
        <http://ex.org/alice> <http://ex.org/name> ?a .
        <http://ex.org/bob> <http://ex.org/name> ?b }""",
    # bnode in the query pattern (acts as a variable)
    "SELECT ?n WHERE { [] <http://ex.org/name> ?n }",
    # OPTIONAL: inner BGP evaluated under outer bindings
    """SELECT ?x ?a WHERE {
        ?x <http://ex.org/name> ?n .
        OPTIONAL { ?x <http://ex.org/age> ?a } }""",
    # UNION
    """SELECT ?o WHERE {
        { <http://ex.org/alice> <http://ex.org/knows> ?o }
        UNION { ?o <http://ex.org/likes> ?o } }""",
    # FILTER over joined bindings
    """SELECT ?x WHERE {
        ?x <http://ex.org/name> ?n .
        FILTER(lang(?n) = "en") }""",
    # VALUES feeding bindings into the BGP
    """SELECT ?n WHERE {
        VALUES ?x { <http://ex.org/bob> <http://ex.org/carol> }
        ?x <http://ex.org/name> ?n }""",
    # literal join value propagating into subject position (unsatisfiable leg)
    """SELECT ?n WHERE {
        ?s <http://ex.org/name> ?n .
        ?n <http://ex.org/name> ?m }""",
    # zero-match pattern
    "SELECT ?s WHERE { ?s <http://ex.org/nothing> ?o }",
    # DISTINCT + ORDER BY on top of a join
    """SELECT DISTINCT ?b WHERE {
        ?a <http://ex.org/knows> ?b .
        ?b <http://ex.org/name> ?n } ORDER BY ?b""",
    # projection narrower than the pattern, and a projected variable the
    # pattern never binds
    "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }",
    "SELECT ?zzz WHERE { ?s <http://ex.org/name> ?o }",
    # LIMIT / OFFSET over single patterns: both evaluation paths see the
    # store's own row order, so the slices must agree exactly
    "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2",
    "SELECT * WHERE { ?s ?p ?o } LIMIT 3 OFFSET 2",
    "SELECT ?s WHERE { ?s ?p ?o } OFFSET 8",
    "SELECT ?s WHERE { ?s ?p ?o } LIMIT 0",
    # ASK: ground, variable, no match, repeated variable, join, empty
    """ASK { <http://ex.org/bob> <http://ex.org/age>
        "42"^^<http://www.w3.org/2001/XMLSchema#integer> }""",
    "ASK { ?s <http://ex.org/knows> ?o }",
    "ASK { ?s <http://ex.org/nothing> ?o }",
    "ASK { ?x <http://ex.org/likes> ?x }",
    "ASK { ?a <http://ex.org/knows> ?b . ?b <http://ex.org/age> ?n }",
    "ASK { ?a <http://ex.org/knows> ?b . ?b <http://ex.org/nothing> ?n }",
    "ASK {}",
]


@pytest.fixture(scope="module")
def dict_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown")
    nt = d / "fixture.nt"
    nt.write_text(FIXTURE_NT)
    out = d / "fixture.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(dict_vortex, request):
    """The fixture store, file-backed and loaded into memory: the native
    match path differs between the two, the pushdown must not."""
    return Graph(store=VortexStore(str(dict_vortex), in_memory=request.param))


def run(graph, sparql):
    """A query's answer in a comparable form: the ASK boolean, or the
    solution rows as a sorted multiset."""
    result = graph.query(sparql)
    if result.type == "ASK":
        return result.askAnswer
    return sorted(tuple(row) for row in result)


def both_ways(graph, sparql):
    """The answer with the pushdown and under rdflib's default evaluator."""
    register_sparql_pushdown()
    with_pushdown = run(graph, sparql)
    try:
        unregister_sparql_pushdown()
        without_pushdown = run(graph, sparql)
    finally:
        register_sparql_pushdown()
    return with_pushdown, without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_pushdown_equals_default_evaluator(graph, sparql):
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_probe_join_path_equals_default_evaluator(graph, monkeypatch, sparql):
    """Same A/B matrix with the probe threshold forced to zero, so every
    shared-variable join takes the per-binding probe path instead of the
    hash join — both strategies must be observationally identical."""
    monkeypatch.setattr(pd, "_PROBE_FANOUT", 0)
    with_probe, without_pushdown = both_ways(graph, sparql)
    assert with_probe == without_pushdown


@pytest.mark.parametrize("sparql", QUERIES)
def test_bgp_only_mode_equals_default_evaluator(graph, monkeypatch, sparql):
    """``VORTEX_RDF_PUSHDOWN_OPS=bgp``: only basic graph patterns are solved
    here, every head is rdflib's own operator over the rows we hand back."""
    monkeypatch.setattr(pd, "_ENABLED_OPS", frozenset({"BGP"}))
    bgp_only, without_pushdown = both_ways(graph, sparql)
    assert bgp_only == without_pushdown


def test_probe_join_triggers_on_skewed_join(tmp_path, monkeypatch):
    """A 1-row anchor joined against a 300-row predicate must take the probe
    path under the real threshold, and still produce the right rows."""
    nt = tmp_path / "skewed.nt"
    nt.write_text(
        '<http://ex.org/s0> <http://ex.org/rare> "anchor" .\n'
        + "".join(
            f"<http://ex.org/s{i}> <http://ex.org/big> <http://ex.org/o{i}> .\n" for i in range(300)
        )
    )
    out = tmp_path / "skewed.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")

    probes = []
    original = pd._probe_join
    monkeypatch.setattr(pd, "_probe_join", lambda *a: probes.append(1) or original(*a))
    graph = Graph(store=VortexStore(str(out)))
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?s ?o WHERE {
            ?s <http://ex.org/rare> "anchor" .
            ?s <http://ex.org/big> ?o }""",
    )
    assert rows == [(URIRef("http://ex.org/s0"), URIRef("http://ex.org/o0"))]
    assert probes, "the skewed join did not take the probe path"


def test_pushdown_is_actually_used(graph, monkeypatch):
    calls = []
    original = pd._solve_bgp
    monkeypatch.setattr(pd, "_solve_bgp", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 4
    assert calls, "pushdown hook was not invoked"


def test_head_intercepts_the_whole_slice_project_chain(graph, monkeypatch):
    """A ``Slice(Project(BGP))`` is one interception at the Slice: rdflib
    never evaluates the Project or the BGP below it itself."""
    heads = []
    original = pd._eval_head
    monkeypatch.setattr(pd, "_eval_head", lambda *a: heads.append(a[2].name) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2
    assert heads == ["Slice"]
    heads.clear()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 4
    assert heads == ["Project"]


def test_solutions_are_built_without_context_push(graph, monkeypatch):
    """Solutions are constructed directly as FrozenBindings; the per-row
    ``ctx.push()`` scope rdflib's own evalBGP opens is never needed."""
    pushes = []
    original = QueryContext.push
    monkeypatch.setattr(QueryContext, "push", lambda self: pushes.append(1) or original(self))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 4
    assert not pushes


def test_limit_decodes_only_the_first_chunk(tmp_path):
    """LIMIT stops decoding: a 700-row match under ``LIMIT 10`` decodes no
    more than the first chunk of rows."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        )
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    rows = run(graph, "SELECT * WHERE { ?s ?p ?o } LIMIT 10")
    assert len(rows) == 10
    assert len(store._decode_cache) <= 3 * pd._CHUNK_START


class _CountingNative:
    """A VortexRdfStore stand-in that counts the calls the pushdown makes."""

    def __init__(self, inner):
        self._inner = inner
        self.counts = 0
        self.matches = 0

    def count_quads(self, *args, **kwargs):
        self.counts += 1
        return self._inner.count_quads(*args, **kwargs)

    def match_codes(self, *args, **kwargs):
        self.matches += 1
        return self._inner.match_codes(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_ask_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = _CountingNative(graph.store._store())
    graph.store._native = native
    assert run(graph, "ASK { ?s <http://ex.org/knows> ?o }") is True
    assert run(graph, "ASK { ?s <http://ex.org/nothing> ?o }") is False
    assert (native.counts, native.matches) == (2, 0)


def test_native_none_falls_back_at_call_time(graph):
    """A store whose code path declines (``match_codes`` -> None) makes the
    hook raise while rdflib is still listening, so the default evaluator
    answers — no lazily raised NotImplementedError reaches the caller."""
    inner = graph.store._store()

    class _NoCodes:
        def match_codes(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._native = _NoCodes()
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2


def test_pushdown_ops_env_switch(dict_vortex, monkeypatch):
    monkeypatch.setattr(pd, "_ENABLED_OPS", pd._ALL_OPS)
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "bgp")
    VortexStore(str(dict_vortex))
    assert pd._ENABLED_OPS == {"BGP"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "BGP, Project")
    VortexStore(str(dict_vortex))
    assert pd._ENABLED_OPS == {"BGP", "Project"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "Bogus")
    with pytest.raises(ValueError, match="Bogus"):
        VortexStore(str(dict_vortex))


def test_non_vortex_graphs_unaffected(dict_vortex):
    register_sparql_pushdown()
    g = Graph()
    g.add((URIRef("http://ex.org/a"), URIRef("http://ex.org/name"), Literal("A")))
    rows = run(g, "SELECT ?n WHERE { ?s <http://ex.org/name> ?n }")
    assert rows == [(Literal("A"),)]


def test_string_fallback_when_code_path_disabled(dict_vortex, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    graph = Graph(store=VortexStore(str(dict_vortex)))
    rows = run(
        graph,
        """SELECT ?n WHERE {
            <http://ex.org/alice> <http://ex.org/knows> ?x .
            ?x <http://ex.org/name> ?n }""",
    )
    assert {row[0] for row in rows} == {Literal("Bob", lang="en"), Literal("Carol")}
