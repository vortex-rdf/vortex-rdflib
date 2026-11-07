"""The native boundary (:mod:`vortex_rdflib.boundary`): every call into
vortex-rdf's store and term dictionary goes through it, which gives a call
the query's cancel token by one policy, traces it while the query is
traced, and counts it while the counters are on — and hands back the bare
handles when nothing observes."""

import ast
import cProfile
import pstats
from pathlib import Path

import pytest
from tests.support import file_codes
from vortex_rdf import Cancelled, CancelToken, serialize_rdf

from vortex_rdflib import VortexRdflibStore, boundary
from vortex_rdflib.cancel import cancel_scope
from vortex_rdflib.settings import SETTINGS

EX = "http://ex.org/"


class _Spy:
    """The native handle with every call recorded as ``(name, keywords)``."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return attr(*args, **kwargs)

        return call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _file(tmp_path, quads: bool = False, layout: str = "dictionary") -> str:
    source = tmp_path / ("data.nq" if quads else "data.nt")
    graph = f" <{EX}g1>" if quads else ""
    lines = [f"<{EX}s{i}> <{EX}p> <{EX}o{i}>{graph} ." for i in range(20)]
    if quads:
        lines += [f"<{EX}s{i}> <{EX}p> <{EX}o{i}> <{EX}g2> ." for i in range(10)]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / f"data-{layout}.vortex"
    options = {"format": "nquads"} if quads else {}
    serialize_rdf(str(source), str(out), layout=layout, **options)
    return str(out)


def _spied(path: str, **kwargs) -> tuple[VortexRdflibStore, _Spy]:
    store = VortexRdflibStore(path, **kwargs)
    spy = _Spy(store._opened().native)
    store._opened().native = spy  # ty: ignore[invalid-assignment]
    return store, spy


SCAN = (None, f"<{EX}p>", None, None)


def _cancelled() -> CancelToken:
    token = CancelToken()
    token.cancel()
    return token


def test_bare_handles_when_nothing_observes(tmp_path):
    store = VortexRdflibStore(_file(tmp_path))
    store._opened()
    assert boundary.native(store) is store._opened().native
    assert boundary.term_dict(store) is file_codes(store).term_dict


def test_a_scan_on_a_file_store_races_the_token(tmp_path):
    store, spy = _spied(_file(tmp_path), in_memory=False)
    with cancel_scope(CancelToken()) as token:
        assert boundary.native(store).count_quads(*SCAN) == 20
        cols = boundary.native(store).match_codes(*SCAN)
        assert cols is not None and len(cols[0]) == 20
        boundary.native(store).match_codes(*SCAN, limit=5)
    assert [kwargs.get("cancel") for _, kwargs in spy.calls] == [token, token, token]


def test_in_memory_point_lookup_and_limit_one_check_instead(tmp_path):
    point = (f"<{EX}s1>", f"<{EX}p>", f"<{EX}o1>", None)
    in_memory, memory_spy = _spied(_file(tmp_path), in_memory=True)
    file_store, file_spy = _spied(_file(tmp_path), in_memory=False)
    with cancel_scope(CancelToken()):
        boundary.native(in_memory).count_quads(*SCAN)
        boundary.native(file_store).count_quads(*SCAN, limit=1)
        boundary.native(file_store).match_codes(*point)
    assert all("cancel" not in kwargs for _, kwargs in memory_spy.calls + file_spy.calls)
    memory_spy.calls.clear()
    file_spy.calls.clear()
    with cancel_scope(_cancelled()):
        for call in (
            lambda: boundary.native(in_memory).count_quads(*SCAN),
            lambda: boundary.native(file_store).count_quads(*SCAN, limit=1),
            lambda: boundary.native(file_store).match_codes(*point),
        ):
            with pytest.raises(Cancelled):
                call()
    assert memory_spy.calls == [] and file_spy.calls == []


def test_batches_and_chunks_always_race(tmp_path):
    store, spy = _spied(_file(tmp_path), in_memory=True)
    with cancel_scope(CancelToken()) as token:
        native = boundary.native(store)
        native.count_quads_many([SCAN, SCAN])
        list(native.match_codes_many([SCAN]))
        chunks = native.match_code_chunks(*SCAN, batch_rows=8)
        assert chunks is not None
        list(chunks)
    assert spy.names() == ["count_quads_many", "match_codes_many", "match_code_chunks"]
    assert all(kwargs.get("cancel") is token for _, kwargs in spy.calls)


def test_a_string_fallback_read_races_the_token(tmp_path):
    store, spy = _spied(_file(tmp_path, layout="default"))
    store._union_has_repeats()  # the file's one-time check, outside the scope
    spy.calls.clear()
    token = _cancelled()
    with cancel_scope(token), pytest.raises(Cancelled):
        list(store.triples((None, None, None)))
    assert spy.names() == ["iter_quads"]
    assert spy.calls[0][1].get("cancel") is token


def test_none_passes_through_with_counters_on(tmp_path, monkeypatch):
    store = VortexRdflibStore(_file(tmp_path, layout="default"))
    store._opened()
    monkeypatch.setattr(SETTINGS, "counters", True)
    assert boundary.native(store).match_code_chunks(*SCAN) is None


def test_the_union_check_stops_when_cancelled(tmp_path):
    """Review Focus 1: the one-time check whether some triple is in two
    graphs scans a file-backed store, so it races the token like any scan."""
    store, spy = _spied(_file(tmp_path, quads=True), in_memory=False)
    with cancel_scope(_cancelled()) as token, pytest.raises(Cancelled):
        store._union_has_repeats()
    assert spy.calls == [("count_quads", {"cancel": token})]


def _python_frames(method, *pattern) -> list[str]:
    """The Python functions ``method(*pattern)`` runs, by name (no builtins)."""
    profile = cProfile.Profile()
    profile.enable()
    method(*pattern)
    profile.disable()
    stats = pstats.Stats(profile).get_stats_profile().func_profiles
    return sorted(name for name, entry in stats.items() if entry.file_name != "~")


def test_a_one_shot_call_under_a_token_costs_main_s_frames(tmp_path):
    """Under a cancel token a one-shot call through the proxy runs no helper
    between it and the native call (R2-S6): a lookup by subject is the
    proxy's one frame, and a scan the proxy's and the open file's
    point-lookup test, which tells it to race the token."""
    store = VortexRdflibStore(_file(tmp_path))
    point = (f"<{EX}s7>", f"<{EX}p>", None, None)
    with cancel_scope(CancelToken()):
        native = boundary.native(store)
        assert _python_frames(native.count_quads, *point) == ["count_quads"]
        assert _python_frames(native.match_codes, *SCAN) == ["match_codes", "point_lookup"]


def test_the_boundary_imports_nothing_from_the_pushdown():
    """The native boundary sits below the pushdown, which calls it: the
    trace helpers it uses are the trace module's, so loading it never
    reaches into a pushdown package still being initialised."""
    tree = ast.parse(Path(boundary.__file__).read_text(encoding="utf-8"))
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [module for module in imported if "pushdown" in module]
