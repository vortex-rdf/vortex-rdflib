"""One triple pattern (:class:`Pattern`): resolved against the context into
a quad pattern, narrowed by the keeps its FILTER conjuncts are, counted
and matched natively, restricted by the conjuncts that are not keeps, and
read as a relation.
"""

from dataclasses import dataclass, field, replace

from rdflib.term import BNode, Literal, URIRef, Variable
from vortex_rdf import U64Column

from .. import boundary, filters, trace, union
from ..settings import SETTINGS
from ..terms import is_range, unparseable_iri
from . import relation
from .algebra import _VAR_LIKE
from .relation import Relation


@dataclass(slots=True, eq=False)
class Pattern:
    """One triple pattern of a block, resolved against the context
    (:func:`_pattern_terms`), and what planning and matching learn about it.

    Its resolution is fixed when it is made. ``n3`` is the quad pattern the
    native layer matches: each position's N-Triples spelling, ``None`` where
    it is open; the fourth is the graph. ``varpos`` maps each variable-like
    term to the position(s) it takes, ``positions`` lists those positions in
    order, and ``repeats`` says some variable takes two (``?x :p ?x``, which
    a match tests row by row, :func:`_materialize`). ``index`` is its place
    in the BGP, ``None`` outside one. ``distinct`` says it is matched in the
    union over every graph of a store where some triple is in several, so
    its match is made a set of triples (:func:`union.distinct_rows`).
    ``unsatisfiable`` says no quad can match (a literal in subject or
    predicate position, an IRI the parser rejects, a keep left empty).

    ``nkeep`` narrows its native match per position (a code range, or an
    ascending code column); ``deferred`` maps variables to the conjuncts
    still to be evaluated over the codes the pattern brings. Both mappings
    are replaced when they change, never mutated (:func:`_set_native_keep`),
    so a keep taken before stays whole and a copy (:meth:`copy`) shares them
    safely.

    Planning sets ``estimate``: its count, ``None`` while uncounted, a lower
    bound when ``estimate_capped``. A match sets ``cols`` — the raw code
    columns, ``None`` at a position nothing reads; ``None`` while unread —
    and ``nrows`` (:meth:`set_match`).

    Identity is the pattern's: two patterns that read alike are still two.
    """

    n3: tuple
    varpos: dict
    unsatisfiable: bool = False
    index: int | None = None
    distinct: bool = False
    nkeep: dict = field(default_factory=dict)
    deferred: dict = field(default_factory=dict)
    estimate: int | None = None
    estimate_capped: bool = False
    cols: tuple | None = None
    nrows: int = 0
    repeats: bool = field(init=False)
    positions: tuple = field(init=False)

    def __post_init__(self) -> None:
        self.n3 = tuple(self.n3)
        self.repeats = any(len(pos) > 1 for pos in self.varpos.values())
        self.positions = tuple(sorted({idx for pos in self.varpos.values() for idx in pos}))

    def set_match(self, cols: tuple, nrows: int) -> None:
        """Hold a match: its columns and row count."""
        self.cols, self.nrows = cols, nrows

    def copy(self) -> "Pattern":
        """A copy for a join step to work on. The mappings a step changes
        (``nkeep``, ``deferred``) are replaced when they change, never
        mutated, so the copy may share them."""
        return replace(self)

    def clear_match(self) -> None:
        """Let go of the match: the pattern is unread again."""
        self.cols, self.nrows = None, 0


def _pattern_static_rank(pat: Pattern) -> tuple:
    """A pattern's likely selectivity from its shape alone — more bound
    positions first, then repeated variables, then query order. It breaks
    ties between equal counts; the counts themselves decide the plan."""
    n3 = pat.n3
    repeated = sum(len(pos) - 1 for pos in pat.varpos.values())
    return (-sum(value is not None for value in n3), -repeated, pat.index or 0)


def _pattern_key(pat: Pattern) -> tuple:
    """What a pattern's native match is memoized by: the resolved quad
    pattern and its native keep (two triples resolving to the same quad
    share a match only when they are narrowed alike; the patterns of one
    block share one scope, so a distinct match only ever meets another). A
    code column stands in the key itself, compared by identity: the key
    holds it, so no later column can take its place while the memo keeps
    the entry."""
    nkeep = pat.nkeep
    if not nkeep:
        return pat.n3
    narrowing = tuple(
        sorted(
            (position, ("range", *keep) if is_range(keep) else ("codes", keep))
            for position, keep in nkeep.items()
        )
    )
    return (*pat.n3, narrowing)


def _count_pattern(store, pat: Pattern, counts: dict, limit: int | None = None) -> int:
    """A planning count: the number of quads a resolved pattern selects,
    from ``count_quads`` — the row selection's size, a fraction of a match's
    cost, and no columns — memoized in ``counts`` across the patterns of one
    block. Zero for an unsatisfiable pattern.

    It reads the quad pattern alone: narrowing it by the pattern's native
    keep costs the native layer a pass over the selected rows, more than an
    estimate is worth, and it decides nothing a narrowed match would not
    repair; a ``distinct`` pattern's quads bound its triples. With ``limit``
    the count stops there: an answer that reaches it is a lower bound, which
    is never memoized as the count. An answer is :func:`_exact_count`'s.

    With ``SETTINGS.count_order`` off (``VORTEX_RDF_COUNT_ORDER=0``) nothing
    is counted: the answer is the pattern's static estimate.
    """
    if pat.unsatisfiable:
        return 0
    if not SETTINGS.count_order:
        return _static_estimate(pat)
    key = pat.n3
    n = counts.get(key)
    if n is None:
        n = boundary.native(store).count_quads(*pat.n3, **_narrowing(None, limit))
        if limit is None or n < limit:
            counts[key] = n
    return n


def _exact_count(store, pat: Pattern) -> int:
    """The rows a resolved pattern's match holds, as an answer: its quads
    narrowed by its native keep (``count_quads``, no columns), or a
    ``distinct`` pattern's match made a set (:func:`_match_resolved_pattern`,
    which leaves the pattern holding it). Zero for an unsatisfiable
    pattern."""
    if pat.unsatisfiable:
        return 0
    if pat.distinct:
        _match_resolved_pattern(store, pat)
        return pat.nrows
    return boundary.native(store).count_quads(*pat.n3, **_narrowing(pat.nkeep))


def _static_estimate(pat: Pattern) -> int:
    """``VORTEX_RDF_COUNT_ORDER=0``: a planning estimate from the pattern's bound positions alone,
    in place of a native count — fewer rows for more bound positions, and never few enough that a
    connected step matches rather than probes (rdflib's nested loop)."""
    return 1 << (62 - 8 * sum(value is not None for value in pat.n3))


def _narrowing(keep=None, limit=None) -> dict:
    """The keyword narrowing of a native count or match: a non-empty keep,
    a limit — only what is set."""
    narrowing: dict = {}
    if keep:
        narrowing["keep"] = keep
    if limit is not None:
        narrowing["limit"] = limit
    return narrowing


def _match_resolved_pattern(store, pat: Pattern, memo=None, limit=None) -> tuple[int, int]:
    """One native match for a resolved pattern, narrowed by its native keep:
    sets its match (:meth:`Pattern.set_match`). Returns the
    native calls made (0 when ``memo`` — keyed by :func:`_pattern_key` —
    already held the columns, or the pattern is unsatisfiable) and the time
    they took when tracing. ``limit`` takes only the first rows of the
    match, in base order — the order an unlimited match returns them where
    no index serves it (see :func:`_limit_reaches_match`); a limited match
    neither reads nor fills the memo. A ``distinct`` pattern's match is made a
    set of triples, unless the limit cut it: a cut match keeps its rows, so
    its caller can tell it was cut by its ``limit`` rows (:func:`_keep_scan`
    drops one).

    Only the columns are shared through the memo, as read-only views;
    ``varpos`` stays per pattern, since two triples matching the same rows
    still bind different variables.
    """
    then = trace.stopwatch()
    if pat.unsatisfiable:
        pat.set_match((), 0)
        return 0, 0
    key = _pattern_key(pat)
    nkeep = pat.nkeep
    cols = memo.get(key) if memo is not None and limit is None else None
    calls = 0
    if cols is None:
        native = boundary.native(store)
        if nkeep or limit is not None:
            cols = native.match_codes(*pat.n3, **_narrowing(nkeep, limit))
        else:
            cols = native.match_codes(*pat.n3)
        if cols is None:
            raise NotImplementedError
        cut = limit is not None and len(cols[0]) >= limit
        if pat.distinct and not cut:
            cols = union.distinct_rows(cols)
        if memo is not None and limit is None:
            memo[key] = cols
        calls = 1
    pat.set_match(cols, len(cols[0]))
    return calls, then()


def _limit_reaches_match(store, pat: Pattern, residual) -> bool:
    """Whether a single pattern's rows are its native match's rows one for
    one, in the match's own order — so a LIMIT may stop the match itself.

    Nothing may be left to drop in Python (a residual conjunct, a repeated
    variable's equality), and the store must hold no secondary index: the
    native layer windows a match in base order, which is the order an
    unlimited match returns only when no index serves it — and rdflib, which
    reads the pattern through ``triples()``, sees that order. A ``distinct``
    pattern's rows are its match's made a set, so a limit on the match would
    cut short the rows it keeps.
    """
    return not residual and not pat.distinct and not store._opened().indexes and not pat.repeats


def _pattern_terms(ctx, store, s, p, o, scope, index: int | None = None) -> Pattern:
    """Resolve one triple pattern against the context, without matching:
    the :class:`Pattern` of its positions as N-Triples strings (``None``
    where variable), the position(s) each variable-like term occupies, and
    whether it can match at all.

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
    # position; that pattern is unsatisfiable, not an error. So is one that
    # holds an IRI vortex-rdf's parser rejects, which is no term of the file.
    unsatisfiable = (
        (rs is not None and not isinstance(rs, (URIRef, BNode)))
        or (rp is not None and not isinstance(rp, URIRef))
        or unparseable_iri(rs)
        or unparseable_iri(rp)
        or unparseable_iri(ro)
    )

    varpos: dict = {}
    for idx, (term, value) in enumerate(zip((s, p, o), (rs, rp, ro), strict=True)):
        if value is None:
            varpos.setdefault(term, []).append(idx)

    if scope.var is not None:
        varpos.setdefault(scope.var, []).append(3)

    n3 = (*(store._node_to_n3(v) for v in (rs, rp, ro)), scope.n3)
    distinct = scope.n3 is None and scope.var is None and store._union_has_repeats()
    pat = Pattern(n3, varpos, unsatisfiable, index=index, distinct=distinct)
    if scope.var is not None:
        _exclude_default_graph(store, pat)
    return pat


def resolve(ctx, store, triple, scope, var_preds=None, index: int | None = None) -> Pattern:
    """``triple`` resolved (:func:`_pattern_terms`), with the conjuncts of
    ``var_preds`` over its variables (:func:`restrict`)."""
    pat = _pattern_terms(ctx, store, *triple, scope=scope, index=index)
    if var_preds:
        restrict(store, pat, var_preds)
    return pat


def restrict(store, pat: Pattern, var_preds) -> None:
    """Give a resolved pattern the conjuncts of ``var_preds`` over its
    variables: those vortex-rdf answers with a keep pushed into its native
    match (:func:`_push_native_restrictions`), the others deferred to the
    codes it brings (``Pattern.deferred``)."""
    pat.deferred = _push_native_restrictions(store, pat, var_preds)


def _exclude_default_graph(store, pat: Pattern) -> None:
    """Drop the default graph's rows from a pattern a ``GRAPH ?g`` scopes,
    inside its native match.

    A ``GRAPH`` clause ranges over the *named* graphs, and there is no "any
    named graph" pattern — but the default graph's empty name sorts before
    every term, so its code is 0 and the named graphs are the code range
    above it: a keep on the graph column, which matches, probes and exact
    counts apply. A file with no default-graph rows has no such code and
    needs no restriction at all.
    """
    default = store._opened().codes.default_graph_code
    if default is None:
        return
    if default != 0:
        raise NotImplementedError  # not a dictionary in byte order
    _set_native_keep(pat, 3, (1, len(store._opened().codes.term_dict)))


def _set_native_keep(pat: Pattern, position: int, keep) -> None:
    """Narrow the pattern's native match at ``position`` by ``keep`` (a code
    range or an ascending code column), on top of any keep already there.
    The pattern gets a new keep mapping, so one taken before stays whole."""
    nkeep = dict(pat.nkeep)
    if position in nkeep:
        keep = filters.native.intersect_keeps([nkeep[position], keep])
    nkeep[position] = keep
    pat.nkeep = nkeep


def _keep_only(pat: Pattern, var, codes) -> None:
    """Narrow the pattern's native match to ``codes``, ascending dictionary
    codes, at every position ``var`` takes (:func:`_set_native_keep`)."""
    keep = U64Column(codes)
    for position in pat.varpos[var]:
        _set_native_keep(pat, position, keep)


def narrow_to(pat: Pattern, rel: Relation) -> None:
    """Narrow ``pat``'s native match, at every position of a variable the
    relation binds, to the dictionary codes it binds that variable to —
    a keep-scan's narrowing, for a BGP step and a block's side alike. A
    columnar body's codes are the dictionary's, read natively; a row body
    may hold a query constant outside the dictionary (a negative code), which
    joins nothing and is left out, and a variable bound to no dictionary
    code makes the pattern unsatisfiable."""
    for var in pat.varpos:
        if var not in rel.schema:
            continue
        index = rel.schema.index(var)
        if rel.cols is not None:
            codes = sorted(relation._distinct_codes(rel.cols[index]))
        else:
            assert rel.rows is not None, "a keep-scan narrows to a held body"
            codes = sorted(code for code in {row[index] for row in rel.rows} if code >= 0)
        if not codes:
            pat.unsatisfiable = True
            return
        _keep_only(pat, var, codes)


def _push_native_restrictions(store, pat: Pattern, var_preds) -> dict:
    """Move the conjuncts vortex-rdf answers with a keep — kind tests and IRI
    equality (:func:`filters.native.native_restriction`) — into the pattern's native
    keep, at every position their variable takes; returns the residual
    conjuncts per variable, evaluated over the codes the pattern brings
    (:func:`_restrict_pattern`, :func:`_probe_join`).

    The keep is exact, so it may narrow a variable the relation will already
    bind when the pattern joins: the rows it drops could not have joined. A
    code column left empty (IRIs the dictionary lacks, an empty ``IN`` list,
    lists with no IRI in common) makes the pattern unsatisfiable, so nothing
    is counted or matched for it.
    """
    residual: dict = {}
    for var, positions in pat.varpos.items():
        conjuncts = var_preds.get(var)
        if not conjuncts:
            continue
        keep, rest = filters.native.native_restriction(store, conjuncts)
        if keep is not None:
            for position in positions:
                _set_native_keep(pat, position, keep)
            if trace.active() is not None:
                trace.event(
                    "bgp_native_restriction",
                    original_pattern_index=pat.index,
                    variable=str(var),
                    pushed_predicate_count=len(conjuncts) - len(rest),
                    residual_predicate_count=len(rest),
                    keep=trace.keep_summary({positions[0]: keep}),
                )
        if rest:
            residual[var] = rest
    if any(not is_range(k) and not len(k) for k in pat.nkeep.values()):
        pat.unsatisfiable = True
    return residual


def _restrict_pattern(store, pat: Pattern, var_preds) -> None:
    """Evaluate per-variable conjuncts over the distinct codes of a matched
    pattern's columns (:func:`filters.evaluate_column`: vortex-rdf answers the
    ones with a native shape over exactly those codes) and compact the match
    to the rows that pass (:func:`_compact_pattern`), so it stays columnar and
    its real size is what a join sees."""
    if pat.nrows == 0:
        return
    traced = trace.active() is not None
    keep: dict = {}
    input_rows = pat.nrows
    records = []
    cols = pat.cols
    assert cols is not None, "a pattern is restricted once matched"
    for var, positions in pat.varpos.items():
        conjuncts = var_preds.get(var)
        if not conjuncts:
            continue
        elapsed = trace.stopwatch()
        values = set(relation._distinct_codes(cols[positions[0]]))
        distinct_binding_count = len(values)
        allowed = filters.evaluate_column(store, conjuncts, values)
        if len(allowed) < distinct_binding_count:
            keep[var] = allowed
        if traced:
            records.append(
                {
                    "variable": str(var),
                    "mode": "match",
                    **filters.restriction_counts(conjuncts),
                    "distinct_binding_count": distinct_binding_count,
                    "allowed_binding_count": len(allowed),
                    "elapsed_ns": elapsed(),
                }
            )
    if keep:
        _compact_pattern(pat, keep)
    for record in records:
        trace.event(
            "bgp_restriction_complete",
            original_pattern_index=pat.index,
            input_rows=input_rows,
            output_rows=pat.nrows,
            **record,
        )


def _compact_pattern(pat: Pattern, keep: dict[Variable, set[int]]) -> None:
    """Keep only the matched rows whose code at each variable of ``keep`` is
    in that variable's allowed set, with the native kernels: ``join_indices``
    pairs the variable's column with the allowed codes, an ascending column of
    unique codes, so a row appears at most once and the rows come in the
    nested-loop order of the join, which is row order; ``take`` then gathers
    every column a variable reads by those rows. A repeated variable is tested
    at its first position, as ``_materialize`` reads it. The columns of
    constant positions are dropped (``None``): nothing reads them."""
    varpos = pat.varpos
    positions = pat.positions
    if not positions:
        return
    matched = pat.cols
    assert matched is not None, "a pattern is compacted once matched"
    columns = {idx: relation._native_column(matched[idx]) for idx in positions}
    for var, allowed in keep.items():
        if not allowed:
            columns = {idx: U64Column([]) for idx in positions}
            break
        rows, _ = columns[varpos[var][0]].join_indices(U64Column(sorted(allowed)))
        if len(rows) != len(columns[positions[0]]):
            columns = {idx: column.take(rows) for idx, column in columns.items()}
    pat.cols = tuple(columns.get(idx) for idx in range(4))
    pat.nrows = len(columns[positions[0]])


def restricted(store, pat: Pattern, bound, stats=None) -> Relation:
    """A matched pattern as the relation it joins with: its deferred
    per-variable restrictions applied over the variables not in ``bound`` —
    the relation it is about to join, which restricted its own — compacting
    it natively to the rows that pass (:func:`_restrict_pattern`), so its
    real size, not the match's, is what the join sees. Their time is booked
    on ``stats`` (``restriction_ns``) when given."""
    clock = trace.stopwatch()
    pending, pat.deferred = pat.deferred, {}
    if pending:
        pending = {v: conjuncts for v, conjuncts in pending.items() if v not in bound}
    if pending:
        _restrict_pattern(store, pat, pending)
    if stats is not None:
        stats.restriction_ns += clock()
    return _rel_from_pattern(pat)


def _rel_from_pattern(pat: Pattern) -> Relation:
    """A matched pattern as a relation — columnar when it can be.

    The zero-copy views are kept as the body; nothing is copied into Python
    objects until rows are consumed. Repeated variables (``?x :p ?x``) and
    ground patterns go through ``_materialize`` instead.
    """
    varpos = pat.varpos
    if pat.nrows == 0 or not varpos or pat.repeats:
        return Relation.from_rows(*_materialize(pat))
    schema = tuple(varpos)
    matched = pat.cols
    assert matched is not None, "a pattern becomes a relation once matched"
    cols = tuple(memoryview(matched[pos[0]]).cast("Q") for pos in varpos.values())
    return Relation(schema, cols, None, pat.nrows)


def _materialize(pat: Pattern):
    """A matched pattern as ``(schema, rows)`` of u64 code tuples.

    Same-variable repeats (e.g. ``?x :p ?x``) become a row filter; a repeated
    variable keeps its first position. An empty match still names its
    variables, so the operators above can pad or project by schema.
    """
    varpos = pat.varpos
    schema = tuple(varpos)
    if pat.nrows == 0:
        return schema, []
    if not schema:
        # Fully ground pattern: no bindings, one (empty) row per match, so a
        # non-matching ground pattern still eliminates all solutions.
        return (), [()] * pat.nrows

    matched = pat.cols
    assert matched is not None, "a pattern is materialized once matched"
    views = {idx: memoryview(matched[idx]).cast("Q").tolist() for idx in pat.positions}
    eq_checks = [(pos[0], later) for pos in varpos.values() for later in pos[1:]]
    if eq_checks:
        keep = [
            i for i in range(pat.nrows) if all(views[a][i] == views[b][i] for a, b in eq_checks)
        ]
        views = {idx: [column[i] for i in keep] for idx, column in views.items()}
    return schema, list(zip(*(views[pos[0]] for pos in varpos.values()), strict=True))
