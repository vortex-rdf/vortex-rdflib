"""The vortex-rdf 0.12 API this package relies on, pinned where it relies on
it. A failure here means the installed vortex-rdf differs from that API."""

from typing import Any

import pytest
import vortex_rdf
from vortex_rdf import TermDict, U64Column, VortexRdfStore, serialize_rdf

XSD = "http://www.w3.org/2001/XMLSchema#"


@pytest.fixture(scope="module")
def contract_store(tmp_path_factory) -> str:
    directory = tmp_path_factory.mktemp("contract")
    nt = directory / "contract.nt"
    nt.write_text(
        '<http://ex.org/a> <http://ex.org/p> "abc" .\n'
        '<http://ex.org/a> <http://ex.org/p> "Abc"@en .\n'
        '<http://ex.org/a> <http://ex.org/p> "x\\n"@en .\n'
        f'<http://ex.org/a> <http://ex.org/p> "042"^^<{XSD}integer> .\n'
        f'<http://ex.org/a> <http://ex.org/p> "9223372036854775808"^^<{XSD}long> .\n'
        "<http://ex.org/a> <http://ex.org/p> <http://ex.org/abc> .\n"
        "<http://ex.org/a> <http://ex.org/p> _:abc .\n",
        encoding="utf-8",
    )
    out = directory / "contract.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return str(out)


def _codes(column: U64Column) -> list[int]:
    return memoryview(column).cast("Q").tolist()


def _dictionary(store: VortexRdfStore) -> TermDict:
    dictionary = store.term_dict()
    assert dictionary is not None
    return dictionary


def _encode(dictionary: TermDict, term: str) -> int:
    code = dictionary.encode(term)
    assert code is not None
    return code


def test_version_is_0_12():
    assert vortex_rdf.__version__.split(".")[:2] == ["0", "12"]


def test_stores_open_mapped_or_in_memory_without_a_residency_budget(contract_store):
    assert _dictionary(VortexRdfStore(contract_store)).file_backed is True
    assert _dictionary(VortexRdfStore(contract_store, in_memory=True)).file_backed is False
    with pytest.raises(TypeError, match="max_resident_bytes"):
        VortexRdfStore(contract_store, max_resident_bytes=1 << 20)  # ty: ignore[unknown-argument]


def test_filter_codes_answers_subsets_of_sorted_unique_candidates(contract_store):
    dictionary = _dictionary(VortexRdfStore(contract_store))
    tagged = sorted({_encode(dictionary, '"Abc"@en'), _encode(dictionary, '"x\\n"@en')})
    every = U64Column(list(range(len(dictionary))))
    passed, undecided = dictionary.filter_codes("lang", "en", every)
    literals = range(*dictionary.prefix_range('"'))
    assert set(_codes(passed)) == set(tagged)
    # a non-literal code under a literal kind is decided as rdflib does, or undecided:
    # never a literal left undecided here, where `lang` decides every literal
    assert set(_codes(undecided)).isdisjoint(literals)
    passed, _ = dictionary.filter_codes("lang", "en", U64Column(tagged))
    assert _codes(passed) == tagged
    with pytest.raises(ValueError, match="sorted and unique"):
        dictionary.filter_codes("lang", "en", U64Column(tagged[::-1]))
    with pytest.raises(TypeError, match="'codes'"):
        # the candidate codes are required
        dictionary.filter_codes("lang", "en")  # ty: ignore[missing-argument]


@pytest.mark.parametrize(
    ("kind", "arg", "options", "text", "expected"),
    [
        ("regex", "^ab", {}, '"abc"', True),
        ("regex", "^AB", {"flags": "i"}, '"abc"', True),
        # rdflib's string() raises on an IRI, so the FILTER fails: decided false
        ("regex", "^ab", {}, "<http://ex.org/abc>", False),
        ("regex", "^http", {"as_str": True}, "<http://ex.org/abc>", True),
        # rdflib's STR(_:abc) is "abc": undecided under as_str
        ("regex", "^ab", {"as_str": True}, "BLANK", None),
        ("contains", '"bc"', {}, '"abc"', True),
        ("contains", '"bc"@en', {}, '"abc"', False),
        # language tags are compared as written: "b"@EN is not compatible with @en
        ("contains", '"b"@EN', {}, '"Abc"@en', False),
        ("strstarts", '"ab"', {"case": "lower"}, '"Abc"@en', True),
        ("strends", '"BC"', {"case": "upper"}, '"abc"', True),
    ],
)
def test_string_kinds(contract_store, kind, arg, options, text, expected):
    dictionary = _dictionary(VortexRdfStore(contract_store))
    code = dictionary.prefix_range("_:")[0] if text == "BLANK" else _encode(dictionary, text)
    passed, undecided = dictionary.filter_codes(kind, arg, U64Column([code]), **options)
    if expected is None:
        assert _codes(undecided) == [code] and not len(passed)
    else:
        assert not len(undecided)
        assert (code in set(_codes(passed))) is expected


def test_an_empty_keep_matches_nothing(contract_store):
    store = VortexRdfStore(contract_store)
    empty: dict[str | int, Any] = {2: U64Column([])}
    assert store.count_quads(None, "<http://ex.org/p>", None, None, keep=empty) == 0


def test_wide_integers_are_undecided(contract_store):
    dictionary = _dictionary(VortexRdfStore(contract_store))
    code = _encode(dictionary, f'"9223372036854775808"^^<{XSD}long>')
    _, undecided = dictionary.filter_codes("num_lt", f'"5"^^<{XSD}integer>', U64Column([code]))
    assert _codes(undecided) == [code]


def test_str_of_a_normalized_typed_literal_is_never_decided_from_its_stored_form(contract_store):
    """rdflib reads STR("042"^^xsd:integer) as "42": a verdict taken from the
    stored "042" would differ, so it must be undecided or rdflib's."""
    dictionary = _dictionary(VortexRdfStore(contract_store))
    code = _encode(dictionary, f'"042"^^<{XSD}integer>')
    passed, _ = dictionary.filter_codes("strstarts", '"0"', U64Column([code]), as_str=True)
    assert code not in set(_codes(passed))
    passed, undecided = dictionary.filter_codes("strstarts", '"4"', U64Column([code]), as_str=True)
    assert code in set(_codes(passed)) | set(_codes(undecided))


def test_counts_honour_limit(contract_store):
    store = VortexRdfStore(contract_store)
    assert store.count_quads(None, "<http://ex.org/p>", None, None) == 7
    assert store.count_quads(None, "<http://ex.org/p>", None, None, limit=3) == 3


def test_an_empty_dictionary_is_not_file_backed(tmp_path):
    """A store without quads has no dictionary child to map, so its
    dictionary reports file_backed False even when opened from a file."""
    nt = tmp_path / "empty.nt"
    nt.write_text("", encoding="utf-8")
    out = tmp_path / "empty.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    assert _dictionary(VortexRdfStore(str(out))).file_backed is False
