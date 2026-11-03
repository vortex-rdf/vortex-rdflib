"""Blocks: a subtree of the algebra solved into one code-space relation
(:func:`_solve_block`) — a BGP, a FILTER over a block, a group join, an
OPTIONAL, a MINUS, a VALUES table, a GRAPH — and the block and ASK
handlers.
"""

from dataclasses import dataclass, replace
from types import MappingProxyType

from rdflib.plugins.sparql.evaluate import evalPart

from .. import boundary, filters, trace
from ..cancel import check_cancelled
from ..settings import SETTINGS
from ..terms import canonical_spelling, unparseable_iri
from . import algebra, bgp, matching, probes, relation, solutions
from .matching import Pattern
from .relation import Relation


def _eval_block_node(ctx, store, part):
    algebra._check_block(part)
    rel = _solve_block(ctx, store, part, _Frame(stream=True))
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
        if not pat.repeats:
            # Existence needs no rows: a count capped at one, which stops a
            # file-backed scan at its first match.
            narrowing = matching._narrowing(pat.nkeep, limit=1)
            check_cancelled()
            return boundary.native(store).count_quads(*pat.n3, **narrowing) > 0
    rel = _solve_block(ctx, store, block, _Frame(scope, limit=1))
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


@dataclass(frozen=True, slots=True)
class _Frame:
    """What a block is solved under (:func:`_solve_block`).

    ``scope`` is the graph every pattern below is matched in
    (:class:`algebra._GraphScope`); ``None`` means the graph the context has
    made active. ``env`` is the set of variables an enclosing lazy join or
    OPTIONAL binds row by row in rdflib's evaluation — here the sides are
    solved independently and joined, which is only exact while nothing
    inside depends on those values (a FILTER that could see them falls
    back). ``pushed`` are the filter conjuncts a FILTER above handed down
    (:class:`bgp._Pushed`). ``limit`` says only that many rows will be read:
    a block that is one native match fetches no more (see
    :func:`bgp._solve_bgp`); any other ignores it. ``stream`` says the
    relation goes straight to :func:`solutions._yield_solutions`: it reaches
    a BGP through FILTER and GRAPH nodes only, where rdflib may finish the
    patterns a join over the pair budget leaves
    (:func:`bgp._join_incremental_bgp`).
    """

    scope: algebra._GraphScope | None = None
    env: frozenset = frozenset()
    pushed: bgp._Pushed = bgp.NOTHING
    limit: int | None = None
    stream: bool = False

    def inner(self, **changes) -> "_Frame":
        """The frame of the block a FILTER or a GRAPH holds: this one with
        ``changes`` — the FILTER's pushed conjuncts and the limit and
        streaming it lets through, the GRAPH's scope."""
        return replace(self, **changes)

    def side(self, env=None, pushed=None) -> "_Frame":
        """The frame of a group join's side, or of an OPTIONAL's or a
        MINUS's left side: the same scope, ``env`` and pushed conjuncts but
        where given, every row read."""
        return _Frame(
            self.scope,
            self.env if env is None else env,
            self.pushed if pushed is None else pushed,
        )

    def right(self, bound=frozenset()) -> "_Frame":
        """The frame of an OPTIONAL's or a MINUS's right side: nothing
        pushed — dropping one of its rows would change the answer — every
        row read, and ``bound``, the variables rdflib binds row by row from
        the left side, added to ``env``."""
        return _Frame(self.scope, self.env | bound)


def _solve_block(ctx, store, node, frame: _Frame | None = None) -> Relation:
    """Solve a block into a relation, under ``frame`` (:class:`_Frame`; by
    default the active graph, nothing pushed, every row read)."""
    if frame is None or frame.scope is None:
        frame = replace(frame or _Frame(), scope=algebra._active_scope(ctx, store))
    name = node.name
    active = trace.active()
    started = trace.stopwatch()
    if active is not None:
        active.depth += 1
    try:
        if name == "BGP":
            rel = bgp._solve_bgp(
                ctx, store, node.triples, frame.scope, frame.pushed, frame.limit, frame.stream
            )
        elif name == "Filter":
            rel = _solve_filter(ctx, store, node, frame)
        elif name == "Join":
            rel = _solve_join(ctx, store, node, frame)
        elif name == "LeftJoin":
            rel = _solve_left_join(ctx, store, node, frame)
        elif name == "Minus":
            rel = _solve_minus(ctx, store, node, frame)
        elif name == "ToMultiSet":
            rel = _solve_values(ctx, store, node, frame.pushed)
        elif name == "Graph":
            inner = algebra._graph_scope(ctx, node)
            if inner.var is not None and not algebra._carries_graph(node.p, inner.var):
                raise NotImplementedError
            rel = _solve_block(ctx, store, node.p, frame.inner(scope=inner))
        else:
            raise NotImplementedError
    finally:
        if active is not None:
            active.depth -= 1
    if active is not None:
        _trace_block(node, rel, active.depth, started())
    return rel


def _trace_block(node, rel: Relation, depth: int, elapsed_ns: int) -> None:
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
        pattern_count=len(node.triples) if node.name == "BGP" else None,
    )


def _solve_values(ctx, store, node, pushed: bgp._Pushed = bgp.NOTHING) -> Relation:
    """An inline VALUES table as a relation: its constants looked up in one
    batch (:func:`_constant_codes`; a term the dictionary does not hold gets
    a negative code, so it joins nothing but is still yielded verbatim),
    UNDEF unbound. A row that contradicts a context binding is dropped, as
    rdflib's ``evalValues`` skips it on AlreadyBound, and so is a row whose
    term fails a conjunct a FILTER above pushed down (``pushed``; the
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
    if pushed.per_var:
        tests = [
            (v, filters.constant_predicate(c)) for v in schema for c in pushed.per_var.get(v, ())
        ]
        rows_in = [row for row in rows_in if all(test(row[v]) for v, test in tests)]
    rows = [
        tuple(None if row.get(v, "UNDEF") == "UNDEF" else codes[row[v]] for v in schema)
        for row in rows_in
    ]
    nullable = frozenset(v for i, v in enumerate(schema) if any(row[i] is None for row in rows))
    return Relation(schema, None, rows, len(rows), nullable)


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
        term_dict = boundary.term_dict(store)
        encoded = iter(term_dict.encode_many(spellings) if spellings else ())
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


def _solve_filter(ctx, store, node, frame: _Frame) -> Relation:
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
    inner, scope, env = node.p, frame.scope, frame.env
    assert scope is not None, "a block's frame has its scope (_solve_block)"
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
    pushed = bgp._Pushed(MappingProxyType(per_var), outer.tuples + fast_tuples, views)
    rel = _solve_block(
        ctx,
        store,
        inner,
        frame.inner(
            pushed=pushed,
            limit=None if residual or plan.exists else frame.limit,
            stream=frame.stream and not plan.exists,
        ),
    )
    residual = [conjunct for conjunct in residual if id(conjunct) not in rel.applied]
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
    for a one-pattern body, as :func:`_pattern_side` plans it: for a small
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
        algebra._check_block(body)
    except NotImplementedError:
        return generic()
    shared = [v for v in algebra._block_vars(ctx, body, scope) if v in rel.schema]
    if any(v in rel.nullable for v in shared):
        return generic()
    env = frozenset(rel.schema)
    try:
        if not shared:
            body_rel = _solve_block(ctx, store, body, _Frame(scope, env, limit=1))
            found = next(relation._code_rows(body_rel), None) is not None
            if found != negate:
                return rel
            return rel.with_rows([])
        if body.name == "BGP" and len(body.triples) == 1:
            # Counted before it is matched: probing needs no match at all.
            side = _pattern_side(ctx, store, body.triples[0], scope, rel, rows, exists=True)
            if side.strategy == "probe":
                flags = probes._probe_exists(store, rel.schema, rows, side.pat)
                kept = [row for row, found in zip(rows, flags, strict=True) if found != negate]
                return rel.with_rows(kept)
            inner = _matched_side(store, side, rel.schema, rows)
        else:
            inner = _solve_block(ctx, store, body, _Frame(scope, env))
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
        for var in filters.exprs.expr_vars(conjunct.expr):
            if var in env and var not in conjunct.consts:
                if (var in conjunct.vars) != (everything or var in allowed):
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
    context's bindings outside ``kept`` forgotten (see
    :func:`filters.analyze_filter`; a condition keeps the ``initBindings``
    alone). An EXISTS here would see them: its body is solved through the
    context, and rdflib's evaluator, handed no binding, thaws the whole
    context. A conjunct holding an EXISTS that names one is left to rdflib."""
    for conjunct in conjuncts:
        if filters.exprs.has_exists(conjunct.expr) and any(
            ctx[var] is not None and var not in kept
            for var in filters.exprs.expr_vars(conjunct.expr)
        ):
            raise NotImplementedError


def _solve_join(ctx, store, node, frame: _Frame) -> Relation:
    """A group join: both sides solved, then a hash join on the shared
    variables — or, for a lazy join whose right side is one pattern, the
    join :func:`_pattern_side` plans. rdflib deduplicates the right side of a
    non-lazy join."""
    lazy, scope = bool(node.lazy), frame.scope
    assert scope is not None, "a block's frame has its scope (_solve_block)"
    left = _solve_block(ctx, store, node.p1, frame.side())
    left_rows = relation._rows_of(left)
    if not left_rows:
        schema = left.schema + tuple(
            v for v in algebra._block_vars(ctx, node.p2, scope) if v not in left.schema
        )
        return Relation(schema, None, [], 0, left.nullable | algebra._block_nullable(node.p2))
    p2 = node.p2
    if lazy and p2.name == "BGP" and len(p2.triples) == 1:
        side = _pattern_side(
            ctx, store, p2.triples[0], scope, left, left_rows, frame.pushed.per_var
        )
        calls = 0
        if side.strategy == "probe":
            schema, rows, calls = _probed(store, side, left, left_rows)
            if rows is not None:
                trace.event(
                    "join_plan_complete",
                    strategy="probe",
                    input_rows=len(left_rows),
                    estimated_right_rows=side.pat.estimate,
                    native_probe_call_count=calls,
                    output_rows=len(rows),
                )
                return Relation(schema, None, rows, len(rows), left.nullable, applied=left.applied)
            side = replace(side, strategy="keep_scan")
        right = _matched_side(store, side, left.schema, left_rows)
        trace.event(
            "join_plan_complete",
            strategy=side.strategy,
            input_rows=len(left_rows),
            estimated_right_rows=side.pat.estimate,
            native_probe_call_count=calls,
            right_rows=side.pat.nrows,
        )
    else:
        # What the left side applied, every joined row passes.
        pushed = frame.pushed.without(left.applied)
        env = frame.env | frozenset(algebra._scoped_vars(node.p1, scope)) if lazy else None
        right = _solve_block(ctx, store, p2, frame.side(env, pushed))
    right_rows = relation._rows_of(right)
    if not lazy:
        right_rows = list(dict.fromkeys(right_rows))
    shared = [v for v in right.schema if v in left.schema]
    relation._shared_key_ok(left, right, shared)
    schema, rows = relation._join(left.schema, left_rows, right.schema, right_rows)
    nullable = left.nullable | right.nullable
    return Relation(schema, None, rows, len(rows), nullable, applied=left.applied | right.applied)


def _solve_left_join(ctx, store, node, frame: _Frame) -> Relation:
    """An OPTIONAL: every left row, extended by the matching right rows that
    pass the hoisted inner FILTER, or padded with unbound variables. The
    FILTER is analysed before either side is solved
    (:func:`_left_join_condition`): one that no pair can pass pads every left
    row without solving the right side, and one rdflib must evaluate hands the
    OPTIONAL back before any native work. A right side of one pattern is
    joined as :func:`_pattern_side` plans."""
    p1, p2, expr = node.p1, node.p2, node.expr
    scope, env = frame.scope, frame.env
    assert scope is not None, "a block's frame has its scope (_solve_block)"
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
        if (
            getattr(expr, "name", None) != "TrueFilter"
            and frozenset(filters.exprs.expr_vars(expr)) & p1_vars & cheated
        ):
            raise NotImplementedError
    conjuncts = _left_join_condition(ctx, store, node, scope)

    left = _solve_block(ctx, store, p1, frame.side())
    left_rows = relation._rows_of(left)
    extra = tuple(v for v in algebra._block_vars(ctx, p2, scope) if v not in left.schema)
    nullable = left.nullable | algebra._block_nullable(p2) | frozenset(extra)
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

    if p2.name == "BGP" and len(p2.triples) == 1:
        # Counted before it is matched: a selective left relation drives the
        # probes without the complete right match the probes exist to avoid.
        plan_started = trace.stopwatch()
        side = _pattern_side(ctx, store, p2.triples[0], scope, left, left_rows)
        calls = 0
        if side.strategy == "probe":
            schema = left.schema + tuple(v for v in side.pat.varpos if v not in left.schema)
            schema, rows, calls = _probed(
                store,
                side,
                left,
                left_rows,
                keep_unmatched=True,
                row_preds=_condition_preds(store, conjuncts, schema),
            )
            if rows is not None:
                padded = len(schema) > len(left.schema)
                _trace_probed_left_join(side, left_rows, rows, padded, calls, plan_started())
                return Relation(schema, None, rows, len(rows), nullable, applied=left.applied)
            side = replace(side, strategy="keep_scan")
        right = _matched_side(store, side, left.schema, left_rows)
        trace.event(
            "left_join_plan_complete",
            strategy=side.strategy,
            input_rows=len(left_rows),
            estimated_right_rows=side.pat.estimate,
            right_rows=side.pat.nrows,
            native_initial_match_count=int(not side.pat.unsatisfiable),
            native_probe_call_count=calls,
            elapsed_ns=plan_started(),
        )
    else:
        right = _solve_block(ctx, store, p2, frame.right(frozenset(vars1)))
    shared = [v for v in right.schema if v in left.schema]
    relation._shared_key_ok(left, right, shared)
    schema = left.schema + tuple(v for v in right.schema if v not in left.schema)
    rows = relation._left_join_rows(
        left.schema,
        left_rows,
        right.schema,
        relation._rows_of(right),
        _condition_preds(store, conjuncts, schema),
    )
    return Relation(schema, None, rows, len(rows), nullable, applied=left.applied)


def _trace_probed_left_join(
    side: "_PatternSide", left_rows, rows, padded: bool, calls: int, elapsed_ns: int
) -> None:
    """Trace a probed OPTIONAL as ``left_join_plan_complete``: its probes,
    and the left rows they matched and left unmatched — told apart where a
    row is ``padded`` with the free variables, unbound in an unmatched
    row."""
    if trace.active() is None:
        return
    unmatched = sum(row[-1] is None for row in rows) if padded else 0
    trace.event(
        "left_join_plan_complete",
        strategy="probe",
        input_rows=len(left_rows),
        estimated_right_rows=side.pat.estimate,
        native_initial_match_count=0,
        native_probe_call_count=calls,
        matched_left_rows=len(left_rows) - unmatched,
        unmatched_left_rows=unmatched,
        output_rows=len(rows),
        elapsed_ns=elapsed_ns,
    )


def _left_join_condition(ctx, store, node, scope) -> tuple | None:
    """An OPTIONAL's condition, analysed before either side is solved: its
    conjuncts (none for ``TrueFilter``), or ``None`` when a constant one is
    false and no pair can pass. Its block variables are those the
    OPTIONAL's own solutions bind; of the context it sees the
    ``initBindings`` alone. A condition holding an EXISTS is left to rdflib:
    a semi-join over the padded rows is not what rdflib computes."""
    expr = node.expr
    if getattr(expr, "name", None) == "TrueFilter":
        return ()
    init = frozenset(ctx.initBindings or ())
    visible = {v: term for v, term in ctx.bindings.items() if v in init}
    plan = filters.analyze_expr(expr, algebra._group_vars(ctx, node), ctx, visible)
    if plan.exists:
        raise NotImplementedError
    conjuncts = plan.tuples + [c for cs in plan.per_var.values() for c in cs]
    _reject_graph_exists(ctx, store, plan.constant + conjuncts, scope)
    _reject_forgotten_exists(ctx, plan.constant + conjuncts, init)
    if not all(filters.evaluate_constant(conjunct) for conjunct in plan.constant):
        return None
    return tuple(conjuncts)


def _condition_preds(store, conjuncts, schema) -> tuple:
    """An OPTIONAL's condition conjuncts as predicates over a joined row of
    ``schema``."""
    return tuple(
        filters.tuple_predicate(store, c, [schema.index(v) for v in c.vars]) for c in conjuncts
    )


def _solve_minus(ctx, store, node, frame: _Frame) -> Relation:
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
    assert scope is not None, "a block's frame has its scope (_solve_block)"
    left_vars, right_vars = algebra._group_vars(ctx, node.p1), algebra._group_vars(ctx, node.p2)
    common = any(v in left_vars for v in right_vars)
    context = bool(dict(ctx.bindings.items()))
    if env and not (common or context):
        raise NotImplementedError  # whether the enclosing row binds anything decides
    if any(v in env and v not in left_vars for v in right_vars):
        raise NotImplementedError  # the right side reads the enclosing row's value
    left = _solve_block(ctx, store, node.p1, frame.side())
    if not (common or context):
        return left
    left_rows = relation._rows_of(left)
    if not left_rows:
        return left
    if scope.var in right_vars and scope.var not in left_vars:
        raise NotImplementedError
    right = _solve_block(ctx, store, node.p2, frame.right())
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


@dataclass(frozen=True, slots=True)
class _PatternSide:
    """The one-pattern right side of a lazy join, an OPTIONAL or an EXISTS,
    counted (:func:`_pattern_side`), and how it joins the left rows:
    ``"probe"`` — re-matched per left row, the pattern unread; ``"keep_scan"``
    — matched once, narrowed to the codes the left rows bind its shared
    variables to; ``"hash"`` — matched whole. The pattern's ``estimate`` is
    the count the decision read."""

    pat: Pattern
    strategy: str


def _pattern_side(
    ctx, store, triple, scope, left: Relation, left_rows, var_preds=None, *, exists=False
) -> _PatternSide:
    """The one-pattern right side of a lazy join, an OPTIONAL or an EXISTS,
    planned by the rule a BGP's join step follows (:func:`bgp._step_rule`).

    The pattern is counted before anything is matched, up to
    ``len(left_rows) * SETTINGS.probe_fanout + 1`` rows, as far as the
    decision needs. When it shares a variable with the left relation and the
    relation is at least ``SETTINGS.probe_fanout`` times smaller than its
    count, it is probed per left row where each probe is a point lookup,
    and keep-scanned otherwise; any other side is matched whole. A probe join
    that passes the probe budget is a keep-scan after all (:func:`_probed`).
    An existence probe is a count capped at one row: it holds no row and
    stops at its first quad, so an EXISTS probes even where each probe would
    scan, unless a free variable takes two positions, which a count cannot
    test.

    A shared variable the left side may leave unbound needs rdflib's
    compatibility semantics (unbound matches anything), and is left to it.
    The per-variable restrictions over the pattern's free variables
    (``var_preds``) narrow its native match where they are keeps, and the
    rest travel with it as deferred restrictions; a variable the left side
    binds was restricted there.
    """
    pat = matching._pattern_terms(ctx, store, *triple, scope=scope)
    shared = [v for v in pat.varpos if v in left.schema]
    if any(v in left.nullable for v in shared):
        raise NotImplementedError
    if var_preds:
        free = {v: var_preds[v] for v in pat.varpos if v in var_preds and v not in left.schema}
        if free:
            pat.deferred = matching._push_native_restrictions(store, pat, free)
    running = len(left_rows)
    count_limit = running * SETTINGS.probe_fanout + 1
    pat.estimate = matching._planning_count(store, pat, {}, limit=count_limit)
    pat.estimate_capped = pat.estimate >= count_limit
    strategy = bgp._step_rule(store, running, pat, shared)
    if exists and strategy != "hash":
        repeated = any(len(pos) > 1 for v, pos in pat.varpos.items() if v not in left.schema)
        strategy = "keep_scan" if repeated else "probe"
    return _PatternSide(pat, strategy)


def _probed(store, side: _PatternSide, left: Relation, left_rows, **options) -> tuple:
    """``(schema, rows, probes sent)`` of the side probed per left row
    (:func:`probes._probe_join`, with ``options``); ``rows`` is ``None`` once
    they pass the probe budget (:func:`bgp._probe_budget`), as a BGP's probe
    step is, and the side is then keep-scanned."""
    try:
        schema, rows = probes._probe_join(
            store, left.schema, left_rows, side.pat, max_rows=bgp._probe_budget(), **options
        )
    except probes.RowsOverBudget as over:
        return (), None, over.calls
    return schema, rows, len(left_rows)


def _matched_side(store, side: _PatternSide, schema, rows) -> Relation:
    """The side's pattern matched — for a keep-scan narrowed first, at each
    position of a variable the left rows (``rows`` over ``schema``) bind, to
    the dictionary codes they bind it to (a VALUES term outside the
    dictionary joins nothing) — with its deferred restrictions applied, as
    the relation a hash join takes. A block's join has no pair budget: the
    match is whole, where a BGP's keep-scan stops one row past the
    budget."""
    pat = side.pat
    if side.strategy == "keep_scan":
        for var in pat.varpos:
            if var in schema:
                index = schema.index(var)
                codes = sorted(code for code in {row[index] for row in rows} if code >= 0)
                if not codes:
                    pat.unsatisfiable = True
                    break
                matching._keep_only(pat, var, codes)
    matching._match_resolved_pattern(store, pat)
    bgp._materialize_eager_restrictions(store, pat, schema)
    return matching._rel_from_pattern(pat)
