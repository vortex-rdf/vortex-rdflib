"""Paired comparison of BSBM stream runs.

usage: python -m bench.bsbm.compare <a.json>[,<a2.json>...] <b.json>[,<b2.json>...]
           [--mode full|exec] [--json OUT]

Instances pair by id; with several rounds per side an instance's figure is its
best. Per template: ratio of means (b / a), median and geometric mean of the
paired ratios, their range, and how many are 3x or more slower. A differing
digest or a failure on either side is a mismatch. A timeout on either side is
neither paired nor compared; an instance only one side reached (a run stopped by
its budget) is unpaired. Memory: peak RssAnon per side, and whether b's last 10
mixes stay within 5% of its first 10 (spec S3).
"""

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field


@dataclass
class Side:
    label: str
    best: dict[int, float] = field(default_factory=dict)  # id -> ns (mode)
    template: dict[int, int] = field(default_factory=dict)  # id -> q
    answers: dict[int, set] = field(default_factory=dict)  # id -> {(rows, digest) | ("error", msg)}
    timeouts: set[int] = field(default_factory=set)
    peak_anon_mb: int | None = None
    rss_per_mix: list[int | None] = field(default_factory=list)
    mode: str = "full"


def load(paths: list[str], mode: str = "full") -> Side:
    side = Side(label=",".join(paths), mode=mode)
    peaks: list[int] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            run = json.load(f)
        if run.get("peak_anon_mb") is not None:
            peaks.append(run["peak_anon_mb"])
        side.rss_per_mix = run.get("rss_anon_per_mix_mb", [])
        side.label = f"{run.get('vortex_rdflib', '?')} / vortex-rdf {run.get('vortex_rdf', '?')}"
        for r in run["results"]:
            i = r["i"]
            side.template[i] = r["q"]
            if r.get("timeout"):
                side.timeouts.add(i)
                continue
            if "error" in r:
                side.answers.setdefault(i, set()).add(("error", r["error"]))
                continue
            side.answers.setdefault(i, set()).add((r["rows"], r["digest"]))
            ns = r["exec_ns"] if mode == "exec" else r["prep_ns"] + r["exec_ns"]
            side.best[i] = min(ns, side.best.get(i, math.inf))
    side.peak_anon_mb = max(peaks) if peaks else None
    return side


def _geo(xs: list[float]) -> float:
    return math.exp(statistics.fmean(math.log(x) for x in xs))


def _flat(rss: list[int | None]) -> bool | None:
    readings = [m for m in rss if m is not None]
    if len(readings) < 20:
        return None
    return max(readings[-10:]) <= max(readings[:10]) * 1.05


def compare(a: Side, b: Side, mode: str = "full") -> dict:
    timeouts = a.timeouts | b.timeouts
    both = sorted(i for i in a.answers if i in b.answers and i not in timeouts)
    unpaired = sorted((set(a.template) ^ set(b.template)) - timeouts)
    mismatches = [
        i
        for i in both
        if a.answers[i] != b.answers[i]
        or any(x[0] == "error" for x in a.answers[i] | b.answers[i])
        or len(a.answers[i]) != 1
    ]
    bad = set(mismatches)
    paired = [i for i in both if i not in bad and i in a.best and i in b.best]
    templates: dict[int, dict] = {}
    for q in sorted({a.template[i] for i in paired}):
        ids = [i for i in paired if a.template[i] == q]
        fa, fb = [a.best[i] for i in ids], [b.best[i] for i in ids]
        ratios = [y / x for x, y in zip(fa, fb, strict=True)]
        templates[q] = {
            "n": len(ids),
            "a_mean_ms": statistics.fmean(fa) / 1e6,
            "b_mean_ms": statistics.fmean(fb) / 1e6,
            "ratio_of_means": statistics.fmean(fb) / statistics.fmean(fa),
            "median_ratio": statistics.median(ratios),
            "geo": _geo(ratios),
            "min": min(ratios),
            "max": max(ratios),
            "tail3": sum(r >= 3 for r in ratios),
        }
    all_ratios = [b.best[i] / a.best[i] for i in paired]
    return {
        "a": a.label,
        "b": b.label,
        "mode": mode,
        "templates": templates,
        "geo_all": _geo(all_ratios) if all_ratios else None,
        "answers_total": len(both),
        "answers_identical": len(both) - len(mismatches),
        "mismatches": mismatches,
        "timeouts": sorted(timeouts),
        "unpaired": unpaired,
        "a_peak_anon_mb": a.peak_anon_mb,
        "b_peak_anon_mb": b.peak_anon_mb,
        "b_flat": _flat(b.rss_per_mix),
    }


def _fmt_mb(mb) -> str:
    return "n/a" if mb is None else f"{mb} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a")
    parser.add_argument("b")
    parser.add_argument("--mode", choices=("full", "exec"), default="full")
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args(argv)
    out = compare(load(args.a.split(","), args.mode), load(args.b.split(","), args.mode), args.mode)
    print(f"a: {out['a']}\nb: {out['b']}\nmode: {out['mode']}")
    print(
        f"answers identical: {out['answers_identical']}/{out['answers_total']}"
        + (f"  MISMATCH ids {out['mismatches'][:10]}" if out["mismatches"] else "")
    )
    if out["timeouts"] or out["unpaired"]:
        print(f"timeouts: {len(out['timeouts'])}, unpaired: {len(out['unpaired'])}")
    print(
        f"{'q':>4} {'n':>4} {'a mean':>9} {'b mean':>9} {'mean ratio':>10} {'median':>7} "
        f"{'geo':>6} {'min..max':>13} {'>=3x':>5}"
    )
    for q, t in out["templates"].items():
        print(
            f"Q{q:<3} {t['n']:>4} {t['a_mean_ms']:>8.1f}m {t['b_mean_ms']:>8.1f}m "
            f"{t['ratio_of_means']:>10.2f} {t['median_ratio']:>7.2f} {t['geo']:>6.2f} "
            f"{t['min']:>5.2f}..{t['max']:>6.2f} {t['tail3']:>5}"
        )
    if out["geo_all"] is not None:
        print(f"all  geo {out['geo_all']:.2f}")
    print(
        f"peak RssAnon: a {_fmt_mb(out['a_peak_anon_mb'])}, b {_fmt_mb(out['b_peak_anon_mb'])}; "
        f"b flat over the stream: {out['b_flat']}"
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
