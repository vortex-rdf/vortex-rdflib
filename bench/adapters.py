"""Store adapters under comparison.

Each adapter runs its full lifecycle (build/parse, queries, memory readings)
in its own worker process (see ``worker.py``), so heavy imports live inside
the factory functions: the rdflib-Memory worker never imports vortex_rdflib
(whose pushdown hook registers into rdflib's ``CUSTOM_EVALS``), and only the
oxrdflib workers import pyoxigraph.

Quads: the dataset is N-Quads, and every store whose format has named graphs
loads it into an rdflib ``Dataset`` whose default graph is the **union** of
its graphs — so the queries that name no graph see exactly the triples they
saw before the dataset had graphs, and the ``graphs`` group can name one.
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
rows. For vortex adapters with a resident dictionary, rdflib's engine hands
the algebra nodes this package understands to its code-space pushdown. The
``pushdown off`` row runs the primary configuration with
``VORTEX_RDF_DISABLE_PUSHDOWN=1``, so the pushdown's own contribution is the
difference between two rows of the same store.

The exception is ``oxrdflib (pyoxigraph engine)``: the same Oxigraph store,
asked without ``use_store_provided=False``, so ``OxigraphStore.query`` answers
from pyoxigraph's own Rust engine and rdflib evaluates nothing. It is here as
the reference point the rdflib rows are all working against, rather than a
like-for-like row — it parses its own SPARQL, plans its own joins and never
builds an rdflib solution per intermediate binding, so it takes the
dashboard's "fastest" marker on most end-to-end columns by not doing the same
work.

It is emphatically **not** a measurement of pyoxigraph. Every solution still
crosses into Python through ``oxrdflib``'s ``from_ox``, one value at a time,
because the row has to produce the rdflib terms every other row produces. For
a query that returns rows that conversion, not the engine, is most of the
number: measured at 20k against the same query text run straight on the
underlying ``pyoxigraph.Store``, the predicate scan is 2.66 ms here against
0.23 ms there, the ``DISTINCT`` 2.16 against 0.25, ``GRAPH ?g`` 2.77 against
0.25 — 88-91% of this row is oxrdflib. Only where the answer is a scalar does
the engine dominate (``COUNT(*)``: 2.91 against 2.47).

Two more consequences worth knowing:

- It reports no ``exec only`` figure (``prepared=False``). rdflib's prepared
  algebra cannot cross that boundary: ``OxigraphStore.query`` raises
  ``NotImplementedError`` for a parsed ``Query``, and ``Graph.query``
  *catches* it and quietly falls back to its own evaluator — so a timing
  taken that way would be rdflib's engine wearing this row's label.
- Its ``full`` figure includes Oxigraph's own parse, which is the honest
  end-to-end comparison: both columns' ``full`` is "here is a query string,
  here are the rows".

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

from rdflib import Dataset, Graph


@dataclass(frozen=True)
class Adapter:
    slug: str
    label: str
    engine: str  # "rdflib" | "native"
    # (nq_path, nt_path, work_dir) -> queryable Graph or Dataset. A quad
    # adapter builds from the N-Quads; a triple-only one from the N-Triples.
    make: Callable[[str, str, str], Graph]
    env: dict[str, str] = field(default_factory=dict)
    query_kwargs: dict = field(default_factory=dict)
    # Whether the store's format has named graphs. False means the `graphs`
    # query group is not asked of it (see the module docstring).
    quads: bool = True
    # Whether a prepared algebra reaches this adapter's evaluator, so the run
    # can be split into "prepare" and "evaluate". False for a store that
    # answers the query string itself, below rdflib: there is no algebra to
    # prepare, so only the end-to-end figure exists (see the module docstring).
    prepared: bool = True
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
) -> Callable[[str, str, str], Graph]:
    """A Dictionary-layout store built with `indexes`, opened per `in_memory`.

    `tag` names the built file, so adapters sharing an index configuration
    share one build: the workers run sequentially and `serialize_rdf` is
    deterministic, so the residency variants rewrite identical bytes.
    """

    def make(nq_path: str, nt_path: str, work_dir: str) -> Graph:
        from vortex_rdf import serialize_rdf

        from vortex_rdflib import VortexRdflibStore

        out = str(Path(work_dir) / f"data-dict-{tag}.vortex")
        serialize_rdf(nq_path, out, format="nquads", layout="dictionary", indexes=list(indexes))
        return Dataset(store=VortexRdflibStore(out, in_memory=in_memory), default_union=True)

    return make


def _make_rdflib_memory(nq_path: str, nt_path: str, work_dir: str) -> Graph:
    ds = Dataset(default_union=True)
    ds.parse(nq_path, format="nquads")
    return ds


def _make_oxrdflib(nq_path: str, nt_path: str, work_dir: str) -> Graph:
    ds = Dataset(store="Oxigraph", default_union=True)
    ds.parse(nq_path, format="nquads")
    return ds


def _make_rdflib_hdt(nq_path: str, nt_path: str, work_dir: str) -> Graph:
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
    from rdflib_hdt import HDTStore  # ty: ignore[unresolved-import]

    out = Path(work_dir) / "data.hdt"
    subprocess.run(["hdt", "convert", nt_path, str(out)], check=True, capture_output=True)
    return Graph(store=HDTStore(str(out)))


def _make_pycottas(nq_path: str, nt_path: str, work_dir: str) -> Graph:
    """A COTTAS file built from the shared N-Triples, served through its store.

    `rdf2cottas` is the build step the load row measures, mirroring
    `serialize_rdf` for the Vortex rows: both turn the same `.nt` into their
    own columnar file before any query runs.
    """
    from pycottas import COTTASStore, rdf2cottas  # ty: ignore[unresolved-import]

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
    # Last, and ruled off in the dashboard: a different engine, not a
    # different store. See the module docstring on how to read it.
    Adapter(
        "oxrdflib_native",
        "oxrdflib (in-mem · pyoxigraph engine)",
        "native",
        _make_oxrdflib,
        requires="oxrdflib",
        prepared=False,
    ),
]

BY_SLUG: dict[str, Adapter] = {a.slug: a for a in ADAPTERS}
