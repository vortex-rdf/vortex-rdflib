"""Store-level benchmarks: opening a `.vortex` file and serving triple patterns.

``Store.triples()`` is the whole service rdflib asks of this package outside
the pushdown path, so its cost per pattern shape — and how that cost changes
between the u32 code path, the N-Triples string fallback, and file-backed
versus in-memory opens — is what these benchmarks track.
"""

import pytest
from bench.dataset import DatasetConfig, Moduli, object_nt, predicate_iri, subject_iri
from rdflib.term import URIRef
from rdflib.util import from_n3

from vortex_rdflib import VortexStore

# Pattern shapes, ordered from most to least selective. "full-scan" is the
# only one that materializes the whole dataset.
PATTERN_NAMES = ("spo-exact", "po-bound", "o-bound", "s-bound", "p-scan", "full-scan")

# Store variants compared on a single mid-selectivity pattern.
STORE_FIXTURES = ("dict_memory_store", "dict_file_store", "no_codes_store", "default_layout_store")


@pytest.fixture(scope="session")
def patterns(dataset_config: DatasetConfig, dataset_moduli: Moduli) -> dict:
    """Triple patterns whose constants are derived from the generator, so none
    of them matches zero rows."""
    s0 = URIRef(subject_iri(0))
    p0 = URIRef(predicate_iri(0))
    p1 = URIRef(predicate_iri(1))
    o0 = from_n3(object_nt(0, dataset_config, dataset_moduli))

    # First object index in the IRI branch that links back into subject space.
    literal_cut = round(dataset_config.literal_frac * 10)
    j_link = next(
        j
        for j in range(dataset_moduli.n_obj)
        if j % 10 >= literal_cut and j < dataset_moduli.n_subj
    )
    o_link = URIRef(subject_iri(j_link))

    return {
        "spo-exact": (s0, p0, o0),
        "po-bound": (None, p0, o0),
        "o-bound": (None, None, o_link),
        "s-bound": (s0, None, None),
        "p-scan": (None, p1, None),
        "full-scan": (None, None, None),
    }


def consume(store: VortexStore, pattern) -> int:
    """Match and materialize every row — a lazy generator must not pass for speed."""
    return sum(1 for _ in store.triples(pattern))


@pytest.mark.parametrize("pattern_name", PATTERN_NAMES)
def test_triples_pattern(benchmark, dict_memory_store: VortexStore, patterns, pattern_name: str):
    pattern = patterns[pattern_name]
    assert consume(dict_memory_store, pattern) > 0
    benchmark(consume, dict_memory_store, pattern)


@pytest.mark.parametrize("store_fixture", STORE_FIXTURES)
def test_triples_predicate_scan(benchmark, request, patterns, store_fixture: str):
    """The same predicate scan across every store variant: code path vs string
    path, resident vs file-backed."""
    store = request.getfixturevalue(store_fixture)
    pattern = patterns["p-scan"]
    assert consume(store, pattern) > 0
    benchmark(consume, store, pattern)


@pytest.mark.parametrize("layout", ("dictionary", "default"))
def test_open_file_backed(benchmark, vortex_files, layout: str):
    """Lazy open: reads the footer/metadata, not the data."""
    path = str(vortex_files[layout])
    benchmark(VortexStore, path)


@pytest.mark.parametrize("layout", ("dictionary", "default"))
def test_open_in_memory(benchmark, vortex_files, layout: str):
    """Eager open: loads the store (and, for dictionary, the term dictionary)."""
    path = str(vortex_files[layout])
    benchmark(VortexStore, path, in_memory=True)


def test_len(benchmark, dict_memory_store: VortexStore):
    benchmark(len, dict_memory_store)
