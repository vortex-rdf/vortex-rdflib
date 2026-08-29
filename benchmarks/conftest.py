"""Fixtures for the CodSpeed suite: one synthetic dataset, shared by every module.

The dataset and the SPARQL set are the ones from ``bench/`` — the same
generator that backs the comparative dashboard — so a CodSpeed regression maps
onto a panel there instead of describing a different workload. What differs is
the scale: CodSpeed runs benchmarks under CPU simulation (~50x slower than
native), so the default is 20k triples rather than the dashboard's 250k.
Override with ``CODSPEED_BENCH_TRIPLES``.

Everything expensive is session-scoped: the N-Triples file, the serialized
``.vortex`` files and the opened stores are built once, outside the measured
region, so each benchmark measures only the operation it names.
"""

import os
from pathlib import Path

import pytest
from bench.dataset import DatasetConfig, Moduli, moduli, write_ntriples
from bench.queries import Query, build_queries
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexStore

DEFAULT_TRIPLES = 20_000

# Layouts under measurement: "dictionary" enables the u32 code path and the
# BGP pushdown, "default" exercises the N-Triples string fallback.
LAYOUTS = ("dictionary", "default")


@pytest.fixture(scope="session")
def dataset_config() -> DatasetConfig:
    return DatasetConfig(
        n=int(os.environ.get("CODSPEED_BENCH_TRIPLES", DEFAULT_TRIPLES)),
        subject_ratio=0.1,
        predicates=32,
        object_ratio=0.5,
        literal_frac=0.4,
    )


@pytest.fixture(scope="session")
def dataset_moduli(dataset_config: DatasetConfig) -> Moduli:
    return moduli(dataset_config)


@pytest.fixture(scope="session")
def nt_path(dataset_config: DatasetConfig, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("codspeed-rdf") / "dataset.nt"
    write_ntriples(str(path), dataset_config)
    return path


@pytest.fixture(scope="session")
def vortex_files(nt_path: Path, tmp_path_factory) -> dict[str, Path]:
    """One serialized `.vortex` file per layout, keyed by layout name."""
    out_dir = tmp_path_factory.mktemp("codspeed-vortex")
    files = {}
    for layout in LAYOUTS:
        out = out_dir / f"dataset-{layout}.vortex"
        serialize_rdf(str(nt_path), str(out), layout=layout)
        files[layout] = out
    return files


@pytest.fixture(scope="session")
def queries(dataset_config: DatasetConfig, dataset_moduli: Moduli) -> dict[str, Query]:
    """The synthetic SPARQL set, keyed by query name."""
    return {query.name: query for query in build_queries(dataset_config, dataset_moduli)}


@pytest.fixture(scope="session")
def dict_memory_store(vortex_files: dict[str, Path]) -> VortexStore:
    """Dictionary layout, loaded in memory: the fast path (codes + pushdown)."""
    return VortexStore(str(vortex_files["dictionary"]), in_memory=True)


@pytest.fixture(scope="session")
def dict_file_store(vortex_files: dict[str, Path]) -> VortexStore:
    """Dictionary layout, lazily file-backed: the default open mode."""
    return VortexStore(str(vortex_files["dictionary"]))


@pytest.fixture(scope="session")
def default_layout_store(vortex_files: dict[str, Path]) -> VortexStore:
    """Default layout: no term dictionary, so matches take the string path."""
    return VortexStore(str(vortex_files["default"]), in_memory=True)


@pytest.fixture(scope="session")
def no_codes_store(vortex_files: dict[str, Path]) -> VortexStore:
    """Dictionary layout with the code path forced off.

    ``VORTEX_RDF_DISABLE_CODE_PATH`` is read in ``__init__``/``open()``, so it
    only has to be set while the store is constructed; paired with
    ``dict_memory_store`` this isolates the cost of decoding u32 codes versus
    parsing N-Triples term strings.
    """
    previous = os.environ.get("VORTEX_RDF_DISABLE_CODE_PATH")
    os.environ["VORTEX_RDF_DISABLE_CODE_PATH"] = "1"
    try:
        return VortexStore(str(vortex_files["dictionary"]), in_memory=True)
    finally:
        if previous is None:
            del os.environ["VORTEX_RDF_DISABLE_CODE_PATH"]
        else:
            os.environ["VORTEX_RDF_DISABLE_CODE_PATH"] = previous
