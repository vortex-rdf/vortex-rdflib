"""The release workflow against the suite it runs on the wheel.

A tag push builds the wheel, runs the test suite against it in an isolated environment, then
publishes. That environment holds the wheel, what the wheel requires and what the step installs
besides: the suite's own dependencies are there only if the step installs them. CI's test job,
which the release runs first, has the lock's dev group (pyoxigraph is the SPARQL reference of
tests/oracle.py). A group CI's tests have and the wheel's tests lack fails the release at
collection, after CI passed, and nothing is published.

Stdlib only, as tests/test_bench_workflows.py reads the CI workflows: regular expressions over the
workflow text, each reader asserting what it found.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SMOKE = "Smoke-test the wheel"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def step_commands(text: str, name: str) -> list[str]:
    """The shell commands of a step's `run:` block: a folded (`>`) block is one command, a literal
    (`|`) block one per line, with a line ending in a backslash joined to the next."""
    found = re.search(
        rf"^( +)- name: {re.escape(name)}\n\1  run: ([>|])\n((?:\1    .*\n|[ \t]*\n)+)",
        text,
        re.MULTILINE,
    )
    assert found, f"no step {name!r} with a run: block"
    lines = [line.strip() for line in found.group(3).splitlines() if line.strip()]
    if found.group(2) == ">":
        return [" ".join(lines)]
    commands, continued = [], ""
    for line in lines:
        if line.endswith("\\"):
            continued += line[:-1]
        else:
            commands.append(continued + line)
            continued = ""
    return commands


def ci_test_groups(ci: str, pyproject: dict) -> set[str]:
    """The dependency groups of CI's pytest run: uv's default groups and those its install adds."""
    found = re.search(r"run: (uv sync.*)\n(?:.*\n){0,3}? +run: uv run pytest\b", ci)
    assert found, "CI runs pytest after no uv sync"
    install = found.group(1).strip()
    assert re.fullmatch(r"uv sync --locked(?: --group \S+)*", install), install
    default = pyproject.get("tool", {}).get("uv", {}).get("default-groups", ["dev"])
    if default == "all":
        default = list(pyproject.get("dependency-groups", {}))
    return {*default, *re.findall(r"--group (\S+)", install)}


def smoke_problems(release: str, ci: str, pyproject: dict) -> list[str]:
    """The wheel's tests must have every dependency group CI's tests have, exported from the lock
    to a file their `uv run` reads."""
    commands = step_commands(release, SMOKE)
    runs = [command for command in commands if re.search(r"\bpytest tests\b", command)]
    assert len(runs) == 1, f"the smoke test runs pytest {len(runs)} times: {commands}"
    exported = {
        file.strip("\"'"): re.findall(r"--(?:only-)?group (\S+)", command)
        for command in commands
        if command.startswith("uv export ")
        for file in re.findall(r"(?:-o|--output-file) (\S+)", command)
    }
    read_by_run = {file.strip("\"'") for file in re.findall(r"--with-requirements (\S+)", runs[0])}
    installed = {group for file in read_by_run & exported.keys() for group in exported[file]}
    return [
        f"the wheel's tests run without the {group} group CI's tests run with"
        for group in sorted(ci_test_groups(ci, pyproject) - installed)
    ]


def test_the_wheel_is_tested_with_the_groups_ci_tests_with():
    assert smoke_problems(read("release.yml"), read("ci.yml"), project()) == []


#: The step as v0.4.0 was released with: pytest alone besides the wheel.
PYTEST_ALONE = """\
      - name: Smoke-test the wheel
        run: >
          uv run --isolated --no-project
          --with pytest --with dist/*.whl
          pytest tests -q
"""


def test_the_smoke_check_notices_pytest_alone():
    assert smoke_problems(PYTEST_ALONE, read("ci.yml"), project()) == [
        "the wheel's tests run without the dev group CI's tests run with"
    ]


def test_the_smoke_check_notices_a_group_ci_adds():
    ci = read("ci.yml")
    faulty = ci.replace("run: uv sync --locked\n", "run: uv sync --locked --group bench\n", 1)
    assert faulty != ci, "CI's test job does not install with the line this test replaces"
    assert smoke_problems(read("release.yml"), faulty, project()) == [
        "the wheel's tests run without the bench group CI's tests run with"
    ]


def test_the_smoke_check_notices_an_export_the_tests_do_not_read():
    release = read("release.yml")
    faulty = re.sub(r"--with-requirements \S+ \\\n\s*", "", release, count=1)
    assert faulty != release, "the smoke test does not read the requirements this test removes"
    assert smoke_problems(faulty, read("ci.yml"), project()) == [
        "the wheel's tests run without the dev group CI's tests run with"
    ]
