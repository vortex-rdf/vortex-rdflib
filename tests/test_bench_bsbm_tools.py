"""The official BSBM tools' plumbing, without Java: the pinned download and its
unpacking, the capture endpoint the driver talks to, and the stream split."""

import hashlib
import io
import os
import socket
import stat
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from bench.bsbm import streams, tools
from bench.bsbm.capture import EMPTY_GRAPH, EMPTY_RESULTS, CaptureServer

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no http_proxy to localhost


def ask(url: str, text: str, accept: str) -> tuple[int, str, bytes]:
    """A request shaped like the driver's: GET, the query form-encoded."""
    request = urllib.request.Request(
        f"{url}?query={urllib.parse.quote_plus(text)}", headers={"Accept": accept}
    )
    with DIRECT.open(request, timeout=10) as response:
        return response.status, response.headers["Content-Type"], response.read()


def test_the_endpoint_records_every_query_in_order_and_answers_by_accept():
    texts = [
        "SELECT ?s WHERE { ?s ?p 1 + 2 }",
        "DESCRIBE <http://example.org/r%20x>",
        'CONSTRUCT { ?s ?p "é" } WHERE { ?s ?p ?o }',
    ]
    accepts = ["application/sparql-results+xml", "application/rdf+xml", "application/rdf+xml"]
    with CaptureServer() as server:
        answers = [ask(server.url, t, a) for t, a in zip(texts, accepts, strict=True)]
    assert server.queries == texts and server.missing == 0
    assert answers[0] == (200, "application/sparql-results+xml", EMPTY_RESULTS)
    assert answers[1] == (200, "application/rdf+xml", EMPTY_GRAPH)


def test_a_request_without_a_query_is_answered_and_counted():
    with CaptureServer() as server:
        with DIRECT.open(server.url, timeout=10) as response:
            assert response.status == 200
    assert server.queries == [] and server.missing == 1


def test_the_warmup_is_split_off_by_count_and_both_streams_number_from_zero():
    mix = list(streams.EXPLORE_MIX)
    texts = [f"SELECT * WHERE {{ ?s ?p {k} }}" for k in range(len(mix) * 3)]
    warmup, measured = streams.split_streams(texts, mix, warmup_mixes=1, mixes=2)
    assert [q["text"] for q in warmup] == texts[:25]
    assert [q["text"] for q in measured] == texts[25:]
    assert [q["i"] for q in measured] == list(range(50))
    assert [q["q"] for q in measured] == mix * 2
    assert [q["mix"] for q in measured] == [0] * 25 + [1] * 25
    assert warmup[0] == {"mix": 0, "pos": 0, "q": 1, "i": 0, "text": texts[0]}


def test_a_capture_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError, match="captured 24 queries, expected 25"):
        streams.split_streams(["SELECT * {}"] * 24, list(streams.EXPLORE_MIX), 0, 1)


def test_an_unfilled_placeholder_is_named():
    texts = ["SELECT * {}"] * 25
    texts[3] = "SELECT * WHERE { ?p a %ProductType% . FILTER (?v > %x%) }"
    with pytest.raises(ValueError, match="%ProductType%, %x%"):
        streams.split_streams(texts, list(streams.EXPLORE_MIX), 0, 1)


def test_the_mix_leaves_out_ignored_queries(tmp_path):
    (tmp_path / "querymix.txt").write_text("1 2 2\n3\n", encoding="ascii")
    (tmp_path / "ignoreQueries.txt").write_text("2\n", encoding="ascii")
    assert streams.read_mix(tmp_path) == [1, 3]


def fake_tools(root: Path) -> Path:
    explore = root / "queries" / "explore"
    explore.mkdir(parents=True)
    for q in range(1, 13):
        (explore / f"query{q}.txt").write_text(f"SELECT {q}", encoding="ascii")
    (explore / "querymix.txt").write_text(streams.EXPLORE_QUERYMIX + "\n", encoding="ascii")
    (explore / "ignoreQueries.txt").write_text("", encoding="ascii")
    return root


def test_a_single_query_use_case_runs_one_official_template(tmp_path):
    usecase, mix_dir = tools.single_query_usecase(fake_tools(tmp_path / "t"), 6, tmp_path / "w")
    assert usecase.read_text(encoding="utf-8") == f"querymix={mix_dir}\n"
    assert mix_dir.is_absolute() and streams.read_mix(mix_dir) == [6]
    assert (mix_dir / "query6.txt").read_text(encoding="ascii") == "SELECT 6"


def test_a_template_the_tools_do_not_have_is_refused(tmp_path):
    with pytest.raises(tools.ToolsError, match="query13.txt"):
        tools.single_query_usecase(fake_tools(tmp_path / "t"), 13, tmp_path / "w")


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"bsbm-tools-{tools.TOOLS_COMMIT}/{name}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_pinned_archive_is_unpacked_with_runnable_scripts(tmp_path, monkeypatch):
    data = archive({"lib/bsbm.jar": b"jar", "generate": b"#!/bin/bash\n", "testdriver": b"x"})
    monkeypatch.setattr(tools, "TOOLS_SHA256", hashlib.sha256(data).hexdigest())
    unpacked = tools.fetch_tools(tmp_path, fetch=lambda url: data)
    assert unpacked == tmp_path / f"bsbm-tools-{tools.TOOLS_COMMIT}"
    assert (unpacked / "lib" / "bsbm.jar").read_bytes() == b"jar"
    if os.name != "nt":
        assert (unpacked / "generate").stat().st_mode & stat.S_IXUSR
    assert tools.fetch_tools(tmp_path, fetch=lambda url: pytest.fail("fetched twice")) == unpacked


def test_an_archive_with_another_checksum_is_refused_not_skipped(tmp_path):
    with pytest.raises(tools.ToolsError, match="refusing to run it") as caught:
        tools.fetch_tools(tmp_path, fetch=lambda url: archive({"lib/bsbm.jar": b"x"}))
    assert not isinstance(caught.value, tools.ToolsUnavailable)
    assert not any(tmp_path.iterdir())


def test_an_unreachable_download_makes_the_tools_unavailable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(tools.ToolsUnavailable, match="could not download"):
        tools.download(f"http://127.0.0.1:{port}/bsbm-tools.tar.gz")


def test_without_java_the_error_says_what_to_install(monkeypatch):
    monkeypatch.setattr(tools, "WINDOWS", False)
    monkeypatch.setattr(tools.shutil, "which", lambda n: None if n == "java" else "/bin/" + n)
    with pytest.raises(tools.ToolsUnavailable, match="java is not on PATH"):
        tools.require_runtime()
