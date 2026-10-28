"""The CI workflows against the code they run.

The dashboard's matrix must cover every store exactly once (the merge job names any store that no
group reported as a failure), the backfill must measure the same BSBM data as the dashboard (its
records go on the same line, and it restores the cache the dashboard saved), the backfill must
measure the anchor it corrects drift with, the page must include the point its own run just
measured and be published whenever it was built, records must keep a commit's newest measurement
wherever they are placed, and a re-run job must be able to upload its artifacts again.

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
from bench.history import CONFIGURATIONS, REFERENCE

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


def history_problems(text: str) -> list[str]:
    """The history's stores must share one matrix job, and that job must write the BSBM record.

    The chart divides rdflib's time by each configuration's. Two jobs land on runners that may
    differ in CPU model, so a store of the history in another job than rdflib's would plot that
    difference as a speedup; a record written by another job would name another machine's CPU.
    """
    wanted = {*CONFIGURATIONS, REFERENCE}
    holding = [group for group, slugs in matrix_groups(text) if wanted & set(slugs)]
    if len(holding) != 1:
        return [f"the history's stores are spread over {len(holding)} matrix groups: {holding}"]
    group = holding[0]
    slugs = dict(matrix_groups(text))[group]
    problems = [f"{slug} is not in the {group} group" for slug in sorted(wanted - set(slugs))]
    bsbm = job_text(text, "bsbm")
    step = re.search(
        r"- name: Record this commit's BSBM point\n\s+if: (.+)\n"
        r"(?:.*\n){0,2}?.*bench\.history record bsbm-partial/(\S+)\.json",
        bsbm,
    )
    if not step:
        problems.append("the bsbm job does not record the BSBM point from its group's results")
    else:
        if f"matrix.group == '{group}'" not in step.group(1):
            problems.append(f"the BSBM record step does not run in the {group} group alone")
        if step.group(2) != group:
            problems.append(f"the BSBM record reads {step.group(2)}.json, not {group}.json")
    if "bench.history record" in job_text(text, "bsbm-merge"):
        problems.append("the merge job writes a BSBM record, on a runner that measured nothing")
    return problems


def backfill_problems(text: str) -> list[str]:
    """The backfill must be able to run as an experiment: measure a commit more than once, hand
    over its raw results, and leave the history alone when its `push` input is off. It must also
    keep what corrects its records for drift: the loop measures in bench.backfill's order (the
    anchor among the others), every record names its place in the job and keeps a folder of its
    own, and the collect step runs after a measure step that ran out of time too, before the
    records are handed over."""
    problems = []
    measure = job_text(text, "measure")
    if "bench.backfill schedule" not in measure or "for commit in $order; do" not in measure:
        problems.append("the loop does not follow bench.backfill's order, so it measures no anchor")
    calls = re.findall(r"bench\.history record\b(.*?); then", measure, re.S)
    if len(calls) != 2:
        problems.append(f"expected a synthetic and a BSBM record call, found {len(calls)}")
    for call in calls:
        if '--position "$position"' not in call:
            problems.append("a record does not name its place in the job (--position)")
        if '--out "out/measured/$run/' not in call:
            problems.append("a record is not kept apart per measurement (out/measured/$run)")
    collect = re.search(
        r"- name: Collect each commit's record\n\s+if: (.+)\n(?:.*\n)*?"
        r'.*bench\.backfill collect --anchor "\$ANCHOR"',
        measure,
    )
    if not collect:
        problems.append("no step collects the records with the anchor")
    else:
        if "!cancelled()" not in collect.group(1):
            problems.append("the collect step skips a measure step that ran out of time")
        if measure.find("name: backfill-records") < collect.start():
            problems.append("the records are handed over before they are collected")
    if re.search(r"(lib|deps|results(?:-bsbm)?)-\$commit", measure):
        problems.append("a commit's files are named by the commit alone, so it cannot repeat")
    upload = re.search(r"name: backfill-results\s+path: (.+)", measure)
    if not upload:
        problems.append("the measure job does not hand over its raw results")
    elif "results" not in upload.group(1):
        problems.append(f"the raw results artifact uploads {upload.group(1)}")
    push = re.search(r"^    if: (.+)$", job_text(text, "push"), re.MULTILINE)
    if not push or "inputs.push" not in push.group(1):
        problems.append("the push job runs whatever the push input says")
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
    """The render job must add this run's own records to the history it fetched, keeping a
    commit's newest measurement.

    The record job pushes them to bench-history, but nothing orders it before the render job, so
    the fetched branch alone can lack the point this run just measured. A re-run render job finds
    a branch a backfill may have re-measured the commit on since, so the run's records go in with
    bench.place_records, which keeps the later measurement. That comes after the fetch because git
    will not add a worktree over a directory that already has files in it.
    """
    render = job_text(text, "render")
    fetch = render.find("name: Fetch the history")
    problems = [] if fetch >= 0 else ["the render job does not fetch the history"]
    for artifact, folder, flag in (
        ("history-record", "history/records", "--history"),
        ("history-record-bsbm", "history/records-bsbm", "--bsbm-history"),
    ):
        download = re.search(rf"name: {artifact}\s+path: (\S+)\s", render)
        place = re.search(rf"bench\.place_records (\S+) {folder}\s", render)
        if not download:
            problems.append(f"the render job does not download {artifact}")
        elif not place or place.group(1) != download.group(1):
            problems.append(f"the render job does not place {artifact} with bench.place_records")
        elif place.start() < fetch:
            problems.append(f"the render job places {artifact} before it fetches the history")
        if f"{flag} {folder}" not in render:
            problems.append(f"the render job does not pass {flag} {folder}")
    return problems


def placement_problems(text: str, job: str) -> list[str]:
    """A job that pushes records to bench-history must keep a commit's newest measurement there:
    a re-run record job, or a backfill that measured before a later run, would otherwise lay an
    earlier measurement over a later one. It places them with bench.place_records, never `cp`."""
    body = job_text(text, job)
    problems = []
    if not re.search(r"bench\.place_records \S*incoming\S* \S*history\S*", body):
        problems.append(f"the {job} job does not place its records with bench.place_records")
    if re.search(r"\bcp\b[^\n]*history/", body):
        problems.append(f"the {job} job copies records into history/, over any later measurement")
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


def test_the_history_is_measured_and_recorded_in_one_job():
    assert history_problems(read(DASHBOARD)) == []


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        # rdflib moved to a job of its own
        (
            lambda t: t.replace(",rdflib_memory\n", "\n", 1).replace(
                "adapters: rdflib_hdt", "adapters: rdflib_hdt,rdflib_memory", 1
            ),
            "spread over 2 matrix groups",
        ),
        # the record written by every group's job
        (lambda t: t.replace("if: matrix.group == 'history' && github", "if: github", 1), "alone"),
        # the record written by the merge job again
        (
            lambda t: t.replace(
                "bench.bsbm.merge --out",
                "bench.history record bench/results-bsbm.json && bench.bsbm.merge --out",
                1,
            ),
            "measured nothing",
        ),
    ],
    ids=["reference-elsewhere", "every-group", "merge-job"],
)
def test_the_history_check_notices_a_split_or_a_record_elsewhere(fault, expected):
    problems = history_problems(fault(read(DASHBOARD)))
    assert any(expected in problem for problem in problems), problems


def test_the_backfill_can_repeat_commits_and_correct_their_drift():
    assert backfill_problems(read(BACKFILL)) == []


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (
            lambda t: t.replace('lib="$RUNNER_TEMP/lib-$run"', 'lib="$RUNNER_TEMP/lib-$commit"', 1),
            "repeat",
        ),
        (lambda t: t.replace("name: backfill-results", "name: backfill-other", 1), "raw results"),
        (lambda t: t.replace("&& inputs.push }}", "}}", 1), "push input"),
        (
            lambda t: t.replace("for commit in $order; do", "for commit in $COMMITS; do", 1),
            "anchor",
        ),
        (lambda t: t.replace(' --position "$position"', "", 1), "place in the job"),
        (lambda t: t.replace('"out/measured/$run/records"', "out/records", 1), "kept apart"),
        (
            lambda t: re.sub(
                r"(- name: Collect each commit's record\n\s+if: ).+", r"\1success()", t, count=1
            ),
            "ran out of time",
        ),
    ],
    ids=[
        "per-commit-paths",
        "no-raw-results",
        "push-ignored",
        "listed-order",
        "no-position",
        "shared-folder",
        "collect-skipped",
    ],
)
def test_the_backfill_check_notices_a_fault(fault, expected):
    text = read(BACKFILL)
    faulty = fault(text)
    assert faulty != text, "the fault did not apply"
    problems = backfill_problems(faulty)
    assert any(expected in problem for problem in problems), problems


#: The jobs that push records to bench-history, how they place a folder of records, and the `cp`
#: that would lay an earlier measurement over a later one.
PUSHING_JOBS = [(DASHBOARD, "record"), (BACKFILL, "push")]
PLACE_PUSHED = 'python3 -m bench.place_records "incoming/$folder" "history/$folder"'
COPY_PUSHED = 'cp "incoming/$folder/"*.json "history/$folder/"'


@pytest.mark.parametrize(("name", "job"), PUSHING_JOBS)
def test_the_jobs_that_push_records_keep_a_commits_newest_measurement(name, job):
    assert placement_problems(read(name), job) == []


@pytest.mark.parametrize(("name", "job"), PUSHING_JOBS)
def test_the_placement_check_notices_a_copy(name, job):
    text = read(name)
    faulty = text.replace(PLACE_PUSHED, COPY_PUSHED, 1)
    assert faulty != text, f"the {job} job does not place records the way this test replaces"
    problems = placement_problems(faulty, job)
    assert any("copies records" in problem for problem in problems), problems


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
{downloads}
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
          path: {folder}/records
      - uses: actions/download-artifact@v4
        with:
          name: history-record-bsbm
          path: {folder}/records-bsbm"""
PLACE = """\
      - name: Add this run's points to the history
        run: |
          uv run python -m bench.place_records own/records history/records
          uv run python -m bench.place_records own/records-bsbm history/records-bsbm"""


@pytest.mark.parametrize(
    ("folder", "first", "second", "expected"),
    [
        ("own", "", PLACE, None),
        ("own", PLACE, "", "before it fetches the history"),
        # the run's records laid straight over the fetched ones, newer or not
        ("history", "", "", "does not place history-record with bench.place_records"),
        (None, "", "", "does not download history-record"),
    ],
    ids=["placed after the fetch", "placed before the fetch", "laid over", "absent"],
)
def test_the_render_check_notices_records_placed_wrongly(folder, first, second, expected):
    downloads = DOWNLOADS.format(folder=folder) if folder else ""
    problems = render_problems(RENDER.format(downloads=downloads, first=first, second=second))
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
