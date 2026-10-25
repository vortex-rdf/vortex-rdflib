"""``COUNT`` aggregates over a block (:func:`_eval_aggregate`): counted over
the code columns, a column at a time by the native kernels where the body
is columnar, with only the group keys decoded.
"""

from dataclasses import dataclass
from typing import Any

from rdflib.plugins.sparql.sparql import FrozenBindings
from rdflib.term import Literal, Variable
from vortex_rdf import U64Column

from ..settings import SETTINGS
from . import algebra, aliasing, blocks, matching, relation
from .algebra import _VAR_LIKE
from .relation import Relation


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
    algebra._check_block(block)
    block, scope = algebra._peel_graph(ctx, block, algebra._active_scope(ctx, store))
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

    # One pattern — under a FILTER the native match decides entirely, or
    # none — no grouping, plain counts: the row selection's size.
    pat = blocks._single_pattern(ctx, store, block, scope) if group_vars is None else None
    if (
        pat is not None
        and all(len(pos) == 1 for pos in pat.varpos.values())
        and all(
            not distinct and (target == "*" or target in pat.varpos or ctx[target] is not None)
            for _, target, distinct in counts
        )
    ):
        n = matching._count_pattern(store, pat, {}, exact=True)
        return iter([FrozenBindings(ctx, {res: Literal(n) for res, _, _ in counts})])

    rel = blocks._solve_block(ctx, store, block, scope=scope)
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
    if rel.cols is not None and not rel.preds and len(key_cols) <= 1:
        groups = _columnar_groups(rel, key_cols, specs, new_group)
    else:
        for row in relation._code_rows(rel):
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
    # count equal terms once (see `_alias_codes`).
    codes = {c for key in groups for c in key if c is not None}
    for group in groups.values():
        for j, spec in enumerate(specs):
            if spec.distinct and spec.kind == "col":
                codes.update(group.sets[j])
            elif spec.distinct and spec.kind == "rows":
                codes.update(c for row in group.sets[j] for c in row if c is not None)
    foreign = rel.foreign
    aliases: dict = {}
    aliasing._alias_codes(store, codes, foreign, aliases)

    def distinct_count(values) -> int:
        if not aliases:
            return len(values)
        return len({aliases.get(c, c) for c in values})

    merged: dict[tuple, tuple[tuple, _Group]] = {}
    for key, group in groups.items():
        tkey = tuple(aliases.get(c, c) for c in key)
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
    cached = store._prime_decode_cache(
        [c for key, _ in merged.values() for c in key if c is not None and c >= 0]
    )

    def solutions():
        for key, group in merged.values():
            row: dict = {}
            for j, spec in enumerate(specs):
                if spec.kind == "rows":
                    if not spec.distinct:
                        n = group.rows
                    elif aliases:
                        n = len({tuple(aliases.get(c, c) for c in r) for r in group.sets[j]})
                    else:
                        n = len(group.sets[j])
                elif spec.kind == "col":
                    n = distinct_count(group.sets[j]) if spec.distinct else group.counts[j]
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


def _columnar_groups(rel: Relation, key_cols, specs, new_group) -> dict:
    """The groups of a columnar body by at most one key column, counted a
    column at a time.

    A columnar body holds no unbound value, so a group's ``COUNT(?v)`` is
    its row count, which the key column's native ``value_counts`` gives. A
    ``COUNT(DISTINCT ?v)`` takes each group's distinct codes of ``?v`` — for
    a handful of groups natively, gathering a group's rows by its key
    (``join_indices``, ``take``, ``distinct``), otherwise from the distinct
    (key, value) pairs.
    """
    cols = rel.cols
    assert cols is not None  # the caller's columnar case
    groups: dict = {}
    key_col = None
    if key_cols:
        key_col = relation._native_column(cols[key_cols[0]])
        values, tallies = key_col.value_counts()
        for code, n in zip(
            memoryview(values).cast("Q").tolist(),
            memoryview(tallies).cast("Q").tolist(),
            strict=True,
        ):
            groups[(code,)] = new_group(n)
    elif rel.nrows:
        groups[()] = new_group(rel.nrows)
    if not groups:
        return groups
    members = keys = None
    for j, spec in enumerate(specs):
        if spec.kind == "col" and not spec.distinct:
            for group in groups.values():
                group.counts[j] = group.rows
        elif spec.kind == "const" and spec.distinct:
            for group in groups.values():
                group.sets[j].add(())
        elif spec.kind == "col":
            column = relation._native_column(cols[spec.col])
            if key_col is None:
                groups[()].sets[j] = set(relation._distinct_codes(column))
            elif len(groups) <= SETTINGS.native_groups_max:
                if members is None:
                    members = {key: key_col.join_indices(U64Column([key[0]]))[0] for key in groups}
                for key, rows in members.items():
                    groups[key].sets[j] = set(relation._distinct_codes(column.take(rows)))
            else:
                if keys is None:
                    keys = memoryview(key_col).cast("Q").tolist()
                for key, code in set(zip(keys, memoryview(column).cast("Q").tolist(), strict=True)):
                    groups[(key,)].sets[j].add(code)
        elif spec.kind == "rows" and spec.distinct:
            rows = set(zip(*(col.tolist() for col in cols), strict=True))
            for row in rows:
                groups[tuple(row[i] for i in key_cols)].sets[j].add(row)
    return groups
