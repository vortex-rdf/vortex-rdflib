"""A query's cancel token (``cancel_scope``): a query started inside the
block passes the token to every native call the token can stop — a batch, a
chunked match, a one-shot call that scans a file-backed store — and checks
it before every other one-shot call (a point lookup, a call capped at one
row, any call on an in-memory store), the per-row probes' among them, and in
the loops that make none — a streamed join's pieces, the rows rdflib
finishes, the slices and decode chunks of the rows it yields — so
``token.cancel()``, from any thread, stops it with ``vortex_rdf.Cancelled``
at its next call, piece, finished row, slice or decode chunk. The token is
the query's own: its lazy results keep it after the block, and a query
started outside a block has none, whatever another query's token did.
Without a token no native call gains a keyword."""

import threading
from collections.abc import Callable

import pytest
from rdflib import Dataset, Graph, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from tests.support import native_spy, run, trace_events
from vortex_rdf import Cancelled, CancelToken, serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, cancel_scope, register_sparql_pushdown
from vortex_rdflib.settings import SETTINGS

EX = "http://ex/"
_PREFIX = f"PREFIX : <{EX}> "

#: The subjects `s{i}`, each with one `p`, `q` and `u` object and `FANOUT`
#: `r` objects.
SUBJECTS = 120
FANOUT = 3

#: The anchor's two `link` rows, of 202: the step probes `link` per row of
#: the relation, each probe binding the subject.
PROBED = _PREFIX + 'SELECT ?a ?s WHERE { ?a :anchor "yes" . ?a :link ?s }'
#: The anchor's two linked subjects, then their `r` rows: 360 rows against
#: the relation's two, so the last step probes `r` once per row, binding the
#: subject.
PROBED_TWICE = _PREFIX + 'SELECT ?s ?z WHERE { ?a :anchor "yes" . ?a :link ?s . ?s :r ?z }'
#: `s0`'s `p` object against the 120 `p` rows: a probe would bind only the
#: object and scan the store, so the step keep-scans.
KEEP_SCANNED = _PREFIX + "SELECT ?x ?f WHERE { :s0 :p ?x . ?f :p ?x }"
#: One `q` row per `p` row: at a budget of 0 the step streams, the matched
#: `q` rows fed through the grouped `p` rows in slices.
PAIRED = _PREFIX + "SELECT ?s ?x ?y WHERE { ?s :p ?x . ?s :q ?y }"
#: `FANOUT` `r` rows per `p` row: at a budget of 0 the step streams the `r`
#: pattern from chunks of its match.
FANNED = _PREFIX + "SELECT ?s ?x ?z WHERE { ?s :p ?x . ?s :r ?z }"
#: Three patterns of one row per subject: at a budget of 0 no second step
#: fits, so rdflib finishes the last two per row of the first (tier 3).
TIERED = _PREFIX + "SELECT ?s ?x ?y ?v WHERE { ?s :p ?x . ?s :q ?y . ?s :u ?v }"
#: Three picked `r` objects, each probed for the subjects that have it with
#: a probe that scans the store: one batch of matches.
OPTIONAL_PROBED = _PREFIX + "SELECT ?z ?f WHERE { :c0 :pick ?z OPTIONAL { ?f :r ?z } }"
#: The same probes as existence counts: one batch of counts.
EXISTS_PROBED = _PREFIX + "SELECT ?z WHERE { :c0 :pick ?z FILTER EXISTS { ?f :r ?z } }"
#: One pattern's existence: a count capped at one row.
ASK = _PREFIX + "ASK { ?s :p ?x }"


def _statements() -> str:
    lines = []
    for i in range(SUBJECTS):
        lines.append(f"<{EX}s{i}> <{EX}p> <{EX}x{i}> .")
        lines.append(f"<{EX}s{i}> <{EX}q> <{EX}y{i}> .")
        lines.append(f"<{EX}s{i}> <{EX}u> <{EX}v{i}> .")
        lines += [f"<{EX}s{i}> <{EX}r> <{EX}z{i}-{j}> ." for j in range(FANOUT)]
    lines.append(f'<{EX}a0> <{EX}anchor> "yes" .')
    lines += [f"<{EX}a0> <{EX}link> <{EX}s{i}> ." for i in range(2)]
    lines += [f"<{EX}b{i}> <{EX}link> <{EX}s{i % SUBJECTS}> ." for i in range(200)]
    lines += [f"<{EX}c0> <{EX}pick> <{EX}z{i}-0> ." for i in range(3)]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def cancel_vortex(tmp_path_factory):
    d = tmp_path_factory.mktemp("cancel")
    nt = d / "cancel.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "cancel.vortex"
    serialize_rdf(str(nt), str(out), layout="dictionary")
    return out


@pytest.fixture(params=[False, True], ids=["file", "mem"])
def graph(cancel_vortex, request):
    return Graph(store=VortexRdflibStore(str(cancel_vortex), in_memory=request.param))


@pytest.fixture
def mem_graph(cancel_vortex):
    return Graph(store=VortexRdflibStore(str(cancel_vortex), in_memory=True))


def _store_calls(graph) -> None:
    """The store's own native calls: ``triples()`` over a pattern and over a
    ground triple, under the union and in the default graph, and a view's
    graphs and size."""
    store = graph.store
    p, s0, x0 = URIRef(f"{EX}p"), URIRef(f"{EX}s0"), URIRef(f"{EX}x0")
    default = Graph(store=store, identifier=DATASET_DEFAULT_GRAPH_ID)
    assert len(list(store.triples((None, p, None)))) == SUBJECTS
    assert len(list(store.triples((s0, p, x0)))) == 1
    assert len(list(store.triples((s0, p, x0), default))) == 1
    assert len(list(store.contexts())) == 1
    assert len(Graph(store=store, identifier=URIRef(f"{EX}g"))) == 0


def _call(name: str, *terms: str, **keywords) -> Callable[[str, tuple, dict], bool]:
    """A test of a native call: its name, the first terms of its pattern, and
    keywords it carries with these values (any value for ``...``)."""

    def test(called: str, args: tuple, kwargs: dict) -> bool:
        return (
            called == name
            and args[: len(terms)] == terms
            and all(
                key in kwargs and (value is ... or kwargs[key] == value)
                for key, value in keywords.items()
            )
        )

    return test


#: Per shape: the query it runs (or the store calls), the pair budget it
#: runs under (the default for ``None``), a test that only a native call the
#: shape exists for passes, and that call's kind — a ``"wait"`` (a batch or a
#: chunked match), a one-shot ``"scan"`` or a one-shot ``"point"`` (a point
#: lookup or a call capped at one row): a probe binding the anchor, a keyed
#: match, a chunked match, rdflib's read of the last pattern for a row, the
#: two batches, a count capped at one row, and a ground triple's count in the
#: default graph.
SHAPES = {
    "probe-join": (PROBED, None, _call("match_codes", f"<{EX}a0>"), "point"),
    "keep-scan": (KEEP_SCANNED, None, _call("match_codes", keep=...), "scan"),
    "stream": (FANNED, 0, _call("match_code_chunks"), "wait"),
    "tier3": (TIERED, 0, _call("match_codes", f"<{EX}s0>", f"<{EX}u>"), "point"),
    "batched-matches": (OPTIONAL_PROBED, None, _call("match_codes_many"), "wait"),
    "batched-counts": (EXISTS_PROBED, None, _call("count_quads_many"), "wait"),
    "existence-count": (ASK, None, _call("count_quads", limit=1), "point"),
    "store": (
        _store_calls,
        None,
        _call("count_quads", f"<{EX}s0>", f"<{EX}p>", f"<{EX}x0>", ""),
        "point",
    ),
}

#: The calls whose pulls wait for the native layer: they race a token on
#: every store.
_WAITING_CALLS = ("count_quads_many", "match_codes_many", "match_code_chunks")


def _races(store, name: str, args: tuple, kwargs: dict) -> bool:
    """Whether a native call races the token: a batch or a chunked match
    always; a one-shot call only on a file-backed store, when it is no point
    lookup (``VortexRdflibStore._point_lookup``) and is not capped at one row."""
    if name in _WAITING_CALLS:
        return True
    limit = kwargs.get("limit")
    return not store.in_memory and (limit is None or limit > 1) and not store._point_lookup(args)


def _run_shape(graph, monkeypatch, shape: str) -> list[tuple[str, tuple, dict]]:
    """The native calls one shape makes, each as ``(name, args, kwargs)``;
    the shape's own call among them."""
    work, budget, own, _ = SHAPES[shape]
    if budget is not None:
        monkeypatch.setattr(SETTINGS, "pair_budget", budget)
    register_sparql_pushdown()
    spy = native_spy(graph)
    if callable(work):
        work(graph)
    else:
        run(graph, work)
    calls = [(name, args, kwargs) for (name, kwargs), args in zip(spy.calls, spy.args, strict=True)]
    assert any(own(*call) for call in calls), calls
    return calls


@pytest.mark.parametrize("shape", list(SHAPES))
def test_without_a_token_no_call_gains_a_keyword(graph, monkeypatch, shape):
    """Outside ``cancel_scope`` no native call carries ``cancel``: each runs
    unraced, as it did before there was a token."""
    calls = _run_shape(graph, monkeypatch, shape)
    assert [call for call in calls if "cancel" in call[2]] == []


@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_token_reaches_every_call_it_can_stop(graph, monkeypatch, shape):
    """Inside ``cancel_scope`` every native call the token can stop carries
    it — a batch, a chunked match, a one-shot scan of a file-backed store —
    and every other one-shot call carries none: of the planner, of the rows
    it streams and of the store."""
    token = CancelToken()
    with cancel_scope(token):
        calls = _run_shape(graph, monkeypatch, shape)
    own, kind = SHAPES[shape][2:]
    raced = {"cancel" in kwargs for name, args, kwargs in calls if own(name, args, kwargs)}
    store = graph.store
    assert raced == {kind == "wait" or (kind == "scan" and not store.in_memory)}
    expected = [token if _races(store, *call) else None for call in calls]
    assert [kwargs.get("cancel") for _, _, kwargs in calls] == expected


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize(
    ("sparql", "node", "refused_calls"),
    [(TIERED, "Project", 1), (ASK, "AskQuery", 0)],
    ids=["raced", "checked"],
)
def test_a_cancelled_token_stops_the_query_at_its_first_call(
    cancel_vortex, monkeypatch, capsys, in_memory, sparql, node, refused_calls
):
    """A token cancelled before the query starts stops it at its first native
    call: the three-pattern block's planning counts, one batch, are refused
    by the native layer, which the spy sees given the token; ASK's count,
    capped at one row, by the check before it, so no native call is made.
    ``Cancelled`` reaches the caller, the node being planned traces
    ``eval_part_cancelled``, and rdflib is not handed the node."""
    store = VortexRdflibStore(str(cancel_vortex), in_memory=in_memory)
    graph = Dataset(store=store, default_union=True)
    register_sparql_pushdown()
    spy = native_spy(graph)
    token = CancelToken()
    token.cancel()
    monkeypatch.setenv("VORTEX_RDF_TRACE_QUERY", "1")
    capsys.readouterr()
    with cancel_scope(token), pytest.raises(Cancelled):
        graph.query(sparql)
    events = trace_events(capsys.readouterr().err)
    assert [kwargs for _, kwargs in spy.calls] == [{"cancel": token}] * refused_calls
    assert [(e["event"], e["algebra_node"]) for e in events if "algebra_node" in e] == [
        ("eval_part_start", node),
        ("eval_part_cancelled", node),
    ]


def _one_row_pieces(monkeypatch) -> None:
    """Tier 1 with a piece per row, the first decoded alone: a budget of 0,
    slices and chunks of one row, and a first decode chunk of one row, so a
    pull past the first row asks for the next piece."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    monkeypatch.setattr(SETTINGS, "piece_rows", 1)
    monkeypatch.setattr(SETTINGS, "chunk_start", 1)


class _Groups:
    """A ``CodeGroups`` whose gathers are recorded in ``gathers``; the first
    one calls ``during`` before it gathers."""

    def __init__(self, inner, gathers: list, during):
        self._inner = inner
        self._gathers = gathers
        self._during = during

    def __len__(self):
        return len(self._inner)

    def count(self, column):
        return self._inner.count(column)

    def gather(self, column, left_columns, right_columns):
        self._gathers.append(len(column))
        if self._during is not None and len(self._gathers) == 1:
            self._during()
        return self._inner.gather(column, left_columns, right_columns)


def _watch_gathers(monkeypatch, during=None) -> list:
    """The gathers of every streamed join step, recorded as they are made
    (see :class:`_Groups`)."""
    gathers: list = []
    gather_pieces = pd.streaming._gather_pieces

    def watched(groups, fed, key, grouped, order):
        groups = _Groups(groups, gathers, during)
        return gather_pieces(groups, fed, key, grouped, order)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(pd.streaming, "_gather_pieces", watched)
    return gathers


@pytest.mark.parametrize("sparql", [PAIRED, FANNED], ids=["slices", "chunks"])
def test_cancel_from_another_thread_stops_a_stream(mem_graph, monkeypatch, sparql):
    """A streamed join, its token cancelled from another thread once the
    first row is read, raises ``Cancelled`` at the next pull, before the
    next piece is gathered: the slices' piece loop checks the token, a chunk
    pull races it."""
    _one_row_pieces(monkeypatch)
    gathers = _watch_gathers(monkeypatch)
    register_sparql_pushdown()
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(mem_graph.query(sparql))
        assert next(rows)
        canceller = threading.Thread(target=token.cancel)
        canceller.start()
        canceller.join()
        with pytest.raises(Cancelled):
            next(rows)
    assert gathers == [1]


@pytest.mark.parametrize("sparql", [PAIRED, FANNED], ids=["slices", "chunks"])
def test_a_cancel_during_a_gather_stops_at_the_next_piece(mem_graph, monkeypatch, sparql):
    """A cancel that lands inside a gather lets that gather finish, and its
    row is read; the stream stops before the next piece, and no other
    gather is made."""
    token = CancelToken()
    _one_row_pieces(monkeypatch)
    gathers = _watch_gathers(monkeypatch, during=token.cancel)
    register_sparql_pushdown()
    with cancel_scope(token):
        rows = iter(mem_graph.query(sparql))
        assert next(rows)
        assert token.cancelled
        with pytest.raises(Cancelled):
            next(rows)
    assert gathers == [1]


#: Per case: the query, its pair budget (the default for ``None``), whether
#: the token is cancelled once the first row is read or while the first
#: piece of a streamed join is gathered, the rows read before ``Cancelled``,
#: and the rows of each piece gathered.
_WITHIN_A_CHUNK = {
    "probed-rows": (PROBED_TWICE, None, "read", 2, []),
    "piece": (PAIRED, 60, "read", 2, [60]),
    "piece-slice": (PAIRED + " OFFSET 1", 60, "gather", 0, [60]),
}


@pytest.mark.parametrize("case", list(_WITHIN_A_CHUNK))
def test_a_cancel_stops_the_rows_within_one_decode_chunk(mem_graph, monkeypatch, case):
    """Rows are decoded two, then eight at a time, and a columnar body is
    read in slices of those sizes. A probe join's six rows, and a streamed
    join's two pieces of 60 rows (a budget of 60 halves its 120 pairs), are
    read one row at a time: cancelled once the first row is read, the query
    delivers the rest of that decode chunk and stops before the next one,
    inside the first piece. Past an ``OFFSET 1`` the first decode chunk ends
    inside the first piece's second slice: cancelled while that piece is
    gathered, the query stops at the end of the piece's first slice, before
    any row."""
    sparql, budget, when, read_before, gathered = _WITHIN_A_CHUNK[case]
    if budget is not None:
        monkeypatch.setattr(SETTINGS, "pair_budget", budget)
    monkeypatch.setattr(SETTINGS, "chunk_start", 2)
    monkeypatch.setattr(SETTINGS, "chunk_max", 8)
    token = CancelToken()
    gathers = _watch_gathers(monkeypatch, during=token.cancel if when == "gather" else None)
    register_sparql_pushdown()
    read = 0
    with cancel_scope(token):
        rows = iter(mem_graph.query(sparql))
        with pytest.raises(Cancelled):
            for _ in rows:
                read += 1
                if when == "read":
                    token.cancel()
    assert read == read_before
    assert gathers == gathered


def test_the_token_is_the_query_s_own(graph, monkeypatch):
    """Query A, started inside ``cancel_scope``, keeps its token for the rows
    read after the block. Query B, run on the same thread while A is half
    read and once A's token is cancelled, has no token outside a block and
    its own fresh one inside one — which its calls carry where the token can
    stop them — and completes both times; A's next pull stops."""
    _one_row_pieces(monkeypatch)
    register_sparql_pushdown()
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(graph.query(PAIRED))
    assert next(rows)
    token.cancel()
    spy = native_spy(graph)
    assert len(run(graph, PAIRED)) == SUBJECTS
    assert spy.calls and all("cancel" not in kwargs for _, kwargs in spy.calls)
    seen = len(spy.calls)
    fresh = CancelToken()
    with cancel_scope(fresh):
        assert len(run(graph, PAIRED)) == SUBJECTS
    tokens = {kwargs.get("cancel") for _, kwargs in spy.calls[seen:]}
    assert tokens == {None if graph.store.in_memory else fresh}
    with pytest.raises(Cancelled):
        next(rows)


def test_tier3_rows_stop_on_cancel(mem_graph, monkeypatch):
    """rdflib finishes the last two patterns per row of the first (tier 3 at
    a budget of 0), each row making one solution. Cancelled once the first
    solution is read, the query stops at the next row of the relation,
    which rdflib never extends."""
    monkeypatch.setattr(SETTINGS, "pair_budget", 0)
    extended = []
    eval_bgp = pd.solutions.evalBGP

    def watched(ctx, bgp):
        extended.append(len(bgp))
        return eval_bgp(ctx, bgp)

    monkeypatch.setattr(pd.solutions, "evalBGP", watched)
    register_sparql_pushdown()
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(mem_graph.query(TIERED))
        assert next(rows)
        token.cancel()
        with pytest.raises(Cancelled):
            next(rows)
    assert extended == [2]


class _CancelAfter:
    """The native store, recording in ``selected`` each call ``when`` selects
    as it is made, and cancelling ``token`` once the first has returned: a
    cancel that lands while a call runs lets it finish."""

    def __init__(self, inner, token, when):
        self._inner = inner
        self._token = token
        self._when = when
        self.selected: list[tuple] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            selected = self._when(name, args)
            if selected:
                self.selected.append(args)
            result = attribute(*args, **kwargs)
            if selected:
                self._token.cancel()
            return result

        return call


def test_probes_stop_on_cancel(mem_graph):
    """A probe step whose token is cancelled during its first probe makes no
    other: the next probe checks the token before its call, and
    ``Cancelled`` ends the planning."""
    register_sparql_pushdown()
    token = CancelToken()
    native = _CancelAfter(
        mem_graph.store._store(),
        token,
        lambda name, args: name == "match_codes" and args[0] is not None and args[1] == f"<{EX}r>",
    )
    mem_graph.store._union_has_repeats()  # the file's one-time bookkeeping is not a query's cost
    mem_graph.store._native = native
    with cancel_scope(token), pytest.raises(Cancelled):
        mem_graph.query(PROBED_TWICE)
    assert len(native.selected) == 1


@pytest.mark.parametrize("chunk_rows", [None, 7], ids=["one-match", "chunked"])
def test_triples_carries_the_token(graph, monkeypatch, chunk_rows):
    """Inside ``cancel_scope`` the match ``triples()`` makes for a pattern
    that scans a file-backed store carries the token, and on an in-memory
    store takes none; it asks for one row more than ``SETTINGS.triples_chunk_rows``.
    A pattern of more rows is read in chunks, every pull of which waits for
    the native layer: the chunk call carries the token on any store."""
    if chunk_rows is not None:
        monkeypatch.setattr(SETTINGS, "triples_chunk_rows", chunk_rows)
    spy = native_spy(graph)
    token = CancelToken()
    with cancel_scope(token):
        assert list(graph.store.triples((None, URIRef(f"{EX}p"), None)))
    raced = {} if graph.store.in_memory else {"cancel": token}
    first = ("match_codes", {"limit": SETTINGS.triples_chunk_rows + 1, **raced})
    if chunk_rows is None:
        assert spy.calls == [first]
    else:
        chunks = {"columns": ("s", "p", "o", "g"), "batch_rows": chunk_rows, "cancel": token}
        assert spy.calls == [first, ("match_code_chunks", chunks)]


def test_a_cancel_stops_a_chunked_triples_read_at_its_next_pull(graph, monkeypatch):
    """A pattern read in chunks keeps the token it started with: once the
    token is cancelled, after the block too, the read yields the rest of the
    chunk it holds and raises ``Cancelled`` at its next pull."""
    monkeypatch.setattr(SETTINGS, "triples_chunk_rows", 7)
    token = CancelToken()
    with cancel_scope(token):
        rows = graph.store.triples((None, URIRef(f"{EX}p"), None))
        next(rows)
    token.cancel()
    read = 1
    with pytest.raises(Cancelled):
        for _ in rows:
            read += 1
    assert read <= 7 < SUBJECTS  # one chunk's rows, not the pattern's


def test_an_in_memory_one_shot_call_checks_the_token_itself(mem_graph):
    """An in-memory one-shot call finishes without waiting, so it does not
    race the token, whatever its shape: inside ``cancel_scope`` the match
    ``triples()`` makes for a pattern that scans takes no token, and once
    the token is cancelled it raises ``Cancelled`` before any native call."""
    spy = native_spy(mem_graph)
    token = CancelToken()
    scan = (None, URIRef(f"{EX}p"), None)
    with cancel_scope(token):
        assert list(mem_graph.store.triples(scan))
        token.cancel()
        with pytest.raises(Cancelled):
            list(mem_graph.store.triples(scan))
    assert spy.calls == [("match_codes", {"limit": SETTINGS.triples_chunk_rows + 1})]


S0, P, X0 = URIRef(f"{EX}s0"), URIRef(f"{EX}p"), URIRef(f"{EX}x0")


@pytest.mark.parametrize(
    ("pattern", "in_default_graph", "call"),
    [
        ((S0, P, X0), False, "match_codes"),
        ((S0, P, X0), True, "count_quads"),
        ((S0, None, None), False, "match_codes"),
    ],
    ids=["ground", "ground-in-one-graph", "subject-bound"],
)
def test_a_point_read_checks_the_token_itself(graph, pattern, in_default_graph, call):
    """A point lookup of ``triples()`` does not race the token: inside
    ``cancel_scope`` its native call takes no token, and once the token is
    cancelled it raises ``Cancelled`` before any native call."""
    store = graph.store
    context = Graph(store=store, identifier=DATASET_DEFAULT_GRAPH_ID) if in_default_graph else None
    spy = native_spy(graph)
    token = CancelToken()
    with cancel_scope(token):
        assert list(store.triples(pattern, context))
        token.cancel()
        with pytest.raises(Cancelled):
            list(store.triples(pattern, context))
    # A pattern with a free position is matched asking for one row more than
    # one match of ``triples()`` holds; a ground one is not.
    limited = {"limit": SETTINGS.triples_chunk_rows + 1} if None in pattern else {}
    assert spy.calls == [(call, limited)]


@pytest.fixture(scope="module")
def default_vortex(tmp_path_factory):
    """This module's statements in the Default layout: with no dictionary the
    store reads through ``match_columns``, which takes no token."""
    d = tmp_path_factory.mktemp("cancel-default")
    nt = d / "cancel.nt"
    nt.write_text(_statements(), encoding="utf-8")
    out = d / "cancel-default.vortex"
    serialize_rdf(str(nt), str(out), layout="default")
    return out


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
@pytest.mark.parametrize(
    "read",
    [
        lambda store: list(store.triples((None, P, None))),
        lambda store: list(store.triples((S0, P, X0))),
        lambda store: list(store.contexts((None, P, None))),
    ],
    ids=["pattern", "ground", "contexts"],
)
def test_the_string_fallback_checks_the_token(default_vortex, in_memory, read):
    """A store without the code path reads through ``match_columns``, which
    takes no token, so each such read checks it before the call: inside
    ``cancel_scope`` a read goes through, and once the token is cancelled
    the next raises ``Cancelled`` before any native call."""
    graph = Graph(store=VortexRdflibStore(str(default_vortex), in_memory=in_memory))
    spy = native_spy(graph)
    token = CancelToken()
    with cancel_scope(token):
        assert read(graph.store)
        token.cancel()
        with pytest.raises(Cancelled):
            read(graph.store)
    assert spy.calls == [("match_columns", {})]


@pytest.mark.parametrize("in_memory", [False, True], ids=["file", "mem"])
def test_a_query_over_the_string_fallback_stops_on_cancel(default_vortex, in_memory):
    """Without the code path rdflib evaluates the query, reading the second
    pattern once per row of the first through ``triples()``. Cancelled once
    the first row is read, the query stops at the next read."""
    graph = Graph(store=VortexRdflibStore(str(default_vortex), in_memory=in_memory))
    register_sparql_pushdown()
    token = CancelToken()
    with cancel_scope(token):
        rows = iter(graph.query(_PREFIX + "SELECT ?s ?x WHERE { ?s :p ?o . ?x :p ?o }"))
        assert next(rows)
        token.cancel()
        with pytest.raises(Cancelled):
            next(rows)
