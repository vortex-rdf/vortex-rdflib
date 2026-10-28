"""The ablation switches (vortex_rdflib.ablation): parsing and defaults, and that no switch changes
an answer.

The switches are read once at import, so each setting runs the pushdown suite's queries in its own
interpreter and the answers (as multisets) are compared with the run with no switch set."""

import json
import os
import subprocess
import sys

import pytest

from vortex_rdflib import ablation

SETTINGS = [
    {"VORTEX_RDF_CODE_PREDICATES": "0"},
    {"VORTEX_RDF_COUNT_ORDER": "0"},
    {"VORTEX_RDF_DECODE_EACH_STEP": "1"},
    {"VORTEX_RDF_DECODE_CHUNK": "1"},
    {"VORTEX_RDF_DECODE_CHUNK": "100000"},
    {"VORTEX_RDF_PROBE_BATCH": "1"},
    {"VORTEX_RDF_PROBE_BATCH": "3"},
    {"VORTEX_RDF_DISABLE_PUSHDOWN": "1"},
    {"VORTEX_RDF_NATIVE_FILTERS": "0"},
    {"VORTEX_RDF_COUNTERS": "1"},
    {
        "VORTEX_RDF_CODE_PREDICATES": "0",
        "VORTEX_RDF_COUNT_ORDER": "0",
        "VORTEX_RDF_DECODE_EACH_STEP": "1",
        "VORTEX_RDF_DECODE_CHUNK": "7",
        "VORTEX_RDF_PROBE_BATCH": "2",
        "VORTEX_RDF_NATIVE_FILTERS": "0",
    },
]

RUNNER = r"""
import json, sys
sys.path.insert(0, sys.argv[2])
from rdflib import Graph
from vortex_rdflib import VortexRdflibStore
from corpus import QUERIES
from rdflib import Literal
def lang(t):
    if isinstance(t, Literal) and t.language:
        return Literal(t, lang=t.language.lower()).n3()
    return t.n3()
out = []
for in_memory in (False, True):
    g = Graph(store=VortexRdflibStore(sys.argv[1], in_memory=in_memory))
    for q in QUERIES:
        r = g.query(q)
        if r.type == "ASK":
            out.append(repr(r.askAnswer))
        else:
            # language tags compared case-insensitively: rdflib's own evaluator
            # (VORTEX_RDF_DISABLE_PUSHDOWN=1) returns a VALUES literal as the query spells
            # its tag, the pushdown as the store does
            out.append(sorted(
                repr(sorted((str(v), lang(t)) for v, t in row.asdict().items())) for row in r
            ))
print(json.dumps(out))
"""


def test_defaults_are_todays_behaviour():
    assert ablation.flag("X", True, {}) is True and ablation.flag("X", False, {"X": ""}) is False
    assert ablation.positive_int("X", {}) is None
    if not any(name in os.environ for name in ablation.NAMES):
        assert ablation.CODE_PREDICATES and ablation.COUNT_ORDER and not ablation.DECODE_EACH_STEP
        assert ablation.DECODE_CHUNK is None and ablation.PROBE_BATCH is None


def test_parsing():
    assert (
        ablation.flag("X", True, {"X": "0"}) is False
        and ablation.flag("X", False, {"X": "1"}) is True
    )
    assert ablation.positive_int("X", {"X": "64"}) == 64
    for bad in ("yes", "2", "-1"):
        with pytest.raises(ValueError):
            ablation.flag("X", True, {"X": bad})
    for bad in ("0", "-3", "x"):
        with pytest.raises(ValueError):
            ablation.positive_int("X", {"X": bad})


def _answers(store, settings):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ablation.NAMES and not k.startswith("VORTEX_RDF_")
    }
    env.update(settings)
    done = subprocess.run(
        [sys.executable, "-c", RUNNER, str(store), os.path.dirname(__file__)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from vortex_rdf import serialize_rdf

    from corpus import FIXTURE_NT

    d = tmp_path_factory.mktemp("ablation")
    (d / "fixture.nt").write_text(FIXTURE_NT, encoding="utf-8")
    serialize_rdf(str(d / "fixture.nt"), str(d / "fixture.vortex"), layout="dictionary")
    return d / "fixture.vortex"


@pytest.fixture(scope="module")
def baseline(store):
    return _answers(store, {})


@pytest.mark.parametrize(
    "settings", SETTINGS, ids=lambda s: ",".join(f"{k}={v}" for k, v in s.items())
)
def test_switch_changes_no_answer(store, baseline, settings):
    answers = _answers(store, settings)
    assert len(answers) == len(baseline)
    wrong = [i for i, (a, b) in enumerate(zip(answers, baseline, strict=True)) if a != b]
    assert not wrong, f"{len(wrong)} queries answer differently, first: {wrong[:5]}"


def test_decode_each_step_returns_the_same_codes(store):
    from vortex_rdf import U64Column

    from vortex_rdflib import VortexRdflibStore
    from vortex_rdflib.pushdown.bgp import _decode_each_step
    from vortex_rdflib.pushdown.relation import Relation

    st = VortexRdflibStore(str(store))
    st._store()
    assert st._dict is not None
    n = len(st._dict)
    codes = list(range(n)) + [-5]
    rel = _decode_each_step(
        st, Relation(("a",), (memoryview(U64Column(list(range(n)))).cast("Q"),), None, n)
    )
    assert rel.rows is None and rel.cols is not None
    assert rel.cols[0].tolist() == list(range(n))
    rel = _decode_each_step(st, Relation(("a", "b"), None, [(c, c) for c in codes], len(codes)))
    assert rel.cols is None and rel.rows == [(c, c) for c in codes]
