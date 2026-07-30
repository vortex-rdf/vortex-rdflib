"""SPARQL BGP pushdown for :class:`~vortex_rdflib.store.VortexStore`.

rdflib's default ``evalBGP`` is a nested-loop join that calls
``Store.triples()`` once per candidate binding, and every one of those calls
pays the native match floor. This module registers a ``CUSTOM_EVALS`` hook
that evaluates a whole basic graph pattern against a VortexStore in one
pass instead:

- every triple pattern is matched natively once up-front (a match is
  near-constant cost, so the actual row counts drive the join order);
- the join runs in code space — hash joins over ``int`` tuples, no term
  strings, no rdflib term construction for intermediate results. When the
  running relation is far smaller than the next pattern's match, that
  pattern is instead re-probed natively per binding (see ``_probe_join``),
  so an anchored star never materializes its unanchored legs;
- terms are decoded only for the final solutions, each distinct code once,
  through the store's decode cache.

The hook applies only when the active graph's store is a VortexStore with
the code path available (Dictionary layout, resident dictionary); anything
else — other stores, other algebra nodes, RDF-star patterns — raises
``NotImplementedError``, which makes rdflib fall through to its default
evaluator. Behavior is therefore identical to the default path, only faster.

Registration happens automatically when the first ``VortexStore`` is
constructed; set ``VORTEX_RDF_DISABLE_PUSHDOWN=1`` to keep the default
evaluator (e.g. for A/B benchmarking).
"""

import os

from rdflib.plugins.sparql import CUSTOM_EVALS
from rdflib.term import BNode, Literal, URIRef, Variable

_EVAL_KEY = "vortex_rdflib_bgp"

# Query bnodes act as variables, exactly as rdflib's evalBGP treats them.
_VAR_LIKE = (Variable, BNode)


def register_sparql_pushdown():
    """Install the BGP hook into rdflib's CUSTOM_EVALS (idempotent)."""
    if os.environ.get("VORTEX_RDF_DISABLE_PUSHDOWN") == "1":
        return
    CUSTOM_EVALS.setdefault(_EVAL_KEY, _eval_part)


def unregister_sparql_pushdown():
    CUSTOM_EVALS.pop(_EVAL_KEY, None)


def _eval_part(ctx, part):
    if part.name != "BGP":
        raise NotImplementedError

    from .store import VortexStore

    store = getattr(getattr(ctx, "graph", None), "store", None)
    if not isinstance(store, VortexStore) or store._dict is None:
        raise NotImplementedError

    # Solve eagerly: the CUSTOM_EVALS dispatcher only catches
    # NotImplementedError at call time, so nothing may fail lazily after a
    # generator is handed back.
    schema, rows = _solve_bgp(ctx, store, part.triples)
    return _yield_solutions(ctx, store, schema, rows)


# Probing beats hash-joining when the running relation is at least this many
# times smaller than the next pattern's match. Measured on the in-memory
# dictionary layout: one native match costs ~70 µs regardless of selectivity,
# while materializing a matched row into Python costs ~0.7 µs — so one probe
# is worth skipping the materialization of ~100 rows.
_PROBE_FANOUT = 100


def _solve_bgp(ctx, store, triples):
    """Evaluate the BGP in code space: ``(schema, rows)`` where ``schema`` is
    a tuple of variable-like terms and each row a tuple of u32 codes."""
    if not triples:
        return (), [()]

    patterns = []
    for s, p, o in triples:
        for term in (s, p, o):
            # RDF-star quoted triples (or anything else exotic) in a pattern
            # position: not supported here, use the default evaluator.
            if not isinstance(term, (*_VAR_LIKE, URIRef, Literal)):
                raise NotImplementedError
        patterns.append(_match_pattern(ctx, store, s, p, o))

    # Start from the smallest pattern result, then greedily prefer patterns
    # sharing a variable with the schema so far (avoids cross products).
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
    return schema, rows


def _match_pattern(ctx, store, s, p, o):
    """One native match for one triple pattern; materialization is deferred.

    Returns ``{"n3", "cols", "nrows", "varpos"}``: the pattern's positions as
    N3 strings (``None`` where variable), the raw code columns, the match's
    row count, and a map from each variable-like term to the position(s) it
    occupies.
    """
    rs, rp, ro = ctx[s], ctx[p], ctx[o]

    # rdflib joins can propagate a literal into subject or predicate
    # position; that pattern is unsatisfiable, not an error.
    unsatisfiable = (rs is not None and not isinstance(rs, (URIRef, BNode))) or (
        rp is not None and not isinstance(rp, URIRef)
    )

    varpos = {}
    for idx, (term, value) in enumerate(zip((s, p, o), (rs, rp, ro), strict=True)):
        if value is None:
            varpos.setdefault(term, []).append(idx)

    n3 = [store._node_to_n3(v) for v in (rs, rp, ro)]
    if unsatisfiable:
        return {"n3": n3, "cols": None, "nrows": 0, "varpos": varpos}

    cols = store._store().match_codes(*n3)
    if cols is None:
        raise NotImplementedError
    nrows = len(memoryview(cols[0]).cast("I"))
    return {"n3": n3, "cols": cols, "nrows": nrows, "varpos": varpos}


def _materialize(pat):
    """A matched pattern as ``(schema, rows)`` of u32 code tuples.

    Same-variable repeats (e.g. ``?x :p ?x``) become row filters; the
    surviving variable keeps its first position.
    """
    if pat["nrows"] == 0:
        return (), []
    varpos = pat["varpos"]
    views = [memoryview(c).cast("I").tolist() for c in pat["cols"][:3]]

    eq_checks = [(pos[0], later) for pos in varpos.values() for later in pos[1:]]
    if eq_checks:
        keep = [
            i for i in range(len(views[0])) if all(views[a][i] == views[b][i] for a, b in eq_checks)
        ]
        views = [[v[i] for i in keep] for v in views]

    schema = tuple(varpos.keys())
    if not schema:
        # Fully ground pattern: no bindings, one (empty) row per match, so a
        # non-matching ground pattern still eliminates all solutions.
        return (), [()] * len(views[0])
    return schema, list(zip(*(views[pos[0]] for pos in varpos.values()), strict=True))


def _probe_join(store, schema, rows, pat):
    """Join the relation against a pattern by re-matching it per binding.

    The adaptive half of the join strategy: when the running relation is far
    smaller than the pattern's match (``_PROBE_FANOUT``), materializing
    thousands of pattern rows to hash-join against a handful is the wrong
    trade. Substituting each relation row's codes into the pattern and
    re-matching natively keeps the work proportional to the small side —
    it is what lets an anchored star beat rdflib's own nested loop instead
    of losing to it.
    """
    match = store._store().match_codes
    decode = store._dict.decode
    bound = [(schema.index(v), pos) for v, pos in pat["varpos"].items() if v in schema]
    free = {v: pos for v, pos in pat["varpos"].items() if v not in schema}
    eq_checks = [(pos[0], later) for pos in free.values() for later in pos[1:]]
    out_positions = [pos[0] for pos in free.values()]
    needed = sorted({idx for pos in free.values() for idx in pos})
    n3_cache: dict[int, str] = {}

    out = []
    for row in rows:
        n3 = list(pat["n3"])
        satisfiable = True
        for row_idx, positions in bound:
            code = row[row_idx]
            term = n3_cache.get(code)
            if term is None:
                term = decode(code)
                if term is None:
                    raise ValueError(f"term code {code} is not in the store dictionary")
                n3_cache[code] = term
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

        cols = match(*n3)
        if cols is None:
            raise NotImplementedError
        if not free:
            if len(memoryview(cols[0]).cast("I")):
                out.append(row)
            continue
        views = {idx: memoryview(cols[idx]).cast("I").tolist() for idx in needed}
        for i in range(len(views[needed[0]])):
            if all(views[a][i] == views[b][i] for a, b in eq_checks):
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
    table = {}
    for rb in rows_b:
        key = tuple(rb[i] for i in ib)
        table.setdefault(key, []).append(tuple(rb[i] for i in keep_b))
    out = []
    for ra in rows_a:
        tails = table.get(tuple(ra[i] for i in ia))
        if tails:
            out.extend(ra + tail for tail in tails)
    return schema, out


def _yield_solutions(ctx, store, schema, rows):
    decode = store._decode_term
    for row in rows:
        c = ctx.push()
        for term, code in zip(schema, row, strict=True):
            c[term] = decode(code)
        yield c.solution()
