"""CodSpeed benchmark for the rdflib integration — vortex variants only.

The instrumented counterpart of ``run_bench.py``. Both drive the *same*
dataset generator (``bench.dataset``) and the *same* twelve SPARQL queries
(``bench.queries``) that the dashboard reports, so a task here and a column
there are asking the store the same question. What differs is the axis of
comparison and how the answer is measured:

* ``run_bench.py`` is **comparative and wall-clock**: it puts VortexStore next
  to rdflib's ``Memory``, oxrdflib, pycottas and rdflib-hdt, one worker
  process per store so peak RSS is attributable, at 250k triples. It feeds the
  Pages dashboard and is NEVER uploaded to CodSpeed.
* THIS file is **self-referential and instrumented**: it runs under
  ``pytest --codspeed`` in simulation mode, where every task gets a
  deterministic instruction count that is comparable across commits. That is
  what makes it a regression gate rather than a leaderboard.

Only the vortex variants are here, deliberately. CodSpeed tracks *this*
package's code over time; another library's instruction count moves when
that library changes, which is a fact about their release cadence and not a
signal this repo can act on. The competitive question is the dashboard's job.

The variants are the dashboard's own vortex matrix — Dictionary layout
crossed over the two axes that change how a store answers:

    residency: in-memory | file-backed
    secondary index: none | by-copy | by-reference

Six store configurations against twelve queries, plus the build and open
paths. ``heavy`` queries need no special handling here: instrumentation
measures a single invocation, so the iteration budget ``worker.py`` applies
to protect wall-clock runs has no analogue.

Run locally (walltime mode, no Valgrind needed):
    uv run pytest bench/test_codspeed.py --codspeed
    CODSPEED_BENCH_TRIPLES=5000 uv run pytest bench/test_codspeed.py --codspeed

Without ``--codspeed`` pytest just executes them as ordinary tests, which
keeps the fixtures and query set honest even when nothing is being measured.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from rdflib import Graph

from bench.dataset import DatasetConfig, config_from_env, moduli, write_ntriples
from bench.queries import Query, build_queries

# ─── Dataset shape ──────────────────────────────────────────────────────────
# Small by default: instrumentation runs under Valgrind, so the dashboard's
# 250k triples would multiply into a job measured in hours while catching the
# same regressions. 32,768 is the size the vortex-rdf Rust and JS CodSpeed
# suites share (32**3, their BENCH_SIZE default), so a regression in the
# shared core lands in every tab at comparable magnitude.
#
# Only the row count is tuned; the term-cardinality ratios stay at the
# dashboard's values, since those are what make the generator exercise
# dictionary construction and per-distinct-term decode at all.
TRIPLES = int(os.environ.get("CODSPEED_BENCH_TRIPLES", 32_768))

_DEFAULTS = config_from_env()
CFG = DatasetConfig(
    n=TRIPLES,
    subject_ratio=_DEFAULTS.subject_ratio,
    predicates=_DEFAULTS.predicates,
    object_ratio=_DEFAULTS.object_ratio,
    literal_frac=_DEFAULTS.literal_frac,
)
MODULI = moduli(CFG)

# Built at import, not in a fixture: parametrize needs the query set at
# collection time. Cheap — the constants come from the same residue
# arithmetic that generates the data, so nothing is queried to derive them.
QUERIES: list[Query] = build_queries(CFG, MODULI)

# ─── Store variants (the dashboard's vortex matrix) ─────────────────────────
#: Secondary-index configurations. One `.vortex` artifact is built per entry
#: and shared by both residencies, exactly as the dashboard's adapters do.
INDEXES: dict[str, list[str]] = {
    "noidx": [],
    "bycopy": ["secondary-by-copy"],
    "byref": ["secondary-by-reference"],
}

#: Residency: how the artifact is opened. `in_memory=True` loads the store up
#: front so queries skip the per-call file-read pipeline.
RESIDENCY: dict[str, bool] = {"mem": True, "file": False}

#: The six store configurations, as `(slug, index_tag, in_memory)`.
VARIANTS: list[tuple[str, str, bool]] = [
    (f"{res}_{idx}", idx, in_memory) for idx in INDEXES for res, in_memory in RESIDENCY.items()
]


def _build(source: Path, out: Path, index_tag: str) -> str:
    from vortex_rdf import serialize_rdf

    serialize_rdf(str(source), str(out), layout="dictionary", indexes=INDEXES[index_tag])
    return str(out)


def _open(path: str, in_memory: bool) -> Graph:
    from vortex_rdflib import VortexStore

    return Graph(store=VortexStore(path, in_memory=in_memory))


def _consume(graph: Graph, query: Query) -> int:
    """Execute and fully consume one query.

    Mirrors ``worker.run_once``: the result is always iterated to exhaustion,
    so lazy result setup cannot masquerade as query speed.
    """
    result = graph.query(query.sparql)
    if query.is_ask:
        return 1 if result.askAnswer else 0
    return sum(1 for _ in result)


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def source_nt(tmp_path_factory) -> Path:
    """The N-Triples dataset, generated once per session."""
    path = tmp_path_factory.mktemp("codspeed-data") / "dataset.nt"
    write_ntriples(str(path), CFG)
    return path


@pytest.fixture(scope="session")
def store_paths(tmp_path_factory, source_nt) -> dict[str, str]:
    """One `.vortex` artifact per index configuration, built once per session.

    Separate from `graphs` because the build and open tasks need the path,
    not an already-opened store.
    """
    root = tmp_path_factory.mktemp("codspeed-stores")
    return {tag: _build(source_nt, root / f"data-{tag}.vortex", tag) for tag in INDEXES}


@pytest.fixture(scope="session")
def graphs(store_paths) -> dict[str, Graph]:
    """Graphs opened once per session for the query tasks.

    Opening is timed separately (`test_open`), so it must not be paid inside a
    query measurement.
    """
    return {slug: _open(store_paths[idx], in_memory) for slug, idx, in_memory in VARIANTS}


# ─── build::<index> ─────────────────────────────────────────────────────────


@pytest.mark.benchmark
@pytest.mark.parametrize("index_tag", list(INDEXES))
def test_build(benchmark, tmp_path_factory, source_nt, index_tag):
    """Parse the RDF file and write the `.vortex` store, per index config.

    The dashboard reports this as its `load` row; here it isolates what each
    secondary index costs to construct.
    """
    out = tmp_path_factory.mktemp(f"build-{index_tag}") / "out.vortex"
    benchmark(lambda: _build(source_nt, out, index_tag))


# ─── open::<variant> ────────────────────────────────────────────────────────


@pytest.mark.benchmark
@pytest.mark.parametrize(("slug", "index_tag", "in_memory"), VARIANTS, ids=[v[0] for v in VARIANTS])
def test_open(benchmark, store_paths, slug, index_tag, in_memory):
    """Opening a built artifact into a queryable graph, with no query behind
    it — so the query tasks can be read without silently containing this."""
    path = store_paths[index_tag]
    benchmark(lambda: _open(path, in_memory))


# ─── query::<variant>::<query> ──────────────────────────────────────────────


@pytest.mark.benchmark
@pytest.mark.parametrize("query", QUERIES, ids=[q.name for q in QUERIES])
@pytest.mark.parametrize("slug", [v[0] for v in VARIANTS])
def test_query(benchmark, graphs, slug, query):
    """One dashboard query against one store configuration, on an open store.

    This is the steady state of a long-lived process: the store is warm, so
    what is measured is the query, not the open. BGP pushdown is active
    wherever the code path applies — that is the shipped default, and the
    behaviour the dashboard's vortex rows report.
    """
    graph = graphs[slug]
    benchmark(lambda: _consume(graph, query))
