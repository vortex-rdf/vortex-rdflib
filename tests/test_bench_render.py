"""The dashboard render: both datasets' results in one page; each tab works without the other."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_bench_dashboard.py"
TEMPLATE = ROOT / "scripts" / "bench_dashboard_template.html"


def results(dataset: str) -> dict:
    group = "Q1" if dataset == "bsbm" else "p-scan"
    row = {
        "group": group,
        "variant": "vortex_dict_mem",
        "id": f"{group}::vortex_dict_mem",
        "mode": "full",
        "median": "1 ms",
        "median_ns": 1e6,
        "mean": "1 ms",
        "mean_ns": 1e6,
        "samples": "3",
    }
    config = {
        "adapters": [
            {
                "slug": "vortex_dict_mem",
                "label": "vortex",
                "engine": "rdflib",
                "quads": True,
                "modes": ["exec", "full"],
            }
        ],
        "queries": [{"name": group, "group": "explore", "sparql": "SELECT * {} # </script><!--"}],
    }
    if dataset == "bsbm":
        config.update(dataset="bsbm", answers={"vortex_dict_mem": {"0": [1, "d"]}})
    return {
        "provenance": f"{dataset} run",
        "results": [row],
        "memory": [],
        "config": config,
        "failures": [],
    }


def render(tmp_path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    out = tmp_path / "index.html"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), args[0], str(out), *args[1:]],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return proc, out


def embedded(page: str) -> dict:
    match = re.search(r"const SETS = (.*?);\n", page)
    assert match, "the page embeds no SETS"
    return json.loads(match.group(1))


def write(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_both_datasets_are_embedded_without_the_answers(tmp_path):
    syn = write(tmp_path / "r.json", results("synthetic"))
    bsbm = write(tmp_path / "rb.json", results("bsbm"))
    proc, out = render(tmp_path, syn, "--bsbm", bsbm)
    assert proc.returncode == 0, proc.stderr
    page = out.read_text(encoding="utf-8")
    sets = embedded(page)
    assert sets["synthetic"]["provenance"] == "synthetic run"
    assert sets["bsbm"]["config"]["dataset"] == "bsbm" and "answers" not in sets["bsbm"]["config"]
    assert "</script><!--" not in page.split("const SETS = ", 1)[1].split(";\n", 1)[0]


def test_a_missing_results_file_disables_its_tab_with_a_note(tmp_path):
    proc, out = render(
        tmp_path,
        write(tmp_path / "r.json", results("synthetic")),
        "--bsbm",
        str(tmp_path / "absent.json"),
    )
    assert proc.returncode == 0, proc.stderr
    sets = embedded(out.read_text(encoding="utf-8"))
    assert sets["bsbm"] is None and "absent.json" in sets["notes"]["bsbm"]


def test_no_results_at_all_is_an_error(tmp_path):
    proc, _ = render(tmp_path, str(tmp_path / "none.json"))
    assert proc.returncode == 1
    assert "nothing to render" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_every_section_switches_datasets_on_its_own():
    template = TEMPLATE.read_text(encoding="utf-8")
    assert re.findall(r'data-tabbed="([a-z]+)"', template) == [
        "overview",
        "history",
        "load",
        "queries",
        "memory",
        "queryset",
        "data",
    ]
    assert template.count("__DATASETS__") == 1
    assert not re.search(
        r"__(BENCH|MEMORY|CONFIG|FAILURES|HISTORY)_DATA__|__PROVENANCE__", template
    )


def test_every_fill_placeholder_names_exactly_one_template():
    template = TEMPLATE.read_text(encoding="utf-8")
    placeholders = set(re.findall(r'data-fill="([^"]+)"', template))
    ids = re.findall(r'<template\b[^>]*\bid="([^"]+)"', template)
    assert placeholders, "the page has no data-fill placeholder"
    assert not placeholders - set(ids), (
        f"placeholders without a template: {placeholders - set(ids)}"
    )
    for name in sorted(placeholders):
        assert ids.count(name) == 1, f'<template id="{name}"> must exist exactly once'


@pytest.mark.skipif(shutil.which("node") is None, reason="node checks the page script's syntax")
def test_the_page_script_parses(tmp_path):
    proc, out = render(tmp_path, write(tmp_path / "r.json", results("synthetic")))
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"<script>(.*)</script>", out.read_text(encoding="utf-8"), re.S)
    assert match
    js = tmp_path / "page.js"
    js.write_text(match.group(1), encoding="utf-8")
    check = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
