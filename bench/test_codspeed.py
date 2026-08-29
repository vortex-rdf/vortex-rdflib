"""CodSpeed benchmark suite — vortex variants only.

The instrumented counterpart of ``run_bench.py``. Both drive the *same*
dataset generator (``bench.dataset``) and the *same* SPARQL query set
(``bench.queries``) that the dashboard reports, so a task here and a column
there ask the store the same question. What differs is the axis of comparison
and how the answer is measured:

* ``run_bench.py`` is **comparative and wall-clock**: VortexStore next to
  rdflib's ``Memory``, oxrdflib, pycottas and rdflib-hdt, one worker process
  per store so peak RSS is attributable, at 250k triples. It feeds the Pages
  dashboard and is NEVER uploaded to CodSpeed.
* THIS file is **self-referential and instrumented**: it runs under
  ``pytest --codspeed`` in simulation mode, where every task gets a
  deterministic instruction count comparable across commits. That is what
  makes it a regression gate rather than a leaderboard.

Only the vortex variants are here, deliberately. CodSpeed tracks *this*
package's code over time; another library's instruction count moves when
that library releases, which is not a signal this repo can act on. The
competitive question is the dashboard's job.

Design: instruction counts are deterministic, so the full cross product of
configurations x queries would mostly measure the same code paths many times
over. Instead, the whole query set runs once on the primary configuration,
and every other axis is isolated on the few queries where it can actually
move the number:

  primary config        dictionary layout, in-memory, no index, pushdown on
  BGP pushdown          the join queries with the hook unregistered — the
                        A/B against rdflib's per-binding nested loop
  residency + index     file-backed stores, where the per-call native floor
                        dominates and secondary indexes earn their keep:
                        the two lookups they target, per index config, plus
                        one join as the control the README says they do
                        not help
  Store.triples()       the raw service rdflib asks of the store, per pattern
                        selectivity, below the SPARQL layer — and one
                        predicate scan across the store variants that change
                        how a match is served (code path vs string fallback
                        vs Default layout vs file-backed)
  open                  each residency, for both layouts

Building (``serialize_rdf``) is not measured: it is the vortex-rdf package's
code and tracked by that repo's own suite. Nor is ``len()`` — it delegates
straight to the native size.

Run locally (walltime mode, no Valgrind needed):
    uv run pytest bench/test_codspeed.py --codspeed
    CODSPEED_BENCH_TRIPLES=5000 uv run pytest bench/test_codspeed.py --codspeed

Without ``--codspeed`` pytest just executes the tasks as ordinary tests,
which keeps the fixtures and query set honest even when nothing is measured.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from rdflib import Graph
from rdflib.term import URIRef
from rdflib.util import from_n3

from bench.dataset import (
    DatasetConfig,
    config_from_env,
    moduli,
    object_nt,
    predicate_iri,
    subject_iri,
    write_ntriples,
)
from bench.queries import Query, build_queries
from vortex_rdflib import VortexStore, register_sparql_pushdown, unregister_sparql_pushdown

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

# Built at import, not in a fixture: parametrize needs the query names at
# collection time. Cheap — the constants come from the same residue
# arithmetic that generates the data, so nothing is queried to derive them.
# Adding a query to bench/queries.py adds a task here, with no list to sync.
QUERIES: dict[str, Query] = {q.name: q for q in build_queries(CFG, MODULI)}

# ─── Store variants ─────────────────────────────────────────────────────────
#: Secondary-index configurations of the Dictionary layout — the dashboard's
#: index axis. One `.vortex` artifact per entry.
INDEXES: dict[str, list[str]] = {
    "noidx": [],
    "bycopy": ["secondary-by-copy"],
    "byref": ["secondary-by-reference"],
}

#: Artifacts built once per session: the three indexed Dictionary files plus
#: a Default-layout file, which has no term dictionary and so serves matches
#: through the N-Triples string path.
FILES: dict[str, dict] = {
    **{f"dict_{tag}": dict(layout="dictionary", indexes=idx) for tag, idx in INDEXES.items()},
    "default": dict(layout="default"),
}

#: Queries whose cost is dominated by the BGP join strategy.
JOIN_QUERIES = ("star-2", "star-3", "chain-2", "optional")

#: File-backed cases as `(index_tag, query)`: the two lookups the secondary
#: indexes target, per index config, plus one join on the unindexed store as
#: the control (indexes should not move it).
FILE_CASES: list[tuple[str, str]] = [
    (tag, query) for tag in INDEXES for query in ("po-lookup", "o-scan")
] + [("noidx", "star-2")]

#: `Store.triples()` pattern shapes, most to least selective; only
#: `full-scan` materializes the whole dataset. Constants derive from the
#: generator, so none of them matches zero rows.
_S0 = URIRef(subject_iri(0))
_P0 = URIRef(predicate_iri(0))
_P1 = URIRef(predicate_iri(1))
_O0 = from_n3(object_nt(0, CFG, MODULI))
# First object index in the IRI branch that links back into subject space —
# the same residue rule the generator and the chain query use.
_LITERAL_CUT = round(CFG.literal_frac * 10)
_O_LINK = URIRef(
    subject_iri(
        next(j for j in range(MODULI.n_obj) if j % 10 >= _LITERAL_CUT and j < MODULI.n_subj)
    )
)
PATTERNS: dict[str, tuple] = {
    "spo-exact": (_S0, _P0, _O0),
    "po-bound": (None, _P0, _O0),
    "o-bound": (None, None, _O_LINK),
    "s-bound": (_S0, None, None),
    "p-scan": (None, _P1, None),
    "full-scan": (None, None, None),
}

#: Store variants that change how a single match is served, compared on one
#: predicate scan.
STORE_VARIANTS = ("mem_noidx", "file_noidx", "mem_nocodes", "mem_default")


def _consume_query(graph: Graph, query: Query) -> int:
    """Execute and fully consume one query; an ASK counts as one row.

    Mirrors ``worker.run_once``: results are always iterated to exhaustion,
    so lazy result setup cannot masquerade as query speed.
    """
    result = graph.query(query.sparql)
    if query.is_ask:
        return 1 if result.askAnswer else 0
    return sum(1 for _ in result)


def _consume_triples(store: VortexStore, pattern: tuple) -> int:
    """Match and materialize every row — a lazy generator must not pass for speed."""
    return sum(1 for _ in store.triples(pattern))


# ─── Fixtures ───────────────────────────────────────────────────────────────
# Everything expensive is session-scoped: the N-Triples file, the `.vortex`
# artifacts and the opened stores are built once, outside any measured
# region, so each task measures only the operation it names.


@pytest.fixture(scope="session")
def source_nt(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("codspeed-data") / "dataset.nt"
    write_ntriples(str(path), CFG)
    return path


@pytest.fixture(scope="session")
def vortex_files(tmp_path_factory, source_nt) -> dict[str, str]:
    """The `.vortex` artifacts, keyed as in `FILES`."""
    from vortex_rdf import serialize_rdf

    root = tmp_path_factory.mktemp("codspeed-stores")
    paths: dict[str, str] = {}
    for key, options in FILES.items():
        out = root / f"{key}.vortex"
        serialize_rdf(str(source_nt), str(out), **options)
        paths[key] = str(out)
    return paths


@pytest.fixture(scope="session")
def stores(vortex_files) -> dict[str, VortexStore]:
    """Opened stores, keyed `<residency>_<variant>`.

    `mem_nocodes` is the Dictionary layout with the u32 code path forced off:
    `VORTEX_RDF_DISABLE_CODE_PATH` is read at construction, so it only has to
    be set while that one store is built. Against `mem_noidx` it isolates the
    cost of decoding codes versus parsing N-Triples term strings.
    """
    opened = {
        "mem_noidx": VortexStore(vortex_files["dict_noidx"], in_memory=True),
        "mem_default": VortexStore(vortex_files["default"], in_memory=True),
        **{f"file_{tag}": VortexStore(vortex_files[f"dict_{tag}"]) for tag in INDEXES},
    }
    previous = os.environ.get("VORTEX_RDF_DISABLE_CODE_PATH")
    os.environ["VORTEX_RDF_DISABLE_CODE_PATH"] = "1"
    try:
        opened["mem_nocodes"] = VortexStore(vortex_files["dict_noidx"], in_memory=True)
    finally:
        if previous is None:
            del os.environ["VORTEX_RDF_DISABLE_CODE_PATH"]
        else:
            os.environ["VORTEX_RDF_DISABLE_CODE_PATH"] = previous
    return opened


@pytest.fixture(scope="session")
def graphs(stores) -> dict[str, Graph]:
    return {key: Graph(store=store) for key, store in stores.items()}


@pytest.fixture
def without_pushdown() -> Iterator[None]:
    """Drop the CUSTOM_EVALS hook for one task, then put it back."""
    unregister_sparql_pushdown()
    yield
    register_sparql_pushdown()


# ─── query::<name> — the full set on the primary configuration ──────────────


@pytest.mark.parametrize("name", list(QUERIES))
def test_query(benchmark, graphs, name):
    """One dashboard query on the primary configuration: Dictionary layout,
    in memory, unindexed, BGP pushdown on — the shipped default, and the
    behaviour the dashboard's fastest vortex row reports."""
    graph, query = graphs["mem_noidx"], QUERIES[name]
    assert _consume_query(graph, query) > 0
    benchmark(_consume_query, graph, query)


@pytest.mark.usefixtures("without_pushdown")
@pytest.mark.parametrize("name", JOIN_QUERIES)
def test_query_no_pushdown(benchmark, graphs, name):
    """The join queries under rdflib's default evaluator — one `triples()`
    call per candidate binding — so the pushdown's own contribution is a
    difference between two tracked numbers, not an inference."""
    graph, query = graphs["mem_noidx"], QUERIES[name]
    assert _consume_query(graph, query) > 0
    benchmark(_consume_query, graph, query)


@pytest.mark.parametrize(("index_tag", "name"), FILE_CASES, ids=[f"{t}-{q}" for t, q in FILE_CASES])
def test_query_file(benchmark, graphs, index_tag, name):
    """File-backed stores: the per-call native floor that in-memory opens
    remove, and the axis on which secondary indexes are meant to pay off."""
    graph, query = graphs[f"file_{index_tag}"], QUERIES[name]
    assert _consume_query(graph, query) > 0
    benchmark(_consume_query, graph, query)


# ─── triples::<pattern> — the store's raw service, below SPARQL ─────────────


@pytest.mark.parametrize("name", list(PATTERNS))
def test_triples(benchmark, stores, name):
    """`Store.triples()` per pattern shape on the primary store. This is the
    whole service rdflib asks of the store outside the pushdown path; the
    SPARQL tasks above include rdflib's parse and algebra on top of it."""
    store, pattern = stores["mem_noidx"], PATTERNS[name]
    assert _consume_triples(store, pattern) > 0
    benchmark(_consume_triples, store, pattern)


@pytest.mark.parametrize("variant", STORE_VARIANTS)
def test_triples_p_scan(benchmark, stores, variant):
    """The same predicate scan across the store variants that change how a
    match is served: u32 code path, code path disabled (string fallback on
    the same file), Default layout (no dictionary at all), file-backed."""
    store, pattern = stores[variant], PATTERNS["p-scan"]
    assert _consume_triples(store, pattern) > 0
    benchmark(_consume_triples, store, pattern)


# ─── open::<residency>::<layout> ────────────────────────────────────────────


@pytest.mark.parametrize("file_key", ("dict_noidx", "default"))
def test_open_file(benchmark, vortex_files, file_key):
    """Lazy open: reads the footer and metadata, not the data."""
    benchmark(VortexStore, vortex_files[file_key])


@pytest.mark.parametrize("file_key", ("dict_noidx", "default"))
def test_open_memory(benchmark, vortex_files, file_key):
    """Eager open: loads the store and, for Dictionary, the term dictionary."""
    benchmark(VortexStore, vortex_files[file_key], in_memory=True)
