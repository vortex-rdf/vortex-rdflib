"""Store adapters under comparison.

Each adapter runs its full lifecycle (build/parse, queries, memory readings)
in its own worker process (see ``worker.py``), so heavy imports live inside
the factory functions: the rdflib-Memory worker never imports vortex_rdflib
(whose pushdown hook registers into rdflib's ``CUSTOM_EVALS``), and only the
oxrdflib workers import pyoxigraph.

``env`` entries are applied by the orchestrator to the worker's environment
before Python starts, so process-wide switches like
``VORTEX_RDF_DISABLE_PUSHDOWN`` are in place before any import runs.

Engines: SPARQL evaluation is rdflib's engine for every adapter, so the store
serving triple patterns is the only variable. A store's own evaluator is
deliberately out of scope — it skips rdflib's parse and algebra entirely,
which dominates the cheap queries, so its rows would not be comparable with
the rest and would capture the "fastest" marker on most columns.
For vortex adapters with a resident dictionary, rdflib's engine hands the
algebra nodes this package understands to its code-space pushdown. The
``pushdown off`` row runs the primary configuration with
``VORTEX_RDF_DISABLE_PUSHDOWN=1``, so the pushdown's own contribution is the
difference between two rows of the same store.

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

from rdflib import Graph


@dataclass(frozen=True)
class Adapter:
    slug: str
    label: str
    engine: str  # "rdflib" | "native"
    make: Callable[[str, str], Graph]  # (nt_path, work_dir) -> queryable Graph
    env: dict[str, str] = field(default_factory=dict)
    query_kwargs: dict = field(default_factory=dict)
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
) -> Callable[[str, str], Graph]:
    """A Dictionary-layout store built with `indexes`, opened per `in_memory`.

    `tag` names the built file, so adapters sharing an index configuration
    share one build: the workers run sequentially and `serialize_rdf` is
    deterministic, so the residency variants rewrite identical bytes.
    """

    def make(nt_path: str, work_dir: str) -> Graph:
        from vortex_rdf import serialize_rdf

        from vortex_rdflib import VortexStore

        out = str(Path(work_dir) / f"data-dict-{tag}.vortex")
        serialize_rdf(nt_path, out, layout="dictionary", indexes=list(indexes))
        return Graph(store=VortexStore(out, in_memory=in_memory))

    return make


def _make_rdflib_memory(nt_path: str, work_dir: str) -> Graph:
    g = Graph()
    g.parse(nt_path, format="nt")
    return g


def _make_oxrdflib(nt_path: str, work_dir: str) -> Graph:
    g = Graph(store="Oxigraph")
    g.parse(nt_path, format="nt")
    return g


def _make_rdflib_hdt(nt_path: str, work_dir: str) -> Graph:
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


def _make_pycottas(nt_path: str, work_dir: str) -> Graph:
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
    ),
    Adapter(
        "rdflib_hdt",
        "rdflib-hdt (in-mem)",
        "rdflib",
        _make_rdflib_hdt,
        venv_packages=("rdflib-hdt>=3.2",),
        requires_cli="hdt",
    ),
]

BY_SLUG: dict[str, Adapter] = {a.slug: a for a in ADAPTERS}
