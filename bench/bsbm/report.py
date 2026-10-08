"""BSBM dashboard results: one store's contribution, and the answer check.

``store_report`` turns a worker's ``execute_stream`` run into dashboard rows (one
per template and mode: mean = BSBM's AQET, and median, over the instances that
finished; timeouts excluded and counted), answers, completed mixes and query
mixes per hour (3600 s over the mean measured-mix time, full mode, a timed-out
query counting its time up to the abort, as the official driver counts it).

``reconcile_answers`` checks every instance across stores: the rdflib-engine
stores must agree on the digest; the agreed answer is the majority's, rdflib
(in-mem)'s on a tie. pyoxigraph spells terms its own way, so it is held to the
agreed row count. A dissent is one failure per store and template, tagged
``"check": "answers"`` so a merge of partial results can recompute them all.
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping

from ..worker import make_row

REFERENCE = "rdflib_memory"
CHECK = "answers"


def template_name(q: int) -> str:
    return f"Q{q}"


def template_number(name: str) -> int:
    return int(name[1:])


def store_report(slug: str, run: dict, native: bool) -> dict:
    exec_ns: dict[str, list[float]] = defaultdict(list)
    full_ns: dict[str, list[float]] = defaultdict(list)
    timeouts: Counter[str] = Counter()
    errors: dict[str, dict] = {}
    answers: dict[str, list | None] = {}
    for record in run["results"]:
        name, i = template_name(record["q"]), str(record["i"])
        if record.get("timeout"):
            timeouts[name] += 1
            answers[i] = None
        elif "error" in record:
            entry = errors.setdefault(name, {"count": 0, "first": record["error"]})
            entry["count"] += 1
            answers[i] = None
        else:
            answers[i] = [record["rows"], record["digest"]]
            if native:
                full_ns[name].append(record["exec_ns"])
            else:
                exec_ns[name].append(record["exec_ns"])
                full_ns[name].append(record["prep_ns"] + record["exec_ns"])
    rows: list[dict] = []
    for name in sorted(full_ns, key=template_number):
        if not native:
            rows.append(make_row(name, slug, exec_ns[name], "exec"))
        rows.append(make_row(name, slug, full_ns[name], "full"))
    mean_mix = statistics.fmean(run["mix_ns"]) if run["mix_ns"] else None
    return {
        "rows": rows,
        "answers": answers,
        "timeouts": dict(sorted(timeouts.items(), key=lambda kv: template_number(kv[0]))),
        "errors": errors,
        "mixes": run["mixes_done"],
        "plannedMixes": run["mixes_planned"],
        "partial": run["partial"],
        "meanMixNs": mean_mix,
        "qmph": 3600e9 / mean_mix if mean_mix else None,
        "warmupNs": run["warmup_ns"],
        "warmupTimeouts": run["warmup_timeouts"],
        "warmupErrors": run["warmup_errors"],
    }


def error_failures(store: dict) -> list[dict]:
    out = []
    for name in sorted(store["errors"], key=template_number):
        entry = store["errors"][name]
        noun = "instance" if entry["count"] == 1 else "instances"
        out.append(
            {"phase": name, "error": f"{entry['count']} {noun} failed; first: {entry['first']}"}
        )
    return out


def reconcile_answers(
    answers: Mapping[str, Mapping[str, list | None]],
    adapters: list[dict],
    instance_templates: list[int],
) -> tuple[list[dict], dict[str, dict]]:
    engine = {a["slug"]: a["engine"] for a in adapters}
    label = {a["slug"]: a["label"] for a in adapters}
    rank = {a["slug"]: n for n, a in enumerate(adapters)}
    ids = sorted({i for per in answers.values() for i in per}, key=int)
    agreed: dict[str, list] = {}
    dissent: dict[tuple[str, str], list[tuple[str, list, list]]] = {}
    for i in ids:
        name = template_name(instance_templates[int(i)])
        votes: dict[str, list] = {}
        for s, per in answers.items():
            answer = per.get(i)
            if engine.get(s) != "native" and answer is not None:
                votes[s] = answer
        if not votes:
            continue
        tally = Counter(tuple(v) for v in votes.values())
        top = max(tally.values())
        leaders = [list(v) for v, n in tally.items() if n == top]
        reference = votes.get(REFERENCE)
        consensus = reference if reference is not None and reference in leaders else leaders[0]
        agreed[i] = consensus
        for slug, per in answers.items():
            answer = per.get(i)
            if answer is None:
                continue
            native = engine.get(slug) == "native"
            if (answer[0] != consensus[0]) if native else (answer != consensus):
                dissent.setdefault((slug, name), []).append((i, answer, consensus))
    failures = []
    order = sorted(dissent, key=lambda k: (rank.get(k[0], len(rank)), template_number(k[1])))
    for slug, name in order:
        cases = dissent[(slug, name)]
        i, answer, consensus = cases[0]
        detail = (
            f"{answer[0]} rows against {consensus[0]}"
            if answer[0] != consensus[0]
            else f"same {answer[0]} rows, different terms"
        )
        what = "row count" if engine.get(slug) == "native" else "answer"
        noun = "instance" if len(cases) == 1 else "instances"
        failures.append(
            {
                "slug": slug,
                "label": label.get(slug, slug),
                "phase": name,
                "check": CHECK,
                "error": (
                    f"{len(cases)} {noun} with another {what} than the other stores "
                    f"(first: instance {i}, {detail})"
                ),
            }
        )
    templates: dict[str, dict] = {}
    for i, consensus in agreed.items():
        name, rows = template_name(instance_templates[int(i)]), consensus[0]
        entry = templates.setdefault(
            name, {"instances": 0, "minRows": rows, "maxRows": rows, "empty": 0}
        )
        entry["instances"] += 1
        entry["minRows"], entry["maxRows"] = (
            min(entry["minRows"], rows),
            max(entry["maxRows"], rows),
        )
        entry["empty"] += rows == 0
    return failures, dict(sorted(templates.items(), key=lambda kv: template_number(kv[0])))


def base_config(meta: dict, limits: dict, instance_templates: list[int]) -> dict:
    return {
        "dataset": "bsbm",
        "products": meta["products"],
        "triples": meta["dataset"]["triples"],
        "graphs": 1,
        "seed": meta["seed"],
        "warmupMixes": meta["warmup_mixes"],
        "mixes": meta["mixes"],
        "mix": meta["mix"],
        "onlyQuery": meta.get("only_query"),
        "tools": {"repo": meta["tools"]["repo"], "commit": meta["tools"]["commit"]},
        "queryTimeoutS": limits["queryTimeoutS"],
        "storeBudgetS": limits["storeBudgetS"],
        "queries": [
            {
                "name": template_name(q),
                "group": "explore",
                "template": q,
                "heavy": False,
                "quads": False,
                "countable": False,
                "sparql": meta["templates"][str(q)],
            }
            for q in sorted(set(meta["mix"]))
        ],
        "instanceTemplates": instance_templates,
    }


def assemble(
    base: dict,
    adapters: list[dict],
    results: list[dict],
    memory: list[dict],
    failures: list[dict],
    stores: dict[str, dict],
    answers: Mapping[str, Mapping[str, list | None]],
    provenance: str,
) -> dict:
    """``results-bsbm.json``: results.json's shape plus the BSBM extras, the answer
    check run over every store given (earlier checks among the failures dropped)."""
    checks, templates = reconcile_answers(answers, adapters, base["instanceTemplates"])
    config = {
        **base,
        "adapters": adapters,
        "skipped": {},
        "disputedRows": sorted({f["phase"] for f in checks}, key=template_number),
        "templates": templates,
        "stores": stores,
        "answers": {s: dict(per) for s, per in answers.items()},
    }
    kept = [f for f in failures if f.get("check") != CHECK]
    return {
        "provenance": provenance,
        "results": results,
        "memory": memory,
        "config": config,
        "failures": kept + checks,
    }
