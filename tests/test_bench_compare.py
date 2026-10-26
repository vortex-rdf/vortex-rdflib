"""The paired comparison of two sides' synthetic runs (``bench.compare``).

A gate's worth is its false-alarm rate and what it misses: a cell is flagged
only when every round agrees beyond a floor that the baseline's own spread
can raise, and a page-fault shift is flagged on its own rule, since it can
move with no timer showing it.
"""

import json
from collections.abc import Mapping

import pytest
from bench import compare
from bench.compare import judge, judge_faults


def test_a_regression_must_hold_in_every_round():
    assert judge([100.0, 100.0], [110.0, 108.0]).verdict == "REGRESSION"
    assert judge([100.0, 100.0], [110.0, 101.0]).verdict == "within"
    assert judge([100.0, 100.0], [90.0, 92.0]).verdict == "FASTER"


def test_the_baselines_own_spread_raises_the_floor():
    # a moved 20 % between its rounds, so a 15 % gap in both rounds is noise here.
    v = judge([100.0, 120.0], [115.0, 138.0])
    assert v.spread == pytest.approx(0.2) and v.floor == pytest.approx(0.2)
    assert v.verdict == "within"
    assert judge([100.0, 101.0], [115.0, 116.0]).floor == compare.FLOOR


def test_one_round_rests_on_the_fixed_floor():
    assert judge([100.0], [106.0]).verdict == "REGRESSION"
    assert judge([100.0], [104.0]).verdict == "within"


def test_page_faults_shift_by_ratio_and_by_count():
    # The count-distinct case: 1,310 -> 4,709 faults a query, in both rounds.
    assert judge_faults([1310, 1290], [4709, 4650]) == "MORE FAULTS"
    assert judge_faults([4709, 4650], [1310, 1290]) == "FEWER FAULTS"
    # Five times as many, but a handful of pages: under the margin.
    assert judge_faults([20, 20], [100, 100]) == "within"
    assert judge_faults([1000, 1000], [1100, 2000]) == "within"
    # A platform without getrusage counts none.
    assert judge_faults([None, None], [5000, 5000]) is None


def results(cells: Mapping[tuple[str, str], tuple[float, int | None]], counts=None, failures=()):
    """A results file holding ``(configuration, query) -> (exec ns, faults)`` cells,
    each with a ``full`` row 10 % slower, and a load row the comparison skips."""
    rows = [{"group": "load", "variant": "s", "mode": "full", "median_ns": 1.0}]
    for (slug, query), (ns, faults) in cells.items():
        for mode, scale in (("exec", 1.0), ("full", 1.1)):
            row = {
                "group": query,
                "variant": slug if mode == "full" else f"{slug}::exec",
                "mode": mode,
                "median_ns": ns * scale,
            }
            if faults is not None:
                row["minflt"] = faults
            rows.append(row)
    row_counts = counts or {q: {s: 1} for (s, q) in cells}
    return {"results": rows, "config": {"rowCounts": row_counts}, "failures": list(failures)}


def test_two_sides_compare_cell_by_cell_round_by_round():
    a = [
        results({("v", "p-scan"): (100.0, 1300), ("rdflib_memory", "p-scan"): (900.0, 10)}),
        results({("v", "p-scan"): (102.0, 1310), ("rdflib_memory", "p-scan"): (910.0, 10)}),
    ]
    b = [
        results({("v", "p-scan"): (120.0, 4700), ("rdflib_memory", "p-scan"): (905.0, 10)}),
        results({("v", "p-scan"): (121.0, 4650), ("rdflib_memory", "p-scan"): (900.0, 10)}),
    ]
    out = compare.compare(a, b)
    by = {(c["configuration"], c["mode"], c["query"]): c for c in out["cells"]}
    assert set(by) == {(s, m, "p-scan") for s in ("v", "rdflib_memory") for m in ("exec", "full")}
    assert by[("v", "exec", "p-scan")]["verdict"] == "REGRESSION"
    assert by[("v", "exec", "p-scan")]["faults"] == "MORE FAULTS"
    assert by[("rdflib_memory", "full", "p-scan")]["verdict"] == "drift"
    assert out["geomean"]["v"]["exec"] == pytest.approx((120 / 100 * 121 / 102) ** 0.5)
    assert out["mismatches"] == [] and compare.flagged(out)
    text = "\n".join(compare.report(out, "a", "b"))
    assert "REGRESSION (2)" in text and "MORE FAULTS (2)" in text
    assert "no vortex-rdflib code" in text


def test_a_row_count_that_differs_anywhere_is_a_mismatch():
    same = {("v", "q"): (100.0, None)}
    a = [results(same, counts={"q": {"v": 3, "rdflib_memory": 3}})]
    b = [results(same, counts={"q": {"v": 4, "rdflib_memory": 3}})]
    out = compare.compare(a, b)
    assert out["mismatches"] == [{"store": "v", "query": "q", "counts": [3, 4]}]
    assert compare.flagged(out)


def test_the_sides_must_pair_round_for_round():
    with pytest.raises(ValueError, match="pair by round"):
        compare.compare([results({})], [results({}), results({})])


def test_check_fails_only_on_something_flagged(tmp_path):
    def write(name: str, payload: dict) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    quiet = write("a.json", results({("v", "q"): (100.0, 50)}))
    same = write("b.json", results({("v", "q"): (101.0, 52)}))
    slower = write("c.json", results({("v", "q"): (130.0, 50)}))
    broken = write("d.json", results({("v", "q"): (100.0, 50)}, failures=[{"phase": "q"}]))
    assert compare.main([quiet, same, "--check"]) == 0
    assert compare.main([quiet, slower, "--check"]) == 1
    assert compare.main([quiet, broken, "--check"]) == 1
    assert compare.main([quiet, slower]) == 0  # a report, unless asked to gate
    out = tmp_path / "out.json"
    compare.main([quiet, slower, "--json", str(out)])
    assert json.loads(out.read_text())["cells"][0]["verdict"] == "REGRESSION"


def runs(*rounds: dict) -> list[dict]:
    return list(rounds)


def test_a_flag_stands_only_when_the_confirmation_rounds_repeat_it():
    # x regresses for real; y drew two unlucky processes on b's side.
    first = compare.compare(
        runs(
            results({("v", "x"): (100.0, None), ("v", "y"): (100.0, None)}),
            results({("v", "x"): (100.0, None), ("v", "y"): (100.0, None)}),
        ),
        runs(
            results({("v", "x"): (112.0, None), ("v", "y"): (108.0, None)}),
            results({("v", "x"): (111.0, None), ("v", "y"): (109.0, None)}),
        ),
    )
    assert {c["query"] for c in first["cells"] if c["verdict"] == "REGRESSION"} == {"x", "y"}
    out = compare.confirm(
        first,
        runs(
            results({("v", "x"): (100.0, None), ("v", "y"): (100.0, None)}),
            results({("v", "x"): (101.0, None), ("v", "y"): (100.0, None)}),
        ),
        runs(
            results({("v", "x"): (113.0, None), ("v", "y"): (101.0, None)}),
            results({("v", "x"): (112.0, None), ("v", "y"): (99.0, None)}),
        ),
    )
    standing = {
        (c["query"], c["mode"])
        for c in out["cells"]
        if compare.stands(c, "verdict") and c["verdict"] == "REGRESSION"
    }
    assert standing == {("x", "exec"), ("x", "full")}
    assert compare.flagged(out)
    text = "\n".join(compare.report(out, "a", "b"))
    assert "REGRESSION (2 confirmed of 4 flagged)" in text and "not confirmed" in text


def test_a_flag_whose_cell_was_not_measured_again_stands():
    first = compare.compare(
        runs(results({("v", "x"): (100.0, None)}), results({("v", "x"): (100.0, None)})),
        runs(results({("v", "x"): (120.0, None)}), results({("v", "x"): (120.0, None)})),
    )
    out = compare.confirm(first, runs(results({}), results({})), runs(results({}), results({})))
    assert compare.flagged(out)


def test_the_reference_store_is_drift_never_a_regression():
    cell = {(compare.REFERENCE, "q"): (100.0, 10_000)}
    slower = {(compare.REFERENCE, "q"): (130.0, 30_000)}
    out = compare.compare(
        runs(results(cell), results(cell)), runs(results(slower), results(slower))
    )
    assert {c["verdict"] for c in out["cells"]} == {"drift"}
    assert not compare.flagged(out)


def test_the_flagged_cells_name_what_a_confirmation_must_measure(tmp_path):
    def write(name: str, payload: dict) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    a = write(
        "a.json",
        results({("v", "x"): (100.0, 50), ("w", "y"): (100.0, 50), ("v", "z"): (100.0, 50)}),
    )
    b = write(
        "b.json",
        results({("v", "x"): (130.0, 50), ("w", "y"): (70.0, 50), ("v", "z"): (101.0, 50)}),
    )
    flagged = tmp_path / "flagged.json"
    compare.main([a, b, "--flagged-out", str(flagged)])
    assert json.loads(flagged.read_text()) == {"adapters": ["v", "w"], "queries": ["x", "y"]}
    # Confirmed by the same files once more: x stands, so the check fails.
    assert compare.main([a, b, "--confirm", a, b, "--check"]) == 1
    quiet = write("q.json", results({("v", "x"): (100.0, 50), ("w", "y"): (100.0, 50)}))
    assert compare.main([a, b, "--confirm", quiet, quiet, "--check"]) == 0


def test_a_round_measured_one_store_at_a_time_is_its_files_merged(tmp_path):
    def write(name: str, payload: dict) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    v = write("v.json", results({("v", "q"): (100.0, None)}, counts={"q": {"v": 3}}))
    w = write("w.json", results({("w", "q"): (200.0, None)}, counts={"q": {"w": 3}}))
    merged = compare.load_round(f"{v}+{w}")
    assert {(c[0], c[2]) for c in compare.cells_of(merged)} == {("v", "q"), ("w", "q")}
    assert merged["config"]["rowCounts"] == {"q": {"v": 3, "w": 3}}
    out = compare.compare(compare.load([f"{v}+{w}"]), compare.load([f"{v}+{w}"]))
    assert {c["configuration"] for c in out["cells"]} == {"v", "w"}
