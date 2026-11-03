"""The FILTER scope tests' datasets: the quads (:mod:`tests.filter_corpus`)
under either secondary index layout, file-backed or in memory, with the
union of the graphs or the default graph alone as the query's default
graph; and the planner's routes."""

import pytest
from rdflib import Dataset
from tests.filter_corpus import QUADS
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore
from vortex_rdflib.settings import SETTINGS


@pytest.fixture(scope="module")
def quad_files(tmp_path_factory):
    """The quads, once per secondary index layout."""
    directory = tmp_path_factory.mktemp("filter-scope")
    nq = directory / "quads.nq"
    nq.write_text(QUADS, encoding="utf-8")
    files = {}
    for layout in ("copy", "reference"):
        out = directory / f"quads-{layout}.vortex"
        index = f"secondary-by-{layout}"
        serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads", indexes=[index])
        files[layout] = out
    return files


@pytest.fixture
def union():
    """Whether the query's default graph is the union of the graphs; a test
    parametrizes it to query the default graph alone too."""
    return True


def _open(quad_files, layout, in_memory, union):
    store = VortexRdflibStore(str(quad_files[layout]), in_memory=in_memory)
    return Dataset(store=store, default_union=union)


@pytest.fixture
def dataset(quad_files, union):
    """The quads file-backed, with the copy layout's index: each shape runs
    once, on the default route, and one shape per family runs in every
    storage configuration and route (``SMOKE``)."""
    return _open(quad_files, "copy", False, union)


@pytest.fixture(
    params=[("copy", True), ("copy", False), ("reference", True), ("reference", False)],
    ids=["copy-mem", "copy-file", "reference-mem", "reference-file"],
)
def any_dataset(quad_files, request, union):
    """The quads in every storage configuration: either index layout, in
    memory or file-backed."""
    layout, in_memory = request.param
    return _open(quad_files, layout, in_memory, union)


@pytest.fixture(params=["default", "probe", "generic"])
def route(request, monkeypatch):
    """The default planning; every one-pattern side probed per row; every
    FILTER value through rdflib's evaluator."""
    if request.param == "probe":
        monkeypatch.setattr(SETTINGS, "probe_fanout", 0)
    elif request.param == "generic":
        monkeypatch.setattr(SETTINGS, "fast_filters", False)
    return request.param
