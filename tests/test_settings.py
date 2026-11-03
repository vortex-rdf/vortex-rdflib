"""The environment switches (:mod:`vortex_rdflib.settings`): one parser per
kind of value, read when the pushdown is registered or a store is
constructed, never on import."""

import os
import subprocess
import sys

import pytest

from vortex_rdflib import settings
from vortex_rdflib.settings import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        (" 1 ", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("Off", False),
    ],
)
def test_flag_accepts_the_usual_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("VORTEX_RDF_X", value)
    assert settings.flag("VORTEX_RDF_X", not expected) is expected


@pytest.mark.parametrize("default", [True, False])
def test_flag_unset_or_empty_is_its_default(monkeypatch, default):
    monkeypatch.delenv("VORTEX_RDF_X", raising=False)
    assert settings.flag("VORTEX_RDF_X", default) is default
    monkeypatch.setenv("VORTEX_RDF_X", "")
    assert settings.flag("VORTEX_RDF_X", default) is default


def test_flag_refuses_anything_else(monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_X", "maybe")
    with pytest.raises(ValueError, match="VORTEX_RDF_X"):
        settings.flag("VORTEX_RDF_X", True)


def test_positive_int(monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_X", "4096")
    assert settings.positive_int("VORTEX_RDF_X") == 4096
    for bad in ("0", "-1", "x"):
        monkeypatch.setenv("VORTEX_RDF_X", bad)
        with pytest.raises(ValueError, match="VORTEX_RDF_X"):
            settings.positive_int("VORTEX_RDF_X")
    monkeypatch.delenv("VORTEX_RDF_X")
    assert settings.positive_int("VORTEX_RDF_X") is None


def _import_with(**environ) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("VORTEX_RDF")}
    env.update(environ)
    return subprocess.run(
        [sys.executable, "-c", "import vortex_rdflib"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "environ",
    [{"VORTEX_RDF_COUNT_ORDER": "maybe"}, {"VORTEX_RDF_PAIR_BUDGET": "-1"}],
    ids=["count-order", "pair-budget"],
)
def test_import_never_raises(environ):
    done = _import_with(**environ)
    assert done.returncode == 0, done.stderr


def test_a_bad_switch_raises_when_read(monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_COUNT_ORDER", "maybe")
    with pytest.raises(ValueError, match="VORTEX_RDF_COUNT_ORDER"):
        settings.apply_switches(Settings())


def test_disable_pushdown_true(monkeypatch):
    monkeypatch.setenv("VORTEX_RDF_DISABLE_PUSHDOWN", "true")
    assert settings.pushdown_disabled()


#: Each switch :func:`settings.apply_switches` reads, a value for it, and the
#: settings it leaves.
SWITCHES = [
    ("VORTEX_RDF_PUSHDOWN_OPS", "bgp", {"enabled_ops": frozenset({"BGP"})}),
    ("VORTEX_RDF_FILTER_FAST", "false", {"fast_filters": False}),
    ("VORTEX_RDF_NATIVE_FILTERS", "off", {"native_filters": False}),
    ("VORTEX_RDF_CODE_PREDICATES", "0", {"code_predicates": False}),
    ("VORTEX_RDF_COUNT_ORDER", "no", {"count_order": False}),
    ("VORTEX_RDF_DECODE_EACH_STEP", "1", {"decode_each_step": True}),
    ("VORTEX_RDF_COUNTERS", "yes", {"counters": True}),
    ("VORTEX_RDF_TRACE_TRIPLES", "on", {"trace_triples": True}),
    ("VORTEX_RDF_PAIR_BUDGET", "0", {"pair_budget": 0}),
    ("VORTEX_RDF_DECODE_CHUNK", "7", {"chunk_start": 7, "chunk_max": 7}),
    (
        "VORTEX_RDF_PROBE_BATCH",
        "3",
        {"probe_batch": 3, "cheap_batch_min": 1, "scan_batch_min": 1, "memory_scan_batch_min": 1},
    ),
]


@pytest.mark.parametrize(("name", "value", "expected"), SWITCHES, ids=[s[0] for s in SWITCHES])
def test_each_switch_sets_its_settings(monkeypatch, name, value, expected):
    for other, _, _ in SWITCHES:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, value)
    target = Settings()
    settings.apply_switches(target)
    assert {field: getattr(target, field) for field in expected} == expected
    untouched = Settings()
    for field in untouched.__slots__:
        if field not in expected:
            assert getattr(target, field) == getattr(untouched, field), field


def test_a_switch_left_unset_changes_nothing(monkeypatch):
    for name, _, _ in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    target = Settings(fast_filters=False, pair_budget=5)
    settings.apply_switches(target)
    assert (target.fast_filters, target.pair_budget) == (False, 5)


def test_switches_are_read_when_a_store_is_built(tmp_path, monkeypatch):
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore
    from vortex_rdflib.settings import SETTINGS

    source = tmp_path / "one.nt"
    source.write_text("<http://ex.org/s> <http://ex.org/p> <http://ex.org/o> .\n", encoding="utf-8")
    serialize_rdf(str(source), str(tmp_path / "one.vortex"), layout="dictionary")
    monkeypatch.setattr(SETTINGS, "trace_triples", False)
    monkeypatch.setenv("VORTEX_RDF_DISABLE_PUSHDOWN", "1")
    monkeypatch.setenv("VORTEX_RDF_TRACE_TRIPLES", "1")
    VortexRdflibStore(str(tmp_path / "one.vortex"))
    assert SETTINGS.trace_triples


def test_a_store_reads_the_switches_once(tmp_path, monkeypatch):
    """Constructing a store reads every switch once, its registration of the
    pushdown included: a store opens in tens of microseconds, and each
    switch read is part of that."""
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore

    source = tmp_path / "one.nt"
    source.write_text("<http://ex.org/s> <http://ex.org/p> <http://ex.org/o> .\n", encoding="utf-8")
    serialize_rdf(str(source), str(tmp_path / "one.vortex"), layout="dictionary")
    reads: list[str] = []
    apply = settings.apply_switches
    monkeypatch.setattr(settings, "apply_switches", lambda *a: reads.append("all") or apply(*a))
    for disabled in ("0", "1"):
        monkeypatch.setenv("VORTEX_RDF_DISABLE_PUSHDOWN", disabled)
        reads.clear()
        VortexRdflibStore(str(tmp_path / "one.vortex"))
        assert reads == ["all"], disabled


def test_switches_see_the_environment_as_it_changes(monkeypatch):
    """A switch is read from the environment as it is when read: set, changed
    and removed through ``os.environ`` alike."""
    monkeypatch.delenv("VORTEX_RDF_X", raising=False)
    assert settings.flag("VORTEX_RDF_X", False) is False
    os.environ["VORTEX_RDF_X"] = "on"
    try:
        assert settings.flag("VORTEX_RDF_X", False) is True
        os.environ["VORTEX_RDF_X"] = "off"
        assert settings.flag("VORTEX_RDF_X", True) is False
    finally:
        del os.environ["VORTEX_RDF_X"]
    assert settings.flag("VORTEX_RDF_X", True) is True
    monkeypatch.setenv("VORTEX_RDF_PROBE_BATCH", "7")
    assert settings.positive_int("VORTEX_RDF_PROBE_BATCH") == 7


def test_a_size_switch_takes_digits_only(monkeypatch):
    """A size is written in ASCII digits: ``int()``'s other spellings (an
    underscore, a sign, another script's digits) are refused, naming the
    variable; blanks around the digits are not part of the value."""
    for bad in ("1_000", "+5", "٣"):
        monkeypatch.setenv("VORTEX_RDF_PROBE_BATCH", bad)
        with pytest.raises(ValueError, match="VORTEX_RDF_PROBE_BATCH"):
            settings.apply_switches(Settings())
    monkeypatch.setenv("VORTEX_RDF_PROBE_BATCH", " 12 ")
    target = Settings()
    settings.apply_switches(target)
    assert target.probe_batch == 12
