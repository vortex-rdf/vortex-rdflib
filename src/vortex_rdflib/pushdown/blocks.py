"""Blocks: a subtree of the algebra solved into one code-space relation
(:func:`_solve_block`) — a BGP, a FILTER over a block, a group join, an
OPTIONAL, a MINUS, a VALUES table, a GRAPH, each a typed block
(:mod:`.algebra`) with its solver (:data:`_SOLVERS`) — and the handlers of
the nodes that are blocks.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from types import MappingProxyType

from rdflib.plugins.sparql.evaluate import evalPart

from .. import boundary, filters, trace
from ..terms import canonical_spelling, unparseable_iri
from . import algebra, bgp, matching, planner, probes, relation, solutions
from .algebra import Block
from .matching import Pattern
from .pushed import NOTHING, Pushed
from .relation import Relation


def _eval_block_node(ctx, store, part):
    node = algebra.block(part)
    rel = _solve_block(ctx, store, node, _Frame(algebra._active_scope(ctx, store), stream=True))
    return solutions._yield_solutions(ctx, store, rel)


def _eval_filter(ctx, store, part):
    """A ``Filter`` node: the pushdown's block, or — when the block goes back
    to rdflib (a join step over the pair budget, a shape the pushdown does not
    take) — rdflib's solutions of the node's pattern, kept by the pushdown's
    own FILTER (:func:`filters.solution_filter`), so the answer does not
    depend on which route solved the pattern."""
    try:
        return _eval_block_node(ctx, store, part)
    except NotImplementedError:
        keep = filters.solution_filter(store, part, ctx)
    trace.event("filter_over_rdflib_rows", algebra_node=part.p.name)
    return (solution for solution in evalPart(ctx, part.p) if keep(solution))


@dataclass(frozen=True, slots=True)
class _Frame:
    """What a block is solved under (:func:`_solve_block`).

    ``scope`` is the graph every pattern below is matched in
    (:class:`algebra._GraphScope`): at the top, the graph the context has
    made active. ``env`` is the set of variables an enclosing lazy join or
    OPTIONAL binds row by row in rdflib's evaluation — here the sides are
    solved independently and joined, which is only exact while nothing
    inside depends on those values (a FILTER that could see them falls
    back). ``pushed`` are the filter conjuncts a FILTER above handed down
    (:class:`.pushed.Pushed`). ``limit`` says only that many rows will be read:
    a block that is one native match fetches no more (see
    :func:`bgp._solve_bgp`); any other ignores it. ``stream`` says the
    relation goes straight to :func:`solutions._yield_solutions`: it reaches
    a BGP through FILTER and GRAPH nodes only, where rdflib may finish the
    patterns a join over the pair budget leaves
    (:func:`planner._join_incremental_bgp`).
    """

    scope: algebra._GraphScope
    env: frozenset = frozenset()
    pushed: Pushed = NOTHING
    limit: int | None = None
    stream: bool = False

    def side(self, **changes) -> "_Frame":
        """The frame of a group join's side, or of an OPTIONAL's or a
        MINUS's left side: this one with ``changes`` (an ``env``, the
        ``pushed`` conjuncts), every row read."""
        return replace(self, limit=None, stream=False, **changes)

    def right(self, bound=frozenset()) -> "_Frame":
        """The frame of an OPTIONAL's or a MINUS's right side: nothing
        pushed — dropping one of its rows would change the answer — every
        row read, and ``bound``, the variables rdflib binds row by row from
        the left side, added to ``env``."""
        return _Frame(self.scope, self.env | bound)


def _solve_block(ctx, store, node: Block, frame: _Frame) -> Relation:
    """Solve a block into a relation, by its solver (:data:`_SOLVERS`), under
    ``frame`` (:class:`_Frame`)."""
    active = trace.active()
    started = trace.stopwatch()
    if active is not None:
        active.depth += 1
    try:
        rel = _SOLVERS[type(node)](ctx, store, node, frame)
    finally:
        if active is not None:
            active.depth -= 1
    if active is not None:
        _trace_block(node, rel, active.depth, started())
    return rel


def _solve_bgp(ctx, store, node: algebra.Bgp, frame: _Frame) -> Relation:
    """A BGP: :func:`bgp._solve_bgp` under the frame."""
    return bgp._solve_bgp(
        ctx, store, node.triples, frame.scope, frame.pushed, frame.limit, frame.stream
    )


def _solve_graph(ctx, store, node: algebra.Graph, frame: _Frame) -> Relation:
    """A GRAPH: its block in the scope it establishes. Under ``GRAPH ?g`` the
    block's rows must carry the graph column (:meth:`algebra.Block.carries_graph`)."""
    inner = algebra._graph_scope(ctx, node)
    if inner.var is not None and not node.inner.carries_graph(inner.var):
        raise NotImplementedError
    return _solve_block(ctx, store, node.inner, replace(frame, scope=inner))


def _trace_block(node: Block, rel: Relation, depth: int, elapsed_ns: int) -> None:
    """Trace a solved block as ``block_operator_complete``: its operator,
    how deep it sits, and the relation it was solved into."""
    trace.event(
        "block_operator_complete",
        operator=node.name,
        depth=depth,
        rows=relation._relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        elapsed_ns=elapsed_ns,
        pattern_count=len(node.triples) if isinstance(node, algebra.Bgp) else None,
    )


def _solve_values(ctx, store, node: algebra.Values, frame: _Frame) -> Relation:
    """An inline VALUES table as a relation: its constants looked up in one
    batch (:func:`_constant_codes`; a term the dictionary does not hold gets
    a negative code, so it joins nothing but is still yielded verbatim),
    UNDEF unbound. A row that contradicts a context binding is dropped, as
    rdflib's ``evalValues`` skips it on AlreadyBound, and so is a row whose
    term fails a conjunct a FILTER above pushed down (the frame's
    ``pushed``; the variables they name are bound in every row), tested on
    the table's own
    terms (:func:`filters.constant_predicate`)."""
    variables, per_var = node.vars, frame.pushed.per_var
    positions = [i for i, v in enumerate(variables) if ctx[v] is None]
    bound = [(i, ctx[v]) for i, v in enumerate(variables) if ctx[v] is not None]
    rows_in = [
        row
        for row in node.rows
        if not any(row[i] is not None and row[i] != term for i, term in bound)
    ]
    terms = list(dict.fromkeys(row[i] for row in rows_in for i in positions if row[i] is not None))
    codes = dict(zip(terms, _constant_codes(store, terms), strict=True))
    if per_var:
        # The conjuncts pushed down name variables bound in every row.
        tests = [
            (i, filters.constant_predicate(c))
            for i in positions
            for c in per_var.get(variables[i], ())
        ]
        rows_in = [row for row in rows_in if all(test(row[i]) for i, test in tests)]
    rows = [tuple(None if row[i] is None else codes[row[i]] for i in positions) for row in rows_in]
    schema = tuple(variables[i] for i in positions)
    nullable = frozenset(v for i, v in enumerate(schema) if any(row[i] is None for row in rows))
    return Relation(schema, None, rows, len(rows), nullable)


def _constant_codes(store, terms: list) -> list:
    """The dictionary codes of query constants, private negative ones for the
    terms it does not hold.

    One ``encode_many`` looks every spelling up, tolerant of spelling: the
    native layer canonicalizes a term the way its pattern parser does, so a
    miss means no match of any pattern could reach the term either. An IRI
    of no IRI's form (:func:`~vortex_rdflib.terms.unparseable_iri`) is not
    looked up, and a term the native parser refuses reads as absent
    (:func:`~vortex_rdflib.boundary.codes_of`): no file holds either, so
    each gets a private code, as an unknown term does.
    """
    if not terms:
        return []
    rejected = [unparseable_iri(term) for term in terms]
    spellings = [canonical_spelling(t) for t, out in zip(terms, rejected, strict=True) if not out]
    encoded = iter(boundary.codes_of(store, spellings) if spellings else ())
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


def _solve_filter(ctx, store, node: algebra.Filter, frame: _Frame) -> Relation:
    """A ``Filter`` over a block: constant conjuncts decide the whole block
    before anything is matched, single-variable conjuncts over variables the
    block always binds restrict the pattern scans and VALUES tables that bind
    them, the rest filters the solved rows but those the block reports
    applied (``Relation.applied``). A ``limit`` reaches the block only when
    nothing is left to filter above it, and ``stream`` when the filter holds
    no EXISTS. Where rdflib still finishes the block's rows
    (``rest``), a conjunct over the variables those rows bind tests them
    before it does; one over a variable only the rest binds cannot, and the
    FILTER goes back to rdflib.

    The expression's block variables are those its group binds
    (:func:`_group_vars`; see :func:`filters.analyze_filter` for the rest of
    its scope)."""
    inner, scope, env = node.inner, frame.scope, frame.env
    # The columns of the empty relation a false constant conjunct returns.
    block_vars = algebra._block_vars(ctx, inner, scope)
    plan = filters.analyze_filter(node.expr, node.kept, algebra._group_vars(ctx, inner), ctx)
    if plan.exists and scope.var is not None:
        # rdflib evaluates an EXISTS body against the *active* graph. Under a
        # graph variable there is no single active graph — the row's graph is
        # a column here — so the body would be answered over the union. Hand
        # the whole block back and let rdflib walk the graphs itself.
        raise NotImplementedError
    decided = plan.conjuncts
    _reject_graph_exists(ctx, store, decided, scope)
    _reject_forgotten_exists(ctx, decided + [e.conjunct for e in plan.exists], plan.allowed)
    if env:
        _reject_env_references(plan, env)
    for conjunct in plan.constant:
        if not filters.evaluate_constant(conjunct):
            return Relation.from_rows(tuple(block_vars), [])
    nullable = inner.nullable
    outer = frame.pushed
    per_var = dict(outer.per_var)
    residual = list(plan.tuples)
    for var, conjuncts in plan.per_var.items():
        if var in nullable:
            residual.extend(conjuncts)
        else:
            per_var[var] = [*per_var.get(var, ()), *conjuncts]
    fast_tuples = tuple(conjunct for conjunct in plan.tuples if conjunct.fast is not None)
    views = {} if outer.views is None else outer.views
    pushed = Pushed(MappingProxyType(per_var), outer.tuples + fast_tuples, views)
    rel = _solve_block(
        ctx,
        store,
        inner,
        replace(
            frame,
            pushed=pushed,
            limit=None if residual or plan.exists else frame.limit,
            stream=frame.stream and not plan.exists,
        ),
    )
    residual = [conjunct for conjunct in residual if conjunct not in rel.applied]
    if residual:
        if rel.rest and any(v not in rel.schema for c in residual for v in c.vars):
            raise NotImplementedError
        positions = [
            [rel.schema.index(variable) for variable in conjunct.vars] for conjunct in residual
        ]
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
    for a one-pattern body, as :func:`_plan_side` plans it: for a small
    block, probed per row with counts capped at one row, in native batches)
    instead of once per row. Shapes
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
        if filters.exprs.has_minus(body) and (
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
        node = algebra.block(body)
    except NotImplementedError:
        return generic()
    shared = [v for v in algebra._block_vars(ctx, node, scope) if v in rel.schema]
    if any(v in rel.nullable for v in shared):
        return generic()
    env = frozenset(rel.schema)
    try:
        if not shared:
            body_rel = _solve_block(ctx, store, node, _Frame(scope, env, limit=1))
            found = next(relation._code_rows(body_rel), None) is not None
            if found != negate:
                return rel
            return rel.with_rows([])
        single = node.single
        if single is not None:
            # Counted before it is matched: probing needs no match at all.
            pat, strategy = _plan_side(ctx, store, single, scope, rel, rows, exists=True)
            if strategy == "probe":
                flags = probes._probe_exists(store, rel.schema, rows, pat)
                kept = [row for row, found in zip(rows, flags, strict=True) if found != negate]
                return rel.with_rows(kept)
            inner = _join_side(store, pat, strategy, rel, rows).right
            assert inner is not None, "a side not probed is matched"
        else:
            inner = _solve_block(ctx, store, node, _Frame(scope, env))
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


def _reject_env_references(plan, env) -> None:
    """rdflib evaluates a filter on a lazy join's or an OPTIONAL's right side,
    or in an EXISTS body, once per row of the left side or of the block the
    EXISTS filters, whose variables are ``env``. Its expression sees the
    row's binding of such a variable only where the node keeps it: in its
    ``_vars`` or the query's ``initBindings``, or anywhere directly inside an
    EXISTS body. On the independently solved block a conjunct sees the
    variable bound only where the block binds it too; ``_vars`` leaves out a
    VALUES table's variables, and a filter in a group of an EXISTS body has
    none. Where the two differ, the conjunct is left to rdflib."""
    allowed = plan.allowed
    for conjunct in plan.conjuncts + [exists.conjunct for exists in plan.exists]:
        for var in filters.exprs.expr_vars(conjunct.expr):
            if var in env and var not in conjunct.consts:
                if (var in conjunct.vars) != (allowed is None or var in allowed):
                    raise NotImplementedError


def _reject_graph_exists(ctx, store, conjuncts, scope) -> None:
    """rdflib's evaluator matches an EXISTS body in the graph the context has
    made active: a conjunct it decides that holds one cannot be evaluated
    under a ``GRAPH`` that scopes the block to another graph."""
    if scope != algebra._active_scope(ctx, store) and any(
        filters.exprs.has_exists(c.expr) for c in conjuncts
    ):
        raise NotImplementedError


def _reject_forgotten_exists(ctx, conjuncts, kept) -> None:
    """rdflib evaluates a FILTER, and an OPTIONAL's condition, with the
    context's bindings outside ``kept`` forgotten (``FilterPlan.allowed``:
    see :func:`filters.analyze_filter`; ``None``, nothing forgotten). An
    EXISTS here would see them: its body is solved through the context, and
    rdflib's evaluator, handed no binding, thaws the whole context. A
    conjunct holding an EXISTS that names one is left to rdflib."""
    if kept is None:
        return
    for conjunct in conjuncts:
        if filters.exprs.has_exists(conjunct.expr) and any(
            ctx[var] is not None and var not in kept
            for var in filters.exprs.expr_vars(conjunct.expr)
        ):
            raise NotImplementedError


def _solve_join(ctx, store, node: algebra.Join, frame: _Frame) -> Relation:
    """A group join: both sides solved, then a hash join on the shared
    variables — or, for a lazy join whose right side is one pattern, the
    join :func:`_plan_side` plans. rdflib deduplicates the right side of a
    non-lazy join."""
    lazy, scope, p2 = node.lazy, frame.scope, node.right
    left = _solve_block(ctx, store, node.left, frame.side())
    left_rows = relation._rows_of(left)
    if not left_rows:
        schema = left.schema + tuple(
            v for v in algebra._block_vars(ctx, p2, scope) if v not in left.schema
        )
        return Relation(schema, None, [], 0, left.nullable | p2.nullable)
    single = p2.single
    if lazy and single is not None:
        pat, strategy = _plan_side(ctx, store, single, scope, left, left_rows, frame.pushed.per_var)
        side = _join_side(store, pat, strategy, left, left_rows)
        _trace_side_join("join_plan_complete", side, left, left_rows)
        if side.rows is not None:
            rows = side.rows
            return Relation(side.schema, None, rows, len(rows), left.nullable, applied=left.applied)
        right = side.right
        assert right is not None, "a side not probed is matched"
    else:
        # What the left side applied, every joined row passes.
        pushed = frame.pushed.without(left.applied)
        env = frame.env | frozenset(algebra._scoped_vars(node.left, scope)) if lazy else frame.env
        right = _solve_block(ctx, store, p2, frame.side(env=env, pushed=pushed))
    right_rows = relation._rows_of(right)
    if not lazy:
        right_rows = list(dict.fromkeys(right_rows))
    shared = [v for v in right.schema if v in left.schema]
    relation._shared_key_ok(left, right, shared)
    schema, rows = relation._join(left.schema, left_rows, right.schema, right_rows)
    nullable = left.nullable | right.nullable
    return Relation(schema, None, rows, len(rows), nullable, applied=left.applied | right.applied)


def _solve_left_join(ctx, store, node: algebra.LeftJoin, frame: _Frame) -> Relation:
    """An OPTIONAL: every left row, extended by the matching right rows that
    pass the hoisted inner FILTER, or padded with unbound variables. The
    FILTER is analysed before either side is solved
    (:func:`_left_join_condition`): one that no pair can pass pads every left
    row without solving the right side, and one rdflib must evaluate hands the
    OPTIONAL back before any native work. A right side of one pattern is
    joined as :func:`_plan_side` plans."""
    p1, p2, expr = node.left, node.right, node.expr
    scope = frame.scope
    vars1, vars2 = algebra._scoped_vars(p1, scope), algebra._scoped_vars(p2, scope)
    if (
        scope.var is not None
        and scope.var in algebra._group_vars(ctx, p2)
        and scope.var not in algebra._group_vars(ctx, p1)
    ):
        # rdflib matches p2's ?g against any term and joins the graph in after
        # the OPTIONAL: a left row whose matches all name another graph is
        # dropped there, where the graph column would pad it.
        raise NotImplementedError
    # Both sides are solved here bottom-up, as SPARQL 1.1's LeftJoin is
    # (§18.5): a left row is extended by every compatible right row whose
    # pairing passes the condition, else kept unextended, and the condition
    # sees what both sides bind (and ``initBindings``). That is the
    # specification's answer where rdflib's evaluator departs: it re-checks
    # an unmatched row with only p1's ``_vars`` bound (its "cheated scope"),
    # which leave a VALUES table's variables out, and its condition forgets
    # a variable an enclosing lazy join substituted into p1. Only a binding
    # rdflib substituted itself into a part it hands here (``ctx.bindings``
    # beyond ``initBindings``: a join it evaluates, an EXISTS body) stays
    # with rdflib, whose evaluation encloses this one: one that reaches p2
    # but not p1, or that the condition reads on p1.
    init = frozenset(ctx.initBindings or ())
    substituted = frozenset(dict(ctx.bindings.items())) - init
    if substituted:
        left_vars = frozenset(vars1)
        if (substituted - left_vars) & frozenset(vars2):
            raise NotImplementedError
        if (
            getattr(expr, "name", None) != "TrueFilter"
            and frozenset(filters.exprs.expr_vars(expr)) & left_vars & substituted
        ):
            raise NotImplementedError
    conjuncts = _left_join_condition(ctx, store, node, scope)

    left = _solve_block(ctx, store, p1, frame.side())
    left_rows = relation._rows_of(left)
    extra = tuple(v for v in algebra._block_vars(ctx, p2, scope) if v not in left.schema)
    nullable = left.nullable | p2.nullable | frozenset(extra)
    if not left_rows or conjuncts is None:
        # No left row, or no pair can pass the condition: every left row is
        # unmatched, and the right side is not solved.
        rows = [row + (None,) * len(extra) for row in left_rows]
        if left_rows:
            trace.event(
                "left_join_plan_complete",
                strategy="constant_false",
                input_rows=len(left_rows),
                native_initial_match_count=0,
                native_probe_call_count=0,
                matched_left_rows=0,
                unmatched_left_rows=len(left_rows),
                output_rows=len(rows),
            )
        return Relation(left.schema + extra, None, rows, len(rows), nullable, applied=left.applied)

    single = p2.single
    # A variable the left side may leave unbound (a VALUES table's UNDEF)
    # cannot key a probe: a right side over it is matched whole, and joined
    # by SPARQL's compatibility (relation._left_join_rows).
    unbound_keys = any(v in left.nullable for v in vars2)
    if single is not None and not unbound_keys:
        # Counted before it is matched: a selective left relation drives the
        # probes without the complete right match the probes exist to avoid.
        plan_started = trace.stopwatch()
        pat, strategy = _plan_side(ctx, store, single, scope, left, left_rows)
        options: dict = {}
        if strategy == "probe":
            schema = left.schema + tuple(v for v in pat.varpos if v not in left.schema)
            options = {
                "keep_unmatched": True,
                "row_preds": _condition_preds(store, conjuncts, schema),
            }
        side = _join_side(store, pat, strategy, left, left_rows, **options)
        _trace_side_join("left_join_plan_complete", side, left, left_rows, plan_started())
        if side.rows is not None:
            rows = side.rows
            return Relation(side.schema, None, rows, len(rows), nullable, applied=left.applied)
        right = side.right
        assert right is not None, "a side not probed is matched"
    else:
        right = _solve_block(ctx, store, p2, frame.right(frozenset(vars1)))
    shared = [v for v in right.schema if v in left.schema]
    if any(v in right.nullable for v in shared):
        # A right row leaving a shared variable unbound needs the
        # compatibility of both sides' gaps, which the hash join does not
        # give; rdflib answers it.
        raise NotImplementedError
    schema = left.schema + tuple(v for v in right.schema if v not in left.schema)
    rows = relation._left_join_rows(
        left.schema,
        left_rows,
        right.schema,
        relation._rows_of(right),
        _condition_preds(store, conjuncts, schema),
        nullable_left=any(v in left.nullable for v in shared),
    )
    return Relation(schema, None, rows, len(rows), nullable, applied=left.applied)


def _left_join_condition(ctx, store, node: algebra.LeftJoin, scope) -> tuple | None:
    """An OPTIONAL's condition, analysed before either side is solved: its
    conjuncts (none for ``TrueFilter``), or ``None`` when a constant one is
    false and no pair can pass. Its block variables are those the
    OPTIONAL's own solutions bind; of the context it sees the
    ``initBindings`` alone. A condition holding an EXISTS is left to rdflib:
    a semi-join over the padded rows is not what rdflib computes."""
    expr = node.expr
    if getattr(expr, "name", None) == "TrueFilter":
        return ()
    plan = filters.analyze_filter(expr, frozenset(), algebra._group_vars(ctx, node), ctx)
    if plan.exists:
        raise NotImplementedError
    _reject_graph_exists(ctx, store, plan.conjuncts, scope)
    _reject_forgotten_exists(ctx, plan.conjuncts, plan.allowed)
    if not all(filters.evaluate_constant(conjunct) for conjunct in plan.constant):
        return None
    return tuple(plan.row_conjuncts)


def _condition_preds(store, conjuncts, schema) -> tuple:
    """An OPTIONAL's condition conjuncts as predicates over a joined row of
    ``schema``."""
    return tuple(
        filters.tuple_predicate(store, c, [schema.index(v) for v in c.vars]) for c in conjuncts
    )


def _solve_minus(ctx, store, node: algebra.Minus, frame: _Frame) -> Relation:
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
    scope, env = frame.scope, frame.env
    left_vars, right_vars = (
        algebra._group_vars(ctx, node.left),
        algebra._group_vars(ctx, node.right),
    )
    common = any(v in left_vars for v in right_vars)
    context = bool(dict(ctx.bindings.items()))
    if env and not (common or context):
        raise NotImplementedError  # whether the enclosing row binds anything decides
    if any(v in env and v not in left_vars for v in right_vars):
        raise NotImplementedError  # the right side reads the enclosing row's value
    left = _solve_block(ctx, store, node.left, frame.side())
    if not (common or context):
        return left
    left_rows = relation._rows_of(left)
    if not left_rows:
        return left
    if scope.var in right_vars and scope.var not in left_vars:
        raise NotImplementedError
    right = _solve_block(ctx, store, node.right, frame.right())
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


def _plan_side(
    ctx, store, triple, scope, left: Relation, left_rows, var_preds=None, *, exists=False
) -> tuple[Pattern, str]:
    """The one-pattern right side of a lazy join, an OPTIONAL or an EXISTS,
    resolved and planned by the rule a BGP's join step follows
    (:func:`planner.step_rule`): the pattern, and how it joins the left rows —
    ``"probe"``, re-matched per left row, the pattern unread; ``"keep_scan"``,
    matched once, narrowed to the codes the left rows bind its shared
    variables to; ``"hash"``, matched whole.

    The pattern is counted before anything is matched, as far as the
    decision needs (:func:`planner.count_at_step`): it is probed per left row
    where it shares a variable with the left relation, the relation is at
    least ``SETTINGS.probe_fanout`` times smaller than its count and each
    probe is a point lookup, and keep-scanned where each probe would scan;
    any other side is matched whole. An existence probe is a count capped
    at one row: it holds no row and stops at its first quad, so an EXISTS
    probes even where each probe would scan, unless a free variable takes
    two positions, which a count cannot test.

    A shared variable the left side may leave unbound needs rdflib's
    compatibility semantics (unbound matches anything), and is left to it.
    The per-variable restrictions over the pattern's free variables
    (``var_preds``) narrow its native match where they are keeps, and the
    rest travel with it as deferred restrictions; a variable the left side
    binds was restricted there.
    """
    pat = matching.resolve(ctx, store, triple, scope)
    shared = [v for v in pat.varpos if v in left.schema]
    if any(v in left.nullable for v in shared):
        raise NotImplementedError
    if var_preds:
        matching.restrict(
            store, pat, {v: cs for v, cs in var_preds.items() if v not in left.schema}
        )
    running = len(left_rows)
    planner.count_at_step(store, pat, running, {})
    strategy = planner.step_rule(store, running, pat, shared)
    if exists and strategy != "hash":
        repeated = any(len(pos) > 1 for v, pos in pat.varpos.items() if v not in left.schema)
        strategy = "keep_scan" if repeated else "probe"
    return pat, strategy


@dataclass(frozen=True, slots=True)
class _SideJoin:
    """How a one-pattern side joined the left rows (:func:`_join_side`): the
    ``strategy`` that did and the probes sent; a probe join's rows
    (``schema``, ``rows``), or the side matched, as the relation the caller
    joins (``right``)."""

    pat: Pattern
    strategy: str
    probe_calls: int
    schema: tuple = ()
    rows: list | None = None
    right: Relation | None = None


def _join_side(
    store, pat: Pattern, strategy: str, left: Relation, left_rows, **options
) -> _SideJoin:
    """The planned side (:func:`_plan_side`) joined to the left rows: probed
    per row (:func:`probes._probe_join`, with ``options``) while its rows
    stay within the probe budget (:func:`planner.probe_budget`), as a BGP's
    probe step is, and keep-scanned past it; a keep-scan matched once,
    narrowed to the codes the left rows bind (:func:`matching.narrow_to`: a
    VALUES term outside the dictionary joins nothing), and a hash side
    matched whole, each with its deferred restrictions applied
    (:func:`matching.restricted`). A block's join has no pair budget: the
    match is whole, where a BGP's keep-scan stops one row past the
    budget."""
    calls = 0
    if strategy == "probe":
        try:
            schema, rows = probes._probe_join(
                store, left.schema, left_rows, pat, max_rows=planner.probe_budget(), **options
            )
        except probes.RowsOverBudget as over:
            calls, strategy = over.calls, "keep_scan"
        else:
            return _SideJoin(pat, "probe", len(left_rows), schema, rows)
    if strategy == "keep_scan":
        matching.narrow_to(pat, Relation.from_rows(left.schema, left_rows))
    matching._match_resolved_pattern(store, pat)
    return _SideJoin(pat, strategy, calls, right=matching.restricted(store, pat, left.schema))


def _trace_side_join(
    event: str, side: _SideJoin, left: Relation, left_rows, elapsed_ns: int | None = None
) -> None:
    """Trace a one-pattern side's join as ``event`` (``join_plan_complete``,
    ``left_join_plan_complete``): its strategy, its probes, and its output —
    a probe join's rows, and for an OPTIONAL the left rows they matched and
    left unmatched (told apart where a row is padded with the free
    variables, unbound in an unmatched row); else the side's rows."""
    if trace.active() is None:
        return
    left_join = event == "left_join_plan_complete"
    fields: dict = {
        "strategy": side.strategy,
        "input_rows": len(left_rows),
        "estimated_right_rows": side.pat.estimate,
    }
    if side.rows is None:
        fields["right_rows"] = side.pat.nrows
    if left_join:
        matched = side.rows is None and not side.pat.unsatisfiable
        fields["native_initial_match_count"] = int(matched)
    fields["native_probe_call_count"] = side.probe_calls
    if side.rows is not None:
        if left_join:
            padded = len(side.schema) > len(left.schema)
            unmatched = sum(row[-1] is None for row in side.rows) if padded else 0
            fields["matched_left_rows"] = len(left_rows) - unmatched
            fields["unmatched_left_rows"] = unmatched
        fields["output_rows"] = len(side.rows)
    if elapsed_ns is not None:
        fields["elapsed_ns"] = elapsed_ns
    trace.event(event, **fields)


#: Each kind of block's solver, called with the context, the store, the
#: block and its frame.
_SOLVERS: dict[type[Block], Callable[..., Relation]] = {
    algebra.Bgp: _solve_bgp,
    algebra.Filter: _solve_filter,
    algebra.Join: _solve_join,
    algebra.LeftJoin: _solve_left_join,
    algebra.Minus: _solve_minus,
    algebra.Values: _solve_values,
    algebra.Graph: _solve_graph,
}
