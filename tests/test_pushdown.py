"""The pushdown must be observationally identical to rdflib's default
evaluator — every query shape is run both ways and compared exactly, on a
file-backed and on an in-memory store."""

import contextlib
import io
import sys
import time
from dataclasses import dataclass
from typing import Any

import pytest
from rdflib import Dataset, Graph, Literal, URIRef, Variable
from rdflib.plugins.sparql.sparql import QueryContext
from tests.corpus import (
    FIXTURE_NQ,
    FIXTURE_NT,
    GRAPH_QUERIES,
    ORDER_QUERIES,
    PROBE_MULTIPLICITY,
    PROBE_QUADS,
    QUERIES,
)
from tests.support import (
    both_ways,
    counting_native,
    counting_term_dict,
    row_key,
    run,
    run_ordered,
    trace_events,
)
from vortex_rdf import Cancelled, CancelToken, VortexRdfStore, serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import (
    VortexRdflibStore,
    filters,
    register_sparql_pushdown,
    settings,
    unregister_sparql_pushdown,
)
from vortex_rdflib.settings import SETTINGS


@pytest.fixture(scope="module")
def dict_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown")
    nt = d / "fixture.nt"
    nt.write_text(FIXTURE_NT, encoding="utf-8")
    out = d / "fixture.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(dict_vortex, request):
    """The fixture store, file-backed and loaded into memory: the native
    match path differs between the two, the pushdown must not."""
    return Graph(store=VortexRdflibStore(str(dict_vortex), in_memory=request.param))


# One param, so the node ids keep their file- prefix.
@pytest.fixture(params=["file"])
def file_graph(dict_vortex):
    """The fixture store, file-backed only: the other routes; the default route runs both modes."""
    return Graph(store=VortexRdflibStore(str(dict_vortex), in_memory=False))


@pytest.fixture(scope="module")
def dict_vortex_quads(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown-quads")
    nq = d / "fixture.nq"
    nq.write_text(FIXTURE_NQ, encoding="utf-8")
    out = d / "fixture.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def dataset(dict_vortex_quads, request):
    """The same statements spread over named graphs, as a Dataset whose
    default graph is the union of every graph — so the query set above asks
    the same questions with the graph column in play."""
    return Dataset(
        store=VortexRdflibStore(str(dict_vortex_quads), in_memory=request.param), default_union=True
    )


# One param, so the node ids keep their file- prefix.
@pytest.fixture(params=["file"])
def file_dataset(dict_vortex_quads):
    """The quad store, file-backed only: the other routes; the default route runs both modes."""
    return Dataset(
        store=VortexRdflibStore(str(dict_vortex_quads), in_memory=False), default_union=True
    )


@pytest.fixture(scope="module")
def variant_vortex(tmp_path_factory):
    """The triple fixture with each secondary index, keyed by index."""
    d = tmp_path_factory.mktemp("pushdown-variants")
    nt = d / "fixture.nt"
    nt.write_text(FIXTURE_NT, encoding="utf-8")
    files = {}
    for name, indexes in (
        ("none", []),
        ("by-copy", ["secondary-by-copy"]),
        ("by-reference", ["secondary-by-reference"]),
    ):
        out = d / f"fixture-{name}.vortex"
        serialize_rdf(str(nt), str(out), layout="dictionary", indexes=indexes)
        files[name] = out
    return files


#: Store configurations the native layer serves differently: a memory-mapped
#: dictionary (`TermDict.file_backed`), and the secondary indexes, whose
#: served views order an unlimited match their own way — which a LIMIT
#: pushed into the match must not change.
STORE_VARIANTS: dict[str, tuple[str, dict[str, Any]]] = {
    "file-backed-dictionary": ("none", {"in_memory": False}),
    "secondary-by-copy": ("by-copy", {}),
    "secondary-by-reference-mem": ("by-reference", {"in_memory": True}),
}


@pytest.fixture(params=sorted(STORE_VARIANTS))
def variant_graph(variant_vortex, request):
    index, kwargs = STORE_VARIANTS[request.param]
    store = VortexRdflibStore(str(variant_vortex[index]), **kwargs)
    if request.param == "file-backed-dictionary":
        assert store._dict is not None and store._dict.file_backed
    return Graph(store=store)


@dataclass(frozen=True)
class Forcing:
    """A path of the planner forced on: the settings that force it, the
    answer it gives every point-lookup question (``None``: the store's own,
    ``False``: every probe would scan, ``True``: every probe is a point
    lookup), and the runs of its equivalence matrix — each a corpus
    (:data:`_CORPORA`) over the views it names (``graph-file`` and
    ``graph-mem`` over the triples, ``dataset-*`` over the quads, as a
    union-default ``Dataset``)."""

    settings: dict
    runs: tuple
    point: bool | None = None


_GRAPH = ("graph-file", "graph-mem")
_DATASET = ("dataset-file", "dataset-mem")
#: Every corpus, file-backed and in memory.
_EVERY = (("queries", _GRAPH), ("order", _GRAPH), ("graph", _DATASET))
#: The paths with no file-or-memory difference of their own, file-backed.
_FILE = (("queries", ("graph-file",)),)
_FILE_AND_GRAPHS = (("queries", ("graph-file",)), ("graph", ("dataset-file",)))

#: The default plan, and every path forced on, by name.
FORCINGS = {
    "default": Forcing(
        {}, (("queries", (*_GRAPH, "dataset-file")), ("order", _GRAPH), ("graph", _DATASET))
    ),
    # Every shared-variable join planned per row: probes where they are point
    # lookups, and a keep-scan in a BGP where they would scan.
    "probe0": Forcing({"probe_fanout": 0}, _FILE_AND_GRAPHS),
    # Only basic graph patterns solved here (VORTEX_RDF_PUSHDOWN_OPS=bgp):
    # every head is rdflib's own operator, and rdflib evaluates a `Graph`
    # node itself, pushing each block down under a constant scope.
    "bgp_only": Forcing({"enabled_ops": frozenset({"BGP"})}, _FILE_AND_GRAPHS),
    # The native term predicates off: every conjunct they would decide runs
    # on the Python fast route.
    "python_filters": Forcing({"native_filters": False}, _FILE),
    # VORTEX_RDF_FILTER_FAST=0: every FILTER value through rdflib's own
    # evaluator, once per distinct value.
    "generic_filters": Forcing({"fast_filters": False}, _FILE_AND_GRAPHS),
    # Every probe a scan, through the batch calls in batches of three, a lone
    # probe included; a BGP step keep-scans instead.
    "batched_scans": Forcing(
        {"probe_fanout": 0, "probe_batch": 3, "scan_batch_min": 1, "memory_scan_batch_min": 1},
        _FILE,
        point=False,
    ),
    # Every probe a point lookup, through the batch calls in batches of
    # three, a lone probe included: BGP steps probe and batch too.
    "batched_points": Forcing(
        {"probe_fanout": 0, "probe_batch": 3, "cheap_batch_min": 1}, _FILE, point=True
    ),
    # COUNT(DISTINCT) groups from the distinct (key, value) pairs rather than
    # one native gather per group.
    "group_pairs": Forcing({"native_groups_max": 0}, (("counts", _DATASET),)),
    # A pair budget of 0: every BGP join step that makes a row is over it.
    "budget0": Forcing({"pair_budget": 0}, _EVERY),
    # Every last hash step that shares one variable and makes a row streams,
    # its fed side cut into pieces of one row, or of three.
    "stream_pieces1": Forcing({"pair_budget": 0, "piece_rows": 1}, _EVERY),
    "stream_pieces3": Forcing({"pair_budget": 0, "piece_rows": 3}, _EVERY),
    # The probe threshold at zero and every probe a scan: every BGP join step
    # that shares a variable keep-scans its pattern.
    "keep_scan": Forcing({"probe_fanout": 0}, _EVERY, point=False),
}

_CORPORA = {
    "queries": (QUERIES, run),
    "order": (ORDER_QUERIES, run_ordered),
    "graph": (GRAPH_QUERIES, run),
    "counts": ([sparql for sparql in QUERIES + GRAPH_QUERIES if "COUNT" in sparql], run),
}


def _view(name: str, triples, quads):
    """A fresh view of the fixture store: ``graph-file`` and the like."""
    kind, residency = name.split("-")
    store = VortexRdflibStore(
        str(triples if kind == "graph" else quads), in_memory=residency == "mem"
    )
    return Graph(store=store) if kind == "graph" else Dataset(store=store, default_union=True)


def _force(forcing: Forcing, monkeypatch) -> None:
    for name, value in forcing.settings.items():
        monkeypatch.setattr(SETTINGS, name, value)
    if forcing.point is not None:
        monkeypatch.setattr(
            VortexRdflibStore, "_point_lookup", lambda store, n3, answer=forcing.point: answer
        )


_MATRIX = [
    pytest.param(name, corpus, view, sparql, id=f"{name}-{corpus}{index}-{view}")
    for name, forcing in FORCINGS.items()
    for corpus, views in forcing.runs
    for view in views
    for index, sparql in enumerate(_CORPORA[corpus][0])
]


@pytest.mark.parametrize(("forcing", "corpus", "view", "sparql"), _MATRIX)
def test_answers_equal_the_default_evaluator(
    dict_vortex, dict_vortex_quads, monkeypatch, forcing, corpus, view, sparql
):
    """Every query shape answers alike with the pushdown and with rdflib's
    default evaluator: on the default plan and on each path forced on
    (:data:`FORCINGS`), file-backed and in memory, an ORDER BY in its order.
    The store is built before the path is forced, as an application builds
    it before a query runs."""
    target = _view(view, dict_vortex, dict_vortex_quads)
    _force(FORCINGS[forcing], monkeypatch)
    _, runner = _CORPORA[corpus]
    with_pushdown, without_pushdown = both_ways(target, sparql, runner)
    assert with_pushdown == without_pushdown


#: The forced paths a trace shows taken, by id: the forcing, and the test
#: of a trace event that shows the path.
FORCED_PATHS = {
    "budget0": ("budget0", lambda event: event["event"] == "bgp_over_budget"),
    "stream": (
        "stream_pieces1",
        lambda event: event["event"] == "bgp_join_step_complete" and event["strategy"] == "stream",
    ),
    "tier3": (
        "budget0",
        lambda event: event["event"] == "bgp_plan_complete" and bool(event["rest_pattern_indices"]),
    ),
    "keep_scan": (
        "keep_scan",
        lambda event: (
            event["event"] == "bgp_join_step_complete" and event["strategy"] == "keep_scan"
        ),
    ),
}


@pytest.mark.parametrize("path", list(FORCED_PATHS))
def test_forcing_reaches_the_path(dict_vortex, dict_vortex_quads, monkeypatch, capsys, path):
    """A forced matrix passes for nothing unless the planner reads what its
    forcing sets: under each forcing, a query of its matrix traces the path,
    file-backed and in memory. The stores are built before the forcing is
    applied, as the matrix builds its own."""
    forcing, taken = FORCED_PATHS[path]
    matrices = {}
    for residency in ("file", "mem"):
        graph = _view(f"graph-{residency}", dict_vortex, dict_vortex_quads)
        dataset = _view(f"dataset-{residency}", dict_vortex, dict_vortex_quads)
        matrix = [(graph, sparql) for sparql in QUERIES + ORDER_QUERIES]
        matrices[residency] = matrix + [(dataset, sparql) for sparql in GRAPH_QUERIES]
    _force(FORCINGS[forcing], monkeypatch)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    register_sparql_pushdown()
    capsys.readouterr()

    def reaches(view, sparql) -> bool:
        run(view, sparql)
        return any(taken(event) for event in trace_events(capsys.readouterr().err))

    for residency, matrix in matrices.items():
        assert any(reaches(view, sparql) for view, sparql in matrix), (path, residency)


@pytest.mark.parametrize("sparql", QUERIES)
def test_store_variants_equal_default_evaluator(variant_graph, sparql):
    """The same shapes over a file-backed dictionary and over the secondary
    indexes."""
    with_pushdown, without_pushdown = both_ways(variant_graph, sparql)
    assert with_pushdown == without_pushdown


@pytest.mark.parametrize("sparql", GRAPH_QUERIES)
def test_graph_pattern_over_a_file_backed_dictionary(dict_vortex_quads, sparql):
    store = VortexRdflibStore(str(dict_vortex_quads))
    assert store._dict is not None and store._dict.file_backed
    dataset = Dataset(store=store, default_union=True)
    with_pushdown, without_pushdown = both_ways(dataset, sparql)
    assert with_pushdown == without_pushdown


def test_solutions_do_not_share_a_context_under_a_pushed_graph(dataset, monkeypatch):
    """rdflib's ``evalGraph`` writes ``solution.ctx.graph`` back to the graph
    it replaced as it yields. Here it evaluates the ``GRAPH`` and the
    ``OPTIONAL`` itself over rows this module produced, so one context shared
    across those rows would carry that write forward and send the OPTIONAL's
    right side to the union instead of the row's graph."""
    monkeypatch.setattr(SETTINGS, "enabled_ops", frozenset({"BGP"}))
    active = []
    original = pd.algebra._active_scope
    monkeypatch.setattr(
        pd.algebra,
        "_active_scope",
        lambda ctx, store: active.append(ctx.graph) or original(ctx, store),
    )
    register_sparql_pushdown()
    rows = run(
        dataset,
        """SELECT ?g ?s ?n ?a WHERE { GRAPH ?g {
            ?s <http://ex.org/name> ?n OPTIONAL { ?s <http://ex.org/age> ?a } } }""",
    )
    assert rows
    assert active, "the hook was never reached"
    # Every match belongs to the graph rdflib pushed, never to the dataset it
    # was pushed over.
    assert all(graph is not dataset for graph in active)


def test_graph_variable_is_bound_from_the_column(dataset):
    """`GRAPH ?g` is one match with the graph as a schema variable, not one
    match per graph: the store's five graphs cost a single `match_codes`."""
    matches = []
    inner = dataset.store._store()

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    dataset.store._native = _Counting()
    register_sparql_pushdown()
    try:
        rows = run(dataset, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    finally:
        dataset.store._native = inner
    # Four named graphs, the default graph excluded, from one wildcard match.
    assert len(rows) == 4
    assert len(matches) == 1
    assert matches[0][3] is None


def test_graph_scope_reaches_the_native_match(dataset, monkeypatch):
    """The graph is the pattern's fourth position, not a filter over the
    union: an absent graph matches nothing and never materializes a row."""
    patterns = []
    original = pd.matching._pattern_terms

    def spy(*args, **kwargs):
        pattern = original(*args, **kwargs)
        patterns.append(pattern.n3[3])
        return pattern

    monkeypatch.setattr(pd.matching, "_pattern_terms", spy)
    register_sparql_pushdown()
    assert run(dataset, "SELECT * WHERE { GRAPH <http://ex.org/g/1> { ?s ?p ?o } }")
    assert run(dataset, "SELECT * WHERE { GRAPH <http://ex.org/absent> { ?s ?p ?o } }") == []
    assert run(dataset, "SELECT * WHERE { ?s ?p ?o }")
    assert patterns == ["<http://ex.org/g/1>", "<http://ex.org/absent>", None]


#: Graph names vortex-rdf's parser rejects, each next to a graph the file
#: holds: a literal ``?g`` is bound to (rdflib's ``GRAPH`` asks for the
#: context ``URIRef("zz")``), and a relative IRI, bound or written (no BASE).
REJECTED_GRAPH_NAMES = {
    "literal": """SELECT ?s WHERE {
        VALUES ?g { "zz" <http://ex.org/g/1> } GRAPH ?g { ?s <http://ex.org/name> ?o } }""",
    "relative-iri": """SELECT ?s WHERE {
        VALUES ?g { <zz> <http://ex.org/g/1> } GRAPH ?g { ?s <http://ex.org/name> ?o } }""",
    "relative-iri-constant": """SELECT ?s WHERE {
        { GRAPH <zz> { ?s <http://ex.org/name> ?o } }
        UNION { GRAPH <http://ex.org/g/1> { ?s <http://ex.org/name> ?o } } }""",
}


#: Terms vortex-rdf's parser rejects in subject, predicate and object
#: position: a relative IRI (no BASE), written or bound through VALUES. Where
#: its VALUES row survives (an OPTIONAL with nothing to match), rdflib yields
#: the IRI.
REJECTED_TERMS = {
    "predicate": "SELECT ?s ?o WHERE { ?s <zz> ?o }",
    "subject": "SELECT ?p ?o WHERE { <zz> ?p ?o }",
    "object": "SELECT ?s ?p WHERE { ?s ?p <zz> }",
    "values": "SELECT ?s ?p ?o WHERE { VALUES ?o { <zz> <http://ex.org/carol> } ?s ?p ?o }",
    "values-optional": """SELECT ?o ?s WHERE {
        VALUES ?o { <zz> <http://ex.org/carol> } OPTIONAL { ?s <http://ex.org/likes> ?o } }""",
}


def _answer_in_mode(monkeypatch, mode, path, view, sparql):
    """``sparql`` over ``view`` of the file-backed store at ``path``, with the
    pushdown on, limited to BGPs (``VORTEX_RDF_PUSHDOWN_OPS=bgp``) or
    unregistered."""
    monkeypatch.setattr(SETTINGS, "enabled_ops", SETTINGS.enabled_ops)
    if mode == "bgp":
        monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "bgp")
    # Building the store registers the pushdown, so it is built first.
    store = VortexRdflibStore(str(path), in_memory=False)
    if mode == "unregistered":
        unregister_sparql_pushdown()
    try:
        return run(view(store), sparql)
    finally:
        register_sparql_pushdown()


@pytest.mark.parametrize("mode", ["pushdown", "bgp", "unregistered"])
@pytest.mark.parametrize(
    "sparql", list(REJECTED_GRAPH_NAMES.values()), ids=list(REJECTED_GRAPH_NAMES)
)
def test_a_graph_name_the_parser_rejects_matches_nothing(
    dict_vortex_quads, monkeypatch, sparql, mode
):
    """A graph name vortex-rdf's parser rejects names no graph of the file,
    so it matches nothing, as in rdflib's own store; the pushdown leaves its
    block to rdflib, and rdflib's evaluation (BGP-only pushdown, or none)
    asks the store for that context."""
    memory = Dataset(default_union=True)
    memory.parse(data=FIXTURE_NQ, format="nquads")
    expected = run(memory, sparql)
    assert expected
    union = _answer_in_mode(
        monkeypatch, mode, dict_vortex_quads, lambda s: Dataset(store=s, default_union=True), sparql
    )
    assert union == expected


@pytest.mark.parametrize("mode", ["pushdown", "bgp", "unregistered"])
@pytest.mark.parametrize("sparql", list(REJECTED_TERMS.values()), ids=list(REJECTED_TERMS))
def test_a_term_the_parser_rejects_matches_nothing(dict_vortex, monkeypatch, sparql, mode):
    """A term vortex-rdf's parser rejects is no term of the file, so a
    pattern holding it matches nothing, as in rdflib's own store: the
    pushdown's pattern is unsatisfiable, a VALUES constant gets a code that
    joins nothing and is still yielded, and ``triples()`` yields nothing."""
    memory = Graph()
    memory.parse(data=FIXTURE_NT, format="nt")
    expected = run(memory, sparql)
    answer = _answer_in_mode(monkeypatch, mode, dict_vortex, lambda s: Graph(store=s), sparql)
    assert answer == expected


def test_a_context_naming_no_graph_is_empty(dict_vortex_quads):
    """A literal, or an IRI the native parser rejects, names no graph of the
    file: a context it identifies has no triples and no length."""
    store = VortexRdflibStore(str(dict_vortex_quads), in_memory=False)
    name = URIRef("http://ex.org/name")
    ground = (URIRef("http://ex.org/alice"), name, Literal("Alice"))
    for context in (Literal("zz"), Graph(store=store, identifier=URIRef("zz"))):
        for pattern in ((None, None, None), (None, name, None), ground):
            assert list(store.triples(pattern, context)) == []
        assert store.__len__(context) == 0


def test_a_term_the_parser_rejects_has_no_triples_and_no_graphs(dict_vortex_quads):
    """An IRI the native parser rejects, in any position, is no term of the
    file: ``triples()`` yields nothing and ``contexts()`` names no graph."""
    store = VortexRdflibStore(str(dict_vortex_quads), in_memory=False)
    relative = URIRef("zz")
    for pattern in ((relative, None, None), (None, relative, None), (None, None, relative)):
        assert list(store.triples(pattern, None)) == []
        assert list(store.contexts(pattern)) == []


def test_probe_join_triggers_on_skewed_join(tmp_path, monkeypatch):
    """A 1-row anchor joined against a 300-row predicate must take the probe
    path under the real threshold, and still produce the right rows."""
    nt = tmp_path / "skewed.nt"
    nt.write_text(
        '<http://ex.org/s0> <http://ex.org/rare> "anchor" .\n'
        + "".join(
            f"<http://ex.org/s{i}> <http://ex.org/big> <http://ex.org/o{i}> .\n" for i in range(300)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "skewed.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")

    probes = []
    original = pd.probes._probe_join
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original(*a, **k)
    )
    graph = Graph(store=VortexRdflibStore(str(out)))
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?s ?o WHERE {
            ?s <http://ex.org/rare> "anchor" .
            ?s <http://ex.org/big> ?o }""",
    )
    assert rows == [(URIRef("http://ex.org/s0"), URIRef("http://ex.org/o0"))]
    assert probes, "the skewed join did not take the probe path"


def test_probe_join_pads_rows_when_the_pattern_shares_no_variable(graph):
    """A left-join probe keeps each relation row that finds no continuation,
    padded with None — also when the pattern shares no variable with the
    relation, so nothing is substituted per row. No caller probes such a
    pattern today, but `_probe_join` must not rely on that."""
    pattern = pd.matching.Pattern(
        [None, "<http://ex.org/nothing>", None, None], {Variable("x"): [0], Variable("a"): [2]}
    )
    schema, rows = pd.probes._probe_join(
        graph.store, (Variable("z"),), [(0,), (1,)], pattern, keep_unmatched=True
    )
    assert schema == (Variable("z"), Variable("x"), Variable("a"))
    assert rows == [(0, None, None), (1, None, None)]


@pytest.fixture(scope="module")
def probe_quads(tmp_path_factory):
    d = tmp_path_factory.mktemp("pushdown-probe-quads")
    nq = d / "probe.nq"
    nq.write_text(PROBE_QUADS, encoding="utf-8")
    out = d / "probe.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


def _recording(calls, name):
    """`pd.probes.<name>`, appending its name to `calls` as it is called."""
    original = getattr(pd.probes, name)

    def record(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    return record


def _probe_dataset(path, union: bool) -> Dataset:
    """The probe quads, file-backed, with the union default graph or the default
    graph alone."""
    store = VortexRdflibStore(str(path), in_memory=False)
    assert store._dict is not None and store._dict.file_backed
    return Dataset(store=store, default_union=union)


@pytest.mark.parametrize("fanout", [0, 100])
@pytest.mark.parametrize("case", PROBE_MULTIPLICITY)
def test_a_probe_join_keeps_the_multiplicity_rdflib_reads(probe_quads, monkeypatch, case, fanout):
    """A pattern with subject, predicate and object bound is one solution per
    quad it matches, probed per row (fanout 0) or hash-joined (fanout 100).
    Where the graph is open, in the union default graph, that is one per graph
    holding the triple; where it is fixed, at most one; an EXISTS test adds
    none."""
    union, sparql, rows, probed, _ = PROBE_MULTIPLICITY[case]
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    probes = []
    for name in ("_probe_join", "_probe_exists"):
        monkeypatch.setattr(pd.probes, name, _recording(probes, name))
    with_pushdown, without_pushdown = both_ways(_probe_dataset(probe_quads, union), sparql)
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == rows
    assert (probed in probes) == (fanout == 0)


@pytest.mark.parametrize("case", PROBE_MULTIPLICITY)
def test_an_existence_probe_asks_for_one_row_unless_the_graph_is_open(
    probe_quads, monkeypatch, case
):
    """A fully bound probe counts every quad only in the union default graph.
    A fixed graph holds at most one and an EXISTS test is boolean in any
    graph, so both ask for one row."""
    union, sparql, _, _, expected = PROBE_MULTIPLICITY[case]
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    dataset = _probe_dataset(probe_quads, union)
    native = counting_native(dataset)
    register_sparql_pushdown()
    run(dataset, sparql)
    asked = [limit for args, limit in native.count_calls if None not in args[:3]]
    assert asked and set(asked) == {expected}


def test_hash_joined_bgp_keeps_a_columnar_body(graph, monkeypatch):
    """A hash-joined BGP's relation is columns, not row tuples — tuples
    appear only as rows are consumed."""
    rels = []
    original = pd.bgp._join_incremental_bgp
    monkeypatch.setattr(
        pd.bgp, "_join_incremental_bgp", lambda *a: rels.append(original(*a)) or rels[-1]
    )
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?a ?b ?c WHERE {
            ?a <http://ex.org/knows> ?b .
            ?c <http://ex.org/knows> ?b }""",
    )
    assert rows
    assert rels and rels[-1].cols is not None


_XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


def _three_predicate_graph(
    tmp_path, q_subject: str, q_value, n: int = 50, r_per_subject: int = 2, graph_name=None
) -> Graph:
    """`n` subjects `s{i}` with an integer `p` of `i` and `r_per_subject`
    `r` values each (`r{j}` for every `j` congruent to `i` mod `n`), plus `n`
    integer `q` values `q_value(i)` on subjects `{q_subject}{i}`. `r` is
    counted above `p` and `q`, so the planner joins it last. With
    `graph_name` every statement is in that named graph, over a Dataset."""
    end = " .\n" if graph_name is None else f" <{graph_name}> .\n"
    lines = []
    for i in range(n):
        q = f'"{q_value(i)}"^^<{_XSD_INTEGER}>'
        lines.append(f'<http://ex.org/s{i}> <http://ex.org/p> "{i}"^^<{_XSD_INTEGER}>{end}')
        lines.append(f"<http://ex.org/{q_subject}{i}> <http://ex.org/q> {q}{end}")
    for j in range(r_per_subject * n):
        lines.append(f'<http://ex.org/s{j % n}> <http://ex.org/r> "r{j}"{end}')
    source = tmp_path / ("data.nt" if graph_name is None else "data.nq")
    source.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / "data.vortex"
    if graph_name is None:
        serialize_rdf(str(source), str(out), layout="dictionary")
        return Graph(store=VortexRdflibStore(str(out)))
    serialize_rdf(str(source), str(out), layout="dictionary", format="nquads")
    return Dataset(store=VortexRdflibStore(str(out)), default_union=True)


@pytest.mark.parametrize(
    ("extra_pattern", "expected_rows"),
    [("", 25), ("?s <http://ex.org/r> ?c .", 50)],
    ids=["ready-at-last-step", "ready-mid-join"],
)
def test_tuple_filtered_bgp_keeps_a_columnar_body(
    tmp_path, monkeypatch, extra_pattern, expected_rows
):
    """A FILTER over two variables of a hash-joined BGP leaves its body
    columnar when pruning cannot pay: at the last join step there is nothing
    left to prune for, and mid-join `r`'s 100 rows are too few for any
    pruned relation to be probed into (that takes fewer than 100 / 100).

    `?a < ?b` holds for the even subjects only, and each subject has two `r`
    values, so 25 rows over two patterns and 50 over three."""
    graph = _three_predicate_graph(tmp_path, "s", lambda i: i + 1000 if i % 2 == 0 else i - 1000)
    rels = []
    original = pd.bgp._join_incremental_bgp
    monkeypatch.setattr(
        pd.bgp, "_join_incremental_bgp", lambda *a: rels.append(original(*a)) or rels[-1]
    )
    register_sparql_pushdown()
    rows = run(
        graph,
        f"""SELECT ?s WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            {extra_pattern}
            FILTER(?a < ?b) }}""",
    )
    assert len(rows) == expected_rows
    assert {str(row[0]) for row in rows} == {f"http://ex.org/s{i}" for i in range(0, 50, 2)}
    assert rels and rels[-1].cols is not None, "the tuple FILTER materialized the BGP as rows"


@pytest.mark.parametrize(
    "extra_pattern", ["", "?s <http://ex.org/r> ?c ."], ids=["two-patterns", "three-patterns"]
)
def test_limit_streams_a_tuple_filter_over_a_hash_join(tmp_path, extra_pattern):
    """Under a LIMIT, a FILTER over two variables of a hash-joined BGP runs
    as the LIMIT pulls rows, not over the whole join first. All 1,000 `p`/`q`
    rows pass and 5 are asked for: streaming decodes the few terms those rows
    need, where evaluating the join first decodes its 2,000 distinct `?a` and
    `?b` codes. With `r` still to join, the FILTER is evaluable mid-join, but
    `r`'s 2,000 rows could only be probed from fewer than 20 survivors, so
    pruning stops as soon as 20 pass and leaves the rest to stream."""
    graph = _three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=1000)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        f"""SELECT ?s WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            {extra_pattern}
            FILTER(?a < ?b) }} LIMIT 5""",
    )
    assert len(rows) == 5
    assert counting.decoded < 1000, f"decoded {counting.decoded} codes for a LIMIT 5"


def test_filter_decodes_a_generic_only_conjunct_once(tmp_path):
    """A FILTER conjunct with no fast route is answered by rdflib's evaluator
    over the store's decoded terms, so the batch decode that readies the fast
    route's parsed terms must skip it: each of the 20 `?a` and 20 `?b` codes
    of this 400-row cross product is decoded once."""
    graph = _three_predicate_graph(tmp_path, "t", lambda i: i + 1000, n=20)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?a WHERE {
            ?s <http://ex.org/p> ?a .
            ?t <http://ex.org/q> ?b
            FILTER(CONCAT(STR(?a), STR(?b)) = "x") }""",
    )
    assert rows == []
    assert counting.decoded == 40


def test_filter_reuses_the_terms_its_early_pass_decoded(tmp_path):
    """Terms a mid-join FILTER attempt decoded before deferring are not
    decoded again by the FILTER above. Pruning stops once 20 of the 1,000
    `p`/`q` rows pass; the FILTER then reads all 2,000 joined rows, so each
    of the 1,000 `?a` and 1,000 `?b` codes is decoded once, plus the 1,000
    subjects the answer names."""
    graph = _three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=1000)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(
        graph,
        """SELECT ?s WHERE {
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            ?s <http://ex.org/r> ?c
            FILTER(?a < ?b) }""",
    )
    assert len(rows) == 2000
    assert counting.decoded == 3000


@contextlib.contextmanager
def _tracing():
    """A query trace around the native wrapper under test; the events it
    emits wait in the trace's pending lines (:func:`_pending_events`)."""
    trace = pd.tracing._QueryTrace(query_id=None)
    token = pd.tracing._TRACE.set(trace)
    try:
        yield trace
    finally:
        pd.tracing._TRACE.reset(token)


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
    return _three_predicate_graph(
        tmp_path, "s", lambda i: 1000 if i == 0 else i - 1000, r_per_subject=8
    )


def test_trace_records_every_native_call(tmp_path, monkeypatch, capsys):
    """Every native call a traced query makes is an event — a single call a
    `native_call_complete`, a batch a `native_batch_complete` carrying its
    probe count — the per-row probes included, so the events add up to the
    calls the store served: 3 counts (one batch), the `p` and `q` matches
    and 1 probe of `r`."""
    graph = _pruned_graph(tmp_path)
    register_sparql_pushdown()
    native = counting_native(graph)
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    run(graph, _PRUNED_SQL)
    trace = trace_events(capsys.readouterr().err)
    calls = [e for e in trace if e["event"] == "native_call_complete"]
    batches = [e for e in trace if e["event"] == "native_batch_complete"]
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
    monkeypatch.setattr(VortexRdflibStore, "_point_lookup", lambda store, n3: True)
    monkeypatch.setattr(SETTINGS, "cheap_batch_min", 1)
    native = _PullCountingNative(file_graph.store._store())
    file_graph.store._native = native
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
    native = _PullCountingNative(file_graph.store._store())
    with _tracing() as trace:
        answers = pd.tracing._TracedNative(native).match_codes_many(_FOUR_PROBES)
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
    store = file_graph.store._store()
    pattern = (None, "<http://ex.org/knows>", None, None)
    keep = {"o": (0, 1_000_000)}
    rows = store.count_quads(*pattern)
    with _tracing() as trace:
        chunks = pd.tracing._TracedNative(store).match_code_chunks(
            *pattern, keep=keep, columns=("s", "o"), batch_rows=4
        )
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
        assert pd.tracing._TracedNative(VortexRdfStore(str(out))).match_code_chunks() is None


def test_the_traced_native_forwards_the_native_keywords(file_graph):
    traced = pd.tracing._TracedNative(file_graph.store._store())
    token = CancelToken()
    with _tracing():
        counts = traced.count_quads_many(_FOUR_PROBES, lookahead=1, cancel=token)
        answers = traced.match_codes_many(_FOUR_PROBES, lookahead=1, cancel=token)
        assert counts == [len(answer[0]) for answer in answers]
        for many in (traced.count_quads_many, traced.match_codes_many):
            with pytest.raises(ValueError, match="lookahead"):
                many(_FOUR_PROBES, lookahead=0)
        token.cancel()
        for single in (traced.count_quads, traced.match_codes, traced.match_code_chunks):
            with pytest.raises(Cancelled):
                single(cancel=token)
        for many in (traced.count_quads_many, traced.match_codes_many):
            with pytest.raises(Cancelled):
                many(_FOUR_PROBES, cancel=token)


def test_trace_reports_a_mid_join_prune_between_the_steps_it_links(tmp_path, monkeypatch, capsys):
    """A mid-join prune is its own trace event, so row counts reconcile step
    to step: the `p`/`q` join's 50 rows are the prune's input, and its one
    surviving row is the input of the `r` probe that follows."""
    graph = _pruned_graph(tmp_path)
    register_sparql_pushdown()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    run(graph, _PRUNED_SQL)
    trace = trace_events(capsys.readouterr().err)
    steps = [e for e in trace if e["event"] == "bgp_join_step_complete"]
    prunes = [e for e in trace if e["event"] == "bgp_prune_complete"]
    assert len(prunes) == 1 and prunes[0]["pruned"] is True
    prune = prunes[0]
    assert (steps[0]["output_rows"], prune["input_rows"]) == (50, 50)
    assert (prune["output_rows"], steps[1]["input_rows"]) == (1, 1)


def test_trace_output_is_not_timed_as_native_work(tmp_path, monkeypatch):
    """Trace lines are written after the planning they describe, so however
    slowly stderr takes them, the native time the events report is the
    native calls' own: a 20 ms write must not show up in it."""
    graph = _three_predicate_graph(tmp_path, "s", lambda i: i + 1000, n=5)
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


@pytest.mark.parametrize("in_graph", [False, True], ids=["bgp", "graph-block"])
def test_tuple_filter_prunes_a_bgp_so_a_remaining_pattern_is_probed(
    tmp_path, monkeypatch, in_graph
):
    """When pruning lets a remaining pattern be probed instead of matched
    whole, the FILTER prunes mid-join, as in BSBM Explore Q5. `?a < ?b`
    holds for `s0` only, so one row survives the `p`/`q` join, and `r`'s 400
    rows are probed from it (1 × 100 < 400) rather than matched and
    hash-joined, as the unpruned 50 rows would be (50 × 100 ≥ 400). A GRAPH
    block is no barrier: the conjunct reaches every BGP the FILTER's
    single-variable conjuncts do."""
    graph = _three_predicate_graph(
        tmp_path,
        "s",
        lambda i: 1000 if i == 0 else i - 1000,
        r_per_subject=8,
        graph_name="http://ex.org/g" if in_graph else None,
    )
    probes = []
    original = pd.probes._probe_join
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original(*a, **k)
    )
    register_sparql_pushdown()
    patterns = "?s <http://ex.org/p> ?a . ?s <http://ex.org/q> ?b . ?s <http://ex.org/r> ?c"
    body = f"GRAPH <http://ex.org/g> {{ {patterns} }}" if in_graph else patterns
    rows = run(graph, f"SELECT ?c WHERE {{ {body} FILTER(?a < ?b) }}")
    assert sorted(str(row[0]) for row in rows) == sorted(f"r{j}" for j in range(0, 400, 50))
    assert probes, "the FILTER did not prune the relation down to a probe of r"


@pytest.mark.parametrize(
    ("q_subject", "q_value", "condition", "r_per_subject"),
    [("t", lambda i: i, "", 2), ("s", lambda i: i + 1000, "FILTER(?a > ?b)", 8)],
    ids=["hash-join-runs-empty", "tuple-filter-rejects-all"],
)
def test_bgp_that_runs_empty_before_its_last_pattern(
    tmp_path, q_subject, q_value, condition, r_per_subject
):
    """When the relation runs empty with a pattern still to join, the BGP
    still answers over every variable its patterns name: a DISTINCT over the
    variable only the unjoined `r` binds is empty, as rdflib says, rather
    than an IndexError from a schema with more variables than columns.

    In the first case `p` and `q` share no subject; in the second `?a > ?b`
    holds for none of them, and `r`'s 400 rows make pruning worth trying, so
    the FILTER empties the relation mid-join."""
    graph = _three_predicate_graph(tmp_path, q_subject, q_value, r_per_subject=r_per_subject)
    with_pushdown, without_pushdown = both_ways(
        graph,
        f"""SELECT DISTINCT ?c WHERE {{
            ?s <http://ex.org/p> ?a .
            ?s <http://ex.org/q> ?b .
            ?s <http://ex.org/r> ?c
            {condition} }}""",
    )
    assert with_pushdown == without_pushdown == []


def test_columnar_join_matches_row_join():
    """The columnar kernel produces the row join's exact rows, in its exact
    order, and declines the shapes the row join handles."""
    import random
    from array import array

    rnd = random.Random(7)
    ka = [rnd.randrange(40) for _ in range(300)]
    va = [rnd.randrange(1000) for _ in range(300)]
    kb = [rnd.randrange(40) for _ in range(200)]
    vb = [rnd.randrange(1000) for _ in range(200)]
    schema_a, schema_b = ("k", "x"), ("k", "y")
    expected = pd.relation._join(
        schema_a, list(zip(ka, va, strict=True)), schema_b, list(zip(kb, vb, strict=True))
    )[1]
    cols_a = (array("Q", ka), array("Q", va))
    cols_b = (array("Q", kb), array("Q", vb))
    joined = pd.relation._join_columns(schema_a, cols_a, schema_b, cols_b)
    assert joined is not None
    schema, cols, pairs = joined
    assert schema == ("k", "x", "y")
    assert pd.relation._cols_to_rows(cols) == expected
    assert pairs is None  # within the product bound: built uncounted
    # Several shared variables decline: the row join handles them.
    assert pd.relation._join_columns(("k", "x"), cols_a, ("k", "x"), cols_a) is None


@pytest.mark.parametrize("tail_width", [0, 1, 2], ids=["no-tail", "one-tail", "two-tails"])
def test_hash_join_kernels_join_like_a_nested_loop(tail_width):
    """Both kernels join exactly as a nested loop does, in its order: the
    row kernel buckets the build side — a key seen once holds its bare
    value, a key seen again a list, inlined per tail shape for speed — and
    the columnar one is the native `join_indices`, which pairs rows in
    nested-loop order. These keys hit every case of every copy: key 5
    twice, first at row index 0 with an empty tail (both falsy, which must
    not read as absent), 7 twice, 9 three times, 3 once, 11 only on the
    build side, and 5 probed twice."""
    from array import array

    keys_a = [5, 7, 9, 3, 5]
    keys_b = [5, 7, 7, 9, 9, 9, 3, 11, 5]
    rows_a = [(k, 100 + i) for i, k in enumerate(keys_a)]
    rows_b = [(k, 200 + j, 300 + j)[: 1 + tail_width] for j, k in enumerate(keys_b)]
    schema_a, schema_b = ("k", "x"), ("k", "y", "z")[: 1 + tail_width]
    expected = [ra + rb[1:] for ra in rows_a for rb in rows_b if ra[0] == rb[0]]

    assert pd.relation._join(schema_a, rows_a, schema_b, rows_b) == (
        schema_a + schema_b[1:],
        expected,
    )
    cols_a = tuple(array("Q", column) for column in zip(*rows_a, strict=True))
    cols_b = tuple(array("Q", column) for column in zip(*rows_b, strict=True))
    joined = pd.relation._join_columns(schema_a, cols_a, schema_b, cols_b)
    assert joined is not None
    assert pd.relation._cols_to_rows(joined[1]) == expected


def test_repeated_pattern_is_matched_once(graph):
    """Two triples that differ only in their variable names resolve to the
    same quad — variables are `None` in a resolved pattern — so a self-chain
    costs one native match, not one per hop."""
    matches = []
    inner = graph.store._store()

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._native = _Counting()
    register_sparql_pushdown()
    try:
        rows = run(
            graph,
            """SELECT ?a ?c WHERE {
                ?a <http://ex.org/knows> ?b .
                ?b <http://ex.org/knows> ?c }""",
        )
    finally:
        graph.store._native = inner
    assert rows, "the self-chain has solutions to find"
    assert len(matches) == 1, matches


def test_distinct_patterns_are_matched_separately(graph):
    """The memo is keyed by the resolved quad, so patterns that differ in a
    bound position still get a match each."""
    matches = []
    inner = graph.store._store()

    class _Counting:
        def match_codes(self, *args, **kwargs):
            matches.append(args)
            return inner.match_codes(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(inner, name)

    graph.store._native = _Counting()
    register_sparql_pushdown()
    try:
        run(
            graph,
            """SELECT ?s WHERE {
                ?s <http://ex.org/knows> ?o .
                ?s <http://ex.org/name> ?n }""",
        )
    finally:
        graph.store._native = inner
    assert len(matches) == 2, matches


def test_pushdown_is_actually_used(graph, monkeypatch):
    calls = []
    original = pd.bgp._solve_bgp
    monkeypatch.setattr(pd.bgp, "_solve_bgp", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert calls, "pushdown hook was not invoked"


def test_head_intercepts_the_whole_slice_project_chain(graph, monkeypatch):
    """A ``Slice(Project(BGP))`` is one interception at the Slice: rdflib
    never evaluates the Project or the BGP below it itself."""
    heads = []
    original = pd.head._eval_head
    monkeypatch.setattr(pd.head, "_eval_head", lambda *a: heads.append(a[2].name) or original(*a))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o } LIMIT 2")
    assert len(rows) == 2
    assert heads == ["Slice"]
    heads.clear()
    rows = run(graph, "SELECT ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert heads == ["Project"]


def test_solutions_are_built_without_context_push(graph, monkeypatch):
    """Solutions are constructed directly as FrozenBindings; the per-row
    ``ctx.push()`` scope rdflib's own evalBGP opens is never needed."""
    pushes = []
    original = QueryContext.push
    monkeypatch.setattr(QueryContext, "push", lambda self: pushes.append(1) or original(self))
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?s ?o WHERE { ?s <http://ex.org/name> ?o }")
    assert len(rows) == 8
    assert not pushes


def test_limit_decodes_only_the_first_chunk(tmp_path):
    """LIMIT stops decoding: a 700-row match under ``LIMIT 10`` decodes no
    more than the first chunk of rows."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(graph, "SELECT * WHERE { ?s ?p ?o } LIMIT 10")
    assert len(rows) == 10
    assert counting.decoded <= 3 * SETTINGS.chunk_start


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


def test_batch_stand_ins_accept_keywords(graph):
    """The counting stand-in passes the native batch keywords on, as the real
    store takes them."""
    native = counting_native(graph)
    token = CancelToken()
    probes = [
        (None, "<http://ex.org/name>", None, None),
        ("<http://ex.org/alice>", None, None, None),
    ]
    counts = native.count_quads_many(probes, lookahead=2, cancel=token)
    answers = native.match_codes_many(probes, lookahead=2, cancel=token)
    assert counts == [len(answer[0]) for answer in answers]
    assert (native.counts, native.matches, native.batches) == (2, 2, 2)


def test_ask_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = counting_native(graph)
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
    monkeypatch.setattr(SETTINGS, "enabled_ops", settings.ALL_OPS)
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "bgp")
    VortexRdflibStore(str(dict_vortex))
    assert SETTINGS.enabled_ops == {"BGP"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "BGP, Project")
    VortexRdflibStore(str(dict_vortex))
    assert SETTINGS.enabled_ops == {"BGP", "Project"}
    monkeypatch.setenv("VORTEX_RDF_PUSHDOWN_OPS", "Bogus")
    with pytest.raises(ValueError, match="Bogus"):
        VortexRdflibStore(str(dict_vortex))


def test_native_filters_env_switch(dict_vortex, monkeypatch):
    """`VORTEX_RDF_NATIVE_FILTERS=0` keeps every conjunct on the Python
    routes: no conjunct carries a native predicate, and the answer stands."""
    monkeypatch.setattr(SETTINGS, "native_filters", True)
    monkeypatch.setenv("VORTEX_RDF_NATIVE_FILTERS", "0")
    graph = Graph(store=VortexRdflibStore(str(dict_vortex)))
    assert SETTINGS.native_filters is False
    with_pushdown, without_pushdown = both_ways(
        graph, "SELECT ?s WHERE { ?s ?p ?o FILTER(isIRI(?o)) }"
    )
    assert with_pushdown == without_pushdown


def test_non_vortex_graphs_unaffected(dict_vortex):
    register_sparql_pushdown()
    g = Graph()
    g.add((URIRef("http://ex.org/a"), URIRef("http://ex.org/name"), Literal("A")))
    rows = run(g, "SELECT ?n WHERE { ?s <http://ex.org/name> ?n }")
    assert rows == [(Literal("A"),)]


def test_string_fallback_when_code_path_disabled(dict_vortex, monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
    graph = Graph(store=VortexRdflibStore(str(dict_vortex)))
    rows = run(
        graph,
        """SELECT ?n WHERE {
            <http://ex.org/alice> <http://ex.org/knows> ?x .
            ?x <http://ex.org/name> ?n }""",
    )
    assert {row[0] for row in rows} == {Literal("Bob", lang="en"), Literal("Carol")}


def test_filter_is_pushed_into_the_pattern_scan(graph, monkeypatch):
    """A single-variable conjunct restricts the pattern before any row is
    built, on the fast route: rdflib's evaluator is never consulted."""
    restrictions = []
    original = pd.matching._restrict_pattern
    monkeypatch.setattr(
        pd.matching, "_restrict_pattern", lambda *a: restrictions.append(1) or original(*a)
    )
    generic_calls = []
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(1) or original_generic(self, b),
    )
    register_sparql_pushdown()
    rows = run(graph, "SELECT ?x ?v WHERE { ?x <http://ex.org/score> ?v FILTER(?v > 1) }")
    assert {row[0] for row in rows} == {URIRef("http://ex.org/erin"), URIRef("http://ex.org/frank")}
    assert restrictions and not generic_calls


def test_values_outside_the_fast_domain_take_the_generic_route(graph, monkeypatch):
    """``str(?a)`` of a numeric literal is rdflib's normalized lexical form:
    the fast route defers those values, one call per distinct value."""
    generic_calls = []
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(b) or original_generic(self, b),
    )
    register_sparql_pushdown()
    rows = run(graph, 'SELECT ?x WHERE { ?x <http://ex.org/age> ?a FILTER(str(?a) = "42") }')
    assert {row[0] for row in rows} == {URIRef("http://ex.org/bob"), URIRef("http://ex.org/dave")}
    assert len(generic_calls) == 5  # one per distinct age code, never per row


def test_init_bindings_are_visible_to_filters(graph):
    sparql = "SELECT ?a WHERE { ?x <http://ex.org/age> ?a FILTER(?x = ?who && ?a > 0) }"
    init = {"who": URIRef("http://ex.org/bob")}
    register_sparql_pushdown()
    with_pushdown = sorted((tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key)
    try:
        unregister_sparql_pushdown()
        without_pushdown = sorted(
            (tuple(r) for r in graph.query(sparql, initBindings=init)), key=row_key
        )
    finally:
        register_sparql_pushdown()
    assert with_pushdown == without_pushdown == [(Literal(42),)]


#: Per path of an IRI keep, a query whose answer changes if the path breaks:
#: ``(query, IRIs bound through initBindings, rows rdflib answers)``.
IRI_KEEP_PATHS = {
    "column-tested-on-probed-rows": (
        """SELECT * WHERE {
            <http://ex.org/alice> <http://ex.org/knows> ?v . ?v <http://ex.org/knows> ?w
            FILTER(?w IN (<http://ex.org/bob>, <http://ex.org/carol>)) }""",
        None,
        1,
    ),
    "columns-matched-apart": (
        """SELECT * WHERE { ?a ?p ?x . ?b ?q ?y
            FILTER(?x IN (<http://ex.org/bob>)) FILTER(?y IN (<http://ex.org/carol>)) }""",
        None,
        8,
    ),
    "columns-intersect": (
        """SELECT ?s ?o WHERE { ?s ?p ?o
            FILTER(?o IN (<http://ex.org/bob>, <http://ex.org/carol>)
                && ?o IN (<http://ex.org/carol>, <http://ex.org/dave>)) }""",
        None,
        4,
    ),
    "columns-intersect-to-nothing": (
        """SELECT ?s ?o WHERE { ?s ?p ?o
            FILTER(?o IN (<http://ex.org/bob>) && ?o IN (<http://ex.org/carol>)) }""",
        None,
        0,
    ),
    "iri-that-is-no-iriref": (
        "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(?o = ?y) }",
        {"y": "http://ex.org/bob>"},
        0,
    ),
}


@pytest.mark.parametrize("fanout", [100, 0])
@pytest.mark.parametrize(
    ("sparql", "init", "expected"), list(IRI_KEEP_PATHS.values()), ids=list(IRI_KEEP_PATHS)
)
def test_iri_keep_paths_agree_with_rdflib(file_graph, monkeypatch, fanout, sparql, init, expected):
    """The paths an IRI keep takes, each with a query whose answer changes if
    the path breaks, hash-joined (``SETTINGS.probe_fanout`` 100) and probed (0):

    - a probed pattern's free variable tests the keep's codes on the rows each
      probe returns (``keep_member``);
    - two patterns narrowed by different code sets at one position are two
      matches, not one (``_pattern_key``);
    - code sets on one variable intersect (``intersect_keeps``);
    - an IRI that is no N-Triples IRIREF makes no keep (``_SPELLABLE_IRI``):
      a bound variable's here, which ``encode_many`` would read as another
      IRI."""
    monkeypatch.setattr(SETTINGS, "probe_fanout", fanout)
    bindings = None if init is None else {name: URIRef(iri) for name, iri in init.items()}

    def answer(graph, sparql):
        rows = graph.query(sparql, initBindings=bindings)
        return sorted((tuple(row) for row in rows), key=row_key)

    with_pushdown, without_pushdown = both_ways(file_graph, sparql, answer)
    assert with_pushdown == without_pushdown
    assert len(without_pushdown) == expected


def test_distinct_and_count_are_answered_in_code_space(graph, monkeypatch):
    distincts, aggregates = [], []
    original_distinct = pd.head._eval_distinct
    original_aggregate = pd.aggregate._eval_aggregate
    monkeypatch.setattr(
        pd.head, "_eval_distinct", lambda *a: distincts.append(1) or original_distinct(*a)
    )
    monkeypatch.setattr(
        pd.aggregate, "_eval_aggregate", lambda *a: aggregates.append(1) or original_aggregate(*a)
    )
    register_sparql_pushdown()
    rows = run(graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }")
    assert len(rows) == 4  # "042" and "42" are the same integer
    rows = run(graph, "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p")
    assert (URIRef("http://ex.org/name"), Literal(8)) in rows
    assert distincts and aggregates


_ALIAS_QUERIES = [
    "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE { ?x <http://ex.org/age> ?a }",
    "SELECT ?a (COUNT(*) AS ?n) WHERE { ?x <http://ex.org/age> ?a } GROUP BY ?a",
    """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT DISTINCT ?a WHERE {
        { VALUES ?a { "0042"^^xsd:integer "43"^^xsd:integer } }
        UNION { ?x <http://ex.org/age> ?a } }""",
    "SELECT DISTINCT ?o WHERE { ?s ?p ?o }",
]


@pytest.mark.parametrize("order", [1, -1], ids=["forward", "reverse"])
def test_term_aliases_are_classified_within_each_query(graph, order):
    """Which codes alias ("042" and "42" are one integer, and so is a query's
    "0042" the dictionary lacks) is classified per query: whatever query runs
    first, each answers as rdflib does, and a query run again decodes exactly
    what it did the first time — nothing carried over from earlier queries."""
    for sparql in _ALIAS_QUERIES[::order]:
        with_pushdown, without_pushdown = both_ways(graph, sparql)
        assert with_pushdown == without_pushdown, sparql
    counting = counting_term_dict(graph)
    decoded = []
    for _ in range(2):
        before = counting.decoded
        run(graph, _ALIAS_QUERIES[0])
        decoded.append(counting.decoded - before)
    assert decoded[0] == decoded[1] > 0


def test_count_over_one_pattern_counts_instead_of_matching(graph):
    register_sparql_pushdown()
    native = counting_native(graph)
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/name> ?o }")
    assert rows == [(Literal(8),)]
    rows = run(graph, "SELECT (COUNT(*) AS ?n) WHERE { ?s <http://ex.org/nothing> ?o }")
    assert rows == [(Literal(0),)]
    assert (native.counts, native.matches) == (2, 0)


def test_distinct_decodes_only_the_survivors(tmp_path):
    """``SELECT DISTINCT ?p`` over 700 rows with 7 predicates decodes 7 codes."""
    nt = tmp_path / "wide.nt"
    nt.write_text(
        "".join(
            f"<http://ex.org/s{i}> <http://ex.org/p{i % 7}> <http://ex.org/o{i}> .\n"
            for i in range(700)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "wide.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    store = VortexRdflibStore(str(out), in_memory=True)
    graph = Graph(store=store)
    register_sparql_pushdown()
    counting = counting_term_dict(graph)
    rows = run(graph, "SELECT DISTINCT ?p WHERE { ?s ?p ?o }")
    assert len(rows) == 7
    assert counting.decoded == 7


def test_left_join_probe_and_hash_paths_are_ours(graph, monkeypatch):
    """An OPTIONAL is solved inside the hook — the inner pattern probed per
    outer row under a low threshold, hash-joined under a high one — never by
    rdflib re-entering the BGP hook once per outer solution."""
    left_joins, probes, bgps = [], [], []
    original_left = pd.blocks._solve_left_join
    original_probe = pd.probes._probe_join
    original_bgp = pd.bgp._solve_bgp
    monkeypatch.setattr(
        pd.blocks, "_solve_left_join", lambda *a: left_joins.append(1) or original_left(*a)
    )
    monkeypatch.setattr(
        pd.probes, "_probe_join", lambda *a, **k: probes.append(1) or original_probe(*a, **k)
    )
    monkeypatch.setattr(pd.bgp, "_solve_bgp", lambda *a: bgps.append(1) or original_bgp(*a))
    register_sparql_pushdown()
    sparql = (
        "SELECT ?x ?a WHERE { ?x <http://ex.org/name> ?n OPTIONAL { ?x <http://ex.org/age> ?a } }"
    )
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    assert len(run(graph, sparql)) == 8
    assert left_joins and probes and len(bgps) == 1
    left_joins.clear(), probes.clear(), bgps.clear()
    monkeypatch.setattr(SETTINGS, "probe_fanout", 10**9)
    assert len(run(graph, sparql)) == 8
    assert left_joins and not probes and len(bgps) == 1


def test_minus_is_an_anti_join_in_code_space(graph, monkeypatch):
    calls = []
    original = pd.blocks._solve_minus
    monkeypatch.setattr(pd.blocks, "_solve_minus", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(
        graph, "SELECT ?x WHERE { ?x <http://ex.org/name> ?n MINUS { ?x <http://ex.org/age> ?a } }"
    )
    subjects = {row[0] for row in rows}
    assert len(rows) == 3  # alice, carol and the blank node have a name but no age
    assert {URIRef("http://ex.org/alice"), URIRef("http://ex.org/carol")} <= subjects
    assert calls


def test_cheated_scope_falls_back_to_rdflib(graph, monkeypatch):
    """An OPTIONAL whose inner pattern uses a variable bound outside its own
    left side is re-checked by rdflib with that binding dropped; that shape
    is handed back, and still answered correctly."""
    left_joins = []
    original = pd.blocks._solve_left_join

    def spy(*args):
        try:
            return original(*args)
        except NotImplementedError:
            left_joins.append("fallback")
            raise

    monkeypatch.setattr(pd.blocks, "_solve_left_join", spy)
    sparql = """SELECT ?x ?a ?y WHERE {
        { VALUES ?a { 42 } }
        { ?x <http://ex.org/name> ?n OPTIONAL { ?y <http://ex.org/age> ?a } } }"""
    with_pushdown, without_pushdown = both_ways(graph, sparql)
    assert with_pushdown == without_pushdown
    assert "fallback" in left_joins


def test_exists_is_a_semi_join_in_code_space(graph, monkeypatch):
    """A (NOT) EXISTS conjunct is applied as a set membership on the shared
    variables — probed per row for a small block, hash-joined otherwise —
    without rdflib's evaluator running per row."""
    applied, probes, generic_calls = [], [], []
    original_apply = pd.blocks._apply_exists
    original_probe = pd.probes._probe_exists
    original_generic = filters.Conjunct.generic
    monkeypatch.setattr(
        pd.blocks, "_apply_exists", lambda *a: applied.append(1) or original_apply(*a)
    )
    monkeypatch.setattr(
        pd.probes, "_probe_exists", lambda *a: probes.append(1) or original_probe(*a)
    )
    monkeypatch.setattr(
        filters.Conjunct,
        "generic",
        lambda self, b: generic_calls.append(1) or original_generic(self, b),
    )
    register_sparql_pushdown()
    sparql = """SELECT ?x WHERE { ?x <http://ex.org/name> ?n
        FILTER NOT EXISTS { ?x <http://ex.org/age> ?a } }"""
    monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    assert len(run(graph, sparql)) == 3 and applied and probes and not generic_calls
    applied.clear(), probes.clear()
    monkeypatch.setattr(SETTINGS, "probe_fanout", 10**9)
    assert len(run(graph, sparql)) == 3 and applied and not probes and not generic_calls
    # a body FILTER that sees the block's bindings: rdflib's evaluator, per distinct tuple
    rows = run(
        graph,
        """SELECT ?x WHERE { ?x <http://ex.org/age> ?a
            FILTER EXISTS { ?x <http://ex.org/score> ?s FILTER(?s > ?a) } }""",
    )
    assert rows == [(URIRef("http://ex.org/frank"),)] and generic_calls


def test_order_by_is_a_rank_sort_in_code_space(graph, monkeypatch):
    ordered = []
    original = pd.head._order_rows
    monkeypatch.setattr(pd.head, "_order_rows", lambda *a: ordered.append(a[4]) or original(*a))
    register_sparql_pushdown()
    rows = run_ordered(
        graph, "SELECT ?x ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a ?x LIMIT 2"
    )
    assert [row[1].toPython() for row in rows] == [-3, 7]  # the 7 is an xsd:byte
    assert ordered == [2]  # the LIMIT bounds the sort to the top 2
    ordered.clear()
    rows = run_ordered(
        graph, "SELECT DISTINCT ?a WHERE { ?x <http://ex.org/age> ?a } ORDER BY ?a LIMIT 2"
    )
    assert [row[0].toPython() for row in rows] == [-3, 7]
    assert ordered == [None]  # DISTINCT narrows after the sort: no top-k


def test_values_is_a_code_space_relation(graph, monkeypatch):
    calls = []
    original = pd.blocks._solve_values
    monkeypatch.setattr(pd.blocks, "_solve_values", lambda *a: calls.append(1) or original(*a))
    register_sparql_pushdown()
    rows = run(
        graph,
        """SELECT ?x ?n WHERE { VALUES ?x { <http://ex.org/bob> <http://ex.org/nobody> }
            ?x <http://ex.org/name> ?n }""",
    )
    assert rows == [(URIRef("http://ex.org/bob"), Literal("Bob", lang="en"))]
    # a constant outside the dictionary is still yielded verbatim
    rows = run(graph, "SELECT ?x WHERE { VALUES ?x { <http://ex.org/nobody> } }")
    assert rows == [(URIRef("http://ex.org/nobody"),)]
    assert calls
