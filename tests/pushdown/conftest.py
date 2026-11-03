"""The pushdown tests' stores: the corpus's triples (``dict_vortex``) and
quads (``dict_vortex_quads``) as dictionary files, served file-backed and
in memory (``graph``, ``dataset``) or file-backed only (``file_graph``,
``file_dataset``)."""

import pytest
from rdflib import Dataset, Graph
from tests.corpus import (
    FIXTURE_NQ,
    FIXTURE_NT,
)
from vortex_rdf import serialize_rdf

from vortex_rdflib import (
    VortexRdflibStore,
)


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
