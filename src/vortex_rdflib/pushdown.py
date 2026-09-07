"""SPARQL pushdown for :class:`~vortex_rdflib.store.VortexRdflibStore`.

rdflib evaluates a query as a tree of algebra operators and offers every node
to the ``CUSTOM_EVALS`` hooks before running its own evaluator. This module's
hook answers the nodes it understands in **code space** — over the ``u32``
term codes of a Dictionary-layout store — and hands everything else back to
rdflib by raising ``NotImplementedError``; rdflib then evaluates that node
itself and re-enters the hook for the nodes below it, so every supported
subtree of a query still runs here.

What runs in code space:

- a basic graph pattern is solved in one pass: every triple pattern is
  matched natively once (a match is near-constant cost, so the actual row
  counts drive the join order), the join gathers ``u32`` code columns, or
  re-probes the store per binding when the running relation is
  far smaller than the next pattern's match (``_probe_join``), and
  intermediate results never decode a term;
- a ``Filter`` over a block is split into conjuncts (:mod:`.filters`):
  those over one variable are evaluated once per distinct code of the
  variable and applied to the pattern scans *before* the join, the others
  once per distinct code tuple after it — a whitelist of expression shapes
  runs as predicates over the stored spellings, anything else (and every
  value outside the fast path's exact domain) is answered by rdflib's own
  evaluator, per distinct value instead of per row;
- group joins (``Join``), ``OPTIONAL`` (``LeftJoin``), ``MINUS`` and
  ``FILTER (NOT) EXISTS`` over blocks run as hash joins, left joins, anti-
  and semi-joins over code tuples, with the inner pattern re-probed per
  outer row when the outer relation is small — instead of rdflib
  re-entering the store once per outer solution; an inline ``VALUES`` table
  is a code-space relation too (a constant the dictionary does not hold
  gets a private negative code and joins nothing);
- ``Project``, ``Distinct``, ``OrderBy`` (on variables) and ``Slice``
  (LIMIT/OFFSET) heads are applied to the code-space relation, and an
  ``AskQuery`` over one pattern is answered from the row selection alone, so
  only the projected variables of the rows that are actually consumed are
  ever decoded — a ``LIMIT 10`` decodes a few dozen codes, an ASK none, a
  ``DISTINCT ?p`` over the whole store only its distinct predicates; an
  ORDER BY ranks each distinct code once (blank nodes and IRIs by code
  order, literals through rdflib's own comparator) and sorts the code rows,
  a top-k when a LIMIT follows;
- ``COUNT`` aggregates (``COUNT(*)``, ``COUNT(?v)``, ``COUNT(DISTINCT ?v)``,
  with or without ``GROUP BY`` variables) are computed over the code
  columns, decoding only the group keys — and a ``COUNT(*)`` over one
  pattern is answered by ``count_quads`` without matching a row;
- solutions are decoded lazily in growing chunks, each distinct code once
  through the store's decode cache, and built directly as
  ``FrozenBindings``, the row shape every rdflib operator above expects.

Named graphs: a ``Graph`` node sets the scope of the block below it rather
than matching anything itself. A bound name restricts every pattern to that
graph (as does the graph the context is active in, for a block under no
``GRAPH`` at all); an unbound ``GRAPH ?g`` binds the graph from the match's
fourth column instead, so ``?g`` is an ordinary variable of the relation and
one match answers what rdflib would walk the store's graphs for. The rows of
the default graph are dropped from a variable's match, which is what makes
``GRAPH`` range over the named graphs only.

rdflib only catches ``NotImplementedError`` at hook-call time, so every
capability check and every native call happens before a generator is handed
back; the generators only decode. The hook applies when the active graph's
store is a VortexRdflibStore with the code path available (Dictionary layout,
resident dictionary); behaviour is identical to rdflib's default evaluation,
only faster, and the equivalence tests compare both paths on every query
shape.

Registration happens automatically when the first ``VortexRdflibStore`` is
constructed. ``VORTEX_RDF_DISABLE_PUSHDOWN=1`` keeps rdflib's evaluator
entirely (the equivalence tests' oracle); ``VORTEX_RDF_PUSHDOWN_OPS`` narrows
the intercepted algebra nodes to a comma-separated list (``bgp`` = basic
graph patterns only) and ``VORTEX_RDF_FILTER_FAST=0`` routes every FILTER
value through rdflib's evaluator, for bisecting and A/B measurements.
"""

import heapq
import json
import os
import sys
import time
from array import array
from collections import Counter
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import islice
from operator import itemgetter
from typing import Any

from rdflib.plugins.sparql import CUSTOM_EVALS
from rdflib.plugins.sparql.evalutils import _val
from rdflib.plugins.sparql.sparql import FrozenBindings
from rdflib.term import BNode, Literal, URIRef, Variable

from . import filters
from .terms import canonical_spelling

_EVAL_KEY = "vortex_rdflib_bgp"

# Query bnodes act as variables, exactly as rdflib's evalBGP treats them.
_VAR_LIKE = (Variable, BNode)

# Algebra nodes this module answers, by handler name. Handlers are resolved
# from the module namespace at call time so tests can spy on them.
_HANDLER_NAMES = {
    "BGP": "_eval_block_node",
    "Filter": "_eval_block_node",
    "Join": "_eval_block_node",
    "LeftJoin": "_eval_block_node",
    "Minus": "_eval_block_node",
    "ToMultiSet": "_eval_block_node",
    "Graph": "_eval_block_node",
    "Project": "_eval_head",
    "Slice": "_eval_head",
    "Distinct": "_eval_head",
    "OrderBy": "_eval_head",
    "AggregateJoin": "_eval_aggregate",
    "AskQuery": "_eval_ask",
}
_ALL_OPS = frozenset(_HANDLER_NAMES)
_ENABLED_OPS = _ALL_OPS

# Solutions decode in chunks that grow geometrically: the first chunk keeps a
# LIMIT (or any consumer that stops early) from decoding more than a few
# dozen rows, the cap keeps a full scan to a handful of decode_many calls.
_CHUNK_START = 64
_CHUNK_MAX = 4096

_TRACE_PREFIX = "VORTEX_RDF_QUERY_TRACE "
_TRACE_SCHEMA = "vortex-rdf-query-trace-v1"
_TRACE_ENV = "VORTEX_RDF_TRACE_QUERY"
_TRACE_ID_ENV = "VORTEX_RDF_TRACE_QUERY_ID"


@dataclass(slots=True)
class _QueryTrace:
    query_id: str | None
    sequence: int = 0
    depth: int = 0

    def emit(self, event: str, **fields) -> None:
        self.sequence += 1
        payload = {
            "schema": _TRACE_SCHEMA,
            "event": event,
            "sequence": self.sequence,
            "query_id": self.query_id,
            **fields,
        }
        try:
            print(
                _TRACE_PREFIX + json.dumps(payload, separators=(",", ":"), allow_nan=False),
                file=sys.stderr,
                flush=True,
            )
        except (OSError, TypeError, ValueError):
            _TRACE.set(None)


_TRACE: ContextVar[_QueryTrace | None] = ContextVar("vortex_rdf_query_trace", default=None)


def _trace_enabled() -> bool:
    value = os.environ.get(_TRACE_ENV)
    if value is None or value == "0":
        return False
    if value != "1":
        raise ValueError(f"{_TRACE_ENV} must be 0 or 1, got {value!r}")
    return True


def _trace_event(event: str, **fields) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.emit(event, **fields)


def _relation_rows(rel: "Relation") -> int | None:
    if rel.rows is not None:
        return len(rel.rows)
    if rel.cols is not None and not rel.preds:
        return rel.nrows
    return None


def register_sparql_pushdown():
    """Install the hook into rdflib's CUSTOM_EVALS (idempotent)."""
    global _ENABLED_OPS
    if os.environ.get("VORTEX_RDF_DISABLE_PUSHDOWN") == "1":
        return
    spec = os.environ.get("VORTEX_RDF_PUSHDOWN_OPS")
    if spec is not None:
        _ENABLED_OPS = _parse_ops(spec)
    if os.environ.get("VORTEX_RDF_FILTER_FAST") == "0":
        filters._FAST_ENABLED = False
    CUSTOM_EVALS.setdefault(_EVAL_KEY, _eval_part)


def unregister_sparql_pushdown():
    CUSTOM_EVALS.pop(_EVAL_KEY, None)


def _parse_ops(spec: str) -> frozenset[str]:
    """``VORTEX_RDF_PUSHDOWN_OPS``: ``bgp`` or a comma-separated node list."""
    if spec.strip().lower() == "bgp":
        return frozenset({"BGP"})
    names = frozenset(name.strip() for name in spec.split(",") if name.strip())
    unknown = names - _ALL_OPS
    if unknown:
        raise ValueError(
            f"VORTEX_RDF_PUSHDOWN_OPS names unknown algebra nodes {sorted(unknown)}; "
            f"known: {sorted(_ALL_OPS)}"
        )
    return names


def _eval_part(ctx, part):
    handler = _HANDLER_NAMES.get(part.name)
    if handler is None or part.name not in _ENABLED_OPS:
        raise NotImplementedError

    from .store import VortexRdflibStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexRdflibStore) or store._dict is None:
        raise NotImplementedError
    active = _TRACE.get()
    token = None
    if active is None and _trace_enabled():
        active = _QueryTrace(os.environ.get(_TRACE_ID_ENV))
        token = _TRACE.set(active)
    started = time.perf_counter_ns() if active is not None else 0
    _trace_event("eval_part_start", algebra_node=part.name, enabled_ops=sorted(_ENABLED_OPS))
    try:
        result = globals()[handler](ctx, store, part)
    except NotImplementedError:
        _trace_event("eval_part_fallback", algebra_node=part.name)
        if token is not None:
            _TRACE.reset(token)
        raise
    except Exception as error:
        _trace_event("eval_part_failed", algebra_node=part.name, error_type=type(error).__name__)
        if token is not None:
            _TRACE.reset(token)
        raise
    _trace_event(
        "eval_part_complete",
        algebra_node=part.name,
        handled=True,
        elapsed_ns=time.perf_counter_ns() - started if active is not None else 0,
    )
    if token is not None:
        # Generators keep the trace object explicitly through _yield_rows.
        if hasattr(result, "__next__"):
            result = _trace_generator(result, active, token)
        else:
            _TRACE.reset(token)
    return result


def _trace_generator(iterator, trace: _QueryTrace, token):
    _TRACE.reset(token)
    started = time.perf_counter_ns()
    rows = 0
    try:
        while True:
            inner_token = _TRACE.set(trace)
            try:
                item = next(iterator)
            except StopIteration:
                return
            finally:
                _TRACE.reset(inner_token)
            rows += 1
            yield item
    finally:
        inner_token = _TRACE.set(trace)
        try:
            _trace_event(
                "query_iterator_complete",
                rows_yielded=rows,
                elapsed_ns=time.perf_counter_ns() - started,
            )
        finally:
            _TRACE.reset(inner_token)


@dataclass(frozen=True, slots=True)
class _GraphScope:
    """How the block below sees the graph column of a match.

    ``n3`` is the graph the patterns are restricted to — ``None`` is the
    wildcard over every graph, ``""`` the default graph, a name its own graph.
    ``var`` is the variable a ``GRAPH ?g`` binds *from* the column instead, in
    which case ``n3`` is the wildcard and the default graph's rows are dropped
    (a ``GRAPH`` clause ranges over the named graphs only).
    """

    n3: str | None = None
    var: Variable | None = None


def _active_scope(ctx, store) -> _GraphScope:
    """The scope of a block that is not under a ``GRAPH`` node of its own: the
    graph the context has made active (the union default graph, a dataset's
    default graph, or the graph rdflib pushed for a ``GRAPH`` it evaluated
    itself)."""
    return _GraphScope(store._graph_n3(getattr(ctx, "graph", None)))


def _graph_scope(ctx, node) -> _GraphScope:
    """The scope a ``Graph`` node establishes, mirroring rdflib's ``evalGraph``:
    a bound term names one graph, an unbound variable ranges over the named
    graphs. A term bound to something that cannot name a graph is left to
    rdflib rather than guessed at."""
    term = node.term
    value = ctx[term]
    if value is None:
        return _GraphScope(None, term)
    if not isinstance(value, (URIRef, BNode)):
        raise NotImplementedError
    return _GraphScope(canonical_spelling(value), None)


def _peel_graph(ctx, node, scope) -> tuple:
    """Look through ``GRAPH`` nodes that name one graph, so the single-pattern
    fast paths below still apply to a block inside a named graph."""
    while getattr(node, "name", None) == "Graph":
        inner = _graph_scope(ctx, node)
        if inner.var is not None:
            break
        node, scope = node.p, inner
    return node, scope


@dataclass(slots=True)
class Relation:
    """A code-space relation.

    ``schema`` names the variable-like terms. The body is either ``u32``
    columns (``cols``, one per schema entry: the zero-copy views of a single
    matched pattern, or the gathered ``array('I')`` columns of a hash join)
    or materialized ``rows`` of ``int`` codes. ``None`` in a row is an unbound variable and
    ``nullable`` lists the variables that may hold it; ``foreign`` maps
    negative codes to terms outside the dictionary; ``preds`` are row
    predicates still to be applied while a columnar body streams (``nrows``
    counts the rows before them).
    """

    schema: tuple
    cols: tuple | None  # memoryview or array('I') per entry; both slice + tolist
    rows: list[tuple] | None
    nrows: int
    nullable: frozenset = frozenset()
    foreign: dict = field(default_factory=dict)
    preds: tuple[Callable[[tuple], bool], ...] = ()

    @classmethod
    def from_rows(cls, schema, rows):
        return cls(schema, None, rows, len(rows))


@dataclass(slots=True)
class _Head:
    block: object
    pv: tuple | None
    start: int
    stop: int | None
    distinct: bool
    order: list  # (variable, descending) per ORDER BY condition


def _plan_head(part) -> _Head:
    """Recognize a ``Slice? -> Distinct? -> Project -> OrderBy? -> block``
    head (or a bare ``OrderBy -> block``), eagerly.

    Anything else is rdflib's: its evaluator runs the node and re-enters the
    hook for the nodes below.
    """
    node, start, stop, distinct, order = part, 0, None, False, []
    if node.name == "Slice":
        start = int(node.start or 0)
        # The key is absent without LIMIT; CompValue then answers None.
        length = node.length
        stop = None if length is None else start + int(length)
        node = node.p
    if node.name == "Distinct":
        distinct = True
        node = node.p
    pv = None
    if node.name == "Project":
        pv = node.PV
        pv = (pv,) if isinstance(pv, Variable) else tuple(pv)
        node = node.p
    elif distinct or start or stop is not None:
        raise NotImplementedError
    if node.name == "OrderBy":
        for condition in node.expr:
            if not isinstance(condition.expr, Variable):
                raise NotImplementedError
            order.append((condition.expr, condition.order == "DESC"))
        node = node.p
    if pv is None and not order:
        raise NotImplementedError
    _check_block(node)
    return _Head(node, pv, start, stop, distinct, order)


def _check_block(node) -> None:
    """Eagerly reject anything the block solver does not handle.

    A *block* is a subtree solved into one code-space relation; this walk and
    `_solve_block`'s dispatch are the two statements of what that subtree may
    contain.
    """
    name = getattr(node, "name", None)
    if name == "BGP":
        return
    if name in ("Filter", "Graph"):
        _check_block(node.p)
        return
    if name in ("Join", "LeftJoin", "Minus"):
        _check_block(node.p1)
        _check_block(node.p2)
        return
    if name == "ToMultiSet" and getattr(node.p, "name", None) == "values":
        return
    raise NotImplementedError


def _node_vars(node, out: list | None = None) -> list:
    """Every variable-like term a block mentions, in first-seen order
    (static: what rdflib's ``_vars`` is made of)."""
    if out is None:
        out = []
    name = node.name
    if name == "BGP":
        for triple in node.triples:
            for term in triple:
                if isinstance(term, _VAR_LIKE) and term not in out:
                    out.append(term)
    elif name == "Filter":
        _node_vars(node.p, out)
    elif name == "ToMultiSet":
        for row in node.p.res:
            for term in row:
                if term not in out:
                    out.append(term)
    elif name == "Graph":
        # The graph term binds in every row of the block below it, and comes
        # last, as it does in a matched pattern's schema.
        _node_vars(node.p, out)
        if isinstance(node.term, _VAR_LIKE) and node.term not in out:
            out.append(node.term)
    else:
        _node_vars(node.p1, out)
        _node_vars(node.p2, out)
    return out


def _scoped_vars(node, scope: _GraphScope) -> list:
    """A block's variable-like terms, plus the graph variable an enclosing
    ``GRAPH ?g`` binds in every row of it."""
    out = _node_vars(node)
    if scope.var is not None and scope.var not in out:
        out.append(scope.var)
    return out


def _block_vars(ctx, node, scope: _GraphScope) -> list:
    """The variables a block binds: its variable-like terms the context has
    not bound, in first-seen order (static, no matching)."""
    return [term for term in _scoped_vars(node, scope) if ctx[term] is None]


def _block_nullable(node) -> frozenset:
    """The variables a block may leave unbound: those an OPTIONAL side
    introduces.

    A graph variable is never among them: a ``GRAPH ?g`` binds it in every row
    of the block below, and when both sides of an OPTIONAL are under the same
    one, the join on it keeps them in the same graph.
    """
    name = node.name
    if name == "BGP":
        return frozenset()
    if name == "Graph":
        return _block_nullable(node.p)
    if name == "ToMultiSet":
        rows = node.p.res
        return frozenset(
            v for v in _node_vars(node) if any(row.get(v, "UNDEF") == "UNDEF" for row in rows)
        )
    if name == "Filter":
        return _block_nullable(node.p)
    if name == "Minus":
        return _block_nullable(node.p1)
    nullable = _block_nullable(node.p1) | _block_nullable(node.p2)
    if name == "LeftJoin":
        nullable |= frozenset(_node_vars(node.p2)) - frozenset(_node_vars(node.p1))
    return nullable


def _eval_head(ctx, store, part):
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    head = _plan_head(part)
    _trace_event(
        "head_planned",
        entry_node=part.name,
        project_variable_count=0 if head.pv is None else len(head.pv),
        order_key_count=len(head.order),
        descending_key_count=sum(descending for _, descending in head.order),
        distinct=head.distinct,
        offset=head.start,
        limit=None if head.stop is None else head.stop - head.start,
    )
    rel = _solve_block(ctx, store, head.block)
    _trace_event(
        "block_solve_complete",
        rows=_relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        foreign_terms=len(rel.foreign),
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
    )
    if head.order:
        # DISTINCT narrows the rows after the sort, so only a plain LIMIT
        # can stop the sort at the top k.
        rows = _order_rows(ctx, store, rel, head.order, None if head.distinct else head.stop)
        rel = Relation(rel.schema, None, rows, len(rows), rel.nullable, rel.foreign)
    if head.distinct:
        return _eval_distinct(ctx, store, rel, head)
    return _yield_solutions(ctx, store, rel, head.pv, head.start, head.stop)


def _order_rows(ctx, store, rel: Relation, order, stop) -> list:
    """The relation's rows in ORDER BY order.

    Each sort variable's distinct codes are ranked once (``_rank_codes``);
    a row's key is the tuple of its ranks, negated for DESC, so one stable
    sort reproduces rdflib's chain of stable sorts, and a following LIMIT
    keeps only the top ``stop`` rows. A variable the block does not bind
    ties every row, as it does for rdflib.
    """
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    rows = _rows_of(rel)
    _trace_event(
        "order_start",
        input_rows=len(rows),
        order_key_count=len(order),
        descending_key_count=sum(descending for _, descending in order),
        limit=stop,
    )
    keys = []
    for key_index, (var, descending) in enumerate(order):
        if var not in rel.schema:
            continue
        index = rel.schema.index(var)
        distinct_codes = {row[index] for row in rows}
        key_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        ranks = _rank_codes(store, rel.foreign, distinct_codes)
        _trace_event(
            "order_key_ranked",
            key_index=key_index,
            descending=descending,
            input_rows=len(rows),
            distinct_codes=len(distinct_codes),
            elapsed_ns=time.perf_counter_ns() - key_started if key_started else 0,
        )
        keys.append((index, ranks, -1 if descending else 1))
    if not keys:
        return rows

    def sort_key(row):
        return tuple(sign * ranks[row[i]] for i, ranks, sign in keys)

    if stop is not None and stop < len(rows):
        output = heapq.nsmallest(stop, rows, key=sort_key)
        algorithm = "bounded_heap_nsmallest"
    else:
        output = sorted(rows, key=sort_key)
        algorithm = "full_stable_sort"
    _trace_event(
        "order_complete",
        algorithm=algorithm,
        input_rows=len(rows),
        output_rows=len(output),
        limit=stop,
        limit_applied_during_order=algorithm == "bounded_heap_nsmallest",
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
    )
    return output


def _rank_codes(store, foreign: dict, codes) -> dict:
    """Ranks reproducing rdflib's ORDER BY comparison (``_val``): unbound
    first, then blank nodes, IRIs and literals. Blank nodes and IRIs order
    as their spellings, so their codes already are their ranks; literals are
    decoded and sorted with rdflib's own comparator, adjacent terms that
    compare equal sharing a rank so the stable sort keeps their input
    order."""
    started = time.perf_counter_ns() if _TRACE.get() is not None else 0
    codes = set(codes)
    cache_before = set(store._decode_cache) if _TRACE.get() is not None else set()
    ranks = {None: -1}
    literal_lo, iri_lo, blank_lo = store._term_kind_bounds()
    blanks, iris, others = [], [], []
    by_kind = all(code is None or code >= 0 for code in codes)
    for code in codes:
        if code is None:
            continue
        if by_kind and code >= blank_lo:
            blanks.append(code)
        elif by_kind and code >= iri_lo:
            iris.append(code)
        else:
            others.append(code)
    rank = 0
    for code in sorted(blanks) + sorted(iris):
        ranks[code] = rank
        rank += 1
    if others:
        store._prime_decode_cache([code for code in others if code >= 0])
        cached = store._decode_cache
        terms = [(_val(cached[c] if c >= 0 else foreign[c]), c) for c in others]
        terms.sort(key=lambda item: item[0])
        previous = None
        for key, code in terms:
            if previous is not None and previous < key:
                rank += 1
            ranks[code] = rank
            previous = key
    if _TRACE.get() is not None:
        nonnegative_others = {code for code in others if code >= 0}
        _trace_event(
            "rank_codes_complete",
            input_codes=len(codes),
            unique_codes=len(codes),
            null_codes=int(None in codes),
            blank_codes=len(blanks),
            iri_codes=len(iris),
            other_codes=len(others),
            foreign_codes=sum(code < 0 for code in others),
            decoded_codes=len(nonnegative_others),
            decode_cache_hits=len(nonnegative_others & cache_before),
            decode_cache_misses=len(nonnegative_others - cache_before),
            elapsed_ns=time.perf_counter_ns() - started,
        )
    return ranks


def _eval_distinct(ctx, store, rel: Relation, head: _Head):
    """DISTINCT over the projected variables, without decoding the
    duplicates: code-distinct tuples first, then a term-level pass over the
    survivors (rdflib compares decoded terms, and two spellings of a typed
    literal can be one term), then LIMIT/OFFSET."""
    pv = head.pv or ()
    keys = tuple(v for v in pv if v in rel.schema)
    idx = [rel.schema.index(v) for v in keys]
    rows = _term_distinct(store, _code_distinct(rel, idx), rel.foreign)
    return _yield_rows(ctx, store, keys, rows, rel.foreign, pv, head.start, head.stop)


def _code_distinct(rel: Relation, idx: list) -> Iterator[tuple]:
    """The relation's rows projected to ``idx``, one per distinct code
    tuple, in first-seen order."""
    if rel.cols is not None and not rel.preds:
        if len(idx) == 1:
            return ((code,) for code in dict.fromkeys(rel.cols[idx[0]]))
        columns = [rel.cols[i].tolist() for i in idx]
        return iter(dict.fromkeys(zip(*columns, strict=True)))

    def rows():
        seen: set = set()
        for row in _code_rows(rel):
            key = tuple(row[i] for i in idx)
            if key not in seen:
                seen.add(key)
                yield key

    return rows()


def _term_distinct(store, tuples: Iterator[tuple], foreign: dict) -> Iterator[tuple]:
    """Drop the code tuples whose decoded terms equal an earlier tuple's."""
    seen: set = set()
    size = _CHUNK_START
    while True:
        chunk = list(islice(tuples, size))
        if not chunk:
            return
        keys = _equivalence_keys(
            store, {c for row in chunk for c in row if c is not None and c >= 0}
        )
        for row in chunk:
            key = tuple(None if c is None else (keys[c] if c >= 0 else foreign[c]) for c in row)
            if key not in seen:
                seen.add(key)
                yield row
        size = min(size * 4, _CHUNK_MAX)


def _equivalence_keys(store, codes) -> dict:
    """Keys equal iff rdflib finds the decoded terms equal.

    IRIs, blank nodes and untyped literals are one term per spelling, so the
    code itself is the key; a typed literal's lexical form may be normalized
    by rdflib (``"042"^^xsd:integer`` is ``42``), so it is keyed by the
    decoded term. Only typed literals are decoded here.
    """
    literal_lo, iri_lo, _ = store._term_kind_bounds()
    keys: dict = {}
    literals = []
    for code in codes:
        if literal_lo <= code < iri_lo:
            literals.append(code)
        else:
            keys[code] = code
    if literals:
        cache = store._decode_cache
        for code, spelling in zip(literals, store._dict.decode_many(literals), strict=True):
            if spelling is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            if spelling.endswith(">"):
                term = cache.get(code)
                if term is None:
                    term = cache[code] = store._from_n3_safe(spelling)
                keys[code] = term
            else:
                keys[code] = code
    return keys


def _eval_aggregate(ctx, store, part):
    """``AggregateJoin`` over a ``Group`` of a block, for COUNT aggregates.

    Counts are taken over the code columns; only the group keys are decoded.
    Groups whose decoded keys are equal terms are merged, as rdflib groups by
    decoded term. ``Aggregate_Sample`` of a GROUP BY variable is the key
    itself (that is how rdflib carries the group variable into the row);
    every other aggregate is rdflib's.
    """
    group = part.p
    if getattr(group, "name", None) != "Group":
        raise NotImplementedError
    block = group.p
    _check_block(block)
    block, scope = _peel_graph(ctx, block, _active_scope(ctx, store))
    group_vars = None if group.expr is None else list(group.expr)
    if group_vars is not None and not all(isinstance(v, Variable) for v in group_vars):
        raise NotImplementedError
    counts, samples = [], []
    for agg in part.A:
        if agg.name == "Aggregate_Count":
            target = agg.vars
            if target != "*" and not isinstance(target, _VAR_LIKE):
                raise NotImplementedError
            counts.append((agg.res, target, bool(agg.distinct)))
        elif agg.name == "Aggregate_Sample":
            if group_vars is None or agg.vars not in group_vars:
                raise NotImplementedError
            samples.append((agg.res, agg.vars))
        else:
            raise NotImplementedError

    # One pattern, no grouping, plain counts: the row selection's size.
    if group_vars is None and block.name == "BGP" and len(block.triples) == 1:
        pat = _pattern_terms(ctx, store, *block.triples[0], scope=scope)
        if all(len(pos) == 1 for pos in pat["varpos"].values()) and all(
            not distinct and (target == "*" or target in pat["varpos"] or ctx[target] is not None)
            for _, target, distinct in counts
        ):
            n = 0 if pat["unsatisfiable"] else store._store().count_quads(*pat["n3"])
            return iter([FrozenBindings(ctx, {res: Literal(n) for res, _, _ in counts})])

    rel = _solve_block(ctx, store, block, scope=scope)
    return _aggregate_rows(ctx, store, rel, group_vars or [], counts, samples, group.expr is None)


@dataclass(slots=True)
class _CountSpec:
    """One COUNT aggregate over the block: ``rows`` (``COUNT(*)``), a
    column (``COUNT(?v)`` for a block variable), ``const`` (a variable the
    context bound, present in every row) or ``none`` (bound nowhere)."""

    res: Any
    kind: str
    col: int = -1
    distinct: bool = False


@dataclass(slots=True)
class _Group:
    rows: int
    counts: list  # per spec: bound values seen (non-distinct column counts)
    sets: list  # per spec: the codes (or rows) seen, for distinct counts


def _aggregate_rows(ctx, store, rel: Relation, group_vars, counts, samples, implicit_group):
    schema = rel.schema
    col = {v: i for i, v in enumerate(schema)}
    # A group key part is a column; a context-bound variable does not split
    # groups and an unbound one contributes a None key part (as rdflib's).
    key_cols = [col[v] for v in group_vars if v in col]
    specs = []
    for res, target, distinct in counts:
        if target == "*":
            specs.append(_CountSpec(res, "rows", distinct=distinct))
        elif target in col:
            specs.append(_CountSpec(res, "col", col[target], distinct))
        elif ctx[target] is not None:
            specs.append(_CountSpec(res, "const", distinct=distinct))
        else:
            specs.append(_CountSpec(res, "none"))
    nspecs = len(specs)

    def new_group(rows=0):
        return _Group(rows, [0] * nspecs, [set() for _ in specs])

    groups: dict[tuple, _Group] = {}
    plain = all(spec.kind in ("rows", "const") and not spec.distinct for spec in specs)
    if rel.cols is not None and not rel.preds and len(key_cols) <= 1 and plain:
        # COUNT(*) [GROUP BY ?v] over one pattern: count the codes directly.
        if key_cols:
            for code, n in Counter(rel.cols[key_cols[0]]).items():
                groups[(code,)] = new_group(n)
        elif rel.nrows:
            groups[()] = new_group(rel.nrows)
    else:
        for row in _code_rows(rel):
            key = tuple(row[i] for i in key_cols)
            group = groups.get(key)
            if group is None:
                group = groups[key] = new_group()
            group.rows += 1
            for j, spec in enumerate(specs):
                if spec.kind == "col":
                    code = row[spec.col]
                    if code is not None:
                        if spec.distinct:
                            group.sets[j].add(code)
                        else:
                            group.counts[j] += 1
                elif spec.distinct:  # rows, const
                    group.sets[j].add(row if spec.kind == "rows" else ())

    if not groups:
        if implicit_group:
            return iter([FrozenBindings(ctx, {res: Literal(0) for res, _, _ in counts})])
        return iter([FrozenBindings(ctx)])

    # Merge groups whose decoded keys are equal terms; distinct counts likewise
    # count equal terms once.
    codes = {c for key in groups for c in key if c is not None}
    for group in groups.values():
        for j, spec in enumerate(specs):
            if spec.distinct and spec.kind == "col":
                codes.update(group.sets[j])
            elif spec.distinct and spec.kind == "rows":
                codes.update(c for row in group.sets[j] for c in row if c is not None)
    equivalence = _equivalence_keys(store, {c for c in codes if c >= 0})
    foreign = rel.foreign

    def term_key(code):
        return None if code is None else (equivalence[code] if code >= 0 else foreign[code])

    merged: dict[tuple, tuple[tuple, _Group]] = {}
    for key, group in groups.items():
        tkey = tuple(term_key(c) for c in key)
        first = merged.get(tkey)
        if first is None:
            merged[tkey] = (key, group)
            continue
        base = first[1]
        base.rows += group.rows
        for j in range(nspecs):
            base.counts[j] += group.counts[j]
            base.sets[j] |= group.sets[j]

    key_index = {v: i for i, v in enumerate(v for v in group_vars if v in col)}
    store._prime_decode_cache(
        [c for key, _ in merged.values() for c in key if c is not None and c >= 0]
    )
    cached = store._decode_cache

    def solutions():
        for key, group in merged.values():
            row: dict = {}
            for j, spec in enumerate(specs):
                if spec.kind == "rows":
                    if spec.distinct:
                        n = len({tuple(term_key(c) for c in r) for r in group.sets[j]})
                    else:
                        n = group.rows
                elif spec.kind == "col":
                    n = (
                        len({term_key(c) for c in group.sets[j]})
                        if spec.distinct
                        else group.counts[j]
                    )
                elif spec.kind == "const":
                    n = 1 if spec.distinct else group.rows
                else:
                    n = 0
                row[spec.res] = Literal(n)
            for res, var in samples:
                if var in key_index:
                    code = key[key_index[var]]
                    row[res] = (
                        None if code is None else (cached[code] if code >= 0 else foreign[code])
                    )
                else:
                    row[res] = ctx[var]
            yield FrozenBindings(ctx, row)

    return solutions()


def _eval_block_node(ctx, store, part):
    _check_block(part)
    return _yield_solutions(ctx, store, _solve_block(ctx, store, part))


def _eval_ask(ctx, store, part):
    project = part.p
    if getattr(project, "name", None) != "Project":
        raise NotImplementedError
    _check_block(project.p)
    return {"type_": "ASK", "askAnswer": _block_nonempty(ctx, store, project.p)}


def _block_nonempty(ctx, store, block) -> bool:
    block, scope = _peel_graph(ctx, block, _active_scope(ctx, store))
    if block.name == "BGP" and len(block.triples) == 1:
        pat = _pattern_terms(ctx, store, *block.triples[0], scope=scope)
        if pat["unsatisfiable"]:
            return False
        if all(len(pos) == 1 for pos in pat["varpos"].values()):
            # Existence needs no rows: count from the row selection.
            return store._store().count_quads(*pat["n3"]) > 0
    rel = _solve_block(ctx, store, block, scope=scope)
    return next(_code_rows(rel), None) is not None


def _solve_block(ctx, store, node, env=frozenset(), var_preds=None, scope=None) -> Relation:
    """Solve a block into a relation.

    ``env`` is the set of variables an enclosing lazy join or OPTIONAL binds
    row by row in rdflib's evaluation — here the sides are solved
    independently and joined, which is only exact while nothing inside
    depends on those values (a FILTER that could see them falls back).
    ``var_preds`` are single-variable filter conjuncts pushed down to the
    pattern scans binding the variable. ``scope`` is the graph every pattern
    below is matched in (:class:`_GraphScope`); ``None`` means the graph the
    context has made active.
    """
    if scope is None:
        scope = _active_scope(ctx, store)
    name = node.name
    trace = _TRACE.get()
    started = time.perf_counter_ns() if trace is not None else 0
    if trace is not None:
        trace.depth += 1
    try:
        if name == "BGP":
            rel = _solve_bgp(ctx, store, node.triples, scope, var_preds)
        elif name == "Filter":
            rel = _solve_filter(ctx, store, node, env, var_preds, scope)
        elif name == "Join":
            rel = _solve_join(ctx, store, node, env, var_preds, scope)
        elif name == "LeftJoin":
            rel = _solve_left_join(ctx, store, node, env, var_preds, scope)
        elif name == "Minus":
            rel = _solve_minus(ctx, store, node, env, var_preds, scope)
        elif name == "ToMultiSet":
            rel = _solve_values(ctx, store, node, scope)
        elif name == "Graph":
            rel = _solve_block(ctx, store, node.p, env, var_preds, _graph_scope(ctx, node))
        else:
            raise NotImplementedError
    finally:
        if trace is not None:
            trace.depth -= 1
    _trace_event(
        "block_operator_complete",
        operator=name,
        depth=0 if trace is None else trace.depth,
        rows=_relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        elapsed_ns=time.perf_counter_ns() - started if started else 0,
        pattern_count=len(node.triples) if name == "BGP" else None,
    )
    return rel


def _solve_values(ctx, store, node, scope) -> Relation:
    """An inline VALUES table as a relation: each constant looked up by its
    canonical spelling (a term the dictionary does not hold gets a negative
    code, so it joins nothing but is still yielded verbatim), UNDEF unbound.
    A row that contradicts a context binding is dropped, as rdflib's
    ``evalValues`` skips it on AlreadyBound."""
    rows_in = node.p.res
    graph_n3 = scope.n3
    variables = _node_vars(node)
    schema = tuple(v for v in variables if ctx[v] is None)
    bound = {v: ctx[v] for v in variables if ctx[v] is not None}
    codes: dict = {}
    rows = []
    for row in rows_in:
        if any(row.get(v, "UNDEF") != "UNDEF" and row[v] != term for v, term in bound.items()):
            continue
        out = []
        for v in schema:
            term = row.get(v, "UNDEF")
            if term == "UNDEF":
                out.append(None)
                continue
            code = codes.get(term)
            if code is None:
                code = codes[term] = _constant_code(store, term, graph_n3)
            out.append(code)
        rows.append(tuple(out))
    nullable = frozenset(v for i, v in enumerate(schema) if any(row[i] is None for row in rows))
    return Relation(schema, None, rows, len(rows), nullable, store._foreign)


def _constant_code(store, term, graph_n3) -> int:
    """The dictionary code of a query constant, or a private negative one.

    ``encode`` is an exact lookup of the canonical spelling; if it misses but
    the store does hold the term under a spelling the pattern parser accepts
    (``count_quads`` is spelling-tolerant), the canonicalization disagreed
    with the store's and the query is left to rdflib rather than guessed.
    The probes are scoped to ``graph_n3``, the graph the rows would join in.
    """
    code = store._dict.encode(canonical_spelling(term))
    if code is not None:
        # rdflib would carry the query's own object into the solutions; when
        # that object is not the decoded dictionary term (a query literal
        # rdflib's parser left unnormalized, "042"^^xsd:integer), the two are
        # observably different rows, so the query is left to rdflib.
        if store._decode_term(code) != term:
            raise NotImplementedError
        return code
    n3 = term.n3()
    count = store._store().count_quads
    positions = [(None, None, n3, graph_n3)]
    if not isinstance(term, Literal):
        positions.append((n3, None, None, graph_n3))
        if isinstance(term, URIRef):
            positions.append((None, n3, None, graph_n3))
    if any(count(*pattern) for pattern in positions):
        raise NotImplementedError
    return store._foreign_code(term)


def _solve_filter(ctx, store, node, env, var_preds, scope) -> Relation:
    """A ``Filter`` over a block: constant conjuncts decide the whole block
    before anything is matched, single-variable conjuncts over variables the
    block always binds restrict the pattern scans, the rest filters the
    solved rows."""
    inner = node.p
    block_vars = _block_vars(ctx, inner, scope)
    plan = filters.analyze_filter(node, block_vars, ctx)
    if plan.exists and scope.var is not None:
        # rdflib evaluates an EXISTS body against the *active* graph. Under a
        # graph variable there is no single active graph — the row's graph is
        # a column here — so the body would be answered over the union. Hand
        # the whole block back and let rdflib walk the graphs itself.
        raise NotImplementedError
    if env:
        _reject_env_references(node, plan, env, ctx)
    for conjunct in plan.constant:
        if not filters.evaluate_constant(conjunct):
            return Relation.from_rows(tuple(block_vars), [])
    nullable = _block_nullable(inner)
    pushed = dict(var_preds or {})
    residual = list(plan.tuples)
    for var, conjuncts in plan.per_var.items():
        if var in nullable:
            residual.extend(conjuncts)
        else:
            pushed[var] = pushed.get(var, []) + conjuncts
    rel = _solve_block(ctx, store, inner, env, pushed or None, scope)
    if residual:
        preds = tuple(
            filters.tuple_predicate(store, conjunct, [rel.schema.index(v) for v in conjunct.vars])
            for conjunct in residual
        )
        rel = _filter_relation(rel, preds)
    for exists in plan.exists:
        rel = _apply_exists(ctx, store, rel, exists, scope)
    return rel


def _apply_exists(ctx, store, rel: Relation, exists, scope) -> Relation:
    """``FILTER (NOT) EXISTS { body }`` as a semi- or anti-join on the
    variables the body shares with the block: the body is solved once (or,
    for a small block and a one-pattern body, probed per row with
    ``count_quads``) instead of once per row. Shapes rdflib would evaluate
    differently on an independently solved body — a nullable shared
    variable, a body the solver cannot take, a body FILTER that sees the
    block's bindings — take the generic route, rdflib's own evaluator per
    distinct tuple."""
    body, negate = _unwrap_empty_joins(exists.body), exists.negate
    rows = _rows_of(rel)
    if not rows:
        return rel

    def generic():
        conjunct = exists.conjunct
        positions = [rel.schema.index(v) for v in conjunct.vars]
        return _filter_relation(rel, (filters.tuple_predicate(store, conjunct, positions),))

    try:
        _check_block(body)
    except NotImplementedError:
        return generic()
    shared = [v for v in _block_vars(ctx, body, scope) if v in rel.schema]
    if any(v in rel.nullable for v in shared):
        return generic()
    env = frozenset(rel.schema)
    try:
        if not shared:
            found = (
                next(_code_rows(_solve_block(ctx, store, body, env, None, scope)), None) is not None
            )
            if found != negate:
                return rel
            return Relation(rel.schema, None, [], 0, rel.nullable, rel.foreign)
        if body.name == "BGP" and len(body.triples) == 1:
            pat = _match_pattern(ctx, store, *body.triples[0], scope=scope)
            repeated_free = any(
                len(pos) > 1 for v, pos in pat["varpos"].items() if v not in rel.schema
            )
            if not repeated_free and len(rows) * _PROBE_FANOUT < pat["nrows"]:
                flags = _probe_exists(store, rel.schema, rows, pat)
                kept = [row for row, found in zip(rows, flags, strict=True) if found != negate]
                return Relation(rel.schema, None, kept, len(kept), rel.nullable, rel.foreign)
            inner = _rel_from_pattern(pat)
        else:
            inner = _solve_block(ctx, store, body, env, None, scope)
    except NotImplementedError:
        return generic()
    ia = [rel.schema.index(v) for v in shared]
    ib = [inner.schema.index(v) for v in shared]
    keys = {tuple(row[i] for i in ib) for row in _code_rows(inner)}
    kept = [row for row in rows if (tuple(row[i] for i in ia) in keys) != negate]
    return Relation(rel.schema, None, kept, len(kept), rel.nullable, rel.foreign)


def _unwrap_empty_joins(node):
    """rdflib never simplifies an EXISTS body, so a group translates to a
    Join with an empty BGP on one side; look through those."""
    while getattr(node, "name", None) == "Join":
        p1, p2 = node.p1, node.p2
        if getattr(p1, "name", None) == "BGP" and not p1.triples:
            node = p2
        elif getattr(p2, "name", None) == "BGP" and not p2.triples:
            node = p1
        else:
            break
    return node


def _probe_exists(store, schema, rows, pat) -> list:
    """Whether the pattern, with each row's codes substituted, matches at
    least one quad — one ``count_quads`` per row, no row materialized."""
    count = store._store().count_quads
    bound = [(schema.index(v), pos) for v, pos in pat["varpos"].items() if v in schema]
    n3_cache = _probe_spellings(store, rows, bound)
    out = []
    for row in rows:
        n3 = list(pat["n3"])
        satisfiable = True
        for row_idx, positions in bound:
            term = n3_cache[row[row_idx]]
            for idx in positions:
                if (
                    (idx == 0 and term[:1] == '"')
                    or (idx == 1 and term[:1] != "<")
                    or (idx == 3 and term[:1] == '"')
                ):
                    satisfiable = False
                n3[idx] = term
        out.append(satisfiable and count(*n3) > 0)
    return out


def _reject_env_references(node, plan, env, ctx) -> None:
    """A conjunct referencing a variable an enclosing join binds row by row,
    where rdflib would let the filter see that binding, cannot be evaluated
    on an independently solved block."""
    everything = getattr(node, "no_isolated_scope", False)
    allowed = set(node._vars or ()) | set(ctx.initBindings or ())
    conjuncts = plan.constant + plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    for conjunct in conjuncts:
        for var in filters.expr_vars(conjunct.expr):
            if var in env and var not in conjunct.vars and var not in conjunct.consts:
                if everything or var in allowed:
                    raise NotImplementedError


def _rows_of(rel: Relation) -> list:
    """The relation's rows, materialized."""
    if rel.rows is not None:
        return rel.rows
    return list(_code_rows(rel))


def _shared_key_ok(left: Relation, right: Relation, shared) -> None:
    # A join key that may be unbound needs rdflib's compatibility semantics
    # (unbound matches anything); a hash join cannot give them.
    if any(v in left.nullable or v in right.nullable for v in shared):
        raise NotImplementedError


def _solve_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """A group join: both sides solved, then a hash join on the shared
    variables (a probe of the right pattern when the left side is small).
    rdflib deduplicates the right side of a non-lazy join."""
    lazy = bool(node.lazy)
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    left_rows = _rows_of(left)
    if not left_rows:
        schema = left.schema + tuple(
            v for v in _block_vars(ctx, node.p2, scope) if v not in left.schema
        )
        return Relation(schema, None, [], 0, left.nullable | _block_nullable(node.p2), left.foreign)
    right_env = env | frozenset(_scoped_vars(node.p1, scope)) if lazy else env
    p2 = node.p2
    if lazy and p2.name == "BGP" and len(p2.triples) == 1:
        pat = _match_pattern(ctx, store, *p2.triples[0], scope=scope)
        if var_preds:
            _restrict_pattern(store, pat, var_preds)
        shares = any(v in left.schema for v in pat["varpos"])
        if shares and len(left_rows) * _PROBE_FANOUT < pat["nrows"]:
            _shared_key_ok(
                left, Relation((), None, [], 0), [v for v in pat["varpos"] if v in left.schema]
            )
            schema, rows = _probe_join(store, left.schema, left_rows, pat)
            return Relation(schema, None, rows, len(rows), left.nullable, left.foreign)
        right = _rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, right_env, var_preds, scope)
    right_rows = _rows_of(right)
    if not lazy:
        right_rows = list(dict.fromkeys(right_rows))
    shared = [v for v in right.schema if v in left.schema]
    _shared_key_ok(left, right, shared)
    schema, rows = _join(left.schema, left_rows, right.schema, right_rows)
    foreign = left.foreign or right.foreign
    return Relation(schema, None, rows, len(rows), left.nullable | right.nullable, foreign)


def _solve_left_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """An OPTIONAL: every left row, extended by the matching right rows that
    pass the hoisted inner FILTER, or padded with unbound variables."""
    p1, p2, expr = node.p1, node.p2, node.expr
    vars1, vars2 = _scoped_vars(p1, scope), _scoped_vars(p2, scope)
    condition = expr if getattr(expr, "name", None) != "TrueFilter" else None
    # rdflib re-evaluates an unmatched OPTIONAL with only p1's `_vars` bound
    # (its "cheated scope" check). That differs from the first pass when a
    # variable that pass drops — bound by the context, by an enclosing join,
    # or by p1 outside its `_vars` (a VALUES table) — reaches p2, or when a
    # variable it keeps was invisible to the condition before; those shapes
    # are left to rdflib.
    outer = dict(ctx.bindings.items())
    init = frozenset(ctx.initBindings or ())
    p1_vars = p1._vars
    if p1_vars is not None:
        p1_vars = frozenset(p1_vars)
        dropped = (env | frozenset(outer) | frozenset(vars1)) - init - p1_vars
        # A graph variable is not a binding that flows from p1 into p2: rdflib
        # evaluates the whole OPTIONAL with the graph *active* and labels the
        # result with it afterwards, so p2 is in that graph however p1 went.
        # Here both sides carry it as a column and the join on it says the
        # same thing — including for a padded row, whose graph comes from p1.
        dropped -= {scope.var}
        if dropped & frozenset(vars2):
            raise NotImplementedError
        cheated = (env | frozenset(outer)) - init
        if condition is not None and frozenset(filters.expr_vars(condition)) & p1_vars & cheated:
            raise NotImplementedError

    left = _solve_block(ctx, store, p1, env, var_preds, scope)
    left_rows = _rows_of(left)
    extra = tuple(v for v in _block_vars(ctx, p2, scope) if v not in left.schema)
    nullable = left.nullable | _block_nullable(p2) | frozenset(extra)
    if not left_rows:
        return Relation(left.schema + extra, None, [], 0, nullable, left.foreign)

    def condition_preds(schema):
        if condition is None:
            return ()
        init = set(ctx.initBindings or ())
        visible = {v: term for v, term in outer.items() if v in init}
        plan = filters.analyze_expr(condition, list(schema), ctx, visible)
        if plan.exists:
            # An OPTIONAL's condition can carry (NOT) EXISTS; a semi-join over
            # the padded rows is not what rdflib computes, so the whole
            # LeftJoin goes back to it.
            raise NotImplementedError
        for conjunct in plan.constant:
            if not filters.evaluate_constant(conjunct):
                return None  # no pair can pass: every left row is unmatched
        conjuncts = plan.tuples + [c for cs in plan.per_var.values() for c in cs]
        return tuple(
            filters.tuple_predicate(store, c, [schema.index(v) for v in c.vars]) for c in conjuncts
        )

    if p2.name == "BGP" and len(p2.triples) == 1:
        # Resolve and count before matching. A selective left relation can
        # drive OPTIONAL probes without first materializing the complete
        # predicate relation that the probes are intended to avoid.
        plan_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        pat = _pattern_terms(ctx, store, *p2.triples[0], scope=scope)
        shares = any(v in left.schema for v in pat["varpos"])
        estimate_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        estimate_rows = 0 if pat["unsatisfiable"] else store._store().count_quads(*pat["n3"])
        estimate_ns = time.perf_counter_ns() - estimate_started if estimate_started else 0
        if shares and len(left_rows) * _PROBE_FANOUT < estimate_rows:
            _shared_key_ok(
                left, Relation((), None, [], 0), [v for v in pat["varpos"] if v in left.schema]
            )
            schema = left.schema + tuple(v for v in pat["varpos"] if v not in left.schema)
            preds = condition_preds(schema)
            if preds is None:
                rows = [row + (None,) * len(extra) for row in left_rows]
                _trace_event(
                    "left_join_plan_complete",
                    strategy="constant_false",
                    input_rows=len(left_rows),
                    estimated_right_rows=estimate_rows,
                    estimate_calls=int(not pat["unsatisfiable"]),
                    native_initial_match_count=0,
                    native_probe_call_count=0,
                    matched_left_rows=0,
                    unmatched_left_rows=len(left_rows),
                    output_rows=len(rows),
                    estimate_ns=estimate_ns,
                    elapsed_ns=time.perf_counter_ns() - plan_started if plan_started else 0,
                )
                return Relation(left.schema + extra, None, rows, len(rows), nullable, left.foreign)
            probe_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
            schema, rows = _probe_join(
                store, left.schema, left_rows, pat, keep_unmatched=True, row_preds=preds
            )
            probe_ns = time.perf_counter_ns() - probe_started if probe_started else 0
            free_count = sum(v not in left.schema for v in pat["varpos"])
            unmatched = (
                sum(all(value is None for value in row[-free_count:]) for row in rows)
                if free_count
                else 0
            )
            _trace_event(
                "left_join_plan_complete",
                strategy="probe",
                input_rows=len(left_rows),
                estimated_right_rows=estimate_rows,
                estimate_calls=int(not pat["unsatisfiable"]),
                native_initial_match_count=0,
                native_probe_call_count=len(left_rows),
                matched_left_rows=len(left_rows) - unmatched,
                unmatched_left_rows=unmatched,
                output_rows=len(rows),
                estimate_ns=estimate_ns,
                probe_ns=probe_ns,
                elapsed_ns=time.perf_counter_ns() - plan_started if plan_started else 0,
            )
            return Relation(schema, None, rows, len(rows), nullable, left.foreign)
        match_started = time.perf_counter_ns() if _TRACE.get() is not None else 0
        pat = _match_pattern(ctx, store, *p2.triples[0], scope=scope)
        match_ns = time.perf_counter_ns() - match_started if match_started else 0
        _trace_event(
            "left_join_plan_complete",
            strategy="hash",
            input_rows=len(left_rows),
            estimated_right_rows=estimate_rows,
            estimate_calls=int(not pat["unsatisfiable"]),
            native_initial_match_count=int(not pat["unsatisfiable"]),
            native_probe_call_count=0,
            matched_left_rows=None,
            unmatched_left_rows=None,
            output_rows=None,
            estimate_ns=estimate_ns,
            native_match_ns=match_ns,
            elapsed_ns=time.perf_counter_ns() - plan_started if plan_started else 0,
        )
        right = _rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, env | frozenset(vars1), None, scope)
    shared = [v for v in right.schema if v in left.schema]
    _shared_key_ok(left, right, shared)
    schema = left.schema + tuple(v for v in right.schema if v not in left.schema)
    foreign = left.foreign or right.foreign
    preds = condition_preds(schema)
    if preds is None:
        rows = [row + (None,) * (len(schema) - len(left.schema)) for row in left_rows]
        return Relation(schema, None, rows, len(rows), nullable, foreign)
    rows = _left_join_rows(left.schema, left_rows, right.schema, _rows_of(right), preds)
    return Relation(schema, None, rows, len(rows), nullable, foreign)


def _left_join_rows(schema_a, rows_a, schema_b, rows_b, preds) -> list:
    """Hash left join: each left row extended by every compatible right row
    that passes ``preds`` (over the combined row), or padded with None."""
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    ia = [schema_a.index(v) for v in shared]
    ib = [schema_b.index(v) for v in shared]
    table: dict = {}
    for rb in rows_b:
        table.setdefault(tuple(rb[i] for i in ib), []).append(tuple(rb[i] for i in keep_b))
    pad = (None,) * len(keep_b)
    out = []
    for ra in rows_a:
        matched = False
        for tail in table.get(tuple(ra[i] for i in ia), ()):
            row = ra + tail
            if all(pred(row) for pred in preds):
                out.append(row)
                matched = True
        if not matched:
            out.append(ra + pad)
    return out


def _solve_minus(ctx, store, node, env, var_preds, scope) -> Relation:
    """MINUS: an anti-join on the shared variables. Without shared block
    variables rdflib's compatibility test turns on the context's own
    bindings, which every row of both sides carries: nothing is removed at
    top level, everything is when the right side is non-empty otherwise."""
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    left_rows = _rows_of(left)
    if not left_rows:
        return left
    right = _solve_block(ctx, store, node.p2, env, None, scope)
    shared = [v for v in right.schema if v in left.schema]
    if not shared:
        if dict(ctx.bindings.items()) and next(_code_rows(right), None) is not None:
            return Relation(left.schema, None, [], 0, left.nullable, left.foreign)
        return Relation(left.schema, None, left_rows, len(left_rows), left.nullable, left.foreign)
    _shared_key_ok(left, right, shared)
    ia = [left.schema.index(v) for v in shared]
    ib = [right.schema.index(v) for v in shared]
    keys = {tuple(rb[i] for i in ib) for rb in _code_rows(right)}
    rows = [ra for ra in left_rows if tuple(ra[i] for i in ia) not in keys]
    return Relation(left.schema, None, rows, len(rows), left.nullable, left.foreign)


def _filter_relation(rel: Relation, preds) -> Relation:
    """Apply row predicates: streamed over a columnar body, eagerly over rows."""
    if rel.cols is not None:
        return Relation(
            rel.schema,
            rel.cols,
            None,
            rel.nrows,
            rel.nullable,
            rel.foreign,
            rel.preds + tuple(preds),
        )
    assert rel.rows is not None  # a relation has exactly one body
    rows = [row for row in rel.rows if all(pred(row) for pred in preds)]
    return Relation(rel.schema, None, rows, len(rows), rel.nullable, rel.foreign)


# Probing beats hash-joining when the running relation is at least this many
# times smaller than the next pattern's match. Measured on the in-memory
# dictionary layout: one native match costs ~70 µs regardless of selectivity,
# while materializing a matched row into Python costs ~0.7 µs — so one probe
# is worth skipping the materialization of ~100 rows.
_PROBE_FANOUT = 100


def _pattern_trace_shape(ctx, triple) -> dict:
    resolved = [ctx[term] for term in triple]
    bound = [value is not None for value in resolved]
    variables = [term for term, value in zip(triple, resolved, strict=True) if value is None]
    return {
        "bound_subject": bound[0],
        "bound_predicate": bound[1],
        "bound_object": bound[2],
        "variable_count": len(set(variables)),
        "repeated_variable_count": len(variables) - len(set(variables)),
        "pattern_shape": "".join("B" if item else "V" for item in bound),
    }


def _solve_bgp(ctx, store, triples, scope, var_preds=None) -> Relation:
    """Evaluate a BGP by matching one seed and probing connected patterns."""
    trace = _TRACE.get()
    traced = trace is not None
    started = time.perf_counter_ns() if traced else 0
    _trace_event("bgp_start", pattern_count=len(triples), initial_bound_variable_count=0)
    if not triples:
        rel = Relation.from_rows((), [()])
        _trace_event(
            "bgp_complete",
            pattern_count=0,
            output_rows=1,
            output_columns=0,
            native_call_count=0,
            native_initial_match_count=0,
            native_probe_call_count=0,
            native_match_ns=0,
            restriction_ns=0,
            join_ns=0,
            other_ns=0,
            elapsed_ns=0,
            timing_reconciled=True,
        )
        return rel

    # Keep the established single-pattern path unchanged in behavior.
    if len(triples) == 1:
        shape = _pattern_trace_shape(ctx, triples[0]) if traced else None
        _trace_event("bgp_pattern_start", original_pattern_index=0, **(shape or {}))
        then = time.perf_counter_ns() if traced else 0
        pat = _match_pattern(ctx, store, *triples[0], scope)
        native_ns = time.perf_counter_ns() - then if then else 0
        if traced:
            pat["_trace_original_index"] = 0
        _trace_event(
            "bgp_pattern_complete",
            original_pattern_index=0,
            cardinality_source="matched",
            native_call_count=1,
            matched_rows=pat["nrows"],
            native_match_ns=native_ns,
            restriction_ns=0,
            relation_extend_ns=0,
            other_ns=0,
            elapsed_ns=native_ns,
            timing_reconciled=True,
            **(shape or {}),
        )
        then = time.perf_counter_ns() if traced else 0
        if var_preds:
            _restrict_pattern(store, pat, var_preds)
        restriction_ns = time.perf_counter_ns() - then if then else 0
        then = time.perf_counter_ns() if traced else 0
        rel = _rel_from_pattern(pat)
        join_ns = time.perf_counter_ns() - then if then else 0
        elapsed = time.perf_counter_ns() - started if started else 0
        known = native_ns + restriction_ns + join_ns
        other_ns = max(0, elapsed - known)
        _trace_event(
            "bgp_complete",
            pattern_count=1,
            output_rows=_relation_rows(rel),
            output_columns=len(rel.schema),
            native_call_count=1,
            native_initial_match_count=1,
            native_probe_call_count=0,
            native_match_ns=native_ns,
            restriction_ns=restriction_ns,
            join_ns=join_ns,
            other_ns=other_ns,
            elapsed_ns=known + other_ns,
            timing_reconciled=True,
        )
        return rel

    patterns = []
    for index, triple in enumerate(triples):
        shape = _pattern_trace_shape(ctx, triple) if traced else None
        _trace_event("bgp_pattern_start", original_pattern_index=index, **(shape or {}))
        pat = _pattern_terms(ctx, store, *triple, scope)
        pat["_trace_original_index"] = index
        if var_preds:
            pending = {v: var_preds[v] for v in pat["varpos"] if v in var_preds}
            if pending:
                pat["deferred_var_preds"] = pending
        patterns.append(pat)
        _trace_event(
            "bgp_pattern_complete",
            original_pattern_index=index,
            cardinality_source="unmatched",
            native_call_count=0,
            matched_rows=None,
            native_match_ns=0,
            restriction_ns=0,
            relation_extend_ns=0,
            other_ns=0,
            elapsed_ns=0,
            timing_reconciled=True,
            **(shape or {}),
        )

    then = time.perf_counter_ns() if traced else 0
    stats = {}
    rel = _join_patterns(store, patterns, stats)
    total_join_ns = time.perf_counter_ns() - then if then else 0
    native_ns = stats["native_ns"]
    restriction_ns = stats["restriction_ns"]
    join_ns = max(0, total_join_ns - native_ns - restriction_ns)
    elapsed = time.perf_counter_ns() - started if started else 0
    known = native_ns + restriction_ns + join_ns
    other_ns = max(0, elapsed - known)
    _trace_event(
        "bgp_complete",
        pattern_count=len(patterns),
        output_rows=_relation_rows(rel),
        output_columns=len(rel.schema),
        native_call_count=len(patterns),
        native_initial_match_count=stats["initial_calls"],
        native_probe_call_count=stats["probe_calls"],
        native_match_ns=native_ns,
        restriction_ns=restriction_ns,
        join_ns=join_ns,
        other_ns=other_ns,
        elapsed_ns=known + other_ns,
        timing_reconciled=True,
    )
    return rel


# Incremental probes use the established fanout rule. count_quads supplies a
# cardinality estimate without materializing the complete predicate relation.


def _pattern_static_rank(pat):
    """Rank a resolved pattern without a native cardinality call."""
    n3 = pat["n3"]
    repeated = sum(len(pos) - 1 for pos in pat["varpos"].values())
    return (
        -sum(value is not None for value in n3),
        -int(n3[1] is not None),
        -int(n3[0] is not None),
        -int(n3[2] is not None),
        -repeated,
        pat.get("_trace_original_index", 0),
    )


def _match_resolved_pattern(store, pat, memo):
    """Attach native columns to a resolved pattern and preserve match memoization."""
    then = time.perf_counter_ns() if _TRACE.get() is not None else 0
    if pat["unsatisfiable"]:
        pat["cols"], pat["nrows"] = None, 0
        return 0, 0
    key = tuple(pat["n3"])
    cols = memo.get(key)
    calls = 0
    if cols is None:
        cols = store._store().match_codes(*pat["n3"])
        if cols is None:
            raise NotImplementedError
        memo[key] = cols
        calls = 1
    pat["cols"], pat["nrows"] = cols, len(cols[0])
    graph_vars = [var for var, positions in pat["varpos"].items() if 3 in positions]
    if graph_vars:
        _exclude_default_graph(store, pat, graph_vars[0])
    return calls, time.perf_counter_ns() - then if then else 0


def _join_incremental_bgp(store, patterns):
    """Match one selective seed, then probe connected unresolved patterns."""
    memo = {}
    stats = {"initial_calls": 0, "probe_calls": 0, "native_ns": 0, "restriction_ns": 0}
    remaining = list(patterns)
    seed = min(remaining, key=_pattern_static_rank)
    remaining.remove(seed)
    calls, native_ns = _match_resolved_pattern(store, seed, memo)
    stats["initial_calls"] += calls
    stats["native_ns"] += native_ns
    then = time.perf_counter_ns() if _TRACE.get() is not None else 0
    _materialize_eager_restrictions(store, seed)
    stats["restriction_ns"] += time.perf_counter_ns() - then if then else 0
    schema, cols, rows = _pattern_body(seed)
    order = [seed.get("_trace_original_index")]
    estimate_calls = 0
    _trace_event(
        "bgp_seed_complete",
        original_pattern_index=order[0],
        cardinality_source="matched",
        matched_rows=seed["nrows"],
        native_call_count=calls,
        native_match_ns=native_ns,
    )

    execution_index = 1
    while remaining and (len(cols[0]) if cols is not None else len(rows)):
        connected = [pat for pat in remaining if any(v in schema for v in pat["varpos"])]
        pat = min(connected or remaining, key=_pattern_static_rank)
        remaining.remove(pat)
        order.append(pat.get("_trace_original_index"))
        running = len(cols[0]) if cols is not None else len(rows)
        step_then = time.perf_counter_ns() if _TRACE.get() is not None else 0
        probe_calls = 0
        probe_ns = 0

        estimate_rows = None
        if connected and not pat["unsatisfiable"]:
            estimate_rows = store._store().count_quads(*pat["n3"])
            estimate_calls += 1
        if connected and estimate_rows is not None and running * _PROBE_FANOUT < estimate_rows:
            if cols is not None:
                rows, cols = _cols_to_rows(cols), None
            probe_calls = len(rows)
            then = time.perf_counter_ns() if _TRACE.get() is not None else 0
            schema, rows = _probe_join(store, schema, rows, pat)
            probe_ns = time.perf_counter_ns() - then if then else 0
            stats["probe_calls"] += probe_calls
            stats["native_ns"] += probe_ns
            strategy = "probe"
            source = "probed"
            match_rows = estimate_rows
            restricted_rows = pat.pop("_trace_probe_restricted_rows", len(rows))
        else:
            calls, native_ns = _match_resolved_pattern(store, pat, memo)
            stats["initial_calls"] += calls
            stats["native_ns"] += native_ns
            then = time.perf_counter_ns() if _TRACE.get() is not None else 0
            _materialize_eager_restrictions(store, pat)
            stats["restriction_ns"] += time.perf_counter_ns() - then if then else 0
            match_rows = pat["nrows"]
            restricted_rows = match_rows
            schema_b, cols_b, rows_b = _pattern_body(pat)
            joined = (
                _join_columns(schema, cols, schema_b, cols_b)
                if cols is not None and cols_b is not None
                else None
            )
            if joined is not None:
                schema, cols = joined
                strategy = "hash_columns"
            else:
                if cols is not None:
                    rows, cols = _cols_to_rows(cols), None
                if rows_b is None:
                    rows_b = _cols_to_rows(cols_b)
                schema, rows = _join(schema, rows, schema_b, rows_b)
                strategy = "hash_rows"
            source = "matched"

        output_rows = len(cols[0]) if cols is not None else len(rows)
        _trace_event(
            "bgp_join_step_complete",
            execution_index=execution_index,
            original_pattern_index=pat.get("_trace_original_index"),
            strategy=strategy,
            cardinality_source=source,
            input_rows=running,
            pattern_match_rows=match_rows,
            pattern_restricted_rows=restricted_rows,
            native_initial_match_count=int(source == "matched"),
            native_probe_call_count=probe_calls,
            native_probe_ns=probe_ns,
            output_rows=output_rows,
            output_columns=len(schema),
            shared_variable_count=sum(v in schema for v in pat["varpos"]),
            elapsed_ns=time.perf_counter_ns() - step_then if step_then else 0,
        )
        execution_index += 1

    for pat in remaining:
        schema += tuple(v for v in pat["varpos"] if v not in schema)
    _trace_event(
        "bgp_plan_complete",
        pattern_count=len(patterns),
        execution_order=order + [pat.get("_trace_original_index") for pat in remaining],
        seed_pattern_index=seed.get("_trace_original_index"),
        seed_selection="static_bound_positions",
        estimated_cardinalities=True,
        estimate_calls=estimate_calls,
        native_initial_match_count=stats["initial_calls"],
        native_probe_call_count=stats["probe_calls"],
        elapsed_ns=0,
    )
    if cols is not None:
        return Relation(schema, cols, None, len(cols[0])), stats
    return Relation.from_rows(schema, rows), stats


def _pattern_terms(ctx, store, s, p, o, scope) -> dict:
    """Resolve one triple pattern against the context, without matching.

    Returns ``{"n3", "varpos", "unsatisfiable"}``: the positions as
    N-Triples strings (``None`` where variable), a map from each
    variable-like term to the position(s) it occupies, and whether the
    pattern can match at all.

    ``n3`` is a *quad* pattern: its fourth position comes from ``scope`` —
    the graph the context has made active, the graph a ``GRAPH <iri>`` names,
    or the wildcard when a ``GRAPH ?g`` binds the graph instead. In that last
    case the graph variable takes position 3 in ``varpos``, so it is an
    ordinary variable of the relation, bound from the match's fourth column.
    """
    for term in (s, p, o):
        # RDF-star quoted triples (or anything else exotic) in a pattern
        # position: not supported here, use the default evaluator.
        if not isinstance(term, (*_VAR_LIKE, URIRef, Literal)):
            raise NotImplementedError
    rs, rp, ro = ctx[s], ctx[p], ctx[o]

    # rdflib joins can propagate a literal into subject or predicate
    # position; that pattern is unsatisfiable, not an error.
    unsatisfiable = (rs is not None and not isinstance(rs, (URIRef, BNode))) or (
        rp is not None and not isinstance(rp, URIRef)
    )

    varpos: dict = {}
    for idx, (term, value) in enumerate(zip((s, p, o), (rs, rp, ro), strict=True)):
        if value is None:
            varpos.setdefault(term, []).append(idx)

    if scope.var is not None:
        varpos.setdefault(scope.var, []).append(3)

    n3 = [store._node_to_n3(v) for v in (rs, rp, ro)]
    n3.append(scope.n3)
    return {"n3": n3, "varpos": varpos, "unsatisfiable": unsatisfiable}


def _match_pattern(ctx, store, s, p, o, scope, matches=None) -> dict:
    """One native match for one triple pattern; materialization is deferred.

    Adds ``"cols"`` (the raw code columns) and ``"nrows"`` to the pattern.

    ``matches`` memoizes the native call across the patterns of one BGP, keyed
    by the resolved quad. Only the columns are shared, and they are read-only
    views; ``varpos`` and the ``keep`` sets built from it stay per pattern,
    since two triples matching the same rows still bind different variables.
    """
    pat = _pattern_terms(ctx, store, s, p, o, scope)
    if pat["unsatisfiable"]:
        pat["cols"], pat["nrows"] = None, 0
        return pat
    key = tuple(pat["n3"])
    cols = matches.get(key) if matches is not None else None
    if cols is None:
        cols = store._store().match_codes(*pat["n3"])
        if cols is None:
            raise NotImplementedError
        if matches is not None:
            matches[key] = cols
    pat["cols"], pat["nrows"] = cols, len(cols[0])
    if scope.var is not None:
        _exclude_default_graph(store, pat, scope.var)
    return pat


def _exclude_default_graph(store, pat, var) -> None:
    """Drop the default graph's rows from a pattern a ``GRAPH ?g`` scopes.

    A ``GRAPH`` clause ranges over the *named* graphs, and the native match
    has no "any named graph" pattern, so the wildcard match is restricted
    afterwards — as a ``keep`` set over the graph column's distinct codes,
    which every row path already applies. A file with no default-graph rows
    has no such code, and needs no restriction at all.
    """
    default = store._default_graph_code()
    if default is None or pat["nrows"] == 0:
        return
    allowed = set(memoryview(pat["cols"][3]).cast("I"))
    if default not in allowed:
        return
    allowed.discard(default)
    keep = pat.setdefault("keep", {})
    keep[var] = (keep[var] & allowed) if var in keep else allowed


def _restrict_pattern(store, pat, var_preds) -> None:
    """Turn per-variable conjuncts into allowed-code restrictions."""
    if pat["nrows"] == 0:
        return
    traced = _TRACE.get() is not None
    keep = dict(pat.get("keep") or {})
    trace_records = []
    for var, positions in pat["varpos"].items():
        conjuncts = var_preds.get(var)
        if conjuncts:
            started = time.perf_counter_ns() if traced else 0
            values = set(memoryview(pat["cols"][positions[0]]).cast("I"))
            if var in keep:
                values &= keep[var]
            distinct_binding_count = len(values)
            allowed = filters.evaluate_column(store, conjuncts, values)
            keep[var] = allowed
            if traced:
                trace_records.append(
                    {
                        "variable": str(var),
                        "predicate_count": len(conjuncts),
                        "fast_predicate_count": sum(c.fast is not None for c in conjuncts),
                        "kind_only_predicate_count": sum(c.kind_only for c in conjuncts),
                        "generic_only_predicate_count": sum(c.fast is None for c in conjuncts),
                        "distinct_binding_count": distinct_binding_count,
                        "allowed_binding_count": len(allowed),
                        "elapsed_ns": time.perf_counter_ns() - started,
                    }
                )
    if keep:
        pat["keep"] = keep
    if trace_records:
        pat["_trace_restrictions"] = trace_records


def _emit_restriction_trace(pat, output_rows: int) -> None:
    """Emit deferred restriction events after row compaction gives a row count."""
    for record in pat.pop("_trace_restrictions", ()):
        _trace_event(
            "bgp_restriction_complete",
            original_pattern_index=pat.get("_trace_original_index"),
            input_rows=pat["nrows"],
            output_rows=output_rows,
            **record,
        )


def _rel_from_pattern(pat) -> Relation:
    """A single matched pattern as a relation — columnar when it can be.

    The zero-copy views are kept as the body and a restriction becomes a
    streamed row predicate; nothing is copied into Python objects until rows
    are consumed. Repeated variables (``?x :p ?x``) and ground patterns go
    through ``_materialize`` instead.
    """
    varpos = pat["varpos"]
    if pat["nrows"] == 0 or not varpos or any(len(pos) > 1 for pos in varpos.values()):
        return Relation.from_rows(*_materialize(pat))
    schema = tuple(varpos)
    cols = tuple(memoryview(pat["cols"][pos[0]]).cast("I") for pos in varpos.values())
    preds = tuple(
        _member_predicate(schema.index(var), allowed)
        for var, allowed in (pat.get("keep") or {}).items()
    )
    return Relation(schema, cols, None, pat["nrows"], preds=preds)


def _member_predicate(index: int, allowed: set) -> Callable[[tuple], bool]:
    return lambda row: row[index] in allowed


def _materialize(pat):
    """A matched pattern as ``(schema, rows)`` of u32 code tuples.

    Same-variable repeats (e.g. ``?x :p ?x``) and the ``keep`` restriction
    become row filters; a repeated variable keeps its first position. An
    empty match still names its variables, so the operators above can pad or
    project by schema.
    """
    varpos = pat["varpos"]
    schema = tuple(varpos)
    if pat["nrows"] == 0:
        return schema, []
    if not schema:
        # Fully ground pattern: no bindings, one (empty) row per match, so a
        # non-matching ground pattern still eliminates all solutions.
        return (), [()] * pat["nrows"]
    if "rows" in pat:
        return schema, pat["rows"]

    needed = sorted({idx for pos in varpos.values() for idx in pos})
    views = {idx: memoryview(pat["cols"][idx]).cast("I").tolist() for idx in needed}
    eq_checks = [(pos[0], later) for pos in varpos.values() for later in pos[1:]]
    members = [(varpos[var][0], allowed) for var, allowed in (pat.get("keep") or {}).items()]
    if eq_checks or members:
        keep = [
            i
            for i in range(pat["nrows"])
            if all(views[a][i] == views[b][i] for a, b in eq_checks)
            and all(views[p][i] in allowed for p, allowed in members)
        ]
        views = {idx: [column[i] for i in keep] for idx, column in views.items()}
    return schema, list(zip(*(views[pos[0]] for pos in varpos.values()), strict=True))


def _pattern_body(pat) -> tuple:
    """The matched pattern as ``(schema, cols, rows)`` — columnar when it
    can be: single-position variables and no pending restriction
    (``_materialize`` turns repeats and ``keep`` sets into row filters)."""
    varpos = pat["varpos"]
    if (
        pat["nrows"]
        and varpos
        and "rows" not in pat
        and not pat.get("keep")
        and all(len(pos) == 1 for pos in varpos.values())
    ):
        cols = tuple(memoryview(pat["cols"][pos[0]]).cast("I") for pos in varpos.values())
        return tuple(varpos), cols, None
    schema, rows = _materialize(pat)
    return schema, None, rows


def _cols_to_rows(cols) -> list:
    """Columnar body to row tuples, in one zip."""
    return list(zip(*(col.tolist() for col in cols), strict=True))


def _join_columns(schema_a, cols_a, schema_b, cols_b):
    """Hash join of two columnar relations sharing exactly one variable,
    columns out — the rows exist only when a consumer materializes them.
    ``None`` when the shape is not its case (several or no shared
    variables); the row join handles those.
    """
    shared = [v for v in schema_b if v in schema_a]
    if len(shared) != 1:
        return None
    ia = schema_a.index(shared[0])
    ib = schema_b.index(shared[0])
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)
    table: dict = {}
    setdefault = table.setdefault
    for j, key in enumerate(cols_b[ib]):
        setdefault(key, []).append(j)
    a_idx = []
    b_idx = []
    get = table.get
    for i, key in enumerate(cols_a[ia]):
        hits = get(key)
        if hits:
            a_idx += [i] * len(hits)
            b_idx += hits
    out = [array("I", map(col.__getitem__, a_idx)) for col in cols_a]
    out += [array("I", map(cols_b[i].__getitem__, b_idx)) for i in keep_b]
    return schema, tuple(out)


def _materialize_eager_restrictions(store, pat) -> None:
    """Apply deferred predicates before a non-probe pattern path."""
    pending = pat.pop("deferred_var_preds", None)
    if pending:
        _restrict_pattern(store, pat, pending)
    if pat.get("keep") and pat["nrows"]:
        input_rows = pat["nrows"]
        _, rows = _materialize(pat)
        _emit_restriction_trace(pat, len(rows))
        pat["rows"], pat["nrows"] = rows, len(rows)
        pat["_trace_match_rows"] = input_rows


def _join_patterns(store, patterns, stats=None) -> Relation:
    """Join a BGP through the incremental planner.

    Keep this function as the observable BGP join boundary. Existing tests,
    diagnostics, and callers can continue to wrap it with the historical
    two-argument signature. _solve_bgp passes an optional dictionary to
    receive physical native-call and timing counters.
    """
    rel, measured = _join_incremental_bgp(store, patterns)
    if stats is not None:
        stats.update(measured)
    return rel


def _probe_join(store, schema, rows, pat, keep_unmatched=False, row_preds=()):
    """Join the relation against a pattern by re-matching it per binding.

    The adaptive half of the join strategy: when the running relation is far
    smaller than the pattern's match (``_PROBE_FANOUT``), materializing
    thousands of pattern rows to hash-join against a handful is the wrong
    trade. Substituting each relation row's codes into the pattern and
    re-matching natively keeps the work proportional to the small side —
    it is what lets an anchored star beat rdflib's own nested loop instead
    of losing to it. A ``keep`` restriction on the pattern's free variables
    is applied to the probed rows, as are ``row_preds`` over the extended
    row; with ``keep_unmatched`` a row without a continuation is kept,
    padded with None (a left join).
    """
    native = store._store()
    match = native.match_codes
    count = native.count_quads
    bound = [(schema.index(v), pos) for v, pos in pat["varpos"].items() if v in schema]
    free = {v: pos for v, pos in pat["varpos"].items() if v not in schema}
    eq_checks = [(pos[0], later) for pos in free.values() for later in pos[1:]]
    keep = pat.get("keep") or {}
    members = [(pos[0], keep[v]) for v, pos in free.items() if v in keep]
    out_positions = [pos[0] for pos in free.values()]
    needed = sorted({idx for pos in free.values() for idx in pos})

    # One GIL-released batch decode covers every code the probes will bind.
    n3_cache = _probe_spellings(store, rows, bound)

    pad = (None,) * len(free)
    out = []
    for row in rows:
        n3 = list(pat["n3"])
        satisfiable = True
        for row_idx, positions in bound:
            term = n3_cache[row[row_idx]]
            for idx in positions:
                # The dictionary stores canonical N-Triples forms: a literal
                # ('"') cannot occupy subject or predicate position or name a
                # graph, and only an IRI ('<') can be a predicate — such a
                # binding simply has no continuation.
                if (
                    (idx == 0 and term[:1] == '"')
                    or (idx == 1 and term[:1] != "<")
                    or (idx == 3 and term[:1] == '"')
                ):
                    satisfiable = False
                n3[idx] = term
        matched = False
        if not satisfiable:
            pass
        elif not free:
            # Existence probe: count from the row selection, materialize
            # no columns.
            if count(*n3) and all(pred(row) for pred in row_preds):
                out.append(row)
                matched = True
        else:
            cols = match(*n3)
            if cols is None:
                raise NotImplementedError
            views = {idx: memoryview(cols[idx]).cast("I").tolist() for idx in needed}
            for i in range(len(views[needed[0]])):
                if all(views[a][i] == views[b][i] for a, b in eq_checks) and all(
                    views[p][i] in allowed for p, allowed in members
                ):
                    extended = row + tuple(views[idx][i] for idx in out_positions)
                    if all(pred(extended) for pred in row_preds):
                        out.append(extended)
                        matched = True
        if keep_unmatched and not matched:
            out.append(row + pad)
    combined_schema = schema + tuple(free.keys())
    pending = pat.pop("deferred_var_preds", None)
    if pending and out:
        restriction_input_rows = len(out)
        for var, conjuncts in pending.items():
            position = combined_schema.index(var)
            values = {row[position] for row in out}
            started = time.perf_counter_ns() if _TRACE.get() is not None else 0
            allowed = filters.evaluate_column(store, conjuncts, values)
            elapsed_ns = time.perf_counter_ns() - started if started else 0
            out = [row for row in out if row[position] in allowed]
            _trace_event(
                "bgp_restriction_complete",
                original_pattern_index=pat.get("_trace_original_index"),
                variable=str(var),
                mode="probe",
                input_rows=restriction_input_rows,
                output_rows=len(out),
                predicate_count=len(conjuncts),
                fast_predicate_count=sum(c.fast is not None for c in conjuncts),
                kind_only_predicate_count=sum(c.kind_only for c in conjuncts),
                generic_only_predicate_count=sum(c.fast is None for c in conjuncts),
                distinct_binding_count=len(values),
                allowed_binding_count=len(allowed),
                elapsed_ns=elapsed_ns,
            )
            restriction_input_rows = len(out)
        pat["_trace_probe_restricted_rows"] = len(out)
    return combined_schema, out


def _probe_spellings(store, rows, bound) -> dict:
    """The N-Triples spelling of every code the probes substitute: one
    batch decode for dictionary codes, the term's own spelling for a
    constant outside the dictionary (which then matches nothing)."""
    distinct = {row[row_idx] for row in rows for row_idx, _ in bound}
    codes = [code for code in distinct if code >= 0]
    n3_cache: dict[int, str] = {code: store._foreign[code].n3() for code in distinct if code < 0}
    for code, term in zip(codes, store._dict.decode_many(codes), strict=True):
        if term is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        n3_cache[code] = term
    return n3_cache


def _join(schema_a, rows_a, schema_b, rows_b):
    """Hash join of two code-space relations on their shared variables.

    Keys come from :func:`~operator.itemgetter`: a bare ``int`` code for one
    shared variable (the common case — no per-row key tuple), a tuple for
    several.
    """
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)

    if not shared:
        return schema, [ra + rb for ra in rows_a for rb in rows_b]

    key_a = itemgetter(*(schema_a.index(v) for v in shared))
    key_b = itemgetter(*(schema_b.index(v) for v in shared))
    table: dict = {}
    setdefault = table.setdefault
    if not keep_b:
        for rb in rows_b:
            setdefault(key_b(rb), []).append(())
    elif len(keep_b) == 1:
        tail_of = itemgetter(keep_b[0])
        for rb in rows_b:
            setdefault(key_b(rb), []).append((tail_of(rb),))
    else:
        tail_of = itemgetter(*keep_b)
        for rb in rows_b:
            setdefault(key_b(rb), []).append(tail_of(rb))
    out: list = []
    extend = out.extend
    get = table.get
    for ra in rows_a:
        tails = get(key_a(ra))
        if tails:
            extend(ra + tail for tail in tails)
    return schema, out


def _code_rows(rel: Relation) -> Iterator[tuple]:
    """The relation's rows as code tuples, lazily for a columnar body."""
    rows = iter(rel.rows) if rel.rows is not None else _column_rows(rel.cols, rel.nrows)
    if rel.preds:
        preds = rel.preds
        rows = (row for row in rows if all(pred(row) for pred in preds))
    return rows


def _column_rows(cols, nrows) -> Iterator[tuple]:
    """Zip column views into row tuples a slice at a time, so a consumer that
    stops early never converts the whole match into Python objects."""
    i, size = 0, _CHUNK_START
    while i < nrows:
        j = min(i + size, nrows)
        yield from zip(*(view[i:j].tolist() for view in cols), strict=True)
        i, size = j, min(size * 4, _CHUNK_MAX)


def _yield_solutions(ctx, store, rel: Relation, project=None, start=0, stop=None):
    """Decode a relation's rows into rdflib solutions, lazily.

    Rows are decoded in growing chunks, one ``decode_many`` per chunk for the
    codes the store's cache does not hold yet. Without ``project`` a row
    carries every schema variable plus the context's own bindings (what
    rdflib's BGP rows contain); with it only the projected variables, the
    way ``Project`` would have narrowed them.
    """
    return _yield_rows(ctx, store, rel.schema, _code_rows(rel), rel.foreign, project, start, stop)


def _pushed_graph(ctx) -> bool:
    """Whether rdflib has made a graph other than the dataset itself active.

    That is what a ``Graph`` node rdflib evaluates *itself* does — and when it
    yields, it writes ``solution.ctx.graph`` back to the graph it replaced.
    A solution's context is therefore not read-only to the caller, so the rows
    below cannot all share one: the write would reach the rows still to come,
    and any pattern rdflib re-evaluates from them (an OPTIONAL's right side, an
    EXISTS body) would run against the wrong graph.
    """
    dataset = getattr(ctx, "_dataset", None)
    return dataset is not None and ctx.graph is not dataset


def _yield_rows(ctx, store, schema, rows: Iterator[tuple], foreign, project, start, stop):
    """Decode code rows over ``schema`` into solutions (see
    ``_yield_solutions``); ``start``/``stop`` slice the rows first."""
    outer = dict(ctx.bindings.items())
    # Normally every solution carries the caller's own context — no per-row
    # scope, unlike rdflib's `evalBGP`. Under a graph rdflib pushed, each row
    # needs its own (see `_pushed_graph`), which is what `evalBGP` does too.
    own_context = _pushed_graph(ctx)
    if project is None:
        keys, idx, base = schema, list(range(len(schema))), outer
    else:
        keys = tuple(v for v in project if v in schema)
        idx = [schema.index(v) for v in keys]
        base = {v: outer[v] for v in project if v not in schema and v in outer}
    rows = islice(rows, start, stop)
    cached = store._decode_cache
    size = _CHUNK_START
    traced = _TRACE.get() is not None
    started = time.perf_counter_ns() if traced else 0
    yielded = chunks = submitted = 0
    try:
        while True:
            chunk = list(islice(rows, size))
            if not chunk:
                return
            codes = [c for row in chunk for c in (row[i] for i in idx) if c is not None and c >= 0]
            if traced:
                chunks += 1
                submitted += len(codes)
            store._prime_decode_cache(codes)
            for row in chunk:
                solution = dict(base)
                for v, i in zip(keys, idx, strict=True):
                    c = row[i]
                    if c is not None:
                        solution[v] = cached[c] if c >= 0 else foreign[c]
                yielded += 1
                yield FrozenBindings(ctx.push() if own_context else ctx, solution)
            size = min(size * 4, _CHUNK_MAX)
    finally:
        if traced:
            _trace_event(
                "yield_rows_complete",
                offset=start,
                stop=stop,
                rows_yielded=yielded,
                chunks=chunks,
                projected_columns=len(idx),
                codes_submitted_for_decode=submitted,
                elapsed_ns=time.perf_counter_ns() - started,
            )
