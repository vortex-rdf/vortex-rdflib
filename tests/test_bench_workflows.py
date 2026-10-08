"""The CI workflows against the code they run.

The dashboard's matrix must cover every store exactly once (the merge job names any store that no
group reported as a failure), the backfill must measure the same BSBM data as the dashboard (its
records go on the same line, and it restores the cache the dashboard saved), the page must
include the point its own run just measured and be published whenever it was built, and a re-run
job must be able to upload its artifacts again.

Stdlib only: PyYAML is not a dependency, so these read the workflow text with regular expressions
and a line scan, which is enough for the few scalar lines they need. Each reader asserts what it
found, so a reformatting it cannot follow fails the test instead of passing it. Every check is a
function from workflow text to a list of problems, so the tests below also run it on text with a
fault in it to show that it notices.
"""

import re
from pathlib import Path

import pytest
from bench.adapters import ADAPTERS
from bench.bsbm import tools

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
DASHBOARD = "bench-dashboard.yml"
BACKFILL = "bench-history-backfill.yml"
#: What makes two BSBM runs the same data and streams, and where the prepared copy lives.
SHARED_ENV = (
    "BSBM_TOOLS_COMMIT",
    "BSBM_PRODUCTS",
    "BSBM_WARMUP_MIXES",
    "BSBM_MIXES",
    "BSBM_SEED",
    "BSBM_DIR",
)
#: The ones that decide what `bench.bsbm.prepare` writes: the cache key must name each.
KEYED_ENV = tuple(name for name in SHARED_ENV if name != "BSBM_DIR")


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def matrix_groups(text: str) -> list[tuple[str, list[str]]]:
    """(group, store slugs) for each `- group:` / `adapters:` pair of the dashboard's matrix."""
    groups: list[tuple[str, list[str]]] = []
    group = None
    for line in text.splitlines():
        if found := re.fullmatch(r"\s*- group: (\S+)\s*", line):
            group = found.group(1)
        elif (found := re.fullmatch(r"\s+adapters: (\S+)\s*", line)) and group:
            groups.append((group, found.group(1).split(",")))
    return groups


def matrix_problems(text: str) -> list[str]:
    groups = matrix_groups(text)
    if not groups:
        return ["the dashboard has no matrix groups"]
    listed = [slug for _group, slugs in groups for slug in slugs]
    known = [adapter.slug for adapter in ADAPTERS]
    problems = [
        f"{slug} is listed more than once" for slug in sorted(set(listed)) if listed.count(slug) > 1
    ]
    problems += [
        f"{slug} is not a store in bench/adapters.py" for slug in sorted(set(listed) - set(known))
    ]
    problems += [f"{slug} is in no group" for slug in known if slug not in listed]
    return problems


def top_level_env(text: str) -> dict[str, str]:
    """The workflow-level `env:` mapping: its scalar lines, quotes dropped."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.rstrip() == "env:"]
    assert len(starts) == 1, "expected exactly one unindented `env:` line"
    env: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = re.fullmatch(r"  (\w+): *(.*?) *", line)
        if not found:
            break
        env[found.group(1)] = found.group(2).strip("'\"")
    return env


def env_problems(dashboard: str, backfill: str) -> list[str]:
    first, second = top_level_env(dashboard), top_level_env(backfill)
    problems = []
    for name in SHARED_ENV:
        if name not in first or name not in second:
            where = "the dashboard" if name not in first else "the backfill"
            problems.append(f"{name} is missing from {where}")
        elif first[name] != second[name]:
            problems.append(
                f"{name} is {first[name]} in the dashboard, {second[name]} in the backfill"
            )
    return problems


def cache_step(text: str) -> dict[str, str]:
    """The `path:` and `key:` of the workflow's one `actions/cache` step."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.search(r"\buses: actions/cache@", line)]
    assert len(starts) == 1, f"expected one actions/cache step, found {len(starts)}"
    found: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if re.match(r"\s*- ", line):  # the next step
            break
        if match := re.fullmatch(r"\s+(path|key): (.+?)\s*", line):
            found[match.group(1)] = match.group(2)
    return found


def cache_problems(dashboard: str, backfill: str) -> list[str]:
    first, second = cache_step(dashboard), cache_step(backfill)
    problems = []
    for field in ("path", "key"):
        if field not in first or field not in second:
            problems.append(f"a cache step has no {field}")
        elif first[field] != second[field]:
            problems.append(
                f"the cache {field} differs: {first[field]!r} against {second[field]!r}"
            )
    key = first.get("key", "")
    problems += [
        f"the cache key leaves out env.{name}" for name in KEYED_ENV if f"env.{name}" not in key
    ]
    hashed = re.search(r"hashFiles\(([^)]*)\)", key)
    if not hashed:
        problems.append("the cache key hashes no files")
    else:
        for name in re.findall(r"'([^']+)'", hashed.group(1)):
            if not (ROOT / name).is_file():
                problems.append(f"the cache key hashes {name}, which does not exist")
    return problems


def job_text(text: str, name: str) -> str:
    """One job's lines: from its `  name:` key to the next job's key."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == f"  {name}:"), None)
    assert start is not None, f"no job {name!r}"
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"  \w[\w-]*:", lines[i])), len(lines)
    )
    return "\n".join(lines[start:end])


def render_problems(text: str) -> list[str]:
    """The render job must read this run's own records, after it has fetched the history.

    The record job pushes them to bench-history, but nothing orders it before the render job, so
    the fetched branch alone can lack the point this run just measured. The download comes after
    the fetch because git will not add a worktree over a directory that already has files in it.
    """
    render = job_text(text, "render")
    fetch = render.find("name: Fetch the history")
    problems = [] if fetch >= 0 else ["the render job does not fetch the history"]
    for artifact, folder, flag in (
        ("history-record", "history/records", "--history"),
        ("history-record-bsbm", "history/records-bsbm", "--bsbm-history"),
    ):
        step = re.search(rf"name: {artifact}\s+path: {folder}\s", render)
        if not step:
            problems.append(f"the render job does not download {artifact} into {folder}")
        elif step.start() < fetch:
            problems.append(f"the render job downloads {artifact} before it fetches the history")
        if f"{flag} {folder}" not in render:
            problems.append(f"the render job does not pass {flag} {folder}")
    return problems


#: What the deploy job's `if:` must hold: `!cancelled()` lifts the implicit `success()` (which is
#: false once any job before it failed), and the render job's result keeps it to a built page.
DEPLOY_CLAUSES = ("!cancelled()", "needs.render.result == 'success'")


def deploy_problems(text: str) -> list[str]:
    """The deploy job must publish the page whenever the render job built it.

    A job without `if:`, or with a condition that calls no status function, runs only when every
    job before it succeeded. One failed or timed-out BSBM leg, prepare, merge or synthetic build
    would then skip publishing the page the render job (under `!cancelled()`) built anyway.
    """
    deploy = job_text(text, "deploy")
    condition = re.search(r"^    if: (.+)$", deploy, re.MULTILINE)
    if not condition:
        return ["the deploy job has no if:, so any failed job before it skips the deploy"]
    return [
        f"the deploy job's if: lacks {clause}"
        for clause in DEPLOY_CLAUSES
        if clause not in condition.group(1)
    ]


def upload_steps(text: str) -> list[list[str]]:
    """Each `actions/upload-artifact` step's lines: from its `- ` line up to the next line that is
    indented no deeper than that dash (the next step, a comment between steps, or the next job)."""
    lines = text.splitlines()
    steps = []
    for i, line in enumerate(lines):
        dash = re.match(r"( *)- ", line)
        if not dash:
            continue
        depth = len(dash.group(1))
        end = next(
            (
                j
                for j in range(i + 1, len(lines))
                if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip(" ")) <= depth
            ),
            len(lines),
        )
        if any(re.search(r"\buses: actions/upload-artifact@", step) for step in lines[i:end]):
            steps.append(lines[i:end])
    return steps


def step_input(step: list[str], key: str) -> str | None:
    """The value of ``key`` under the step's `with:`."""
    inside = False
    for line in step:
        if re.fullmatch(r"\s+with:\s*", line):
            inside = True
        elif inside and (found := re.fullmatch(rf"\s+{key}: (.+?)\s*", line)):
            return found.group(1)
    return None


def upload_problems(text: str) -> list[str]:
    """Every artifact upload must replace an artifact of the same name.

    A name is unique within a run, and "Re-run failed jobs" keeps the first attempt's artifacts.
    Without `overwrite: true`, a re-run job fails on the name its first attempt uploaded: the
    merge job on `results-bsbm`, once a failed BSBM leg is re-run.
    """
    steps = upload_steps(text)
    named = len(re.findall(r"\buses: actions/upload-artifact@", text))
    problems = [] if steps and len(steps) == named else [f"read {len(steps)} of {named} uploads"]
    for step in steps:
        if step_input(step, "overwrite") != "true":
            artifact = step_input(step, "name") or "?"
            problems.append(f"the upload of {artifact} does not set overwrite: true")
    return problems


def test_every_store_is_in_exactly_one_matrix_group():
    assert matrix_problems(read(DASHBOARD)) == []


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        # the last store of the first multi-store group, dropped
        (lambda t: re.sub(r"(adapters: \w+(?:,\w+)*),\w+", r"\1", t, count=1), "is in no group"),
        # a store that no adapter defines
        (lambda t: re.sub(r"(adapters: \S+)", r"\1,not_a_store", t, count=1), "not a store"),
        # a store in two groups
        (lambda t: re.sub(r"(adapters: \S+)", r"\1,vortex_dict_mem", t, count=2), "more than once"),
    ],
    ids=["missing", "unknown", "duplicate"],
)
def test_the_matrix_check_notices_a_wrong_group(fault, expected):
    problems = matrix_problems(fault(read(DASHBOARD)))
    assert any(expected in problem for problem in problems), problems


def test_both_workflows_measure_the_same_bsbm_data():
    assert env_problems(read(DASHBOARD), read(BACKFILL)) == []


def test_the_env_check_notices_a_different_scale():
    mixes = top_level_env(read(DASHBOARD))["BSBM_MIXES"]
    other = re.sub(r"(BSBM_MIXES: )\d+", r"\g<1>99", read(BACKFILL), count=1)
    assert f"BSBM_MIXES is {mixes} in the dashboard, 99 in the backfill" in env_problems(
        read(DASHBOARD), other
    )


@pytest.mark.parametrize("name", [DASHBOARD, BACKFILL])
def test_the_tools_commit_is_the_one_the_code_pins(name):
    assert top_level_env(read(name)).get("BSBM_TOOLS_COMMIT") == tools.TOOLS_COMMIT


def test_the_backfill_restores_the_cache_the_dashboard_saves():
    assert cache_problems(read(DASHBOARD), read(BACKFILL)) == []


def test_the_cache_check_notices_a_different_key():
    other = read(BACKFILL).replace("bench/bsbm/prepare.py", "bench/bsbm/prepare_.py")
    assert any("key differs" in problem for problem in cache_problems(read(DASHBOARD), other))


def test_the_cache_check_notices_a_file_that_is_gone():
    gone = [
        read(name).replace("bench/bsbm/prepare.py", "bench/bsbm/gone.py")
        for name in (DASHBOARD, BACKFILL)
    ]
    assert "the cache key hashes bench/bsbm/gone.py, which does not exist" in cache_problems(*gone)


def test_the_page_includes_the_records_its_own_run_measured():
    assert render_problems(read(DASHBOARD)) == []


RENDER = """\
jobs:
  render:
    steps:
{first}
      - name: Fetch the history
        run: git worktree add --detach history FETCH_HEAD
{second}
      - name: Render dashboard
        run: render --history history/records --bsbm-history history/records-bsbm
  deploy:
"""
DOWNLOADS = """\
      - uses: actions/download-artifact@v4
        with:
          name: history-record
          path: history/records
      - uses: actions/download-artifact@v4
        with:
          name: history-record-bsbm
          path: history/records-bsbm"""


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("", DOWNLOADS, None),
        (DOWNLOADS, "", "before it fetches the history"),
        ("", "", "does not download history-record into history/records"),
    ],
    ids=["after the fetch", "before the fetch", "absent"],
)
def test_the_render_check_notices_a_download_in_the_wrong_place(first, second, expected):
    problems = render_problems(RENDER.format(first=first, second=second))
    if expected is None:
        assert problems == []
    else:
        assert any(expected in problem for problem in problems), problems


def test_the_page_is_deployed_whenever_the_render_job_built_it():
    assert deploy_problems(read(DASHBOARD)) == []


DEPLOY_IF = "    if: ${{ !cancelled() && needs.render.result == 'success' }}\n"


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("", "has no if:"),
        ("    if: needs.render.result == 'success'\n", "lacks !cancelled()"),
        ("    if: ${{ !cancelled() }}\n", "lacks needs.render.result == 'success'"),
    ],
    ids=["removed", "implicit success()", "render unchecked"],
)
def test_the_deploy_check_notices_a_wrong_condition(condition, expected):
    text = read(DASHBOARD)
    deploy = text.index("\n  deploy:\n")
    faulty = text[:deploy] + text[deploy:].replace(DEPLOY_IF, condition, 1)
    assert faulty != text, "the deploy job's if: is not the line this test replaces"
    problems = deploy_problems(faulty)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize("name", [DASHBOARD, BACKFILL])
def test_every_upload_replaces_what_an_earlier_attempt_uploaded(name):
    assert upload_problems(read(name)) == []


def test_the_upload_check_notices_one_missing_overwrite():
    text = read(DASHBOARD)
    faulty = re.sub(r"\n +overwrite: true(?=\n)", "", text, count=1)
    assert faulty != text
    problems = upload_problems(faulty)
    assert len(problems) == 1 and "does not set overwrite: true" in problems[0], problems
