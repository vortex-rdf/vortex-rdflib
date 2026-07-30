"""Store adapters under comparison.

Each adapter runs its full lifecycle (build/parse, queries, memory readings)
in its own worker process (see ``worker.py``), so heavy imports live inside
the factory functions: the rdflib-Memory worker never imports vortex_rdflib
(whose pushdown hook registers into rdflib's ``CUSTOM_EVALS``), and only the
oxrdflib workers import pyoxigraph.

``env`` entries are applied by the orchestrator to the worker's environment
before Python starts, so process-wide switches like
``VORTEX_RDF_DISABLE_PUSHDOWN`` are in place before any import runs.

Engines: SPARQL evaluation is rdflib's engine for every adapter except
``oxrdflib_native``, which delegates the query string to Oxigraph's own
evaluator (``Store.query``) — a different engine entirely, kept in the matrix
as the "what would a native engine do" reference point and labeled as such.
For vortex adapters with a resident dictionary, rdflib's engine hands whole
BGPs to this package's code-space pushdown; ``vortex_dict_nopush`` disables
that to isolate its effect.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph


@dataclass(frozen=True)
class Adapter:
    slug: str
    label: str
    engine: str  # "rdflib+pushdown" | "rdflib" | "native"
    make: Callable[[str, str], Graph]  # (nt_path, work_dir) -> queryable Graph
    env: dict[str, str] = field(default_factory=dict)
    query_kwargs: dict = field(default_factory=dict)


def _make_vortex(layout: str, in_memory: bool) -> Callable[[str, str], Graph]:
    def make(nt_path: str, work_dir: str) -> Graph:
        from vortex_rdf import serialize_rdf

        from vortex_rdflib import VortexStore

        out = str(Path(work_dir) / f"data-{layout}.vortex")
        serialize_rdf(nt_path, out, layout=layout)
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


ADAPTERS: list[Adapter] = [
    Adapter(
        "vortex_dict_mem",
        "Vortex dict · in-mem",
        "rdflib+pushdown",
        _make_vortex("dictionary", in_memory=True),
    ),
    Adapter(
        "vortex_dict_file",
        "Vortex dict · file",
        "rdflib+pushdown",
        _make_vortex("dictionary", in_memory=False),
    ),
    Adapter(
        "vortex_dict_nopush",
        "Vortex dict · no pushdown",
        "rdflib",
        _make_vortex("dictionary", in_memory=True),
        env={"VORTEX_RDF_DISABLE_PUSHDOWN": "1"},
    ),
    Adapter(
        "vortex_default_mem",
        "Vortex default · in-mem",
        "rdflib",
        _make_vortex("default", in_memory=True),
    ),
    Adapter("rdflib_memory", "rdflib Memory", "rdflib", _make_rdflib_memory),
    Adapter(
        "oxrdflib",
        "oxrdflib (rdflib engine)",
        "rdflib",
        _make_oxrdflib,
        query_kwargs={"use_store_provided": False},
    ),
    Adapter("oxrdflib_native", "oxrdflib (native SPARQL)", "native", _make_oxrdflib),
]

BY_SLUG: dict[str, Adapter] = {a.slug: a for a in ADAPTERS}
