"""What a store knows about the file it has open (:mod:`vortex_rdflib.open_file`),
how its constructor reads its arguments, and that a store without term codes
answers as one with them."""

import pytest
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.term import URIRef
from tests.conftest import GRAPH_1, GRAPH_2
from tests.support import file_codes

from vortex_rdflib import VortexRdflibStore
from vortex_rdflib.open_file import UnionFacts
from vortex_rdflib.terms import KindRanges


def test_unknown_keyword_raises(vortex_files):
    """A misspelt keyword is an error, not a store opened some other way."""
    with pytest.raises(TypeError, match="in_memroy"):
        VortexRdflibStore(str(vortex_files["dictionary"]), in_memroy=True)  # ty: ignore[unknown-argument]


def test_layout_and_backend_are_gone(vortex_files):
    """The layout is the file's, and there is one backend: neither is an
    argument any more."""
    path = str(vortex_files["dictionary"])
    with pytest.raises(TypeError):
        VortexRdflibStore(None, None, path, "dictionary")  # ty: ignore[too-many-positional-arguments]
    with pytest.raises(TypeError, match="backend"):
        VortexRdflibStore(path, backend="native")  # ty: ignore[unknown-argument]


def test_layout_reads_the_open_file(vortex_files):
    store = VortexRdflibStore(str(vortex_files["dictionary"]))
    assert store.layout == "dictionary"
    assert VortexRdflibStore(str(vortex_files["default"])).layout == "default"
    store.close()
    assert store.layout is None


def test_close_drops_every_file_fact(vortex_quad_files):
    """``close()`` drops the open file's record whole, and the next open
    learns the file again: its union facts are read anew."""
    path = str(vortex_quad_files["dictionary"])
    store = VortexRdflibStore(path)
    assert store._union_has_repeats()
    first = store._file
    assert first is not None and first.union == UnionFacts(has_repeats=True, size=5)
    store.close()
    assert store._file is None
    store.open(path)
    reopened = store._file
    assert reopened is not None and reopened is not first and reopened.union is None
    assert store._union_has_repeats() and reopened.union == UnionFacts(True, 5)


def test_kind_of_outside_every_range(vortex_files):
    """A code past the dictionary's end, or a negative one, is of no kind,
    as is the default graph's empty name; each other code inside it is of
    the kind its spelling starts with."""
    store = VortexRdflibStore(str(vortex_files["dictionary"]))
    term_dict = file_codes(store).term_dict
    assert term_dict is not None
    ranges = KindRanges.from_dictionary(term_dict)
    assert ranges.kind_of(len(term_dict)) == "" and ranges.kind_of(-1) == ""
    starts = {"literal": '"', "iri": "<", "blank": "_:"}
    for code in range(len(term_dict)):
        kind, spelled = ranges.kind_of(code), term_dict.decode(code)
        if kind == "":
            assert spelled == ""
            continue
        lo, hi = ranges.range(kind)
        assert lo <= code < hi and spelled is not None and spelled.startswith(starts[kind])


_VIEWS = [None, URIRef(GRAPH_1), URIRef(GRAPH_2), DATASET_DEFAULT_GRAPH_ID]


def _answers(store) -> dict:
    """Every store call's answer over each view of the file: the union, each
    named graph and the default graph."""

    def rows(view) -> list:
        return sorted(
            (triple, sorted(str(graph.identifier) for graph in contexts))
            for triple, contexts in store.triples((None, None, None), view)
        )

    return {
        "triples": [rows(view) for view in _VIEWS],
        "contexts": sorted(str(graph.identifier) for graph in store.contexts()),
        "len": [store.__len__(view) for view in _VIEWS],
    }


@pytest.mark.parametrize("variant", ["default", "typed-object", "code-path-off"])
def test_stores_without_codes_answer_alike(vortex_quad_files, monkeypatch, variant):
    """Review Focus 4: a store that reads no term codes — a layout without
    them, or the code path switched off — answers a multi-graph file as the
    dictionary store does: its matches and their graphs, its graphs, and
    the size of each view, the union's (a set) included."""
    expected = _answers(VortexRdflibStore(str(vortex_quad_files["dictionary"])))
    if variant == "code-path-off":
        monkeypatch.setenv("VORTEX_RDF_DISABLE_CODE_PATH", "1")
        store = VortexRdflibStore(str(vortex_quad_files["dictionary"]))
    else:
        store = VortexRdflibStore(str(vortex_quad_files[variant]))
    assert store._opened().codes is None
    assert expected["len"] == [5, 2, 2, 2]
    assert _answers(store) == expected


def test_the_code_path_is_one_gate(vortex_files):
    """Whether a store reads term codes is one fact of its open file:
    ``codes`` is the file's dictionary and what it answers, or ``None`` —
    a layout without one."""
    opened = VortexRdflibStore(str(vortex_files["dictionary"]))._opened()
    codes = opened.codes
    assert codes is not None
    lo, hi = codes.kind_ranges.range("iri")
    assert codes.spelling(lo).startswith("<") and codes.spellings([lo, hi - 1])[1].startswith("<")
    assert codes.default_graph_code == codes.term_dict.encode("")
    with pytest.raises(ValueError, match="not in the store dictionary"):
        codes.spelling(len(codes.term_dict) + 5)
    assert VortexRdflibStore(str(vortex_files["default"]))._opened().codes is None


def test_a_lazy_result_read_after_close_still_answers(vortex_files, monkeypatch):
    """``close()`` drops the open file; whatever reads it after — the rest
    of a lazy result, decoded a row at a time, and ``len()`` — opens the
    file again rather than finding the record gone."""
    from rdflib import Graph

    from vortex_rdflib import register_sparql_pushdown
    from vortex_rdflib.settings import SETTINGS

    monkeypatch.setattr(SETTINGS, "chunk_start", 1)
    monkeypatch.setattr(SETTINGS, "chunk_max", 1)
    register_sparql_pushdown()
    path = str(vortex_files["dictionary"])
    query = "SELECT ?s ?o WHERE { ?s ?p ?o FILTER(!isBlank(?o)) }"
    expected = sorted(Graph(store=VortexRdflibStore(path)).query(query))
    store = VortexRdflibStore(path)
    rows = iter(Graph(store=store).query(query))
    first = next(rows)
    store.close()
    assert sorted([first, *rows]) == expected and len(expected) > 2
    store.close()
    assert len(store) == len(VortexRdflibStore(path))
