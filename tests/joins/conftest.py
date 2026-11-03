"""The join tests' files and graphs (:mod:`tests.join_corpus`)."""

import pytest
from rdflib import Graph
from tests.corpus import PROBE_QUADS
from tests.join_corpus import EX, KEEP_SCAN_QUADS, _statements, _tier_statements
from vortex_rdf import serialize_rdf

from vortex_rdflib import (
    VortexRdflibStore,
)


@pytest.fixture(scope="module")
def joins_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("join-budget")
    nt = d / "joins.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "joins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(scope="module")
def tier_quads_vortex(tmp_path_factory):
    """``_TIER_BODY``'s data in two named graphs: all of it in `g1`; in `g2`,
    that of the first six seeds, two of their objects linked."""
    d = tmp_path_factory.mktemp("join-budget-quads")
    nq = d / "tiers.nq"
    quads = [
        line[: -len(" .")] + f" <{EX}{name}> ."
        for name, seeds, linked in (("g1", 120, 5), ("g2", 6, 2))
        for line in _tier_statements(seeds, linked)
    ]
    nq.write_text("\n".join(quads) + "\n", encoding="utf-8")
    out = d / "tiers.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


@pytest.fixture(scope="module")
def indexed_joins_vortex(tmp_path_factory):
    """This module's statements with a secondary index, which answers a probe
    binding a predicate and an object as a point lookup."""
    d = tmp_path_factory.mktemp("join-budget-indexed")
    nt = d / "joins.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "joins.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary", indexes=["secondary-by-copy"])
    return out


@pytest.fixture(scope="module")
def multiplicity_vortex(tmp_path_factory):
    """`PROBE_QUADS` and `KEEP_SCAN_QUADS`."""
    d = tmp_path_factory.mktemp("join-budget-multiplicity")
    nq = d / "probe.nq"
    nq.write_text(PROBE_QUADS + KEEP_SCAN_QUADS, encoding="utf-8")
    out = d / "probe.vortex"
    serialize_rdf(str(nq), str(out), layout="dictionary", format="nquads")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(joins_vortex, request):
    return Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=request.param))


@pytest.fixture
def file_graph(joins_vortex):
    """This module's store, file-backed only: where point lookups batch."""
    return Graph(store=VortexRdflibStore(str(joins_vortex), in_memory=False))
