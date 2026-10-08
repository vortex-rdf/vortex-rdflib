"""The official BSBM tools: fetched once, checksummed, run with Java.

``Tpt/bsbm-tools`` at ``TOOLS_COMMIT`` is SourceForge ``bsbmtools`` 0.2 in git
(same ``lib/bsbm.jar``; text files differ only CRLF -> LF). The archive is
checked against ``TOOLS_SHA256`` before anything is unpacked, into
``$BENCH_CACHE`` or ``~/.cache/vortex-rdflib/bsbm``. The ``generate`` and
``testdriver`` bash scripts start ``java -cp ... -Xmx256M`` and refuse to run
outside the tools directory, so every call runs there (the driver also writes
``run.log`` and ``steadystate.tsv`` there).
"""

import hashlib
import http.client
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

TOOLS_REPO = "Tpt/bsbm-tools"
TOOLS_COMMIT = "59d0a8a605b26f21506789fa1a713beb5abf1cab"
TOOLS_URL = f"https://github.com/{TOOLS_REPO}/archive/{TOOLS_COMMIT}.tar.gz"
TOOLS_SHA256 = "e06763d6a92702f27fc4c81be75427c0ff9fd9528e31a6979d387f13a422913f"
DOWNLOAD_TIMEOUT_S = 60
GENERATE_TIMEOUT_S = 3 * 3600
DRIVER_TIMEOUT_S = 3600
#: The scripts join the classpath with ":", which Java on Windows does not read.
WINDOWS = os.name == "nt"
TRIPLES_LINE = re.compile(r"(\d+) triples generated")
_java_runs: bool | None = None


class ToolsError(RuntimeError):
    """The tools misbehaved: a failure to report, never one to skip."""


class ToolsUnavailable(ToolsError):
    """Java, bash or the download is missing here: nothing ran."""


def cache_dir() -> Path:
    configured = os.environ.get("BENCH_CACHE")
    return Path(configured) if configured else Path.home() / ".cache" / "vortex-rdflib" / "bsbm"


def require_runtime() -> None:
    """Refuse, saying what to install, when the scripts cannot start here."""
    global _java_runs
    if WINDOWS:
        raise ToolsUnavailable("the BSBM scripts build a POSIX classpath: use Linux or macOS")
    if shutil.which("bash") is None:
        raise ToolsUnavailable("bash is not on PATH: the BSBM tools start through bash scripts")
    if shutil.which("java") is None:
        raise ToolsUnavailable(
            "java is not on PATH: the official BSBM generator and test driver are Java "
            "programs. Install a Java runtime (e.g. `sudo apt install default-jre-headless`; "
            "in CI, actions/setup-java) and run again."
        )
    if _java_runs is None:
        try:
            probe = subprocess.run(["java", "-version"], capture_output=True, timeout=60)
            _java_runs = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _java_runs = False
    if not _java_runs:
        raise ToolsUnavailable("java is on PATH but `java -version` fails: install a Java runtime")


def download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
            return response.read()
    except (OSError, http.client.HTTPException) as error:
        # URLError, HTTPError and timeouts are OSErrors; a truncated or garbled response
        # (IncompleteRead, BadStatusLine) is an HTTPException, which is not.
        raise ToolsUnavailable(f"could not download {url}: {error}") from error


def fetch_tools(cache: Path | None = None, fetch: Callable[[str], bytes] = download) -> Path:
    """The unpacked tools: downloaded, checked and unpacked on first use."""
    root = cache or cache_dir()
    tools_dir = root / f"bsbm-tools-{TOOLS_COMMIT}"
    if (tools_dir / "lib" / "bsbm.jar").is_file():
        return tools_dir
    data = fetch(TOOLS_URL)
    digest = hashlib.sha256(data).hexdigest()
    if digest != TOOLS_SHA256:
        raise ToolsError(
            f"{TOOLS_URL} has SHA-256 {digest}, expected {TOOLS_SHA256}: refusing to run it"
        )
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(staging, filter="data")
            else:
                # Python < 3.11.4 has no extraction filters; the checksum above vouches
                # for the archive.
                archive.extractall(staging)
        unpacked = staging / f"bsbm-tools-{TOOLS_COMMIT}"
        if not (unpacked / "lib" / "bsbm.jar").is_file():
            raise ToolsError(f"{TOOLS_URL} holds no bsbm-tools-{TOOLS_COMMIT}/lib/bsbm.jar")
        for script in ("generate", "testdriver"):
            path = unpacked / script
            if path.is_file():
                path.chmod(path.stat().st_mode | 0o111)
        try:
            unpacked.rename(tools_dir)
        except OSError:
            if not (tools_dir / "lib" / "bsbm.jar").is_file():
                raise  # not another process having got there first
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tools_dir


def generate(tools_dir: Path, products: int, out_dir: Path) -> int:
    """``./generate -pc N -s nt``: ``out_dir/dataset.nt`` and ``out_dir/td_data``.

    No ``-fc``: the official default (the driver draws product types that type
    products directly, so the queries are the same). Deterministic per N.
    Returns the triple count the generator reports."""
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "./generate",
        "-pc",
        str(products),
        "-s",
        "nt",
        "-fn",
        str(out_dir / "dataset"),
        "-dir",
        str(out_dir / "td_data"),
    ]
    proc = _run(tools_dir, args, GENERATE_TIMEOUT_S, "the BSBM generator")
    match = TRIPLES_LINE.search(proc.stdout)
    if match is None:
        raise ToolsError(f"the BSBM generator reported no triple count:\n{_tail(proc)}")
    return int(match.group(1))


def run_testdriver(
    tools_dir: Path,
    endpoint: str,
    *,
    td_data: Path,
    warmup_mixes: int,
    mixes: int,
    seed: int,
    usecase: Path,
    work_dir: Path,
) -> None:
    """The unmodified official test driver, run against ``endpoint``."""
    args = [
        "./testdriver",
        "-w",
        str(warmup_mixes),
        "-runs",
        str(mixes),
        "-seed",
        str(seed),
        "-idir",
        str(td_data.resolve()),
        "-ucf",
        str(usecase.resolve()),
        "-o",
        str((work_dir / "benchmark_result.xml").resolve()),
        endpoint,
    ]
    proc = _run(tools_dir, args, DRIVER_TIMEOUT_S, "the BSBM test driver")
    if "Received error code" in proc.stderr or "SAX Error" in proc.stderr:
        raise ToolsError(f"the test driver rejected a capture answer:\n{_tail(proc)}")


def official_usecase(tools_dir: Path) -> tuple[Path, Path]:
    """The official Explore use case and the query-mix directory it names."""
    return tools_dir / "usecases" / "explore" / "sparql.txt", tools_dir / "queries" / "explore"


def single_query_usecase(tools_dir: Path, query: int, work_dir: Path) -> tuple[Path, Path]:
    """A use case whose mix is the official template ``query`` alone: a copy of
    ``queries/explore`` with ``querymix.txt`` = ``query`` and an empty
    ``ignoreQueries.txt``, so template and parameters stay official."""
    source = tools_dir / "queries" / "explore"
    if not (source / f"query{query}.txt").is_file():
        raise ToolsError(f"the official Explore queries have no query{query}.txt")
    work_dir.mkdir(parents=True, exist_ok=True)
    mix_dir = (work_dir / f"explore-q{query}").resolve()
    if "=" in str(mix_dir):  # the driver splits use-case lines on "="
        raise ToolsError(f"the test driver cannot name {mix_dir} in a use case")
    shutil.copytree(source, mix_dir, dirs_exist_ok=True)
    (mix_dir / "querymix.txt").write_text(f"{query}\n", encoding="ascii")
    (mix_dir / "ignoreQueries.txt").write_text("", encoding="ascii")
    usecase = work_dir / f"usecase-q{query}.txt"
    usecase.write_text(f"querymix={mix_dir}\n", encoding="utf-8")
    return usecase, mix_dir


def _run(
    tools_dir: Path, args: list[str], timeout_s: int, what: str
) -> subprocess.CompletedProcess[str]:
    require_runtime()
    try:
        proc = subprocess.run(
            ["bash", *args],
            cwd=tools_dir,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolsError(f"{what} ran over {timeout_s} s") from error
    if proc.returncode != 0:
        raise ToolsError(f"{what} exited {proc.returncode}:\n{_tail(proc)}")
    return proc


def _tail(proc: subprocess.CompletedProcess[str], lines: int = 20) -> str:
    return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-lines:])
