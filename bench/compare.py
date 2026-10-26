"""Paired comparison of two sides' synthetic runs (``run_bench`` results files).

usage: python -m bench.compare <a.json>[,<a2.json>...] <b.json>[,<b2.json>...]
           [--confirm <a.json>[,...] <b.json>[,...]] [--flagged-out OUT] [--json OUT] [--check]

A side's rounds are comma-separated, one results file each; a round measured
one store at a time is its files joined with ``+``. Round i of a pairs with
round i of b: run the rounds on one machine, the sides alternating, so drift
lands on both (``scripts/perf-gate.sh``). Every cell
(configuration × mode × query, the load rows left out) gets b/a of its medians
in every round and a verdict against a floor of max(A/A spread, 5 %), the A/A
spread being a's last round over its first:

- REGRESSION: b slower than a beyond the floor in every round;
- FASTER: b faster than a beyond the floor in every round;
- within: anything else.

Minor page faults per query (``minflt``) get the same rule against max(their
A/A spread, 25 %), above a margin of 256 faults (1 MiB of 4 KiB pages): MORE
FAULTS when b faults more in every round, FEWER FAULTS when it faults less.

Some 600 cells judged at once flag a few by chance in every run.
``--flagged-out`` names the flagged cells' configurations and queries, for a
run of those alone (``BENCH_QUERIES``) in fresh processes. ``--confirm`` judges
the flagged cells again over that run's rounds: a flag stands when the
confirmation repeats it, or when the confirmation did not measure its cell.

The reference store (``rdflib_memory``) runs no vortex-rdflib code, so its b/a
is the machine's drift between the sides: its cells are reported as drift and
never flagged. Answers: every store's row count for every query, in every run
of both sides, must agree; one that differs is a mismatch. ``--check`` exits 1
on a standing REGRESSION or MORE FAULTS cell, a mismatch or a failure.
"""

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass

FLOOR = 0.05
FAULT_FLOOR = 0.25
FAULT_MARGIN = 256
REFERENCE = "rdflib_memory"


@dataclass(frozen=True)
class Verdict:
    ratios: tuple[float, ...]  # b / a, per round
    spread: float  # A/A: |a's last round / its first - 1|; 0 with one round
    floor: float
    verdict: str  # REGRESSION | FASTER | within


def spread(a: list[float]) -> float:
    return abs(a[-1] / a[0] - 1) if len(a) > 1 and a[0] > 0 else 0.0


def judge(a: list[float], b: list[float], floor: float = FLOOR) -> Verdict:
    """The verdict on one cell from its per-round figures (lower is better)."""
    ratios = tuple(y / x for x, y in zip(a, b, strict=True))
    aa = spread(a)
    limit = max(aa, floor)
    if all(r > 1 + limit for r in ratios):
        verdict = "REGRESSION"
    elif all(r < 1 - limit for r in ratios):
        verdict = "FASTER"
    else:
        verdict = "within"
    return Verdict(ratios, aa, limit, verdict)


def judge_faults(a: list[int | None], b: list[int | None]) -> str | None:
    """MORE FAULTS, FEWER FAULTS or within; None when a round counted none."""
    xs = [float(x) for x in a if x is not None]
    ys = [float(y) for y in b if y is not None]
    if len(xs) != len(a) or len(ys) != len(b) or min(xs) <= 0:
        return None
    limit = max(spread(xs), FAULT_FLOOR)
    pairs = list(zip(xs, ys, strict=True))
    if all(y > x * (1 + limit) and y - x > FAULT_MARGIN for x, y in pairs):
        return "MORE FAULTS"
    if all(y < x * (1 - limit) and x - y > FAULT_MARGIN for x, y in pairs):
        return "FEWER FAULTS"
    return "within"


Cell = tuple[str, str, str]  # (configuration, mode, query)


def cells_of(results: dict) -> dict[Cell, tuple[float, int | None]]:
    """(median ns, median minor faults) per cell of one results file."""
    out: dict[Cell, tuple[float, int | None]] = {}
    for row in results["results"]:
        if row["group"] == "load":
            continue
        slug = row["variant"].split("::")[0]
        out[(slug, row.get("mode", "full"), row["group"])] = (row["median_ns"], row.get("minflt"))
    return out


def load_round(spec: str) -> dict:
    """One round of a side: a results file, or several joined with ``+``, merged."""
    runs = []
    for path in spec.split("+"):
        with open(path, encoding="utf-8") as f:
            runs.append(json.load(f))
    if len(runs) == 1:
        return runs[0]
    row_counts: dict[str, dict[str, int]] = {}
    for run in runs:
        for query, per_store in run["config"].get("rowCounts", {}).items():
            row_counts.setdefault(query, {}).update(per_store)
    return {
        "results": [row for run in runs for row in run["results"]],
        "config": {"rowCounts": row_counts},
        "failures": [f for run in runs for f in run.get("failures", [])],
    }


def load(specs: list[str]) -> list[dict]:
    return [load_round(spec) for spec in specs]


def mismatches(a_runs: list[dict], b_runs: list[dict]) -> list[dict]:
    """Every (store, query) whose row count is not one number across all runs."""
    seen: dict[tuple[str, str], set] = {}
    for run in a_runs + b_runs:
        for query, per_store in run["config"].get("rowCounts", {}).items():
            for slug, n in per_store.items():
                seen.setdefault((slug, query), set()).add(n)
    return [
        {"store": slug, "query": query, "counts": sorted(counts)}
        for (slug, query), counts in sorted(seen.items())
        if len(counts) > 1
    ]


def _figures(a_cells: list[dict], b_cells: list[dict], cell: Cell) -> dict:
    a_ns, b_ns = [c[cell][0] for c in a_cells], [c[cell][0] for c in b_cells]
    a_flt, b_flt = [c[cell][1] for c in a_cells], [c[cell][1] for c in b_cells]
    return {
        "a_ns": a_ns,
        "b_ns": b_ns,
        "a_minflt": a_flt,
        "b_minflt": b_flt,
        "faults": judge_faults(a_flt, b_flt),
        **asdict(judge(a_ns, b_ns)),
    }


def failure_count(runs: list[dict]) -> int:
    return sum(len(r.get("failures", [])) for r in runs)


def compare(a_runs: list[dict], b_runs: list[dict]) -> dict:
    if len(a_runs) != len(b_runs):
        raise ValueError(f"a has {len(a_runs)} rounds and b {len(b_runs)}: they pair by round")
    a_cells, b_cells = [cells_of(r) for r in a_runs], [cells_of(r) for r in b_runs]
    common = sorted(set.intersection(*(set(c) for c in a_cells + b_cells)))
    rows: list[dict] = []
    ratios_by: dict[tuple[str, str], list[float]] = {}  # (configuration, mode) -> b/a
    for cell in common:
        if min(c[cell][0] for c in a_cells + b_cells) <= 0:
            continue
        slug, mode, query = cell
        row = {
            "configuration": slug,
            "mode": mode,
            "query": query,
            **_figures(a_cells, b_cells, cell),
        }
        if slug == REFERENCE:
            row["verdict"], row["faults"] = "drift", None
        ratios_by.setdefault((slug, mode), []).extend(row["ratios"])
        rows.append(row)
    geo: dict[str, dict[str, float]] = {}
    for (slug, mode), ratios in sorted(ratios_by.items()):
        geo.setdefault(slug, {})[mode] = math.exp(statistics.fmean(math.log(r) for r in ratios))
    return {
        "rounds": len(a_runs),
        "cells": rows,
        "geomean": geo,
        "mismatches": mismatches(a_runs, b_runs),
        "failures": {"a": failure_count(a_runs), "b": failure_count(b_runs)},
    }


def flagged_cells(out: dict) -> list[dict]:
    """The cells with a timing or a fault verdict, either way."""
    return [
        c
        for c in out["cells"]
        if c["verdict"] in ("REGRESSION", "FASTER")
        or c["faults"] in ("MORE FAULTS", "FEWER FAULTS")
    ]


def confirm(out: dict, a_runs: list[dict], b_runs: list[dict]) -> dict:
    """Judge ``out``'s flagged cells again over the confirmation rounds, in place.

    Each flagged cell gets ``confirmation``: its figures and verdicts over these
    rounds, or None when they did not measure it."""
    if len(a_runs) != len(b_runs):
        raise ValueError(f"a has {len(a_runs)} rounds and b {len(b_runs)}: they pair by round")
    a_cells, b_cells = [cells_of(r) for r in a_runs], [cells_of(r) for r in b_runs]
    for c in flagged_cells(out):
        cell = (c["configuration"], c["mode"], c["query"])
        measured = bool(a_cells) and all(cell in x for x in a_cells + b_cells)
        c["confirmation"] = _figures(a_cells, b_cells, cell) if measured else None
    out["confirmation_rounds"] = len(a_runs)
    out["failures"]["a"] += failure_count(a_runs)
    out["failures"]["b"] += failure_count(b_runs)
    return out


def stands(cell: dict, kind: str) -> bool:
    """Whether a cell's ``verdict`` or ``faults`` flag stands: always without a
    confirmation, else when the confirmation repeated it or did not measure it."""
    if "confirmation" not in cell:
        return True
    again = cell["confirmation"]
    return again is None or again[kind] == cell[kind]


def flagged(out: dict) -> bool:
    return bool(
        out["mismatches"]
        or out["failures"]["a"]
        or out["failures"]["b"]
        or any(
            (c["verdict"] == "REGRESSION" and stands(c, "verdict"))
            or (c["faults"] == "MORE FAULTS" and stands(c, "faults"))
            for c in out["cells"]
        )
    )


def fmt_ns(ns: float) -> str:
    if ns >= 1e9:
        return f"{ns / 1e9:.2f} s"
    if ns >= 1e6:
        return f"{ns / 1e6:.2f} ms"
    return f"{ns / 1e3:.0f} µs"


def pct(r: float) -> str:
    return f"{(r - 1) * 100:+.1f}%"


def _name(c: dict) -> str:
    return f"{c['configuration']} / {c['mode']} / {c['query']}"


def _times(c: dict) -> str:
    return (
        f"{fmt_ns(c['a_ns'][0])} -> {fmt_ns(c['b_ns'][0])}, b/a "
        + ", ".join(pct(r) for r in c["ratios"])
        + f" (floor {c['floor'] * 100:.0f}%)"
    )


def _faults(c: dict) -> str:
    shifts = zip(c["a_minflt"], c["b_minflt"], strict=True)
    return ", ".join(f"{x:,} -> {y:,}" for x, y in shifts)


def report(out: dict, a_label: str, b_label: str) -> list[str]:
    lines = [f"a: {a_label}", f"b: {b_label}"]
    one = " (one round: verdicts rest on the 5 % floor alone)" if out["rounds"] == 1 else ""
    lines.append(f"rounds: {out['rounds']}{one}")
    confirmed = "confirmation_rounds" in out
    if confirmed:
        lines.append(f"confirmation: {out['confirmation_rounds']} rounds of the flagged cells")
    if out["mismatches"]:
        lines.append(f"answers: MISMATCH on {len(out['mismatches'])} store/query pairs")
        lines += [f"  {m['store']} / {m['query']}: {m['counts']}" for m in out["mismatches"]]
    else:
        lines.append("answers: every store's row count agrees across both sides")
    lines.append(f"failures: a {out['failures']['a']}, b {out['failures']['b']}")
    for kind, verdicts, show in (
        ("verdict", ("REGRESSION", "FASTER"), _times),
        ("faults", ("MORE FAULTS", "FEWER FAULTS"), _faults),
    ):
        for verdict in verdicts:
            found = [c for c in out["cells"] if c[kind] == verdict]
            standing = [c for c in found if stands(c, kind)]
            count = (
                f"{len(standing)} confirmed of {len(found)} flagged" if confirmed else len(found)
            )
            lines.append(f"{verdict} ({count})")
            for c in found:
                line = f"  {_name(c)}: {show(c)}"
                again = c.get("confirmation")
                if confirmed:
                    if again is None:
                        line += "; not measured again, so it stands"
                    else:
                        state = "confirmed" if stands(c, kind) else "not confirmed"
                        line += f"; {state}: {show(again)}"
                lines.append(line)
    judged = [c for c in out["cells"] if c["verdict"] != "drift"]
    within = sum(c["verdict"] == "within" for c in judged)
    lines.append(f"within the floor or mixed: {within} of {len(judged)} cells")
    lines.append("geometric mean of b/a over the rounds (exec, full):")
    for slug, modes in out["geomean"].items():
        note = "   no vortex-rdflib code: the machine's drift" if slug == REFERENCE else ""
        shown = "  ".join(f"{modes[m]:.3f}" if m in modes else "  -  " for m in ("exec", "full"))
        lines.append(f"  {slug:30} {shown}{note}")
    return lines


def flagged_out(out: dict) -> dict:
    """What a confirmation must measure: the flagged cells' configurations and queries."""
    cells = flagged_cells(out)
    return {
        "adapters": sorted({c["configuration"] for c in cells}),
        "queries": sorted({c["query"] for c in cells}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a", help="side a's results files, comma-separated, one per round")
    parser.add_argument("b", help="side b's results files, in the same round order")
    parser.add_argument(
        "--confirm",
        nargs=2,
        metavar=("A", "B"),
        help="the confirmation rounds' results files of a and of b, comma-separated",
    )
    parser.add_argument(
        "--flagged-out", help="write the flagged cells' configurations and queries here"
    )
    parser.add_argument("--json", dest="json_out", help="write every cell's figures here")
    parser.add_argument("--check", action="store_true", help="exit 1 when anything is flagged")
    args = parser.parse_args(argv)
    a_paths, b_paths = args.a.split(","), args.b.split(",")
    a_runs, b_runs = load(a_paths), load(b_paths)
    out = compare(a_runs, b_runs)
    if args.flagged_out:
        with open(args.flagged_out, "w", encoding="utf-8") as f:
            json.dump(flagged_out(out), f)
    if args.confirm:
        a_more, b_more = load(args.confirm[0].split(",")), load(args.confirm[1].split(","))
        confirm(out, a_more, b_more)
        out["mismatches"] = mismatches(a_runs + a_more, b_runs + b_more)
    print("\n".join(report(out, ", ".join(a_paths), ", ".join(b_paths))))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 1 if args.check and flagged(out) else 0


if __name__ == "__main__":
    sys.exit(main())
