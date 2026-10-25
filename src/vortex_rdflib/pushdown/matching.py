"""One triple pattern (:class:`Pattern`): resolved against the context into
a quad pattern, narrowed by the keeps its FILTER conjuncts are, counted
and matched natively, restricted by the conjuncts that are not keeps, and
read as a relation.
"""

from dataclasses import dataclass, replace

from rdflib.term import BNode, Literal, URIRef, Variable
from vortex_rdf import U64Column

from .. import filters
from ..cancel import CANCEL_TOKEN, cancel_kw_for
from ..terms import unparseable_iri
from . import relation, tracing
from .algebra import _VAR_LIKE
from .relation import Relation
from .tracing import _TRACE


@dataclass(slots=True, eq=False)
class Pattern:
    """One triple pattern of a block, resolved against the context
    (:func:`_pattern_terms`), and what planning learns about it.

    ``n3`` is the quad pattern the native layer matches: each position's
    N-Triples spelling, ``None`` where it is open; the fourth is the graph.
    ``varpos`` maps each variable-like term to the position(s) it takes.
    ``unsatisfiable`` says no quad can match (a literal in subject or
    predicate position, an IRI the parser rejects, a keep left empty).
    ``index`` is its place in the BGP, ``None`` outside one. ``nkeep``
    narrows its native match per position (a code range, or an ascending
    code column). ``deferred`` maps variables to the conjuncts still to be
    evaluated over the codes the pattern brings. Both mappings are replaced
    when they change, never mutated (:func:`_set_native_keep`), so a keep
    taken before stays whole and a copy (:meth:`copy`) shares them safely.

    Planning sets ``estimate`` (its count, ``None`` while uncounted; a lower
    bound when ``estimate_capped``) or marks it ``probe`` (left uncounted
    next to a small pattern, :func:`_count_patterns`). A match sets ``cols``
    (the raw code columns, ``None`` at a position nothing reads; none for an
    unsatisfiable pattern) and ``nrows`` (:meth:`set_match`); ``matched``
    says whether it holds one.
    ``match_rows`` and ``probe_rows`` are for the trace: the rows of its match
    before its restrictions, and the rows its probes kept after theirs.

    Identity is the pattern's: two patterns that read alike are still two.
    """

    n3: list
    varpos: dict
    unsatisfiable: bool = False
    index: int | None = None
    nkeep: dict | None = None
    deferred: dict | None = None
    estimate: int | None = None
    estimate_capped: bool = False
    probe: bool = False
    matched: bool = False
    cols: tuple = ()
    nrows: int = 0
    match_rows: int | None = None
    probe_rows: int | None = None

    def set_match(self, cols: tuple, nrows: int) -> None:
        """Hold a match: its columns and row count."""
        self.cols, self.nrows, self.matched = cols, nrows, True

    def copy(self) -> "Pattern":
        """A copy for a join step to work on. The mappings a step changes
        (``nkeep``, ``deferred``) are replaced when they change, never
        mutated, so the copy may share them."""
        return replace(self)

    def clear_match(self) -> None:
        """Let go of the match: the pattern is unread again."""
        self.cols, self.nrows, self.matched, self.match_rows = (), 0, False, None


def _pattern_static_rank(pat: Pattern) -> tuple:
    """A pattern's likely selectivity from its shape alone — more bound
    positions first, then repeated variables, then query order. It breaks
    ties between equal counts; the counts themselves decide the plan."""
    n3 = pat.n3
    repeated = sum(len(pos) - 1 for pos in pat.varpos.values())
    return (-sum(value is not None for value in n3), -repeated, pat.index or 0)


def _pattern_key(pat: Pattern) -> tuple:
    """What a pattern's native count and match are memoized by: the resolved
    quad pattern and its native keep (two triples resolving to the same quad
    share both only when they are narrowed alike). A code column stands in
    the key itself, compared by identity: the key holds it, so no later
    column can take its place while the memo keeps the entry."""
    nkeep = pat.nkeep
    if not nkeep:
        return tuple(pat.n3)
    narrowing = tuple(
        sorted(
            (position, ("range", *keep) if isinstance(keep, tuple) else ("codes", keep))
            for position, keep in nkeep.items()
        )
    )
    return (*pat.n3, narrowing)


def _count_pattern(
    store, pat: Pattern, counts: dict, exact: bool = False, limit: int | None = None
) -> int:
    """The number of quads a resolved pattern selects, from ``count_quads``
    — the row selection's size, a fraction of a match's cost, and no
    columns. Memoized across the patterns of one block, like the matches.
    Zero for an unsatisfiable pattern.

    A planning count (the default) reads the quad pattern alone: narrowing
    it by the pattern's native keep costs the native layer a pass over the
    selected rows, more than an estimate is worth, and it decides nothing a
    narrowed match would not repair. An ``exact`` count — an answer, or a
    size a join decision hinges on — narrows by the keep. With ``limit`` the
    count stops there: an answer that reaches it is a lower bound, which is
    never memoized as the count. A count already memoized is returned whole.
    """
    if pat.unsatisfiable:
        return 0
    nkeep = pat.nkeep if exact else None
    key = _pattern_key(pat) if nkeep else tuple(pat.n3)
    n = counts.get(key)
    if n is None:
        token = CANCEL_TOKEN.get()
        cancel = {} if token is None else cancel_kw_for({"cancel": token}, store, pat.n3, limit)
        n = tracing._native(store).count_quads(*pat.n3, **_narrowing(nkeep, limit), **cancel)
        if limit is None or n < limit:
            counts[key] = n
    return n


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
    neither reads nor fills the memo.

    Only the columns are shared through the memo, as read-only views;
    ``varpos`` stays per pattern, since two triples matching the same rows
    still bind different variables.
    """
    then = tracing._stopwatch()
    if pat.unsatisfiable:
        pat.set_match((), 0)
        return 0, 0
    nkeep = pat.nkeep
    key = _pattern_key(pat) if nkeep else tuple(pat.n3)
    cols = memo.get(key) if memo is not None and limit is None else None
    calls = 0
    if cols is None:
        native = tracing._native(store)
        token = CANCEL_TOKEN.get()
        cancel = {} if token is None else cancel_kw_for({"cancel": token}, store, pat.n3, limit)
        if nkeep or limit is not None:
            cols = native.match_codes(*pat.n3, **_narrowing(nkeep, limit), **cancel)
        else:
            cols = native.match_codes(*pat.n3, **cancel)
        if cols is None:
            raise NotImplementedError
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
    reads the pattern through ``triples()``, sees that order.
    """
    return (
        not residual
        and not store._indexes
        and all(len(positions) == 1 for positions in pat.varpos.values())
    )


def _pattern_terms(ctx, store, s, p, o, scope) -> Pattern:
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

    n3 = [store._node_to_n3(v) for v in (rs, rp, ro)]
    n3.append(scope.n3)
    pat = Pattern(n3, varpos, unsatisfiable)
    if scope.var is not None:
        _exclude_default_graph(store, pat)
    return pat


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
    default = store._default_graph_code()
    if default is None:
        return
    if default != 0:
        raise NotImplementedError  # not a dictionary in byte order
    _set_native_keep(pat, 3, (1, len(store._dict)))


def _set_native_keep(pat: Pattern, position: int, keep) -> None:
    """Narrow the pattern's native match at ``position`` by ``keep`` (a code
    range or an ascending code column), on top of any keep already there.
    The pattern gets a new keep mapping, so one taken before stays whole."""
    nkeep = dict(pat.nkeep or {})
    if position in nkeep:
        keep = filters.native.intersect_keeps([nkeep[position], keep])
    nkeep[position] = keep
    pat.nkeep = nkeep


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
            if _TRACE.get() is not None:
                tracing._trace_event(
                    "bgp_native_restriction",
                    original_pattern_index=pat.index,
                    variable=str(var),
                    pushed_predicate_count=len(conjuncts) - len(rest),
                    residual_predicate_count=len(rest),
                    keep=tracing._keep_summary({positions[0]: keep}),
                )
        if rest:
            residual[var] = rest
    if any(not isinstance(k, tuple) and not len(k) for k in (pat.nkeep or {}).values()):
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
    traced = _TRACE.get() is not None
    keep: dict = {}
    input_rows = pat.nrows
    records = []
    for var, positions in pat.varpos.items():
        conjuncts = var_preds.get(var)
        if not conjuncts:
            continue
        elapsed = tracing._stopwatch()
        values = set(relation._distinct_codes(pat.cols[positions[0]]))
        distinct_binding_count = len(values)
        allowed = filters.evaluate_column(store, conjuncts, values)
        if len(allowed) < distinct_binding_count:
            keep[var] = allowed
        if traced:
            records.append(
                {
                    "variable": str(var),
                    "mode": "match",
                    **tracing._restriction_counts(conjuncts),
                    "distinct_binding_count": distinct_binding_count,
                    "allowed_binding_count": len(allowed),
                    "elapsed_ns": elapsed(),
                }
            )
    if keep:
        _compact_pattern(pat, keep)
    for record in records:
        tracing._trace_event(
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
    positions = sorted({idx for pos in varpos.values() for idx in pos})
    if not positions:
        return
    columns = {idx: relation._native_column(pat.cols[idx]) for idx in positions}
    for var, allowed in keep.items():
        if not allowed:
            columns = {idx: U64Column([]) for idx in positions}
            break
        rows, _ = columns[varpos[var][0]].join_indices(U64Column(sorted(allowed)))
        if len(rows) != len(columns[positions[0]]):
            columns = {idx: column.take(rows) for idx, column in columns.items()}
    pat.cols = tuple(columns.get(idx) for idx in range(4))
    pat.nrows = len(columns[positions[0]])


def _rel_from_pattern(pat: Pattern) -> Relation:
    """A matched pattern as a relation — columnar when it can be.

    The zero-copy views are kept as the body; nothing is copied into Python
    objects until rows are consumed. Repeated variables (``?x :p ?x``) and
    ground patterns go through ``_materialize`` instead.
    """
    varpos = pat.varpos
    if pat.nrows == 0 or not varpos or any(len(pos) > 1 for pos in varpos.values()):
        return Relation.from_rows(*_materialize(pat))
    schema = tuple(varpos)
    cols = tuple(memoryview(pat.cols[pos[0]]).cast("Q") for pos in varpos.values())
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

    needed = sorted({idx for pos in varpos.values() for idx in pos})
    views = {idx: memoryview(pat.cols[idx]).cast("Q").tolist() for idx in needed}
    eq_checks = [(pos[0], later) for pos in varpos.values() for later in pos[1:]]
    if eq_checks:
        keep = [
            i for i in range(pat.nrows) if all(views[a][i] == views[b][i] for a, b in eq_checks)
        ]
        views = {idx: [column[i] for i in keep] for idx, column in views.items()}
    return schema, list(zip(*(views[pos[0]] for pos in varpos.values()), strict=True))
