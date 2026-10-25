"""Blocks: a subtree of the algebra solved into one code-space relation
(:func:`_solve_block`) — a BGP, a FILTER over a block, a group join, an
OPTIONAL, a MINUS, a VALUES table, a GRAPH — and the block and ASK
handlers.
"""

from .. import filters
from ..cancel import check_cancelled
from ..settings import SETTINGS
from ..terms import canonical_spelling, unparseable_iri
from . import algebra, bgp, matching, probes, relation, solutions, tracing
from .matching import Pattern
from .relation import Relation
from .tracing import _TRACE


def _eval_block_node(ctx, store, part):
    algebra._check_block(part)
    return solutions._yield_solutions(ctx, store, _solve_block(ctx, store, part, stream=True))


def _eval_ask(ctx, store, part):
    project = part.p
    if getattr(project, "name", None) != "Project":
        raise NotImplementedError
    algebra._check_block(project.p)
    return {"type_": "ASK", "askAnswer": _block_nonempty(ctx, store, project.p)}


def _block_nonempty(ctx, store, block) -> bool:
    block, scope = algebra._peel_graph(ctx, block, algebra._active_scope(ctx, store))
    pat = _single_pattern(ctx, store, block, scope)
    if pat is not None:
        if pat.unsatisfiable:
            return False
        if all(len(pos) == 1 for pos in pat.varpos.values()):
            # Existence needs no rows: a count capped at one, which stops a
            # file-backed scan at its first match.
            narrowing = matching._narrowing(pat.nkeep, limit=1)
            check_cancelled()
            return tracing._native(store).count_quads(*pat.n3, **narrowing) > 0
    rel = _solve_block(ctx, store, block, scope=scope, limit=1)
    # One row is pulled, and the rows are closed after it: a streamed join
    # gathers one piece and releases its feed.
    rows = relation._code_rows(rel)
    try:
        return next(rows, None) is not None
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


def _single_pattern(ctx, store, block, scope) -> Pattern | None:
    """``block`` as one triple pattern the native layer answers whole: a
    ``BGP`` of one pattern, or a ``FILTER`` over one whose conjuncts all push
    into the native match as keeps. The resolved pattern, ready to count, or
    ``None``."""
    var_preds = None
    if block.name == "Filter":
        inner = block.p
        if inner.name != "BGP" or len(inner.triples) != 1:
            return None
        plan = filters.analyze_filter(block, algebra._block_vars(ctx, inner, scope), ctx)
        if plan.constant or plan.tuples or plan.exists:
            return None
        var_preds, block = plan.per_var, inner
    elif block.name != "BGP" or len(block.triples) != 1:
        return None
    pat = matching._pattern_terms(ctx, store, *block.triples[0], scope=scope)
    if var_preds and matching._push_native_restrictions(store, pat, var_preds):
        return None  # a conjunct the native match cannot decide
    return pat


class _Pushed(dict):
    """The filter conjuncts a FILTER hands down into its block. As a dict,
    each variable's single-variable conjuncts, for the scans and VALUES
    tables that bind it;
    ``tuples``, the fast conjuncts over several variables, which a BGP that
    binds all of theirs may prune with mid-join (a pre-filter: the FILTER
    still applies them); ``views``, the terms parsed for those, shared with
    the FILTER so none is decoded twice; ``applied``, the ids of the tuple
    conjuncts a BGP turned into restrictions of one of its patterns (see
    :func:`_specialize_tuple_conjuncts`), which every row the block yields
    therefore passes. They travel together wherever the block solvers pass
    them — a group join's sides, an OPTIONAL's or a MINUS's left side, a
    GRAPH block — and never an OPTIONAL's or a MINUS's right side, where
    dropping a row would change the answer. Empty, it is falsy like
    ``None``."""

    __slots__ = ("tuples", "views", "applied")

    def __init__(self, per_var=(), tuples=(), views=None, applied=None):
        super().__init__(per_var)
        self.tuples = tuple(tuples)
        self.views = {} if views is None else views
        self.applied = set() if applied is None else applied


def _solve_block(
    ctx, store, node, env=frozenset(), var_preds=None, scope=None, limit=None, stream=False
) -> Relation:
    """Solve a block into a relation.

    ``env`` is the set of variables an enclosing lazy join or OPTIONAL binds
    row by row in rdflib's evaluation — here the sides are solved
    independently and joined, which is only exact while nothing inside
    depends on those values (a FILTER that could see them falls back).
    ``var_preds`` (a :class:`_Pushed`) are the filter conjuncts a FILTER
    above pushed down: single-variable ones to the pattern scans and VALUES
    tables binding the variable, tuple ones to the BGPs binding all of
    theirs. ``scope`` is the graph every pattern below is matched in
    (:class:`_GraphScope`); ``None`` means the graph the context has made
    active. ``limit`` says only that many rows will be read: a block that is
    one native match fetches no more (see :func:`_solve_bgp`); any other
    ignores it. ``stream`` says the relation goes straight to
    :func:`_yield_solutions`: it reaches a BGP through FILTER and GRAPH nodes
    only, where rdflib may finish the patterns a join over the pair budget
    leaves (:func:`_join_incremental_bgp`).
    """
    if scope is None:
        scope = algebra._active_scope(ctx, store)
    name = node.name
    trace = _TRACE.get()
    started = tracing._stopwatch()
    if trace is not None:
        trace.depth += 1
    try:
        if name == "BGP":
            rel = bgp._solve_bgp(ctx, store, node.triples, scope, var_preds, limit, stream)
        elif name == "Filter":
            rel = _solve_filter(ctx, store, node, env, var_preds, scope, limit, stream)
        elif name == "Join":
            rel = _solve_join(ctx, store, node, env, var_preds, scope)
        elif name == "LeftJoin":
            rel = _solve_left_join(ctx, store, node, env, var_preds, scope)
        elif name == "Minus":
            rel = _solve_minus(ctx, store, node, env, var_preds, scope)
        elif name == "ToMultiSet":
            rel = _solve_values(ctx, store, node, var_preds)
        elif name == "Graph":
            inner = algebra._graph_scope(ctx, node)
            if inner.var is not None and not algebra._carries_graph(node.p, inner.var):
                raise NotImplementedError
            rel = _solve_block(ctx, store, node.p, env, var_preds, inner, limit, stream)
        else:
            raise NotImplementedError
    finally:
        if trace is not None:
            trace.depth -= 1
    tracing._trace_event(
        "block_operator_complete",
        operator=name,
        depth=0 if trace is None else trace.depth,
        rows=relation._relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        elapsed_ns=started(),
        pattern_count=len(node.triples) if name == "BGP" else None,
    )
    return rel


def _solve_values(ctx, store, node, var_preds=None) -> Relation:
    """An inline VALUES table as a relation: its constants looked up in one
    batch (:func:`_constant_codes`; a term the dictionary does not hold gets
    a negative code, so it joins nothing but is still yielded verbatim),
    UNDEF unbound. A row that contradicts a context binding is dropped, as
    rdflib's ``evalValues`` skips it on AlreadyBound, and so is a row whose
    term fails a conjunct a FILTER above pushed down (``var_preds``; the
    variables they name are bound in every row), tested on the table's own
    terms (:func:`filters.constant_predicate`)."""
    variables = algebra._node_vars(node)
    schema = tuple(v for v in variables if ctx[v] is None)
    bound = {v: ctx[v] for v in variables if ctx[v] is not None}
    rows_in = [
        row
        for row in node.p.res
        if not any(row.get(v, "UNDEF") != "UNDEF" and row[v] != term for v, term in bound.items())
    ]
    terms = list(
        dict.fromkeys(row[v] for row in rows_in for v in schema if row.get(v, "UNDEF") != "UNDEF")
    )
    codes = dict(zip(terms, _constant_codes(store, terms), strict=True))
    if var_preds:
        tests = [(v, filters.constant_predicate(c)) for v in schema for c in var_preds.get(v, ())]
        rows_in = [row for row in rows_in if all(test(row[v]) for v, test in tests)]
    rows = [
        tuple(None if row.get(v, "UNDEF") == "UNDEF" else codes[row[v]] for v in schema)
        for row in rows_in
    ]
    nullable = frozenset(v for i, v in enumerate(schema) if any(row[i] is None for row in rows))
    return Relation(schema, None, rows, len(rows), nullable, store._terms().foreign)


def _constant_codes(store, terms: list) -> list:
    """The dictionary codes of query constants, private negative ones for the
    terms it does not hold.

    One ``encode_many`` looks every spelling up, tolerant of spelling: the
    native layer canonicalizes a term the way its pattern parser does, so a
    miss means no match of any pattern could reach the term either. An IRI
    the parser rejects (:func:`~vortex_rdflib.terms.unparseable_iri`) is no
    term of the file and is not looked up; any other term the native parser
    rejects leaves the table to rdflib.
    """
    if not terms:
        return []
    rejected = [unparseable_iri(term) for term in terms]
    spellings = [canonical_spelling(t) for t, out in zip(terms, rejected, strict=True) if not out]
    try:
        encoded = iter(store._dict.encode_many(spellings) if spellings else ())
    except ValueError:
        raise NotImplementedError from None
    found = [None if out else next(encoded) for out in rejected]
    store._prime_decode_cache([code for code in found if code is not None])
    codes = []
    for term, code in zip(terms, found, strict=True):
        if code is None:
            codes.append(store._foreign_code(term))
            continue
        # rdflib would carry the query's own object into the solutions; when
        # that object is not the decoded dictionary term (a query literal
        # rdflib's parser left unnormalized, "042"^^xsd:integer), the two are
        # observably different rows, so the query is left to rdflib.
        if store._decode_term(code) != term:
            raise NotImplementedError
        codes.append(code)
    return codes


def _solve_filter(ctx, store, node, env, var_preds, scope, limit=None, stream=False) -> Relation:
    """A ``Filter`` over a block: constant conjuncts decide the whole block
    before anything is matched, single-variable conjuncts over variables the
    block always binds restrict the pattern scans and VALUES tables that bind
    them, the rest filters the solved rows. A ``limit`` reaches the block
    only when nothing is left to filter above it, and ``stream`` when the
    filter holds no EXISTS. Where rdflib still finishes the block's rows
    (``rest``), a conjunct over the variables those rows bind tests them
    before it does; one over a variable only the rest binds cannot, and the
    FILTER goes back to rdflib.

    The expression's block variables are those its group binds
    (:func:`_group_vars`; see :func:`filters.analyze_filter` for the rest of
    its scope)."""
    inner = node.p
    # The columns of the empty relation a false constant conjunct returns.
    block_vars = algebra._block_vars(ctx, inner, scope)
    plan = filters.analyze_filter(node, algebra._group_vars(ctx, inner), ctx)
    if plan.exists and scope.var is not None:
        # rdflib evaluates an EXISTS body against the *active* graph. Under a
        # graph variable there is no single active graph — the row's graph is
        # a column here — so the body would be answered over the union. Hand
        # the whole block back and let rdflib walk the graphs itself.
        raise NotImplementedError
    decided = plan.constant + plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    _reject_graph_exists(ctx, store, decided, scope)
    if not getattr(node, "no_isolated_scope", False):
        kept = set(node._vars or ()) | set(ctx.initBindings or ())
        _reject_forgotten_exists(ctx, decided + [e.conjunct for e in plan.exists], kept)
    if env:
        _reject_env_references(node, plan, env, ctx)
    for conjunct in plan.constant:
        if not filters.evaluate_constant(conjunct):
            return Relation.from_rows(tuple(block_vars), [])
    nullable = algebra._block_nullable(inner)
    outer = var_preds if var_preds is not None else _Pushed()
    fast_tuples = tuple(conjunct for conjunct in plan.tuples if conjunct.fast is not None)
    pushed = _Pushed(outer, outer.tuples + fast_tuples, outer.views, outer.applied)
    residual = list(plan.tuples)
    for var, conjuncts in plan.per_var.items():
        if var in nullable:
            residual.extend(conjuncts)
        else:
            pushed[var] = pushed.get(var, []) + conjuncts
    limit = None if residual or plan.exists else limit
    stream = stream and not plan.exists
    rel = _solve_block(ctx, store, inner, env, pushed, scope, limit, stream)
    residual = [conjunct for conjunct in residual if id(conjunct) not in pushed.applied]
    if residual:
        if rel.rest and any(v not in rel.schema for c in residual for v in c.vars):
            raise NotImplementedError
        positions = [
            [rel.schema.index(variable) for variable in conjunct.vars] for conjunct in residual
        ]
        views = pushed.views
        if rel.rows is not None:
            # Only the fast route reads parsed terms; a generic-only conjunct
            # decodes through the query's term cache.
            columns = {
                index
                for conjunct, indexes in zip(residual, positions, strict=True)
                if conjunct.fast is not None
                for index in indexes
            }
            filters.prime_tuple_views(store, rel.rows, columns, views)
        preds = tuple(
            filters.tuple_predicate(store, conjunct, indexes, views)
            for conjunct, indexes in zip(residual, positions, strict=True)
        )
        rel = relation._filter_relation(rel, preds)
    for exists in plan.exists:
        rel = _apply_exists(ctx, store, rel, exists, scope)
    return rel


def _apply_exists(ctx, store, rel: Relation, exists, scope) -> Relation:
    """``FILTER (NOT) EXISTS { body }`` as a semi- or anti-join on the
    variables the body shares with the block: the body is solved once (or,
    for a small block and a one-pattern body, probed per row with counts
    capped at one row, in native batches) instead of once per row. Shapes
    rdflib would evaluate differently on an independently solved body — a
    shared variable the block or the body's solutions may leave unbound, a
    body the solver cannot take, a body FILTER that sees the block's bindings
    — take the generic route, rdflib's own evaluator per distinct tuple of
    the variables the EXISTS names. The FILTER is left to rdflib where that
    route would differ: under a ``GRAPH`` scoping the block to a graph other
    than the context's (see :func:`_reject_graph_exists`), and where the
    body holds a MINUS and the EXISTS names no variable of the block, or one
    the block may leave unbound."""
    body, negate = algebra._unwrap_empty_joins(exists.body), exists.negate
    rows = relation._rows_of(rel)
    if not rows:
        return rel

    def generic():
        conjunct = exists.conjunct
        _reject_graph_exists(ctx, store, [conjunct], scope)
        if filters.has_minus(body) and (
            not conjunct.vars or any(v in rel.nullable for v in conjunct.vars)
        ):
            # rdflib binds the whole row in the body, so a MINUS there always
            # shares a binding; this route binds only the variables the EXISTS
            # names, so it is not taken where it names none of the block's,
            # or one the block may leave unbound.
            raise NotImplementedError
        positions = [rel.schema.index(v) for v in conjunct.vars]
        return relation._filter_relation(
            rel, (filters.tuple_predicate(store, conjunct, positions),)
        )

    try:
        algebra._check_block(body)
    except NotImplementedError:
        return generic()
    shared = [v for v in algebra._block_vars(ctx, body, scope) if v in rel.schema]
    if any(v in rel.nullable for v in shared):
        return generic()
    env = frozenset(rel.schema)
    try:
        if not shared:
            found = (
                next(relation._code_rows(_solve_block(ctx, store, body, env, None, scope, 1)), None)
                is not None
            )
            if found != negate:
                return rel
            return rel.with_rows([])
        if body.name == "BGP" and len(body.triples) == 1:
            # Counted before it is matched: probing needs no match at all.
            pat = matching._pattern_terms(ctx, store, *body.triples[0], scope=scope)
            repeated_free = any(
                len(pos) > 1 for v, pos in pat.varpos.items() if v not in rel.schema
            )
            if not repeated_free and len(rows) * SETTINGS.probe_fanout < matching._count_pattern(
                store, pat, {}, limit=len(rows) * SETTINGS.probe_fanout + 1
            ):
                flags = probes._probe_exists(store, rel.schema, rows, pat)
                kept = [row for row, found in zip(rows, flags, strict=True) if found != negate]
                return rel.with_rows(kept)
            matching._match_resolved_pattern(store, pat)
            inner = matching._rel_from_pattern(pat)
        else:
            inner = _solve_block(ctx, store, body, env, None, scope)
    except NotImplementedError:
        return generic()
    if any(v not in inner.schema or v in inner.nullable for v in shared):
        # Where the body's solutions leave a key unbound, rdflib's body sees
        # the row's own binding of it, which a semi-join cannot.
        return generic()
    ia = [rel.schema.index(v) for v in shared]
    ib = [inner.schema.index(v) for v in shared]
    keys = {tuple(row[i] for i in ib) for row in relation._code_rows(inner)}
    kept = [row for row in rows if (tuple(row[i] for i in ia) in keys) != negate]
    return rel.with_rows(kept)


def _reject_env_references(node, plan, env, ctx) -> None:
    """rdflib evaluates a filter on a lazy join's or an OPTIONAL's right side,
    or in an EXISTS body, once per row of the left side or of the block the
    EXISTS filters, whose variables are ``env``. Its expression sees the
    row's binding of such a variable only where the node keeps it: in its
    ``_vars`` or the query's ``initBindings``, or anywhere directly inside an
    EXISTS body. On the independently solved block a conjunct sees the
    variable bound only where the block binds it too; ``_vars`` leaves out a
    VALUES table's variables, and a filter in a group of an EXISTS body has
    none. Where the two differ, the conjunct is left to rdflib."""
    everything = getattr(node, "no_isolated_scope", False)
    allowed = set(node._vars or ()) | set(ctx.initBindings or ())
    conjuncts = plan.constant + plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    conjuncts += [exists.conjunct for exists in plan.exists]
    for conjunct in conjuncts:
        for var in filters.expr_vars(conjunct.expr):
            if var in env and var not in conjunct.consts:
                if (var in conjunct.vars) != (everything or var in allowed):
                    raise NotImplementedError


def _reject_graph_exists(ctx, store, conjuncts, scope) -> None:
    """rdflib's evaluator matches an EXISTS body in the graph the context has
    made active: a conjunct it decides that holds one cannot be evaluated
    under a ``GRAPH`` that scopes the block to another graph."""
    if scope != algebra._active_scope(ctx, store) and any(
        filters.has_exists(c.expr) for c in conjuncts
    ):
        raise NotImplementedError


def _reject_forgotten_exists(ctx, conjuncts, kept) -> None:
    """rdflib evaluates a FILTER, and an OPTIONAL's condition, with the
    context's bindings outside ``kept`` forgotten (see
    :func:`filters.analyze_filter`; a condition keeps the ``initBindings``
    alone). An EXISTS here would see them: its body is solved through the
    context, and rdflib's evaluator, handed no binding, thaws the whole
    context. A conjunct holding an EXISTS that names one is left to rdflib."""
    for conjunct in conjuncts:
        if filters.has_exists(conjunct.expr) and any(
            ctx[var] is not None and var not in kept for var in filters.expr_vars(conjunct.expr)
        ):
            raise NotImplementedError


def _solve_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """A group join: both sides solved, then a hash join on the shared
    variables (a probe of the right pattern when the left side is small).
    rdflib deduplicates the right side of a non-lazy join."""
    lazy = bool(node.lazy)
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    left_rows = relation._rows_of(left)
    if not left_rows:
        schema = left.schema + tuple(
            v for v in algebra._block_vars(ctx, node.p2, scope) if v not in left.schema
        )
        return Relation(
            schema, None, [], 0, left.nullable | algebra._block_nullable(node.p2), left.foreign
        )
    right_env = env | frozenset(algebra._scoped_vars(node.p1, scope)) if lazy else env
    p2 = node.p2
    if lazy and p2.name == "BGP" and len(p2.triples) == 1:
        pat, estimate, probe = _plan_pattern_side(
            ctx, store, p2.triples[0], scope, left, left_rows, var_preds
        )
        if probe:
            schema, rows = probes._probe_join(store, left.schema, left_rows, pat)
            tracing._trace_event(
                "join_plan_complete",
                strategy="probe",
                input_rows=len(left_rows),
                estimated_right_rows=estimate,
                native_probe_call_count=len(left_rows),
                output_rows=len(rows),
            )
            return Relation(schema, None, rows, len(rows), left.nullable, left.foreign)
        tracing._trace_event(
            "join_plan_complete",
            strategy="hash",
            input_rows=len(left_rows),
            estimated_right_rows=estimate,
            right_rows=pat.nrows,
        )
        right = matching._rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, right_env, var_preds, scope)
    right_rows = relation._rows_of(right)
    if not lazy:
        right_rows = list(dict.fromkeys(right_rows))
    shared = [v for v in right.schema if v in left.schema]
    relation._shared_key_ok(left, right, shared)
    schema, rows = relation._join(left.schema, left_rows, right.schema, right_rows)
    foreign = left.foreign or right.foreign
    return Relation(schema, None, rows, len(rows), left.nullable | right.nullable, foreign)


def _solve_left_join(ctx, store, node, env, var_preds, scope) -> Relation:
    """An OPTIONAL: every left row, extended by the matching right rows that
    pass the hoisted inner FILTER, or padded with unbound variables."""
    p1, p2, expr = node.p1, node.p2, node.expr
    vars1, vars2 = algebra._scoped_vars(p1, scope), algebra._scoped_vars(p2, scope)
    condition = expr if getattr(expr, "name", None) != "TrueFilter" else None
    if (
        scope.var is not None
        and scope.var in algebra._group_vars(ctx, p2)
        and scope.var not in algebra._group_vars(ctx, p1)
    ):
        # rdflib matches p2's ?g against any term and joins the graph in after
        # the OPTIONAL: a left row whose matches all name another graph is
        # dropped there, where the graph column would pad it.
        raise NotImplementedError
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
    left_rows = relation._rows_of(left)
    extra = tuple(v for v in algebra._block_vars(ctx, p2, scope) if v not in left.schema)
    nullable = left.nullable | algebra._block_nullable(p2) | frozenset(extra)
    if not left_rows:
        return Relation(left.schema + extra, None, [], 0, nullable, left.foreign)

    def condition_preds(schema):
        if condition is None:
            return ()
        init = set(ctx.initBindings or ())
        visible = {v: term for v, term in outer.items() if v in init}
        sides = set(algebra._group_vars(ctx, node))
        plan = filters.analyze_expr(condition, [v for v in schema if v in sides], ctx, visible)
        if plan.exists:
            # An OPTIONAL's condition can carry (NOT) EXISTS; a semi-join over
            # the padded rows is not what rdflib computes, so the whole
            # LeftJoin goes back to it.
            raise NotImplementedError
        conjuncts = plan.tuples + [c for cs in plan.per_var.values() for c in cs]
        _reject_graph_exists(ctx, store, plan.constant + conjuncts, scope)
        _reject_forgotten_exists(ctx, plan.constant + conjuncts, init)
        for conjunct in plan.constant:
            if not filters.evaluate_constant(conjunct):
                return None  # no pair can pass: every left row is unmatched
        return tuple(
            filters.tuple_predicate(store, c, [schema.index(v) for v in c.vars]) for c in conjuncts
        )

    if p2.name == "BGP" and len(p2.triples) == 1:
        # Counted before it is matched: a selective left relation drives the
        # probes without the complete right match the probes exist to avoid.
        plan_started = tracing._stopwatch()
        pat, estimate, probe = _plan_pattern_side(
            ctx, store, p2.triples[0], scope, left, left_rows, None
        )
        if probe:
            schema = left.schema + tuple(v for v in pat.varpos if v not in left.schema)
            preds = condition_preds(schema)
            if preds is None:
                rows = [row + (None,) * len(extra) for row in left_rows]
                schema, unmatched = left.schema + extra, len(left_rows)
            else:
                schema, rows = probes._probe_join(
                    store, left.schema, left_rows, pat, keep_unmatched=True, row_preds=preds
                )
                free_count = len(schema) - len(left.schema)
                unmatched = sum(row[-1] is None for row in rows) if free_count else 0
            tracing._trace_event(
                "left_join_plan_complete",
                strategy="constant_false" if preds is None else "probe",
                input_rows=len(left_rows),
                estimated_right_rows=estimate,
                native_initial_match_count=0,
                native_probe_call_count=0 if preds is None else len(left_rows),
                matched_left_rows=len(left_rows) - unmatched,
                unmatched_left_rows=unmatched,
                output_rows=len(rows),
                elapsed_ns=plan_started(),
            )
            return Relation(schema, None, rows, len(rows), nullable, left.foreign)
        tracing._trace_event(
            "left_join_plan_complete",
            strategy="hash",
            input_rows=len(left_rows),
            estimated_right_rows=estimate,
            right_rows=pat.nrows,
            native_initial_match_count=int(not pat.unsatisfiable),
            native_probe_call_count=0,
            elapsed_ns=plan_started(),
        )
        right = matching._rel_from_pattern(pat)
    else:
        right = _solve_block(ctx, store, p2, env | frozenset(vars1), None, scope)
    shared = [v for v in right.schema if v in left.schema]
    relation._shared_key_ok(left, right, shared)
    schema = left.schema + tuple(v for v in right.schema if v not in left.schema)
    foreign = left.foreign or right.foreign
    preds = condition_preds(schema)
    if preds is None:
        rows = [row + (None,) * (len(schema) - len(left.schema)) for row in left_rows]
        return Relation(schema, None, rows, len(rows), nullable, foreign)
    rows = relation._left_join_rows(
        left.schema, left_rows, right.schema, relation._rows_of(right), preds
    )
    return Relation(schema, None, rows, len(rows), nullable, foreign)


def _solve_minus(ctx, store, node, env, var_preds, scope) -> Relation:
    """MINUS: an anti-join on the shared variables.

    rdflib removes a left row only where a compatible right row shares a
    binding with it. Both sides' rows carry the context's bindings and, where
    rdflib solves the MINUS once per row of an enclosing lazy join, OPTIONAL
    or EXISTS (``env``), that row's, which the right side is also matched
    with. With no variable both sides bind, nothing is removed at top level,
    and everything is under a context binding when the right side is
    non-empty. Under ``GRAPH ?g`` rdflib solves both sides in each named
    graph with ``?g`` unbound: the graph column keeps the anti-join in one
    graph but is a shared binding only where both sides bind ``?g``, and a
    right side's own ``?g`` matches any term. The shapes that turn on the
    enclosing row, or on that ``?g``, are left to rdflib."""
    left_vars, right_vars = algebra._group_vars(ctx, node.p1), algebra._group_vars(ctx, node.p2)
    common = any(v in left_vars for v in right_vars)
    context = bool(dict(ctx.bindings.items()))
    if env and not (common or context):
        raise NotImplementedError  # whether the enclosing row binds anything decides
    if any(v in env and v not in left_vars for v in right_vars):
        raise NotImplementedError  # the right side reads the enclosing row's value
    left = _solve_block(ctx, store, node.p1, env, var_preds, scope)
    if not (common or context):
        return left
    left_rows = relation._rows_of(left)
    if not left_rows:
        return left
    if scope.var in right_vars and scope.var not in left_vars:
        raise NotImplementedError
    right = _solve_block(ctx, store, node.p2, env, None, scope)
    shared = [v for v in right.schema if v in left.schema]
    if not shared:
        if next(relation._code_rows(right), None) is not None:
            return left.with_rows([])
        return left.with_rows(left_rows)
    relation._shared_key_ok(left, right, shared)
    ia = [left.schema.index(v) for v in shared]
    ib = [right.schema.index(v) for v in shared]
    keys = {tuple(rb[i] for i in ib) for rb in relation._code_rows(right)}
    rows = [ra for ra in left_rows if tuple(ra[i] for i in ia) not in keys]
    return left.with_rows(rows)


def _plan_pattern_side(ctx, store, triple, scope, left: Relation, left_rows, var_preds):
    """The one-pattern right side of a lazy join or an OPTIONAL, counted
    before it is matched.

    Returns ``(pat, estimate, probe)``. The count stops at
    ``len(left_rows) * SETTINGS.probe_fanout + 1``, as far as the decision needs:
    ``estimate`` is exact below that and a lower bound at it. When the
    pattern shares a variable with the left relation and the relation is at
    least ``SETTINGS.probe_fanout`` times smaller than the pattern's count, ``probe``
    is true and ``pat`` is left unmatched — the caller re-probes it per left
    row, and the per-variable restrictions over its free variables travel
    with it. Otherwise ``pat`` is matched, restricted on its free variables,
    and ready for a hash join. A variable the left side binds was restricted
    there.
    """
    pat = matching._pattern_terms(ctx, store, *triple, scope=scope)
    shared = [v for v in pat.varpos if v in left.schema]
    free_preds = (
        {v: var_preds[v] for v in pat.varpos if v in var_preds and v not in left.schema}
        if var_preds
        else None
    )
    if free_preds:
        free_preds = matching._push_native_restrictions(store, pat, free_preds)
    estimate = matching._count_pattern(
        store, pat, {}, limit=len(left_rows) * SETTINGS.probe_fanout + 1
    )
    if shared and len(left_rows) * SETTINGS.probe_fanout < estimate:
        relation._shared_key_ok(left, Relation((), None, [], 0), shared)
        if free_preds:
            pat.deferred = free_preds
        return pat, estimate, True
    matching._match_resolved_pattern(store, pat)
    if free_preds:
        matching._restrict_pattern(store, pat, free_preds)
    return pat, estimate, False
