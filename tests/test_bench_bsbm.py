"""The BSBM stream core (``bench/bsbm/execute.py``), its CLI (``run_stream``),
comparison, recorder and replayer, on a tiny BSBM-shaped store and on stand-in
stores that sleep."""

import argparse
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bench.bsbm import cli, compare, execute, record_native, replay_native, run_stream, streams
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


def test_a_zero_limit_on_the_command_line_is_no_limit(tmp_path, tiny_store):
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
            "0",
            "--store-budget",
            "0",
        ]
    )
    assert code == 0
    out = json.loads((tmp_path / "run.json").read_text())
    assert out["partial"] is False and out["mixes_done"] == out["mixes_planned"] == 2
    assert out["query_timeout_s"] is None and out["store_budget_s"] is None


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
    # QMpH rule: a timed-out query's time, up to the abort, counts in its mix's time
    assert run["mix_ns"][first["mix"]] >= first["elapsed_ns"]


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


class AnswerStore:
    """Native-style stand-in: ``query`` returns what ``make()`` builds, a new answer each time."""

    def __init__(self, make):
        self.make = make

    def query(self, text, **kwargs):
        return self.make()


class AskAnswer:
    """A native ASK answer: it has a truth value and cannot be iterated."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def __bool__(self) -> bool:
        return self.answer


def rows_then_type_error():
    yield ("x",)
    raise TypeError("not a row")


def test_a_native_type_error_mid_iteration_is_an_error_not_an_ask_answer():
    store = AnswerStore(rows_then_type_error)
    with pytest.raises(TypeError, match="not a row"):
        execute.run_instance(store, "SELECT * {}", native=True)
    first = execute.execute_stream(store, [], stream([[0]]), native=True)["results"][0]
    assert first["error"] == "TypeError: not a row" and "rows" not in first


@pytest.mark.parametrize("answer", [True, False])
def test_a_native_ask_answer_is_one_boolean_row(answer):
    store = AnswerStore(lambda: AskAnswer(answer))
    rows, prep_ns, _exec_ns = execute.run_instance(store, "ASK {}", native=True)
    assert len(rows) == 1 and rows[0] is answer and prep_ns is None


def _run_file(
    path: Path,
    times_ms: dict[int, float],
    q_of: dict[int, int],
    digest: dict[int, str],
    rss: list[int],
) -> str:
    results = [
        {
            "q": q_of[i],
            "i": i,
            "mix": i // 2,
            "prep_ns": 0,
            "exec_ns": int(ms * 1e6),
            "rows": 1,
            "digest": digest[i],
        }
        for i, ms in times_ms.items()
    ]
    path.write_text(
        json.dumps(
            {
                "vortex_rdflib": "x",
                "vortex_rdf": "y",
                "peak_anon_mb": max(rss),
                "rss_anon_per_mix_mb": rss,
                "results": results,
            }
        )
    )
    return str(path)


def test_ratios_are_paired_by_instance(tmp_path):
    q_of, same = {0: 1, 1: 1, 2: 2, 3: 2}, {i: "d" for i in range(4)}
    a = _run_file(tmp_path / "a.json", {0: 10, 1: 10, 2: 4, 3: 4}, q_of, same, [100, 100])
    b = _run_file(tmp_path / "b.json", {0: 40, 1: 20, 2: 4, 3: 2}, q_of, same, [200, 205])
    out = compare.compare(compare.load([a]), compare.load([b]))
    t1, t2 = out["templates"][1], out["templates"][2]
    assert t1["ratio_of_means"] == pytest.approx(3.0) and t1["geo"] == pytest.approx(math.sqrt(8))
    assert t1["tail3"] == 1 and t2["median_ratio"] == pytest.approx(0.75)
    assert out["answers_identical"] == 4 and out["mismatches"] == []
    assert out["a_peak_anon_mb"] == 100 and out["b_peak_anon_mb"] == 205


def test_rounds_keep_the_best_time_per_instance(tmp_path):
    a1 = _run_file(tmp_path / "a1.json", {0: 30}, {0: 1}, {0: "d"}, [1])
    a2 = _run_file(tmp_path / "a2.json", {0: 10}, {0: 1}, {0: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 20}, {0: 1}, {0: "d"}, [1])
    assert compare.compare(compare.load([a1, a2]), compare.load([b]))["templates"][1][
        "ratio_of_means"
    ] == pytest.approx(2.0)


def test_a_different_answer_or_an_error_is_a_mismatch(tmp_path):
    a = _run_file(tmp_path / "a.json", {0: 1, 1: 1}, {0: 1, 1: 1}, {0: "x", 1: "y"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 1, 1: 1}, {0: 1, 1: 1}, {0: "x", 1: "z"}, [1])
    data = json.loads(Path(b).read_text())
    data["results"][0] = {"q": 1, "i": 0, "mix": 0, "error": "SPARQLError: boom"}
    Path(b).write_text(json.dumps(data))
    assert compare.compare(compare.load([a]), compare.load([b]))["mismatches"] == [
        0,
        1,
    ]


def test_a_timeout_is_neither_paired_nor_a_mismatch(tmp_path):
    a = _run_file(tmp_path / "a.json", {0: 10, 1: 10}, {0: 1, 1: 1}, {0: "d", 1: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 20, 1: 20}, {0: 1, 1: 1}, {0: "d", 1: "d"}, [1])
    data = json.loads(Path(b).read_text())
    data["results"][1] = {
        "q": 1,
        "i": 1,
        "mix": 0,
        "timeout": True,
        "elapsed_ns": 5_000_000_000,
    }
    Path(b).write_text(json.dumps(data))
    out = compare.compare(compare.load([a]), compare.load([b]))
    assert out["timeouts"] == [1] and out["mismatches"] == []
    assert out["answers_total"] == 1 and out["templates"][1]["n"] == 1


def test_instances_one_side_never_reached_are_unpaired(tmp_path):
    a = _run_file(tmp_path / "a.json", {0: 10, 1: 10}, {0: 1, 1: 1}, {0: "d", 1: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 20}, {0: 1}, {0: "d"}, [1])
    out = compare.compare(compare.load([a]), compare.load([b]))
    assert out["unpaired"] == [1] and out["mismatches"] == [] and out["answers_total"] == 1


def test_flatness_compares_the_first_and_last_ten_mixes(tmp_path):
    flat = _run_file(tmp_path / "f.json", {0: 1}, {0: 1}, {0: "d"}, [100] * 10 + [104] * 10)
    grows = _run_file(tmp_path / "g.json", {0: 1}, {0: 1}, {0: "d"}, [100] * 10 + [130] * 10)
    a = compare.load([flat])
    assert compare.compare(a, compare.load([flat]))["b_flat"] is True
    assert compare.compare(a, compare.load([grows]))["b_flat"] is False


def test_main_prints_and_writes_json(tmp_path, capsys):
    a = _run_file(tmp_path / "a.json", {0: 10}, {0: 1}, {0: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 5}, {0: 1}, {0: "d"}, [1])
    assert compare.main([a, b, "--json", str(tmp_path / "c.json")]) == 0
    assert "Q1" in capsys.readouterr().out
    assert json.loads((tmp_path / "c.json").read_text())["templates"]["1"]["ratio_of_means"] == 0.5


def test_geo_all_is_geometric_mean_of_all_ratios(tmp_path):
    q_of, same = {0: 1, 1: 1}, {i: "d" for i in range(2)}
    a = _run_file(tmp_path / "a.json", {0: 10, 1: 20}, q_of, same, [100, 100])
    b = _run_file(tmp_path / "b.json", {0: 20, 1: 40}, q_of, same, [100, 100])
    out = compare.compare(compare.load([a]), compare.load([b]))
    assert math.isclose(out["geo_all"], 2.0)


def test_flatness_per_round_not_last_file(tmp_path):
    leaky = _run_file(tmp_path / "leaky.json", {0: 1}, {0: 1}, {0: "d"}, [100] * 10 + [130] * 10)
    steady = _run_file(tmp_path / "steady.json", {0: 1}, {0: 1}, {0: "d"}, [100] * 10 + [104] * 10)
    assert compare.compare(compare.load([leaky]), compare.load([leaky, steady]))["b_flat"] is False
    assert compare.compare(compare.load([steady]), compare.load([steady, leaky]))["b_flat"] is False


def test_template_disagreement_is_a_mismatch(tmp_path):
    a = _run_file(tmp_path / "a.json", {0: 1}, {0: 1}, {0: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 1}, {0: 2}, {0: "d"}, [1])
    out = compare.compare(compare.load([a]), compare.load([b]))
    assert 0 in out["mismatches"]


def test_rounds_best_first_order(tmp_path):
    a1 = _run_file(tmp_path / "a1.json", {0: 10}, {0: 1}, {0: "d"}, [1])
    a2 = _run_file(tmp_path / "a2.json", {0: 30}, {0: 1}, {0: "d"}, [1])
    b = _run_file(tmp_path / "b.json", {0: 20}, {0: 1}, {0: "d"}, [1])
    assert compare.compare(compare.load([a1, a2]), compare.load([b]))["templates"][1][
        "ratio_of_means"
    ] == pytest.approx(2.0)


def test_b_flat_is_none_with_fewer_than_20_readings(tmp_path):
    a = _run_file(tmp_path / "a.json", {0: 1}, {0: 1}, {0: "d"}, [1, 2])
    b = _run_file(tmp_path / "b.json", {0: 1}, {0: 1}, {0: "d"}, [1, 2])
    out = compare.compare(compare.load([a]), compare.load([b]))
    assert out["b_flat"] is None


def test_values_round_trip_through_the_trace_encoding():
    from vortex_rdf import U32Column

    for value in (None, 3, "x", (None, "<p>", None, None), range(2, 9), [1, (2, 3)]):
        assert replay_native.decode_value(record_native.encode_value(value)) == value
    codes = record_native.encode_value(U32Column([1, 4, 9]))
    assert codes == {"codes": [1, 4, 9]}
    assert list(memoryview(replay_native.decode_value(codes)).cast("I")) == [1, 4, 9]


def test_a_recorded_stream_replays_and_is_timed(tmp_path, tiny_store):
    warmup, measured = tiny_streams(1, 2)
    trace = tmp_path / "trace.jsonl"
    calls = record_native.record(str(tiny_store), False, warmup, measured, str(trace))
    assert calls > 0
    lines = trace.read_text().splitlines()
    assert json.loads(lines[0])["trace"] == 1 and len(lines) == 1 + len(measured)
    methods = {c[1] for line in lines[1:] for c in json.loads(line)["calls"]}
    assert "match_codes" in methods or "count_quads" in methods
    out = replay_native.replay(str(tiny_store), str(trace), False, skip=set(), passes=1)
    assert sum(m["calls"] for m in out["methods"].values()) == calls
    assert set(out["templates"]) == set(streams.EXPLORE_MIX)


def test_skipped_methods_are_not_replayed(tmp_path, tiny_store):
    warmup, measured = tiny_streams(1, 2)
    trace = tmp_path / "trace.jsonl"
    record_native.record(str(tiny_store), False, warmup, measured, str(trace))
    lines = trace.read_text().splitlines()
    assert "decode_many" in {c[1] for line in lines[1:] for c in json.loads(line)["calls"]}
    out = replay_native.replay(str(tiny_store), str(trace), False, skip={"decode_many"}, passes=1)
    assert "decode_many" not in out["methods"]


def test_a_call_the_installed_api_rejects_is_counted_not_fatal(tmp_path, tiny_store):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps({"trace": 1, "store": str(tiny_store), "in_memory": False, "vortex_rdf": "?"})
        + "\n"
        + json.dumps({"i": 0, "q": 1, "calls": [["store", "count_quads", [], {"bogus": 1}, 0]]})
        + "\n"
    )
    out = replay_native.replay(str(tiny_store), str(trace), False, skip=set(), passes=1)
    assert out["methods"]["count_quads"]["errors"] == 1


def test_the_int_positions_of_a_keep_survive_the_json_trace():
    call = {"keep": {2: (1, 5), 3: range(1, 9)}, "limit": 3}
    wire = json.loads(json.dumps(record_native.encode_value(call)))
    assert wire["keep"] == {"2": {"tuple": [1, 5]}, "3": {"range": [1, 9]}}
    assert replay_native.decode_value(wire) == call


@pytest.mark.parametrize("in_memory", [False, True])
def test_a_recorded_stream_replays_without_a_rejected_call(tmp_path, tiny_store, in_memory):
    warmup, measured = tiny_streams(1, 2)
    trace = tmp_path / "trace.jsonl"
    record_native.record(str(tiny_store), in_memory, warmup, measured, str(trace))
    lines = trace.read_text().splitlines()
    assert json.loads(lines[0])["in_memory"] is in_memory
    calls = [c for line in lines[1:] for c in json.loads(line)["calls"]]
    assert any(c[3].get("keep") for c in calls), "no keep-narrowed match was recorded"
    out = replay_native.replay(str(tiny_store), str(trace), in_memory, skip=set(), passes=1)
    assert all(m["errors"] == 0 for m in out["methods"].values()), out["methods"]


def test_the_command_lines_record_then_replay(tmp_path, tiny_store, capsys):
    warmup, measured = tiny_streams(1, 2)
    (tmp_path / "w.json").write_text(json.dumps(warmup))
    (tmp_path / "m.json").write_text(json.dumps(measured))
    trace, report = tmp_path / "trace.jsonl", tmp_path / "replay.json"
    argv = [str(tiny_store), "--file", str(tmp_path / "w.json"), str(tmp_path / "m.json")]
    assert record_native.main([*argv, str(trace)]) == 0
    assert f"over {len(measured)} queries (0 raised)" in capsys.readouterr().err
    assert replay_native.main([str(tiny_store), str(trace), "--file", "--json", str(report)]) == 0
    printed = capsys.readouterr().out
    assert "skipped: filter_codes" in printed and "Q12" in printed
    assert "first:" not in printed  # no method erred
    out = json.loads(report.read_text())
    assert "filter_codes" not in out["methods"]
    assert set(out["templates"]) == {str(q) for q in streams.EXPLORE_MIX}
    with pytest.raises(SystemExit):  # one residency is required
        replay_native.main([str(tiny_store), str(trace)])


def _trace_with(tmp_path: Path, store: Path, calls: list) -> str:
    """A one-query trace of ``calls``, as ``record_native`` writes one."""
    trace = tmp_path / "trace.jsonl"
    header = {"trace": 1, "store": str(store), "in_memory": False, "vortex_rdf": "?"}
    trace.write_text(
        json.dumps(header) + "\n" + json.dumps({"i": 0, "q": 1, "calls": calls}) + "\n"
    )
    return str(trace)


def test_an_out_of_range_argument_is_counted_not_fatal(tmp_path, tiny_store):
    trace = _trace_with(tmp_path, tiny_store, [["store", "count_quads", [], {"limit": -1}, 0]])
    out = replay_native.replay(str(tiny_store), trace, False, skip=set(), passes=2)
    stats = out["methods"]["count_quads"]
    assert stats["calls"] == 1 and stats["errors"] == 1
    assert stats["first_error"].startswith("OverflowError")


def test_a_method_the_installed_api_lacks_is_counted_not_fatal(tmp_path, tiny_store):
    trace = _trace_with(tmp_path, tiny_store, [["store", "no_such_method", [1], {}, 0]])
    out = replay_native.replay(str(tiny_store), trace, False, skip=set(), passes=1)
    assert out["methods"]["no_such_method"] == {
        "calls": 1,
        "ms": 0.0,
        "errors": 1,
        "first_error": "missing: no_such_method",
    }


def test_a_method_keeps_the_first_of_its_errors(tmp_path, tiny_store):
    calls = [
        ["store", "count_quads", [], {}, 0],
        ["store", "count_quads", [], {"limit": -1}, 0],
        ["store", "count_quads", [], {"bogus": 1}, 0],
        ["dict", "decode", [1], {}, 0],
    ]
    trace = _trace_with(tmp_path, tiny_store, calls)
    out = replay_native.replay(str(tiny_store), trace, False, skip=set(), passes=1)
    count_quads, decode = out["methods"]["count_quads"], out["methods"]["decode"]
    assert count_quads["calls"] == 3 and count_quads["errors"] == 2
    assert count_quads["first_error"].startswith("OverflowError")
    assert decode["errors"] == 0 and decode["first_error"] is None


def test_a_keyword_named_like_an_encoding_is_still_a_keyword(tmp_path, tiny_store):
    from vortex_rdf import U32Column

    kwargs = record_native.encode_value({"codes": U32Column([1, 4, 9])})
    assert kwargs == {"codes": {"codes": [1, 4, 9]}}
    trace = _trace_with(tmp_path, tiny_store, [["dict", "decode_many", [], kwargs, 0]])
    out = replay_native.replay(str(tiny_store), trace, False, skip=set(), passes=1)
    assert out["methods"]["decode_many"]["calls"] == 1
    assert out["methods"]["decode_many"]["errors"] == 0


def test_the_replay_table_says_why_a_method_erred(tmp_path, tiny_store, capsys):
    trace = _trace_with(tmp_path, tiny_store, [["store", "count_quads", [], {"limit": -1}, 0]])
    assert replay_native.main([str(tiny_store), trace, "--file", "--passes", "1"]) == 0
    row = next(r for r in capsys.readouterr().out.splitlines() if "count_quads" in r)
    assert "errors 1" in row and "OverflowError" in row


def test_a_query_that_raises_is_counted_and_the_stream_goes_on(tmp_path, tiny_store, capsys):
    warmup, measured = tiny_streams(1, 2)
    broken = [dict(measured[0], text="SELECT * WHERE {"), *measured[1:]]
    trace = tmp_path / "trace.jsonl"
    calls = record_native.record(str(tiny_store), False, warmup, broken, str(trace))
    lines = trace.read_text().splitlines()
    assert len(lines) == 1 + len(broken) and json.loads(lines[1])["calls"] == []
    assert calls > 0
    assert f"over {len(broken)} queries (1 raised)" in capsys.readouterr().err


def test_a_query_that_raises_is_recorded_up_to_the_raise(tmp_path, tiny_store, monkeypatch, capsys):
    def native_call_then_raise(graph, text, **kwargs):
        graph.store._store().count_quads()  # through the proxy once the measured stream runs
        raise RuntimeError("boom")

    monkeypatch.setattr(record_native, "run_instance", native_call_then_raise)
    warmup, measured = tiny_streams(1, 1)
    trace = tmp_path / "trace.jsonl"
    calls = record_native.record(str(tiny_store), False, warmup, measured, str(trace))
    lines = trace.read_text().splitlines()
    assert calls == len(measured) and len(lines) == 1 + len(measured)
    for line in lines[1:]:
        assert [c[1] for c in json.loads(line)["calls"]] == ["count_quads"]
    assert f"({len(measured)} raised)" in capsys.readouterr().err
