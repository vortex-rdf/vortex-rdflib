"""The ablation switches (:mod:`vortex_rdflib.ablation`): each changes how
the pushdown answers, never what it answers. Each setting runs the pushdown
suite's queries over the fixture, file-backed and in memory, with its
:data:`~vortex_rdflib.settings.SETTINGS` fields set — as its environment
variable sets them (``tests/test_settings.py``) — and the answers (as
multisets) are compared with the run with none set."""

import pytest
from rdflib import Graph, Literal
from tests.corpus import FIXTURE_NT, QUERIES, RDFLIB_DIVERGES
from tests.support import file_codes
from vortex_rdf import serialize_rdf

from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown, unregister_sparql_pushdown
from vortex_rdflib.settings import SETTINGS


def _batched(n: int) -> dict:
    """``VORTEX_RDF_PROBE_BATCH=n``: ``n`` probes a call, every step batched."""
    return {"probe_batch": n, "cheap_batch_min": 1, "scan_batch_min": 1, "memory_scan_batch_min": 1}


#: Each ablation, by the switch that sets it: the settings it changes.
ABLATIONS = {
    "VORTEX_RDF_CODE_PREDICATES=0": {"code_predicates": False},
    "VORTEX_RDF_COUNT_ORDER=0": {"count_order": False},
    "VORTEX_RDF_DECODE_EACH_STEP=1": {"decode_each_step": True},
    "VORTEX_RDF_DECODE_CHUNK=1": {"chunk_start": 1, "chunk_max": 1},
    "VORTEX_RDF_DECODE_CHUNK=100000": {"chunk_start": 100_000, "chunk_max": 100_000},
    "VORTEX_RDF_PROBE_BATCH=1": _batched(1),
    "VORTEX_RDF_PROBE_BATCH=3": _batched(3),
    "VORTEX_RDF_NATIVE_FILTERS=0": {"native_filters": False},
    "VORTEX_RDF_COUNTERS=1": {"counters": True},
    "all-at-once": {
        "code_predicates": False,
        "count_order": False,
        "decode_each_step": True,
        "chunk_start": 7,
        "chunk_max": 7,
        **_batched(2),
        "native_filters": False,
    },
}


def _spelled(term) -> str:
    """A term's N-Triples spelling, its language tag in lower case: rdflib's
    own evaluator returns a VALUES literal as the query spells its tag, the
    pushdown as the store does."""
    if isinstance(term, Literal) and term.language:
        return Literal(term, lang=term.language.lower()).n3()
    return term.n3()


def _answers(path) -> list:
    """Every corpus query's answer, file-backed then in memory: the ASK
    boolean, or the solutions as a sorted multiset. The stores are built
    under the settings in force, as an application builds them."""
    out = []
    for in_memory in (False, True):
        graph = Graph(store=VortexRdflibStore(str(path), in_memory=in_memory))
        for sparql in QUERIES:
            result = graph.query(sparql)
            if result.type == "ASK":
                out.append(result.askAnswer)
            else:
                out.append(
                    sorted(
                        repr(sorted((str(v), _spelled(t)) for v, t in row.asdict().items()))
                        for row in result
                    )
                )
    return out


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    d = tmp_path_factory.mktemp("ablation")
    (d / "fixture.nt").write_text(FIXTURE_NT, encoding="utf-8")
    serialize_rdf(str(d / "fixture.nt"), str(d / "fixture.vortex"), layout="dictionary")
    return d / "fixture.vortex"


@pytest.fixture(scope="module")
def baseline(store):
    register_sparql_pushdown()
    return _answers(store)


def _differences(answers: list, baseline: list, rdflib_answers: bool = False) -> list:
    """The indexes of the queries answered differently from the baseline —
    with the pushdown off, but for those on which rdflib breaks SPARQL 1.1
    (``RDFLIB_DIVERGES``, whose answers ``test_pushdown`` checks)."""
    queries = QUERIES * 2  # file-backed, then in memory
    assert len(answers) == len(baseline)
    return [
        i
        for i, (a, b) in enumerate(zip(answers, baseline, strict=True))
        if a != b and not (rdflib_answers and " ".join(queries[i].split()) in RDFLIB_DIVERGES)
    ]


@pytest.mark.parametrize("ablation", list(ABLATIONS))
def test_switch_changes_no_answer(store, baseline, monkeypatch, ablation):
    for name, value in ABLATIONS[ablation].items():
        monkeypatch.setattr(SETTINGS, name, value)
    wrong = _differences(_answers(store), baseline)
    assert not wrong, f"{len(wrong)} queries answer differently, first: {wrong[:5]}"


def test_disabling_the_pushdown_changes_only_what_rdflib_breaks(store, baseline, monkeypatch):
    """``VORTEX_RDF_DISABLE_PUSHDOWN=1``: rdflib's evaluator answers every
    query — the stores built under it register no hook — and differs on the
    queries where it breaks the specification, and only there."""
    monkeypatch.setenv("VORTEX_RDF_DISABLE_PUSHDOWN", "1")
    unregister_sparql_pushdown()
    try:
        answers = _answers(store)
    finally:
        monkeypatch.delenv("VORTEX_RDF_DISABLE_PUSHDOWN")
        register_sparql_pushdown()
    assert _differences(answers, baseline), "rdflib answered every query as the pushdown"
    wrong = _differences(answers, baseline, rdflib_answers=True)
    assert not wrong, f"{len(wrong)} queries answer differently, first: {wrong[:5]}"


def test_decode_each_step_returns_the_same_codes(store):
    from vortex_rdf import U64Column

    from vortex_rdflib import VortexRdflibStore
    from vortex_rdflib.pushdown.relation import Relation, _decode_each_step

    st = VortexRdflibStore(str(store))
    st._opened()
    assert st._opened().codes is not None
    n = len(file_codes(st).term_dict)
    codes = list(range(n)) + [-5]
    rel = _decode_each_step(
        st, Relation(("a",), (memoryview(U64Column(list(range(n)))).cast("Q"),), None, n)
    )
    assert rel.rows is None and rel.cols is not None
    assert rel.cols[0].tolist() == list(range(n))
    rel = _decode_each_step(st, Relation(("a", "b"), None, [(c, c) for c in codes], len(codes)))
    assert rel.cols is None and rel.rows == [(c, c) for c in codes]
