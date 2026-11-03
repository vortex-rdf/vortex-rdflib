"""The pushdown must be observationally identical to rdflib's default
evaluator — every query shape is run both ways and compared exactly, on a
file-backed and on an in-memory store — except where rdflib breaks SPARQL 1.1
on a FILTER, where the pushdown gives the specification's answer
(``tests/corpus.py``, ``RDFLIB_DIVERGES``)."""

from dataclasses import dataclass
from typing import Any

import pytest
from rdflib import Dataset, Graph
from tests.corpus import (
    FIXTURE_NT,
    GRAPH_QUERIES,
    ORDER_QUERIES,
    QUERIES,
    RDFLIB_DIVERGES,
    spec_corrected,
)
from tests.support import (
    both_ways,
    file_codes,
    run,
    run_ordered,
    trace_events,
)
from vortex_rdf import serialize_rdf

from vortex_rdflib import (
    VortexRdflibStore,
    register_sparql_pushdown,
)
from vortex_rdflib.open_file import OpenFile
from vortex_rdflib.settings import SETTINGS


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
        assert store._opened().codes is not None and file_codes(store).term_dict.file_backed
    return Graph(store=store)


@dataclass(frozen=True)
class Forcing:
    """A path of the planner forced on: the settings that force it, the
    answer it gives every point-lookup question (``None``: the store's own,
    ``False``: every probe would scan, ``True``: every probe is a point
    lookup), and the runs of its equivalence matrix — each a corpus
    (:data:`_CORPORA`) over the views it names (``graph-file`` and
    ``graph-mem`` over the triples, ``dataset-*`` over the quads, as a
    union-default ``Dataset``). Where the path leaves every FILTER to
    rdflib's evaluator (``rdflib_filters``), rdflib's semantics apply, so its
    answer is the expected one even where it breaks SPARQL 1.1."""

    settings: dict
    runs: tuple
    point: bool | None = None
    rdflib_filters: bool = False


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
    "bgp_only": Forcing({"enabled_ops": frozenset({"BGP"})}, _FILE_AND_GRAPHS, rdflib_filters=True),
    # The native term predicates off: every conjunct they would decide runs
    # on the Python fast route.
    "python_filters": Forcing({"native_filters": False}, _FILE),
    # VORTEX_RDF_FILTER_FAST=0: every FILTER value through rdflib's own
    # evaluator, once per distinct value.
    "generic_filters": Forcing({"fast_filters": False}, _FILE_AND_GRAPHS, rdflib_filters=True),
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
            OpenFile, "point_lookup", lambda opened, n3, answer=forcing.point: answer
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
    if FORCINGS[forcing].rdflib_filters:
        assert with_pushdown == without_pushdown
    else:
        assert with_pushdown == spec_corrected(sparql, without_pushdown)


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
    assert with_pushdown == spec_corrected(sparql, without_pushdown)


@pytest.mark.parametrize("sparql", GRAPH_QUERIES)
def test_graph_pattern_over_a_file_backed_dictionary(dict_vortex_quads, sparql):
    store = VortexRdflibStore(str(dict_vortex_quads))
    assert store._opened().codes is not None and file_codes(store).term_dict.file_backed
    dataset = Dataset(store=store, default_union=True)
    with_pushdown, without_pushdown = both_ways(dataset, sparql)
    assert with_pushdown == spec_corrected(sparql, without_pushdown)


def test_every_divergence_is_a_corpus_query():
    """The corrections apply to the queries they were written for."""
    corpus = {" ".join(sparql.split()) for sparql in QUERIES + ORDER_QUERIES + GRAPH_QUERIES}
    assert set(RDFLIB_DIVERGES) <= corpus
