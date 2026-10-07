"""``python -m bench.bsbm.prepare``. Unit tests fake the two Java runs
(``tools.generate``, ``prepare.capture_streams``); the last test runs the real
tools and skips when Java, bash or the download is missing."""

import json
import os
import shutil
from pathlib import Path

import pytest
from bench.bsbm import prepare, streams, tools
from tests.bsbm_tiny import tiny_texts, write_prepared, write_tiny_bsbm


def forbidden(*args, **kwargs):
    raise AssertionError("a BSBM tool ran")


@pytest.fixture
def no_java(monkeypatch):
    monkeypatch.setattr(prepare.tools, "generate", forbidden)
    monkeypatch.setattr(prepare, "capture_streams", forbidden)
    monkeypatch.setattr(prepare.tools, "require_runtime", lambda: None)
    monkeypatch.setattr(prepare.tools, "fetch_tools", lambda *a, **k: Path("unused"))


def fake_capture(tools_dir, td_data, *, warmup_mixes, mixes, seed, only_query):
    mix = [only_query] if only_query is not None else list(streams.EXPLORE_MIX)
    return tiny_texts(mix, warmup_mixes + mixes), mix, {q: f"template {q}" for q in set(mix)}


def fake_generate(tools_dir, products, out_dir):
    """What the generator does to ``out_dir``: opens ``dataset.nt`` and writes into
    ``td_data/``, so through any symlink standing at either path. The bytes depend on
    ``products``, so a write into another directory's dataset shows."""
    (out_dir / "dataset.nt").write_text(f'<urn:s> <urn:p> "{products}" .\n', encoding="utf-8")
    (out_dir / "td_data").mkdir(exist_ok=True)
    (out_dir / "td_data" / "pool.dat").write_text(f"{products}\n", encoding="utf-8")
    return 1


def args(out, *extra):
    return ["--warmup-mixes", "1", "--mixes", "2", "--out", str(out), *extra]


def test_the_same_parameters_again_are_a_cache_hit(tmp_path, no_java):
    out = write_prepared(tmp_path / "p", warmup_mixes=1, mixes=2)
    before = (out / "meta.json").read_text()
    assert prepare.main(args(out, "--products", "6")) == 0
    assert (out / "meta.json").read_text() == before


def test_a_cache_hit_needs_neither_java_nor_a_download(tmp_path, no_java, monkeypatch):
    out = tmp_path / "p"

    def same_parameters() -> dict:
        return prepare.prepare(
            out, products=6, seed=prepare.DRIVER_SEED, warmup_mixes=1, mixes=2, only_query=None
        )

    with monkeypatch.context() as first_run:
        first_run.setattr(prepare.tools, "generate", fake_generate)
        first_run.setattr(prepare, "capture_streams", fake_capture)
        first = same_parameters()
    monkeypatch.setattr(prepare.tools, "require_runtime", forbidden)
    monkeypatch.setattr(prepare.tools, "fetch_tools", forbidden)
    assert same_parameters() == first  # generate and capture_streams are forbidden again too


def test_a_meta_of_another_schema_is_not_a_cache_hit(tmp_path, no_java, monkeypatch):
    out = write_prepared(tmp_path / "p", warmup_mixes=1, mixes=2)
    meta = streams.read_meta(out)
    (out / "meta.json").write_text(
        json.dumps({**meta, "schema": prepare.SCHEMA + 1}), encoding="utf-8"
    )
    captures: list[int] = []

    def capture(*a, **k):
        captures.append(1)
        return fake_capture(*a, **k)

    monkeypatch.setattr(prepare, "capture_streams", capture)
    assert prepare.main(args(out, "--products", "6")) == 0
    assert captures == [1]  # captured again, not a hit
    assert streams.read_meta(out)["schema"] == prepare.SCHEMA


def test_other_mixes_reuse_the_dataset_and_capture_again(tmp_path, no_java, monkeypatch):
    out = write_prepared(tmp_path / "p", warmup_mixes=1, mixes=2)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    assert (
        prepare.main(["--products", "6", "--warmup-mixes", "0", "--mixes", "3", "--out", str(out)])
        == 0
    )
    warmup, measured = streams.load_streams(out)
    assert warmup == [] and len(measured) == 75
    meta = streams.read_meta(out)
    assert (meta["mixes"], meta["warmup_mixes"], meta["dataset"]["path"]) == (3, 0, "dataset.nt")


def test_another_scale_generates_again(tmp_path, no_java, monkeypatch):
    out = write_prepared(tmp_path / "p", warmup_mixes=1, mixes=2)
    calls: list[int] = []

    def generate(tools_dir, products, out_dir):
        calls.append(products)
        write_tiny_bsbm(out_dir / "dataset.nt")
        return 1

    monkeypatch.setattr(prepare.tools, "generate", generate)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    assert prepare.main(args(out, "--products", "7")) == 0
    assert calls == [7] and streams.read_meta(out)["products"] == 7


def test_streams_cut_off_midway_leave_no_meta_json_to_vouch_for_them(
    tmp_path, no_java, monkeypatch
):
    out = write_prepared(tmp_path / "p", warmup_mixes=1, mixes=2)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    write_text = Path.write_text

    def cut_off(self, *a, **k):
        if self.name == "measured.json":
            raise KeyboardInterrupt
        return write_text(self, *a, **k)

    with monkeypatch.context() as cutting:
        cutting.setattr(Path, "write_text", cut_off)
        with pytest.raises(KeyboardInterrupt):
            prepare.main(
                ["--products", "6", "--warmup-mixes", "2", "--mixes", "2", "--out", str(out)]
            )
    assert len(streams.load_streams(out)[0]) == 50  # the new warm-up stream is on disk...
    assert not (out / "meta.json").exists()  # ...so the old parameters must not hit the cache


def test_from_reuses_the_source_dataset_without_generating_or_copying(
    tmp_path, no_java, monkeypatch
):
    source = write_prepared(tmp_path / "s", warmup_mixes=1, mixes=1)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    out = tmp_path / "q6"
    q6 = [
        "--from",
        str(source),
        "--only-query",
        "6",
        "--warmup-mixes",
        "1",
        "--mixes",
        "2",
        "--out",
        str(out),
    ]
    assert prepare.main(q6) == 0
    meta = streams.read_meta(out)
    assert (meta["products"], meta["mix"], meta["only_query"]) == (6, [6], 6)
    assert meta["dataset"]["from"] == str(source.resolve())
    assert streams.dataset_path(out, meta) == (source / "dataset.nt").resolve()
    copied = out / "dataset.nt"
    assert not copied.exists() or copied.is_symlink()
    warmup, measured = streams.load_streams(out)
    assert [q["q"] for q in warmup + measured] == [6, 6, 6]
    monkeypatch.setattr(prepare, "capture_streams", forbidden)
    assert prepare.main(q6) == 0  # a cache hit: neither tool runs


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="os.symlink is unavailable here")
@pytest.mark.parametrize("keep_source", [True, False], ids=["source-kept", "source-deleted"])
def test_regenerating_over_a_from_directory_never_writes_through_its_links(
    tmp_path, no_java, monkeypatch, keep_source
):
    source = write_prepared(tmp_path / "s", warmup_mixes=1, mixes=1)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    out = tmp_path / "q"
    assert prepare.main(args(out, "--from", str(source), "--only-query", "6")) == 0
    if not ((out / "dataset.nt").is_symlink() and (out / "td_data").is_symlink()):
        pytest.skip("this platform left no symlinks for --from")
    dataset_bytes = (source / "dataset.nt").read_bytes()
    pool_files = sorted(p.name for p in (source / "td_data").iterdir())
    if not keep_source:
        shutil.rmtree(source)  # the links dangle now
    monkeypatch.setattr(prepare.tools, "generate", fake_generate)
    assert prepare.main(args(out, "--products", "7")) == 0
    if keep_source:  # the source is still what its own meta.json describes
        assert (source / "dataset.nt").read_bytes() == dataset_bytes
        assert sorted(p.name for p in (source / "td_data").iterdir()) == pool_files
    assert not (out / "dataset.nt").is_symlink() and not (out / "td_data").is_symlink()
    meta = streams.read_meta(out)
    assert (meta["products"], meta["dataset"]["from"]) == (7, None)
    assert (out / "dataset.nt").read_text(encoding="utf-8") == '<urn:s> <urn:p> "7" .\n'
    assert (out / "td_data" / "pool.dat").read_text(encoding="utf-8") == "7\n"


def test_from_and_other_products_are_refused(tmp_path, no_java, capsys):
    source = write_prepared(tmp_path / "s", warmup_mixes=1, mixes=1)
    with pytest.raises(SystemExit) as stop:
        prepare.main(args(tmp_path / "o", "--from", str(source), "--products", "99"))
    assert stop.value.code == 2 and "disagrees with --from" in capsys.readouterr().err


def test_from_and_the_same_products_are_accepted(tmp_path, no_java, monkeypatch):
    source = write_prepared(tmp_path / "s", warmup_mixes=1, mixes=1)
    monkeypatch.setattr(prepare, "capture_streams", fake_capture)
    assert prepare.main(args(tmp_path / "o", "--from", str(source), "--products", "6")) == 0


def test_without_java_prepare_says_so(tmp_path, monkeypatch, capsys):
    def missing():
        raise tools.ToolsUnavailable("java is not on PATH: install a Java runtime")

    monkeypatch.setattr(prepare.tools, "require_runtime", missing)
    assert prepare.main(args(tmp_path / "o", "--products", "10")) == 1
    assert "java is not on PATH" in capsys.readouterr().err


@pytest.fixture(scope="module")
def official_tools() -> Path:
    try:
        tools.require_runtime()
        return tools.fetch_tools()
    except tools.ToolsUnavailable as error:
        pytest.skip(f"the official BSBM tools cannot run here: {error}")


def test_the_official_tools_prepare_streams_and_a_q6_stream(official_tools, tmp_path, monkeypatch):
    out = tmp_path / "p100"
    real = ["--products", "100", "--warmup-mixes", "1", "--mixes", "1", "--out", str(out)]
    assert prepare.main(real) == 0
    meta = streams.read_meta(out)
    assert meta["mix"] == list(streams.EXPLORE_MIX)
    assert meta["dataset"]["triples"] > 10_000 and len(meta["dataset"]["sha256"]) == 64
    warmup, measured = streams.load_streams(out)
    assert len(warmup) == len(measured) == 25
    assert [q["q"] for q in measured] == list(streams.EXPLORE_MIX)
    assert [q["text"] for q in warmup] != [q["text"] for q in measured]  # fresh parameters
    assert all("%" not in q["text"] for q in warmup + measured)
    monkeypatch.setattr(prepare.tools, "generate", forbidden)
    monkeypatch.setattr(prepare, "capture_streams", forbidden)
    assert prepare.main(real) == 0  # identical parameters: nothing runs
    monkeypatch.undo()
    monkeypatch.setattr(prepare.tools, "generate", forbidden)
    q6 = tmp_path / "q6"
    assert (
        prepare.main(
            [
                "--from",
                str(out),
                "--only-query",
                "6",
                "--warmup-mixes",
                "1",
                "--mixes",
                "2",
                "--out",
                str(q6),
            ]
        )
        == 0
    )
    w6, m6 = streams.load_streams(q6)
    assert [q["q"] for q in w6 + m6] == [6, 6, 6] and all("regex" in q["text"] for q in w6 + m6)
