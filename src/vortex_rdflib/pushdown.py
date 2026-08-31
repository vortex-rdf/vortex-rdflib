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
  counts drive the join order), the join runs as hash joins over ``int``
  tuples, or re-probes the store per binding when the running relation is
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
import os
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import islice
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
    # Everything that can raise NotImplementedError — shape checks and native
    # calls — happens inside this call; only decoding is deferred.
    return globals()[handler](ctx, store, part)


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

    ``schema`` names the variable-like terms. The body is either zero-copy
    ``u32`` column views (``cols``, one per schema entry: a single matched
    pattern, never copied into Python objects) or materialized ``rows`` of
    ``int`` codes (joins). ``None`` in a row is an unbound variable and
    ``nullable`` lists the variables that may hold it; ``foreign`` maps
    negative codes to terms outside the dictionary; ``preds`` are row
    predicates still to be applied while a columnar body streams (``nrows``
    counts the rows before them).
    """

    schema: tuple
    cols: tuple[memoryview, ...] | None
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
    head = _plan_head(part)
    rel = _solve_block(ctx, store, head.block)
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
    rows = _rows_of(rel)
    keys = []
    for var, descending in order:
        if var not in rel.schema:
            continue
        index = rel.schema.index(var)
        ranks = _rank_codes(store, rel.foreign, {row[index] for row in rows})
        keys.append((index, ranks, -1 if descending else 1))
    if not keys:
        return rows

    def sort_key(row):
        return tuple(sign * ranks[row[i]] for i, ranks, sign in keys)

    if stop is not None and stop < len(rows):
        return heapq.nsmallest(stop, rows, key=sort_key)
    return sorted(rows, key=sort_key)


def _rank_codes(store, foreign: dict, codes) -> dict:
    """Ranks reproducing rdflib's ORDER BY comparison (``_val``): unbound
    first, then blank nodes, IRIs and literals. Blank nodes and IRIs order
    as their spellings, so their codes already are their ranks; literals are
    decoded and sorted with rdflib's own comparator, adjacent terms that
    compare equal sharing a rank so the stable sort keeps their input
    order."""
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
    if name == "BGP":
        return _solve_bgp(ctx, store, node.triples, scope, var_preds)
    if name == "Filter":
        return _solve_filter(ctx, store, node, env, var_preds, scope)
    if name == "Join":
        return _solve_join(ctx, store, node, env, var_preds, scope)
    if name == "LeftJoin":
        return _solve_left_join(ctx, store, node, env, var_preds, scope)
    if name == "Minus":
        return _solve_minus(ctx, store, node, env, var_preds, scope)
    if name == "ToMultiSet":
        return _solve_values(ctx, store, node, scope)
    if name == "Graph":
        return _solve_block(ctx, store, node.p, env, var_preds, _graph_scope(ctx, node))
    raise NotImplementedError


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
        pat = _match_pattern(ctx, store, *p2.triples[0], scope=scope)
        shares = any(v in left.schema for v in pat["varpos"])
        if shares and len(left_rows) * _PROBE_FANOUT < pat["nrows"]:
            _shared_key_ok(
                left, Relation((), None, [], 0), [v for v in pat["varpos"] if v in left.schema]
            )
            schema = left.schema + tuple(v for v in pat["varpos"] if v not in left.schema)
            preds = condition_preds(schema)
            if preds is None:
                rows = [row + (None,) * len(extra) for row in left_rows]
                return Relation(left.schema + extra, None, rows, len(rows), nullable, left.foreign)
            schema, rows = _probe_join(
                store, left.schema, left_rows, pat, keep_unmatched=True, row_preds=preds
            )
            return Relation(schema, None, rows, len(rows), nullable, left.foreign)
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


def _solve_bgp(ctx, store, triples, scope, var_preds=None) -> Relation:
    """Evaluate a basic graph pattern into a code-space relation.

    ``var_preds`` maps a variable to the filter conjuncts over it alone; they
    restrict every pattern scan binding the variable before the join, so
    filter selectivity drives the join order and the probe decision.
    """
    if not triples:
        return Relation.from_rows((), [()])
    # Variables are `None` in a resolved pattern, so triples that differ only
    # in their variable names — the two hops of `?s <p> ?m . ?m <p> ?o`, every
    # leg of a self-join — are one and the same native match.
    matches: dict[tuple, tuple] = {}
    patterns = [_match_pattern(ctx, store, s, p, o, scope, matches) for s, p, o in triples]
    if var_preds:
        for pat in patterns:
            _restrict_pattern(store, pat, var_preds)
    if len(patterns) == 1:
        return _rel_from_pattern(patterns[0])
    return _join_patterns(store, patterns)


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
    """Turn the per-variable filter conjuncts into a row restriction
    (``keep``: variable -> allowed codes), evaluated once over the distinct
    codes of the variable's column.

    A restriction already on the pattern — the default graph excluded from a
    graph variable — narrows the codes the conjuncts are evaluated over and is
    intersected with their verdict, so the two compose, and the default
    graph's empty name (which is no RDF term) never reaches a predicate.
    """
    if pat["nrows"] == 0:
        return
    keep = dict(pat.get("keep") or {})
    for var, positions in pat["varpos"].items():
        conjuncts = var_preds.get(var)
        if conjuncts:
            values = set(memoryview(pat["cols"][positions[0]]).cast("I"))
            if var in keep:
                values &= keep[var]
            keep[var] = filters.evaluate_column(store, conjuncts, values)
    if keep:
        pat["keep"] = keep


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


def _join_patterns(store, patterns) -> Relation:
    """Join matched patterns: smallest first, then greedily prefer patterns
    sharing a variable with the schema so far (avoids cross products)."""
    # A restricted pattern is materialized up front so its real size, not
    # the match's, drives the order.
    for pat in patterns:
        if pat.get("keep") and pat["nrows"]:
            _, rows = _materialize(pat)
            pat["rows"], pat["nrows"] = rows, len(rows)
    patterns.sort(key=lambda pat: pat["nrows"])
    schema, rows = _materialize(patterns[0])
    remaining = patterns[1:]
    while remaining and rows:
        pick = next(
            (i for i, pat in enumerate(remaining) if any(v in pat["varpos"] for v in schema)),
            0,
        )
        pat = remaining.pop(pick)
        shares = any(v in pat["varpos"] for v in schema)
        if shares and len(rows) * _PROBE_FANOUT < pat["nrows"]:
            schema, rows = _probe_join(store, schema, rows, pat)
        else:
            schema, rows = _join(schema, rows, *_materialize(pat))
    # An empty result still names every variable of the pattern.
    for pat in remaining:
        schema += tuple(v for v in pat["varpos"] if v not in schema)
    return Relation.from_rows(schema, rows)


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
    return schema + tuple(free.keys()), out


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
    """Hash join of two code-space relations on their shared variables."""
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)

    if not shared:
        return schema, [ra + rb for ra in rows_a for rb in rows_b]

    ia = [schema_a.index(v) for v in shared]
    ib = [schema_b.index(v) for v in shared]
    table: dict = {}
    for rb in rows_b:
        key = tuple(rb[i] for i in ib)
        table.setdefault(key, []).append(tuple(rb[i] for i in keep_b))
    out = []
    for ra in rows_a:
        tails = table.get(tuple(ra[i] for i in ia))
        if tails:
            out.extend(ra + tail for tail in tails)
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
    while True:
        chunk = list(islice(rows, size))
        if not chunk:
            return
        store._prime_decode_cache(
            [c for row in chunk for c in (row[i] for i in idx) if c is not None and c >= 0]
        )
        for row in chunk:
            solution = dict(base)
            for v, i in zip(keys, idx, strict=True):
                c = row[i]
                if c is not None:
                    solution[v] = cached[c] if c >= 0 else foreign[c]
            yield FrozenBindings(ctx.push() if own_context else ctx, solution)
        size = min(size * 4, _CHUNK_MAX)
