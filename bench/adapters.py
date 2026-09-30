"""Store adapters under comparison.

Each adapter runs its full lifecycle (build/parse, queries, memory readings)
in its own worker process (see ``worker.py``), so heavy imports live inside
the factory functions: the rdflib-Memory worker never imports vortex_rdflib
(whose pushdown hook registers into rdflib's ``CUSTOM_EVALS``), only the
oxrdflib and pyoxigraph workers import pyoxigraph, and the pyoxigraph worker
never imports rdflib.

Quads: the dataset is N-Quads, and every store whose format has named graphs
loads it into an rdflib ``Dataset`` whose default graph is the **union** of
its graphs — or, for pyoxigraph, into its own store, queried with that same
union as the default graph — so the queries that name no graph see exactly
the triples they saw before the dataset had graphs, and the ``graphs`` group
can name one.
Two contenders cannot serve named graphs *through rdflib*, for different
reasons, and both load the flattened N-Triples instead (``quads=False``) —
the same statements in one graph, by construction of the generator. They
answer every query but the ``graphs`` group, which the worker skips for them
and the dashboard leaves empty.

- **HDT** has none to serve: the format is triples-only.
- **COTTAS** does have them — ``rdf2cottas`` reads N-Quads and always writes
  an ``(s, p, o, g)`` table, its SQL translator can filter on ``g``, and
  ``COTTASStore.is_quad_table`` reports it — but ``COTTASStore`` (pycottas
  1.1.0) does not expose any of that to rdflib: it never sets
  ``context_aware``, its ``triples()`` ignores the ``context`` argument and
  yields ``None`` for every row's graph, and it does not override
  ``contexts()``. A ``Dataset`` over it is refused and a ``GRAPH`` query
  raises. Feeding it the N-Quads would only cost it a column it cannot be
  asked about, so it gets the same triples the other stores are asked.

``env`` entries are applied by the orchestrator to the worker's environment
before Python starts, so process-wide switches like
``VORTEX_RDF_DISABLE_PUSHDOWN`` are in place before any import runs.

Engines: SPARQL evaluation is rdflib's engine for every adapter but one, so
the store serving triple patterns is the only variable across the comparable
rows. oxrdflib is among them: it is asked with ``use_store_provided=False``,
which stops ``OxigraphStore.query`` from handing the query text to
pyoxigraph. For vortex adapters with a resident dictionary, rdflib's engine
hands the algebra nodes this package understands to its code-space pushdown.
The two ``pushdown off`` rows run the primary configuration of each
residency with ``VORTEX_RDF_DISABLE_PUSHDOWN=1``, so the pushdown's own
contribution is the difference between two rows of the same store.

The exception is ``pyoxigraph``: Oxigraph's own Python bindings, used
directly. The store bulk-loads the N-Quads and ``Store.query`` takes the
query text, so pyoxigraph parses it, plans its own joins and returns its own
terms; that worker never imports rdflib, so both its timings and its peak RSS
are pyoxigraph's alone. It is here as the reference point the rdflib rows are
all working against, rather than a like-for-like row, and it takes the
dashboard's "fastest" marker on most end-to-end columns by not doing the same
work. What keeps it comparable at all:

- Same answers. Its default graph is the union of the graphs
  (``use_default_graph_as_union``), as the rdflib rows' ``Dataset`` has it,
  and ``GRAPH ?g`` ranges over the named graphs alone, so it returns the
  same row count on every query.
- Same finish line. A pyoxigraph solution builds each Python term only when
  it is read, where an rdflib row arrives holding its terms, so the worker
  reads every value (``worker.consume_native``): the row ends, like every
  other, with the answer's terms in Python.
- No ``exec only`` figure. pyoxigraph exposes no parsed-query object, so its
  parse cannot be timed apart from its evaluation. Its ``full`` figure is the
  honest comparison: every ``full`` cell is "here is a query string, here are
  the rows".

The Vortex rows are all Dictionary layout — the layout that enables the term
code path and so the only one worth tuning — crossed over the two axes that
change how a store answers: **residency** (file-backed lazy open vs fully
in-memory) and **secondary index** (none, ``secondary-by-copy``,
``secondary-by-reference``).
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyoxigraph import Store
    from rdflib import Graph


@dataclass(frozen=True)
class Adapter:
    slug: str
    label: str
    # "rdflib": rdflib's engine evaluates, over the store `make` wraps in a
    # Graph or Dataset. "native": the store answers the query text itself and
    # rdflib has no part in it; with no algebra to prepare, the row reports
    # only the end-to-end mode (see the module docstring).
    engine: str
    # (nq_path, nt_path, work_dir) -> what queries are asked of: an rdflib
    # Graph or Dataset, or the native store itself. A quad adapter builds from
    # the N-Quads; a triple-only one from the N-Triples.
    make: Callable[[str, str, str], "Graph | Store"]
    env: dict[str, str] = field(default_factory=dict)
    query_kwargs: dict = field(default_factory=dict)
    # Whether the store's format has named graphs. False means the `graphs`
    # query group is not asked of it (see the module docstring).
    quads: bool = True
    # Import name of the third-party package this adapter needs from the
    # project environment, so the orchestrator can fail fast with the fix
    # instead of losing a worker.
    requires: str | None = None
    # Packages for an isolated venv this adapter's worker runs in. Needed when
    # a contender's pins cannot share the project environment: pycottas pins
    # pyoxigraph exactly (against oxrdflib's range) and rdflib-hdt pins one
    # rdflib patch release, which would otherwise re-baseline every other row.
    venv_packages: tuple[str, ...] = ()
    # External command the adapter shells out to during its build step.
    requires_cli: str | None = None


def _make_vortex(
    tag: str, in_memory: bool, indexes: tuple[str, ...] = ()
) -> Callable[[str, str, str], "Graph"]:
    """A Dictionary-layout store built with `indexes`, opened per `in_memory`.

    `tag` names the built file, so adapters sharing an index configuration
    share one build: the workers run sequentially and `serialize_rdf` is
    deterministic, so the residency variants rewrite identical bytes.
    """

    def make(nq_path: str, nt_path: str, work_dir: str) -> "Graph":
        from rdflib import Dataset
        from vortex_rdf import serialize_rdf

        from vortex_rdflib import VortexRdflibStore

        out = str(Path(work_dir) / f"data-dict-{tag}.vortex")
        serialize_rdf(nq_path, out, format="nquads", layout="dictionary", indexes=list(indexes))
        return Dataset(store=VortexRdflibStore(out, in_memory=in_memory), default_union=True)

    return make


def _make_rdflib_memory(nq_path: str, nt_path: str, work_dir: str) -> "Graph":
    from rdflib import Dataset

    ds = Dataset(default_union=True)
    ds.parse(nq_path, format="nquads")
    return ds


def _make_oxrdflib(nq_path: str, nt_path: str, work_dir: str) -> "Graph":
    from rdflib import Dataset

    ds = Dataset(store="Oxigraph", default_union=True)
    ds.parse(nq_path, format="nquads")
    return ds


def _make_pyoxigraph(nq_path: str, nt_path: str, work_dir: str) -> "Store":
    """Oxigraph's own store, loaded and queried with no rdflib in between.

    `bulk_load` is pyoxigraph's documented path for a file this size: its own
    parser straight into the store, twice as fast as the transactional `load`
    here (15 ms against 32 ms at 20k quads). It is this row's counterpart of
    the other stores' own fastest way in — `serialize_rdf`, `rdf2cottas`,
    `hdt convert`.
    """
    import pyoxigraph

    store = pyoxigraph.Store()
    store.bulk_load(path=nq_path, format=pyoxigraph.RdfFormat.N_QUADS)
    return store


def _make_rdflib_hdt(nq_path: str, nt_path: str, work_dir: str) -> "Graph":
    """An HDT file built from the shared N-Triples, served through its store.

    `rdflib-hdt` reads HDT but cannot write it, and hdt-cpp's `rdf2hdt` is not
    packaged for Python, so the build step shells out to the Rust `hdt` crate's
    CLI (`cargo install hdt --features cli`). Its output is the HDT default
    format, which hdt-cpp loads unchanged.

    rdflib-hdt ships `optimize_sparql()`, a BGP fast path that would be the
    counterpart of this package's pushdown, but it is deliberately NOT called:
    it answers this suite's joins wrongly (chain-2 26 rows against a verified
    18, optional 152 against 1) and, despite its docstring, its global patch
    also drops every other store's graph to zero rows. So this row is rdflib's
    engine over the HDT store, exactly like the other contenders.

    Labelled in-mem on the residency test the whole matrix uses: where a
    query's bytes come from. Measured over repeated pattern scans, this store
    issues no read syscalls and takes no page faults — `mapped=True` mmaps the
    file, but `indexed=True` builds HDT's extra index at open and that walk
    makes every page resident, so queries never go back to the file. The
    file-backed rows (`vortex-rdflib (dict . file)`, `pycottas-rdflib`) do
    read per query, tens of MB across the same scans.
    """
    from rdflib import Graph
    from rdflib_hdt import HDTStore  # ty: ignore[unresolved-import]

    out = Path(work_dir) / "data.hdt"
    subprocess.run(["hdt", "convert", nt_path, str(out)], check=True, capture_output=True)
    return Graph(store=HDTStore(str(out)))


def _make_pycottas(nq_path: str, nt_path: str, work_dir: str) -> "Graph":
    """A COTTAS file built from the shared N-Triples, served through its store.

    `rdf2cottas` is the build step the load row measures, mirroring
    `serialize_rdf` for the Vortex rows: both turn the same `.nt` into their
    own columnar file before any query runs.
    """
    from pycottas import COTTASStore, rdf2cottas  # ty: ignore[unresolved-import]
    from rdflib import Graph

    out = str(Path(work_dir) / "data.cottas")
    rdf2cottas(nt_path, out)
    return Graph(store=COTTASStore(out))


BY_COPY = ("secondary-by-copy",)
BY_REFERENCE = ("secondary-by-reference",)

ADAPTERS: list[Adapter] = [
    Adapter(
        "vortex_dict_mem",
        "vortex-rdflib (dict · in-mem)",
        "rdflib",
        _make_vortex("noidx", in_memory=True),
    ),
    Adapter(
        "vortex_dict_mem_copy",
        "vortex-rdflib (dict · in-mem · by-copy)",
        "rdflib",
        _make_vortex("copy", in_memory=True, indexes=BY_COPY),
    ),
    Adapter(
        "vortex_dict_mem_ref",
        "vortex-rdflib (dict · in-mem · by-reference)",
        "rdflib",
        _make_vortex("ref", in_memory=True, indexes=BY_REFERENCE),
    ),
    Adapter(
        "vortex_dict_mem_nopushdown",
        "vortex-rdflib (dict · in-mem · pushdown off)",
        "rdflib",
        _make_vortex("noidx", in_memory=True),
        env={"VORTEX_RDF_DISABLE_PUSHDOWN": "1"},
    ),
    Adapter(
        "vortex_dict_file",
        "vortex-rdflib (dict · file)",
        "rdflib",
        _make_vortex("noidx", in_memory=False),
    ),
    Adapter(
        "vortex_dict_file_copy",
        "vortex-rdflib (dict · file · by-copy)",
        "rdflib",
        _make_vortex("copy", in_memory=False, indexes=BY_COPY),
    ),
    Adapter(
        "vortex_dict_file_ref",
        "vortex-rdflib (dict · file · by-reference)",
        "rdflib",
        _make_vortex("ref", in_memory=False, indexes=BY_REFERENCE),
    ),
    Adapter(
        "vortex_dict_file_nopushdown",
        "vortex-rdflib (dict · file · pushdown off)",
        "rdflib",
        _make_vortex("noidx", in_memory=False),
        env={"VORTEX_RDF_DISABLE_PUSHDOWN": "1"},
    ),
    Adapter("rdflib_memory", "rdflib (in-mem)", "rdflib", _make_rdflib_memory),
    Adapter(
        "oxrdflib",
        "oxrdflib (in-mem)",
        "rdflib",
        _make_oxrdflib,
        query_kwargs={"use_store_provided": False},
        requires="oxrdflib",
    ),
    Adapter(
        "pycottas",
        "pycottas-rdflib (file)",
        "rdflib",
        _make_pycottas,
        venv_packages=("rdflib>=7,<8", "pycottas>=1.1"),
        quads=False,
    ),
    Adapter(
        "rdflib_hdt",
        "rdflib-hdt (in-mem)",
        "rdflib",
        _make_rdflib_hdt,
        venv_packages=("rdflib-hdt>=3.2",),
        requires_cli="hdt",
        quads=False,
    ),
    # Last, and ruled off in the dashboard: no rdflib at all, so a different
    # engine rather than a different store. See the module docstring on how
    # to read it.
    Adapter(
        "pyoxigraph",
        "pyoxigraph (in-mem)",
        "native",
        _make_pyoxigraph,
        query_kwargs={"use_default_graph_as_union": True},
        requires="pyoxigraph",
    ),
]

BY_SLUG: dict[str, Adapter] = {a.slug: a for a in ADAPTERS}
