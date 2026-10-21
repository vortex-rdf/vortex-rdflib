"""The vortex-rdf 0.13 API this package relies on, pinned where it relies on
it. A failure here means the installed vortex-rdf differs from that API."""

import ctypes
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


def _columns(answer) -> list[list[int]]:
    """The codes of each column of a native answer."""
    assert answer is not None
    return [_codes(column) for column in answer]


class _PyBuffer(ctypes.Structure):
    """CPython's ``Py_buffer``, what a buffer export fills in."""

    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("obj", ctypes.py_object),
        ("len", ctypes.c_ssize_t),
        ("itemsize", ctypes.c_ssize_t),
        ("readonly", ctypes.c_int),
        ("ndim", ctypes.c_int),
        ("format", ctypes.c_char_p),
        ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
        ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
        ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)),
        ("internal", ctypes.c_void_p),
    ]


def _address(column: U64Column) -> int:
    """Where the column's exported buffer starts in memory."""
    view = _PyBuffer()
    ctypes.pythonapi.PyObject_GetBuffer(ctypes.py_object(column), ctypes.byref(view), 0)
    try:
        assert view.buf is not None
        return view.buf
    finally:
        ctypes.pythonapi.PyBuffer_Release(ctypes.byref(view))


def test_version_is_0_13():
    assert vortex_rdf.__version__.split(".")[:2] == ["0", "13"]


#: The 0.13 names, and the members of each that the batch, chunk, cancel and
#: join code of this package calls.
_SURFACE_0_13 = {
    "VortexRdfStore": ("match_codes_many", "count_quads_many", "match_code_chunks"),
    "ProbeAnswers": ("__iter__", "__next__", "__len__"),
    "CodeChunks": ("__iter__", "__next__", "columns", "batch_rows"),
    "CancelToken": ("cancel", "cancelled"),
    "U64Column": ("slice", "join_count", "join_indices", "groups"),
    "CodeGroups": ("__len__", "count", "join", "gather"),
    "JoinTooLarge": ("pairs",),
    "Cancelled": (),
}


def test_surface_is_0_13():
    for name, members in _SURFACE_0_13.items():
        assert hasattr(vortex_rdf, name), name
        for member in members:
            assert hasattr(getattr(vortex_rdf, name), member), f"{name}.{member}"
    assert issubclass(vortex_rdf.Cancelled, vortex_rdf.VortexRdfError)
    assert issubclass(vortex_rdf.JoinTooLarge, vortex_rdf.VortexRdfError)


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


#: Patterns for the batch calls: every row of the predicate, one row, no row.
_PROBES = [
    (None, "<http://ex.org/p>", None, None),
    ("<http://ex.org/a>", None, '"abc"', None),
    ("<http://ex.org/none>", None, None, None),
]


def test_probe_answers_are_an_iterator_with_a_length(contract_store):
    store = VortexRdfStore(contract_store)
    answers = store.match_codes_many(_PROBES)
    assert isinstance(answers, vortex_rdf.ProbeAnswers)
    assert len(answers) == len(_PROBES)
    assert iter(answers) is answers
    pulled = [next(answers), *answers]
    assert len(answers) == len(_PROBES)  # the probes' number, answered or not
    with pytest.raises(StopIteration):
        next(answers)
    assert [len(_columns(answer)[0]) for answer in pulled] == [7, 1, 0]
    assert [_columns(answer) for answer in pulled] == [
        _columns(store.match_codes(*probe)) for probe in _PROBES
    ]


def test_batch_calls_take_lookahead_and_cancel(contract_store):
    store = VortexRdfStore(contract_store)
    token = vortex_rdf.CancelToken()
    assert store.count_quads_many(_PROBES, lookahead=1, cancel=token) == [7, 1, 0]
    answers = store.match_codes_many(_PROBES, lookahead=1, cancel=token)
    assert [len(_columns(answer)[0]) for answer in answers] == [7, 1, 0]
    for call in (store.count_quads_many, store.match_codes_many):
        with pytest.raises(ValueError, match="lookahead"):
            call(_PROBES, lookahead=0)
    assert store.count_quads(cancel=token) == 7
    assert len(_columns(store.match_codes(cancel=token))[0]) == 7
    assert token.cancelled is False


_CANCELLABLE = {
    "match_codes": lambda store, token: store.match_codes(cancel=token),
    "count_quads": lambda store, token: store.count_quads(cancel=token),
    "match_codes_many": lambda store, token: store.match_codes_many(_PROBES, cancel=token),
    "count_quads_many": lambda store, token: store.count_quads_many(_PROBES, cancel=token),
    "match_code_chunks": lambda store, token: store.match_code_chunks(cancel=token),
}


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize("call", _CANCELLABLE)
def test_a_cancelled_token_is_refused_at_the_call(contract_store, in_memory, call):
    store = VortexRdfStore(contract_store, in_memory=in_memory)
    token = vortex_rdf.CancelToken()
    assert token.cancelled is False
    token.cancel()
    token.cancel()  # final: cancelling again does nothing
    assert token.cancelled is True
    with pytest.raises(vortex_rdf.Cancelled):
        _CANCELLABLE[call](store, token)


def test_join_too_large_carries_the_pair_count():
    left = U64Column([1, 1, 1, 2])
    right = U64Column([1, 1, 2, 2, 2])  # 3 x 2 pairs on 1, 1 x 3 on 2
    assert left.join_count(right) == 9
    assert len(left.join_indices(right, max_pairs=9)[0]) == 9  # a join at its cap is made
    groups = right.groups()
    for join in (
        lambda: left.join_indices(right, max_pairs=8),
        lambda: groups.join(left, max_pairs=8),
        lambda: groups.gather(left, [left], [right], max_pairs=8),
    ):
        with pytest.raises(vortex_rdf.JoinTooLarge) as caught:
            join()
        assert caught.value.pairs == caught.value.args[1] == 9


def test_groups_gather_equals_join_and_take():
    left_key, left_value = U64Column([3, 1, 2, 3, 9]), U64Column([30, 10, 20, 31, 90])
    right_key, right_value = U64Column([1, 3, 3, 2, 1]), U64Column([100, 300, 301, 200, 101])
    groups = right_key.groups()
    assert isinstance(groups, vortex_rdf.CodeGroups) and len(groups) == len(right_key)
    pairs = [
        (i, j)
        for i, left in enumerate(_codes(left_key))
        for j, right in enumerate(_codes(right_key))
        if left == right
    ]
    left_rows, right_rows = groups.join(left_key)
    assert list(zip(_codes(left_rows), _codes(right_rows), strict=True)) == pairs
    assert groups.count(left_key) == len(pairs) == left_key.join_count(right_key)
    joined = left_key.join_indices(right_key)
    assert [_codes(rows) for rows in joined] == [_codes(left_rows), _codes(right_rows)]
    gathered = groups.gather(left_key, [left_key, left_value], [right_value])
    assert [_codes(column) for column in gathered] == [
        _codes(left_key.take(left_rows)),
        _codes(left_value.take(left_rows)),
        _codes(right_value.take(right_rows)),
    ]
    # the joins of consecutive slices, offset by where each starts, make the whole join
    by_slice = []
    for start, stop in ((0, 2), (2, 5)):
        rows, matches = groups.join(left_key.slice(start, stop))
        by_slice += [(start + i, j) for i, j in zip(_codes(rows), _codes(matches), strict=True)]
    assert by_slice == pairs


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_chunks_concatenate_to_match_codes(contract_store, in_memory):
    store = VortexRdfStore(contract_store, in_memory=in_memory)
    dictionary = _dictionary(store)
    wanted = sorted({_encode(dictionary, '"abc"'), _encode(dictionary, "<http://ex.org/abc>")})
    pattern = (None, "<http://ex.org/p>", None, None)
    narrowings: list[dict[str, Any]] = [
        {},
        {"limit": 5, "offset": 1},
        {"keep": {"o": U64Column(wanted)}},
    ]
    for narrowing in narrowings:
        whole = _columns(store.match_codes(*pattern, **narrowing))
        chunks = store.match_code_chunks(*pattern, batch_rows=3, **narrowing)
        assert isinstance(chunks, vortex_rdf.CodeChunks)
        assert chunks.columns == ("s", "p", "o", "g") and chunks.batch_rows == 3
        pulled = [_columns(chunk) for chunk in chunks]
        assert all(len(chunk[0]) <= 3 for chunk in pulled)
        assert [[code for chunk in pulled for code in chunk[i]] for i in range(4)] == whole
    chosen = store.match_code_chunks(*pattern, columns=("o", "s"), batch_rows=4)
    assert chosen is not None and chosen.columns == ("o", "s")
    pulled = [_columns(chunk) for chunk in chosen]
    whole = _columns(store.match_codes(*pattern))
    assert [[code for chunk in pulled for code in chunk[i]] for i in range(2)] == [
        whole[2],
        whole[0],
    ]


def test_slice_shares_memory():
    column = U64Column(list(range(100)))
    part = column.slice(10, 20)
    assert _codes(part) == list(range(10, 20))
    assert _address(part) == _address(column) + 10 * 8
    assert _address(part.slice(2, 4)) == _address(column) + 12 * 8
    assert len(column.slice(5, 5)) == 0
    with pytest.raises(IndexError):
        column.slice(20, 10)
    with pytest.raises(IndexError):
        column.slice(0, 101)
