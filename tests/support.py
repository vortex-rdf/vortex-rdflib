"""What the test modules share: running a query both ways, reading the
query trace, spies over the native store and its dictionary, building
small stores, and FILTER expressions with rdflib's own answer for them."""

import json
import weakref

from rdflib import Graph, Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.evalutils import _ebv
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.sparql import FrozenBindings, QueryContext
from vortex_rdf import serialize_rdf

import vortex_rdflib.pushdown as pd
from vortex_rdflib import VortexRdflibStore, register_sparql_pushdown, unregister_sparql_pushdown

XSD = "http://www.w3.org/2001/XMLSchema#"


def row_key(row):
    # rdflib's term ordering is not total (NaN literals), so sort on the
    # N-Triples spellings, which is.
    return tuple("" if term is None else term.n3() for term in row)


def run(graph, sparql):
    """A query's answer in a comparable form: the ASK boolean, or the
    solution rows as a sorted multiset."""
    result = graph.query(sparql)
    if result.type == "ASK":
        return result.askAnswer
    return sorted((tuple(row) for row in result), key=row_key)


def run_ordered(graph, sparql):
    """The solution rows in the order the query produced them."""
    return [tuple(row) for row in graph.query(sparql)]


def both_ways(graph, sparql, runner=run):
    """The answer with the pushdown and under rdflib's default evaluator."""
    register_sparql_pushdown()
    with_pushdown = runner(graph, sparql)
    try:
        unregister_sparql_pushdown()
        without_pushdown = runner(graph, sparql)
    finally:
        register_sparql_pushdown()
    return with_pushdown, without_pushdown


def trace_events(text: str) -> list[dict]:
    """The query-trace events printed to ``text`` (captured stderr)."""
    prefix = pd.tracing._TRACE_PREFIX
    return [
        json.loads(line[len(prefix) :]) for line in text.splitlines() if line.startswith(prefix)
    ]


def is_graph_name_check(args) -> bool:
    """Whether a native count is the store's own question whether a bare
    Graph's blank-node label names a graph of the file
    (`VortexRdflibStore._blank_graph_n3`, once per query): bookkeeping, not
    what a query plan costs."""
    return (
        len(args) == 4
        and args[:3] == (None, None, None)
        and isinstance(args[3], str)
        and args[3].startswith("_:")
    )


class CountingTermDict:
    """A TermDict stand-in that counts the codes the pushdown decodes."""

    def __init__(self, inner):
        self._inner = inner
        self.decoded = 0

    def decode(self, code):
        self.decoded += 1
        return self._inner.decode(code)

    def decode_many(self, codes):
        codes = list(codes)
        self.decoded += len(codes)
        return self._inner.decode_many(codes)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def counting_term_dict(graph) -> CountingTermDict:
    """Install the counter over the store's term dictionary."""
    counting = CountingTermDict(graph.store._dict)
    graph.store._dict = counting
    return counting


class CountingNative:
    """A VortexRdfStore stand-in that counts the counts and matches the
    pushdown asks for — each probe of a batch call is one. ``count_calls``
    lists the pattern and limit of each plain ``count_quads`` call."""

    def __init__(self, inner):
        self._inner = inner
        self.counts = 0
        self.matches = 0
        self.batches = 0
        self.count_calls: list[tuple[tuple, int | None]] = []

    def count_quads(self, *args, **kwargs):
        if not is_graph_name_check(args):
            self.counts += 1
            self.count_calls.append((args, kwargs.get("limit")))
        return self._inner.count_quads(*args, **kwargs)

    def match_codes(self, *args, **kwargs):
        self.matches += 1
        return self._inner.match_codes(*args, **kwargs)

    def count_quads_many(self, probes, **options):
        self.counts += len(probes)
        self.batches += 1
        return self._inner.count_quads_many(probes, **options)

    def match_codes_many(self, probes, **options):
        self.matches += len(probes)
        self.batches += 1
        return self._inner.match_codes_many(probes, **options)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def counting_native(graph) -> CountingNative:
    """Install the counter. The store's own graph-name check is not counted
    (`is_graph_name_check`): it is not what a query plan costs."""
    native = CountingNative(graph.store._store())
    graph.store._native = native
    return native


def store_file(tmp_path, lines, name="data", indexes=()) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"{name}.nt"
    source.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / f"{name}.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary", indexes=list(indexes))
    return str(out)


def products_file(tmp_path, n=200, indexes=()) -> str:
    """`n` products, each with an integer price, a label in English or
    French, and a type; product 0 is the reference the band queries use."""
    lines = []
    for i in range(n):
        s = f"<http://ex.org/p{i}>"
        lines.append(f'{s} <http://ex.org/price> "{(i * 37) % 1000}"^^<{XSD}integer> .\n')
        lines.append(f'{s} <http://ex.org/label> "product {i}"@{"en" if i % 2 else "fr"} .\n')
        lines.append(f"{s} <http://ex.org/type> <http://ex.org/Product> .\n")
    return store_file(tmp_path, lines, "products", indexes)


def captured_trace(capsys) -> list[dict]:
    return [
        json.loads(line[len(pd.tracing._TRACE_PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(pd.tracing._TRACE_PREFIX)
    ]


def native_call_events(trace, operation) -> list[dict]:
    return [
        e for e in trace if e["event"] == "native_call_complete" and e["operation"] == operation
    ]


class _Chunks:
    """A chunk iterator the spy handed out, counting in ``pulls[slot]`` the
    chunks pulled from it."""

    def __init__(self, inner, pulls: list[int]):
        self._inner = inner
        self._pulls = pulls
        self._slot = len(pulls)
        pulls.append(0)

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._inner)
        self._pulls[self._slot] += 1
        return chunk


class NativeSpy:
    """The native store with every call recorded by name. The chunk
    iterators ``match_code_chunks`` returns count their pulls (``pulls``)
    and are watched by weak references (``feeds``), so a test sees whether
    they were released."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []
        self.args: list[tuple] = []
        self.pulls: list[int] = []
        self.feeds: list[weakref.ref] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            if not (name == "count_quads" and is_graph_name_check(args)):
                self.calls.append((name, kwargs))
                self.args.append(args)
            result = attribute(*args, **kwargs)
            if name == "match_code_chunks" and result is not None:
                result = _Chunks(result, self.pulls)
                self.feeds.append(weakref.ref(result))
            return result

        return call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def native_spy(graph) -> NativeSpy:
    spy = NativeSpy(graph.store._store())
    graph.store._native = spy
    return spy


V = Variable("v")


W = Variable("w")


def typed(lexical: str, datatype: str) -> str:
    return f'"{lexical}"^^<{XSD}{datatype}>'


PREFIXES = (
    f"PREFIX xsd: <{XSD}> PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
    "PREFIX ex: <http://ex.org/> "
)


def filter_expr(sparql_expr: str):
    query = translateQuery(
        parseQuery(f"{PREFIXES}SELECT * WHERE {{ ?s ?p ?v FILTER({sparql_expr}) }}")
    )
    node = query.algebra.p.p
    assert node.name == "Filter"
    return node.expr


# rdflib's own contexts always carry an initBindings mapping; an unbound
# variable is looked up there last (Graph.query defaults it to {}).
CTX = QueryContext(graph=Graph(), initBindings={})


RAISES = "rdflib raises"


def rdflib_answer(expr, spelling):
    bindings = {} if spelling is None else {V: VortexRdflibStore._from_n3_safe(spelling)}
    try:
        return _ebv(expr, FrozenBindings(CTX, bindings))
    except Exception:  # noqa: BLE001 - rdflib's own bugs (e.g. NaN vs Decimal) are the oracle
        return RAISES


class RecordingTermDict:
    """A TermDict that records every filter_codes call as ``(kind, arg,
    options, candidate codes)`` (or refuses every argument, with
    ``refuse``)."""

    def __init__(self, inner, refuse: bool = False):
        self._inner = inner
        self.refuse = refuse
        self.calls: list[tuple] = []

    @property
    def sent(self) -> list[list[int]]:
        """The candidate codes of each call."""
        return [codes for _, _, _, codes in self.calls]

    def filter_codes(self, kind, arg, codes, **options):
        self.calls.append((kind, arg, options, memoryview(codes).cast("Q").tolist()))
        if self.refuse:
            raise ValueError(f"refused: {kind} {arg}")
        return self._inner.filter_codes(kind, arg, codes, **options)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def outcome(graph, sparql):
    """The answer, or the exception rdflib raises — a NaN double against a
    decimal raises `decimal.InvalidOperation` in rdflib's own comparator,
    and the pushdown, which leaves that value to rdflib, raises it too."""
    try:
        return run(graph, sparql)
    except Exception as error:  # noqa: BLE001 - the exception is the answer
        return type(error)


def filter_spy(graph) -> RecordingTermDict:
    """Record every ``filter_codes`` call the store's dictionary receives."""
    spy = RecordingTermDict(graph.store._dict)
    graph.store._dict = spy
    return spy
