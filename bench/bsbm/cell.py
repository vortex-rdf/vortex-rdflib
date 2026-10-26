"""The perf gate's BSBM cell: the official Explore streams on the flagship
configuration, a Dictionary store with the ``secondary-by-copy`` index.

usage:
  python -m bench.bsbm.cell prepare <prepared-dir> <store.vortex>
  python -m bench.bsbm.cell compare <a.json>[,<a2.json>...] <b.json>[,<b2.json>...]
      [--json OUT] [--check]

``prepare`` builds the store from the ``dataset.nt`` of a ``bench.bsbm.prepare``
directory (the dashboard's has 10,000 products, 3,534,773 triples). A run is
``python -m bench.bsbm.run_stream <store> --file|--in-memory <dir>/warmup.json
<dir>/measured.json <out.json>``, each in a fresh process.

``compare`` takes each side's runs in round order, file-backed and in-memory
runs in any order, and pairs them by residency and round. A cell is a
residency, an Explore template and a mode (``full``: prepare plus evaluate;
``exec``: evaluate alone); its figure in a round is the median over the
template's measured instances. The rounds come in pairs, each pair in the
reverse order of the one before (``scripts/perf-gate.sh``):
``bench.compare``'s floor rule judges every pair, and a cell is flagged only
when every pair flags it the same way. The median minor faults per instance
follow ``bench.compare``'s fault rule the same way. Every instance must answer
with the same rows in every run, and neither time out nor fail; anything else
is a mismatch. ``--check`` exits 1 on a REGRESSION, a MORE FAULTS cell or a
mismatch.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

from ..compare import fmt_ns, judge, judge_faults, pct
from .streams import dataset_path, read_meta

MODES = ("full", "exec")
INDEXES = ["secondary-by-copy"]


def prepare(prepared: str, store: str) -> None:
    from vortex_rdf import serialize_rdf

    nt = dataset_path(prepared, read_meta(prepared))
    serialize_rdf(str(nt), store, format="ntriples", layout="dictionary", indexes=INDEXES)


def residency(run: dict) -> str:
    return "in-memory" if run["in_memory"] else "file"


def medians(run: dict) -> dict[int, dict]:
    """Per template: the median full and exec ns and minor faults over its answered instances."""
    answered: dict[int, list[dict]] = {}
    for r in run["results"]:
        if "exec_ns" in r:
            answered.setdefault(r["q"], []).append(r)
    out: dict[int, dict] = {}
    for q, rs in answered.items():
        faults = [r["minflt"] for r in rs if r.get("minflt") is not None]
        out[q] = {
            "full": statistics.median((r["prep_ns"] or 0) + r["exec_ns"] for r in rs),
            "exec": statistics.median(r["exec_ns"] for r in rs),
            "minflt": round(statistics.median(faults)) if faults else None,
        }
    return out


def mismatches(runs: list[dict]) -> list[str]:
    """Instances that timed out, failed, or answered differently in some run; partial runs."""
    found: list[str] = []
    answers: dict[int, set] = {}
    for run in runs:
        where = residency(run)
        if run.get("partial"):
            found.append(f"a {where} run stopped after {run['mixes_done']} mixes")
        for r in run["results"]:
            if r.get("timeout"):
                found.append(f"instance {r['i']} (Q{r['q']}) timed out, {where}")
            elif "error" in r:
                found.append(f"instance {r['i']} (Q{r['q']}) failed, {where}: {r['error']}")
            else:
                answers.setdefault(r["i"], set()).add((r["rows"], r["digest"]))
    found += [
        f"instance {i} answered {len(a)} ways" for i, a in sorted(answers.items()) if len(a) > 1
    ]
    return found


def _agreed(verdicts: list[str | None]) -> str | None:
    """Every pair's verdict when they agree, else within; None when a pair has none."""
    if any(v is None for v in verdicts):
        return None
    return verdicts[0] if all(v == verdicts[0] for v in verdicts) else "within"


def compare(a_runs: list[dict], b_runs: list[dict]) -> dict:
    sides: dict[str, dict[str, list[dict]]] = {"a": {}, "b": {}}
    for side, runs in (("a", a_runs), ("b", b_runs)):
        for run in runs:
            sides[side].setdefault(residency(run), []).append(run)
    cells: list[dict] = []
    for where in sorted(set(sides["a"]) | set(sides["b"])):
        a, b = sides["a"].get(where, []), sides["b"].get(where, [])
        if len(a) != len(b):
            raise ValueError(f"{where}: a has {len(a)} rounds and b {len(b)}: they pair by round")
        am, bm = [medians(r) for r in a], [medians(r) for r in b]
        templates = sorted(set.intersection(*(set(m) for m in am + bm)))
        pairs = [range(i, min(i + 2, len(a))) for i in range(0, len(a), 2)]
        for q in templates:
            faults = _agreed(
                [
                    judge_faults([am[i][q]["minflt"] for i in p], [bm[i][q]["minflt"] for i in p])
                    for p in pairs
                ]
            )
            for mode in MODES:
                a_ns, b_ns = [m[q][mode] for m in am], [m[q][mode] for m in bm]
                verdicts = [judge([a_ns[i] for i in p], [b_ns[i] for i in p]) for p in pairs]
                cells.append(
                    {
                        "residency": where,
                        "template": q,
                        "mode": mode,
                        "a_ns": a_ns,
                        "b_ns": b_ns,
                        "ratios": [y / x for x, y in zip(a_ns, b_ns, strict=True)],
                        "pairs": [v.verdict for v in verdicts],
                        "verdict": _agreed([v.verdict for v in verdicts]),
                        "a_minflt": [m[q]["minflt"] for m in am],
                        "b_minflt": [m[q]["minflt"] for m in bm],
                        "faults": faults,
                    }
                )
    return {"cells": cells, "mismatches": mismatches(a_runs + b_runs)}


def flagged(out: dict) -> bool:
    return bool(
        out["mismatches"]
        or any(c["verdict"] == "REGRESSION" or c["faults"] == "MORE FAULTS" for c in out["cells"])
    )


def report(out: dict) -> list[str]:
    lines = [
        f"{'residency':10} {'template':8} {'mode':4} {'a r1':>9} {'b r1':>9}  "
        f"{'b/a per round':30} {'faults a -> b (r1)':20} verdict"
    ]
    for c in out["cells"]:
        fa, fb = c["a_minflt"][0], c["b_minflt"][0]
        faults = "n/a" if fa is None or fb is None else f"{fa:,} -> {fb:,}"
        verdict = str(c["verdict"])
        if c["faults"] not in (None, "within"):
            verdict += f", {c['faults']}"
        lines.append(
            f"{c['residency']:10} Q{c['template']:<7} {c['mode']:4} {fmt_ns(c['a_ns'][0]):>9} "
            f"{fmt_ns(c['b_ns'][0]):>9}  {', '.join(pct(r) for r in c['ratios']):30} "
            f"{faults:20} {verdict}"
        )
    flags = sum(c["verdict"] in ("REGRESSION", "FASTER") for c in out["cells"])
    lines.append(f"flagged by every pair: {flags} of {len(out['cells'])} cells")
    if out["mismatches"]:
        lines.append(f"MISMATCH: {len(out['mismatches'])}")
        lines += [f"  {m}" for m in out["mismatches"][:20]]
    else:
        lines.append("answers: every instance answered the same rows in every run")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare", help="build the by-copy store")
    p_prepare.add_argument("prepared", help="a directory bench.bsbm.prepare wrote")
    p_prepare.add_argument("store")
    p_compare = sub.add_parser("compare", help="pair two sides' stream runs by round")
    p_compare.add_argument("a", help="side a's run_stream files, comma-separated, in round order")
    p_compare.add_argument("b", help="side b's run_stream files, in the same order")
    p_compare.add_argument("--json", dest="json_out", help="write every cell's figures here")
    p_compare.add_argument("--check", action="store_true", help="exit 1 when anything is flagged")
    args = parser.parse_args(argv)

    if args.command == "prepare":
        prepare(args.prepared, args.store)
        print(f"{args.store}: {Path(args.store).stat().st_size / 1e6:,.0f} MB, indexes {INDEXES}")
        return 0

    def runs(arg: str) -> list[dict]:
        found = []
        for path in arg.split(","):
            with open(path, encoding="utf-8") as f:
                found.append(json.load(f))
        return found

    out = compare(runs(args.a), runs(args.b))
    print("\n".join(report(out)))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 1 if args.check and flagged(out) else 0


if __name__ == "__main__":
    sys.exit(main())
