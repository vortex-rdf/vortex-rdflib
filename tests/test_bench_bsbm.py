"""The BSBM stream core (``bench/bsbm/execute.py``), its CLI (``run_stream``),
comparison, recorder and replayer, on a tiny BSBM-shaped store and on stand-in
stores that sleep."""

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bench.bsbm import cli, execute, run_stream
from tests.bsbm_tiny import tiny_streams, write_tiny_bsbm

ROOT = Path(__file__).resolve().parent.parent
needs_timer = pytest.mark.skipif(
    not hasattr(signal, "setitimer"), reason="the per-query timeout uses SIGALRM (POSIX only)"
)


@pytest.fixture(scope="module")
def tiny_store(tmp_path_factory) -> Path:
    from vortex_rdf import serialize_rdf

    root = tmp_path_factory.mktemp("tiny-bsbm")
    write_tiny_bsbm(root / "data.nt")
    out = root / "tiny.vortex"
    serialize_rdf(str(root / "data.nt"), str(out), format="ntriples", layout="dictionary")
    return out


@pytest.mark.parametrize("in_memory", [False, True])
def test_a_run_times_every_query_and_digests_its_answers(tiny_store, in_memory):
    warmup, measured = tiny_streams(1, 2)
    out = run_stream.run(str(tiny_store), in_memory, warmup, measured)
    assert out["in_memory"] is in_memory and out["code_path"] is True
    assert [r["i"] for r in out["results"]] == [q["i"] for q in measured]
    for r in out["results"]:
        assert "error" not in r and "timeout" not in r, r
        assert r["prep_ns"] > 0 and r["exec_ns"] > 0 and isinstance(r["rows"], int)
        assert len(r["digest"]) == 16
    q2 = [r for r in out["results"] if r["q"] == 2]
    assert q2 and all(r["rows"] > 0 for r in q2)
    assert len(out["rss_anon_per_mix_mb"]) == 2
    assert out["mixes_done"] == out["mixes_planned"] == 2 and out["partial"] is False


def test_the_digest_is_the_answer_not_the_run(tiny_store):
    warmup, measured = tiny_streams(1, 2)
    a = run_stream.run(str(tiny_store), False, warmup, measured)
    b = run_stream.run(str(tiny_store), True, warmup, measured)
    assert any(r["rows"] > 0 for r in a["results"])
    assert [r["digest"] for r in a["results"]] == [r["digest"] for r in b["results"]]


def test_an_xsd_string_literal_digests_as_the_simple_literal():
    from rdflib import XSD, Literal, URIRef

    s = URIRef("http://example.org/s")
    simple = execute.answer_digest([(s, Literal("x"))])
    assert execute.answer_digest([(s, Literal("x", datatype=XSD.string))]) == simple
    assert execute.answer_digest([(s, Literal("x", lang="en"))]) != simple


def test_rdflib_and_vortex_answers_have_one_digest(tiny_store):
    from rdflib import Graph

    from vortex_rdflib import VortexRdflibStore

    plain = Graph()
    plain.parse(tiny_store.parent / "data.nt", format="nt")
    vortex = Graph(store=VortexRdflibStore(str(tiny_store)))
    _, measured = tiny_streams(1, 2)
    spellings: set[str] = set()
    for q in measured:
        expected = execute.run_instance(plain, q["text"])[0]
        got = execute.run_instance(vortex, q["text"])[0]
        assert execute.answer_digest(got) == execute.answer_digest(expected), q["text"]
        spellings.update(term.n3() for row in expected for term in row if term is not None)
    # Not vacuous: rdflib's answers do spell an explicit xsd:string, vortex's do not.
    assert any("XMLSchema#string" in spelling for spelling in spellings)


def test_a_failing_query_is_recorded_and_the_stream_goes_on(tiny_store):
    warmup, measured = tiny_streams(1, 2)
    broken = [dict(measured[0], text="SELECT * WHERE {"), *measured[1:]]
    out = run_stream.run(str(tiny_store), False, warmup, broken)
    assert "error" in out["results"][0]
    assert all("error" not in r for r in out["results"][1:])


def test_main_writes_the_run_with_its_limits(tmp_path, tiny_store):
    warmup, measured = tiny_streams(1, 2)
    (tmp_path / "w.json").write_text(json.dumps(warmup))
    (tmp_path / "m.json").write_text(json.dumps(measured))
    code = run_stream.main(
        [
            str(tiny_store),
            "--file",
            str(tmp_path / "w.json"),
            str(tmp_path / "m.json"),
            str(tmp_path / "run.json"),
            "--query-timeout",
            "30",
            "--store-budget",
            "600",
        ]
    )
    assert code == 0
    out = json.loads((tmp_path / "run.json").read_text())
    assert len(out["results"]) == 50 and out["in_memory"] is False
    assert out["query_timeout_s"] == 30.0 and out["store_budget_s"] == 600.0


def test_the_shared_cli_helpers_need_a_residency_and_read_both_streams(tmp_path):
    parser = argparse.ArgumentParser()
    cli.add_residency(parser)
    assert parser.parse_args(["--file"]).in_memory is False
    assert parser.parse_args(["--in-memory"]).in_memory is True
    for argv in ([], ["--file", "--in-memory"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    warmup, measured = tiny_streams(1, 2)
    (tmp_path / "w.json").write_text(json.dumps(warmup))
    (tmp_path / "m.json").write_text(json.dumps(measured))
    streams = cli.read_streams(str(tmp_path / "w.json"), str(tmp_path / "m.json"))
    assert streams == (warmup, measured)


class SleepyStore:
    """Native-style stand-in: the query text is a number of seconds to sleep."""

    def query(self, text, **kwargs):
        time.sleep(float(text))
        return [("x",)]


def stream(seconds: list[list[float]]) -> list[dict]:
    out: list[dict] = []
    for mix, sleeps in enumerate(seconds):
        for pos, s in enumerate(sleeps):
            out.append({"mix": mix, "pos": pos, "q": pos + 1, "i": len(out), "text": str(s)})
    return out


@needs_timer
def test_a_query_over_the_timeout_is_aborted_and_the_stream_goes_on():
    run = execute.execute_stream(
        SleepyStore(), [], stream([[5, 0]]), native=True, query_timeout_s=0.2
    )
    first, second = run["results"]
    assert first["timeout"] is True and first["elapsed_ns"] < 2e9
    assert second["rows"] == 1 and "timeout" not in second


@needs_timer
def test_a_warmup_query_over_the_timeout_is_counted():
    run = execute.execute_stream(
        SleepyStore(), stream([[5]]), stream([[0]]), native=True, query_timeout_s=0.2
    )
    assert run["warmup_timeouts"] == 1 and run["results"][0]["rows"] == 1


def test_over_budget_a_store_finishes_its_mix_and_stops():
    run = execute.execute_stream(
        SleepyStore(), [], stream([[0.1, 0.1]] * 3), native=True, store_budget_s=0.15
    )
    assert run["partial"] is True and run["mixes_done"] == 1 and run["mixes_planned"] == 3
    assert len(run["results"]) == 2 and len(run["mix_ns"]) == 1


def test_without_limits_every_mix_runs():
    run = execute.execute_stream(SleepyStore(), [], stream([[0, 0]] * 3), native=True)
    assert run["mixes_done"] == 3 and run["partial"] is False and len(run["results"]) == 6


def test_a_limit_of_zero_is_off(monkeypatch):
    monkeypatch.setenv("BSBM_QUERY_TIMEOUT_S", "0")
    assert execute.limit_from_env("BSBM_QUERY_TIMEOUT_S", 5) is None
    monkeypatch.setenv("BSBM_QUERY_TIMEOUT_S", "2.5")
    assert execute.limit_from_env("BSBM_QUERY_TIMEOUT_S", 5) == 2.5
    monkeypatch.delenv("BSBM_QUERY_TIMEOUT_S")
    assert execute.limit_from_env("BSBM_QUERY_TIMEOUT_S", 5) == 5


def test_a_native_stream_never_imports_rdflib():
    code = (
        "import sys\n"
        "from bench.bsbm.execute import execute_stream\n"
        "class Store:\n"
        "    def query(self, text, **kwargs):\n"
        "        return [('x',)]\n"
        "s = [{'mix': 0, 'pos': 0, 'q': 1, 'i': 0, 'text': 'SELECT * {}'}]\n"
        "execute_stream(Store(), s, s, native=True)\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'rdflib'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
