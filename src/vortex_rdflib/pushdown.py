"""SPARQL pushdown for :class:`~vortex_rdflib.store.VortexStore`.

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
- a ``Filter`` over a pattern is split into conjuncts (:mod:`.filters`):
  those over one variable are evaluated once per distinct code of the
  variable and applied to the pattern scans *before* the join, the others
  once per distinct code tuple after it — a whitelist of expression shapes
  runs as predicates over the stored spellings, anything else (and every
  value outside the fast path's exact domain) is answered by rdflib's own
  evaluator, per distinct value instead of per row;
- ``Project`` and ``Slice`` (LIMIT/OFFSET) heads are applied to the
  code-space relation, and an ``AskQuery`` over one pattern is answered from
  the row selection alone, so only the projected variables of the rows that
  are actually consumed are ever decoded — a ``LIMIT 10`` decodes a few
  dozen codes, an ASK none;
- solutions are decoded lazily in growing chunks, each distinct code once
  through the store's decode cache, and built directly as
  ``FrozenBindings``, the row shape every rdflib operator above expects.

rdflib only catches ``NotImplementedError`` at hook-call time, so every
capability check and every native call happens before a generator is handed
back; the generators only decode. The hook applies when the active graph's
store is a VortexStore with the code path available (Dictionary layout,
resident dictionary); behaviour is identical to rdflib's default evaluation,
only faster, and the equivalence tests compare both paths on every query
shape.

Registration happens automatically when the first ``VortexStore`` is
constructed. ``VORTEX_RDF_DISABLE_PUSHDOWN=1`` keeps rdflib's evaluator
entirely (the equivalence tests' oracle); ``VORTEX_RDF_PUSHDOWN_OPS`` narrows
the intercepted algebra nodes to a comma-separated list (``bgp`` = basic
graph patterns only) and ``VORTEX_RDF_FILTER_FAST=0`` routes every FILTER
value through rdflib's evaluator, for bisecting and A/B measurements.
"""

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import islice

from rdflib.plugins.sparql import CUSTOM_EVALS
from rdflib.plugins.sparql.sparql import FrozenBindings
from rdflib.term import BNode, Literal, URIRef, Variable

from . import filters

_EVAL_KEY = "vortex_rdflib_bgp"

# Query bnodes act as variables, exactly as rdflib's evalBGP treats them.
_VAR_LIKE = (Variable, BNode)

# Algebra nodes this module answers, by handler name. Handlers are resolved
# from the module namespace at call time so tests can spy on them.
_HANDLER_NAMES = {
    "BGP": "_eval_block_node",
    "Filter": "_eval_block_node",
    "Project": "_eval_head",
    "Slice": "_eval_head",
    "AskQuery": "_eval_ask",
}
_ALL_OPS = frozenset(_HANDLER_NAMES)
_ENABLED_OPS = _ALL_OPS
# Nodes a "block" is made of: a subtree solved into one code-space relation.
_BLOCK_NODES = frozenset({"BGP", "Filter"})

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

    from .store import VortexStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexStore) or store._dict is None:
        raise NotImplementedError
    # Everything that can raise NotImplementedError — shape checks and native
    # calls — happens inside this call; only decoding is deferred.
    return globals()[handler](ctx, store, part)


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


def _plan_head(part) -> _Head:
    """Recognize a ``Slice? -> Project -> block`` head, eagerly.

    Anything else is rdflib's: its evaluator runs the node and re-enters the
    hook for the nodes below.
    """
    node, start, stop = part, 0, None
    if node.name == "Slice":
        start = int(node.start or 0)
        # The key is absent without LIMIT; CompValue then answers None.
        length = node.length
        stop = None if length is None else start + int(length)
        node = node.p
    if node.name != "Project":
        raise NotImplementedError
    pv = node.PV
    pv = (pv,) if isinstance(pv, Variable) else tuple(pv)
    _check_block(node.p)
    return _Head(node.p, pv, start, stop)


def _check_block(node) -> None:
    """Eagerly reject anything the block solver does not handle."""
    name = getattr(node, "name", None)
    if name == "BGP":
        return
    if name == "Filter":
        _check_block(node.p)
        return
    raise NotImplementedError


def _block_vars(ctx, node) -> list:
    """The variables a block binds: its variable-like terms the context has
    not bound, in first-seen order (static, no matching)."""
    if node.name == "Filter":
        return _block_vars(ctx, node.p)
    out: list = []
    for triple in node.triples:
        for term in triple:
            if isinstance(term, _VAR_LIKE) and term not in out and ctx[term] is None:
                out.append(term)
    return out


def _eval_head(ctx, store, part):
    head = _plan_head(part)
    rel = _solve_block(ctx, store, head.block)
    return _yield_solutions(ctx, store, rel, head.pv, head.start, head.stop)


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
    if block.name == "BGP" and len(block.triples) == 1:
        pat = _pattern_terms(ctx, store, *block.triples[0])
        if pat["unsatisfiable"]:
            return False
        if all(len(pos) == 1 for pos in pat["varpos"].values()):
            # Existence needs no rows: count from the row selection.
            return store._store().count_quads(*pat["n3"]) > 0
    rel = _solve_block(ctx, store, block)
    return next(_code_rows(rel), None) is not None


def _solve_block(ctx, store, node) -> Relation:
    if node.name == "BGP":
        return _solve_bgp(ctx, store, node.triples)
    if node.name == "Filter":
        return _solve_filter(ctx, store, node)
    raise NotImplementedError


def _solve_filter(ctx, store, node) -> Relation:
    """A ``Filter`` over a block: constant conjuncts decide the whole block
    before anything is matched, single-variable conjuncts restrict the
    pattern scans, the rest filters the joined rows."""
    inner = node.p
    if inner.name != "BGP":
        raise NotImplementedError
    block_vars = _block_vars(ctx, inner)
    plan = filters.analyze_filter(node, block_vars, ctx)
    for conjunct in plan.constant:
        if not filters.evaluate_constant(conjunct):
            return Relation.from_rows(tuple(block_vars), [])
    rel = _solve_bgp(ctx, store, inner.triples, plan.per_var or None)
    if plan.tuples:
        preds = tuple(
            filters.tuple_predicate(store, conjunct, [rel.schema.index(v) for v in conjunct.vars])
            for conjunct in plan.tuples
        )
        rel = _filter_relation(rel, preds)
    return rel


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


def _solve_bgp(ctx, store, triples, var_preds=None) -> Relation:
    """Evaluate a basic graph pattern into a code-space relation.

    ``var_preds`` maps a variable to the filter conjuncts over it alone; they
    restrict every pattern scan binding the variable before the join, so
    filter selectivity drives the join order and the probe decision.
    """
    if not triples:
        return Relation.from_rows((), [()])
    patterns = [_match_pattern(ctx, store, s, p, o) for s, p, o in triples]
    if var_preds:
        for pat in patterns:
            _restrict_pattern(store, pat, var_preds)
    if len(patterns) == 1:
        return _rel_from_pattern(patterns[0])
    return _join_patterns(store, patterns)


def _pattern_terms(ctx, store, s, p, o) -> dict:
    """Resolve one triple pattern against the context, without matching.

    Returns ``{"n3", "varpos", "unsatisfiable"}``: the positions as
    N-Triples strings (``None`` where variable), a map from each
    variable-like term to the position(s) it occupies, and whether the
    pattern can match at all.
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

    n3 = [store._node_to_n3(v) for v in (rs, rp, ro)]
    return {"n3": n3, "varpos": varpos, "unsatisfiable": unsatisfiable}


def _match_pattern(ctx, store, s, p, o) -> dict:
    """One native match for one triple pattern; materialization is deferred.

    Adds ``"cols"`` (the raw code columns) and ``"nrows"`` to the pattern.
    """
    pat = _pattern_terms(ctx, store, s, p, o)
    if pat["unsatisfiable"]:
        pat["cols"], pat["nrows"] = None, 0
        return pat
    cols = store._store().match_codes(*pat["n3"])
    if cols is None:
        raise NotImplementedError
    pat["cols"], pat["nrows"] = cols, len(cols[0])
    return pat


def _restrict_pattern(store, pat, var_preds) -> None:
    """Turn the per-variable filter conjuncts into a row restriction
    (``keep``: variable -> allowed codes), evaluated once over the distinct
    codes of the variable's column."""
    if pat["nrows"] == 0:
        return
    keep = {}
    for var, positions in pat["varpos"].items():
        conjuncts = var_preds.get(var)
        if conjuncts:
            column = memoryview(pat["cols"][positions[0]]).cast("I")
            keep[var] = filters.evaluate_column(store, conjuncts, set(column))
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


def _probe_join(store, schema, rows, pat):
    """Join the relation against a pattern by re-matching it per binding.

    The adaptive half of the join strategy: when the running relation is far
    smaller than the pattern's match (``_PROBE_FANOUT``), materializing
    thousands of pattern rows to hash-join against a handful is the wrong
    trade. Substituting each relation row's codes into the pattern and
    re-matching natively keeps the work proportional to the small side —
    it is what lets an anchored star beat rdflib's own nested loop instead
    of losing to it. A ``keep`` restriction on the pattern's free variables
    is applied to the probed rows.
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
    distinct = {row[row_idx] for row in rows for row_idx, _ in bound}
    codes = list(distinct)
    n3_cache: dict[int, str] = {}
    for code, term in zip(codes, store._dict.decode_many(codes), strict=True):
        if term is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        n3_cache[code] = term

    out = []
    for row in rows:
        n3 = list(pat["n3"])
        satisfiable = True
        for row_idx, positions in bound:
            term = n3_cache[row[row_idx]]
            for idx in positions:
                # The dictionary stores canonical N-Triples forms: a literal
                # ('"') cannot occupy subject or predicate position, and only
                # an IRI ('<') can be a predicate — such a binding simply has
                # no continuation.
                if (idx == 0 and term[0] == '"') or (idx == 1 and term[0] != "<"):
                    satisfiable = False
                n3[idx] = term
        if not satisfiable:
            continue

        if not free:
            # Existence probe: count from the row selection, materialize
            # no columns.
            if count(*n3):
                out.append(row)
            continue
        cols = match(*n3)
        if cols is None:
            raise NotImplementedError
        views = {idx: memoryview(cols[idx]).cast("I").tolist() for idx in needed}
        for i in range(len(views[needed[0]])):
            if all(views[a][i] == views[b][i] for a, b in eq_checks) and all(
                views[p][i] in allowed for p, allowed in members
            ):
                out.append(row + tuple(views[idx][i] for idx in out_positions))
    return schema + tuple(free.keys()), out


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
    schema = rel.schema
    outer = dict(ctx.bindings.items())
    if project is None:
        keys, idx, base = schema, list(range(len(schema))), outer
    else:
        keys = tuple(v for v in project if v in schema)
        idx = [schema.index(v) for v in keys]
        base = {v: outer[v] for v in project if v not in schema and v in outer}
    rows = islice(_code_rows(rel), start, stop)
    cached = store._decode_cache
    foreign = rel.foreign
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
            yield FrozenBindings(ctx, solution)
        size = min(size * 4, _CHUNK_MAX)
