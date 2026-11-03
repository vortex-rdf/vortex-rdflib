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
from . import algebra, aliasing, blocks, head, matching, relation
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
    block = algebra.block(group.p)
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
    # none — no grouping, and every count the rows' count: the row
    # selection's size.
    pat = head._single_pattern(ctx, store, block, scope) if group_vars is None else None
    if pat is not None and not pat.repeats:
        specs = _count_specs(ctx, {v: i for i, v in enumerate(pat.varpos)}, counts)
        if all(_counts_rows(spec) for spec in specs):
            n = Literal(matching._exact_count(store, pat))
            return iter([FrozenBindings(ctx, {spec.res: n for spec in specs})])

    rel = blocks._solve_block(ctx, store, block, blocks._Frame(scope))
    return _aggregate_rows(ctx, store, rel, group_vars or [], counts, samples, group.expr is None)


@dataclass(frozen=True, slots=True)
class _Fixed:
    """A COUNT a group's size decides: its rows (``of_rows``) — ``COUNT(*)``,
    or ``COUNT(?v)`` of a variable the context binds, in every row — or
    ``value`` — 1 for ``COUNT(DISTINCT ?v)`` of such a variable, one term,
    and 0 for a variable bound nowhere."""

    res: Any
    of_rows: bool
    value: int = 0


@dataclass(frozen=True, slots=True)
class _ColumnCount:
    """``COUNT(?v)`` — its bound values — or ``COUNT(DISTINCT ?v)`` — its
    distinct terms — of a variable the block binds, column ``col``."""

    res: Any
    col: int
    distinct: bool


@dataclass(frozen=True, slots=True)
class _DistinctRows:
    """``COUNT(DISTINCT *)``: a group's distinct rows."""

    res: Any


def _count_specs(ctx, col: dict, counts) -> list:
    """Each ``(res, target, distinct)`` COUNT as the case that answers it,
    over a body whose columns ``col`` maps by variable."""
    specs: list = []
    for res, target, distinct in counts:
        if target == "*":
            specs.append(_DistinctRows(res) if distinct else _Fixed(res, True))
        elif target in col:
            specs.append(_ColumnCount(res, col[target], distinct))
        elif ctx[target] is not None:
            specs.append(_Fixed(res, False, 1) if distinct else _Fixed(res, True))
        else:
            specs.append(_Fixed(res, False))
    return specs


def _counts_rows(spec) -> bool:
    """Whether a COUNT over one pattern's match is its row count: every
    column of a match is bound."""
    if isinstance(spec, _Fixed):
        return spec.of_rows
    return isinstance(spec, _ColumnCount) and not spec.distinct


@dataclass(slots=True)
class _Group:
    """One group's rows, and a tally per counted COUNT (a
    :class:`_ColumnCount` or :class:`_DistinctRows`): a count of bound
    values, or the set of codes (rows, for ``COUNT(DISTINCT *)``) seen."""

    rows: int
    tallies: list


def _aggregate_rows(ctx, store, rel: Relation, group_vars, counts, samples, implicit_group):
    schema = rel.schema
    col = {v: i for i, v in enumerate(schema)}
    # A group key part is a column; a context-bound variable does not split
    # groups and an unbound one contributes a None key part (as rdflib's).
    key_cols = [col[v] for v in group_vars if v in col]
    specs = _count_specs(ctx, col, counts)
    counted = [spec for spec in specs if not isinstance(spec, _Fixed)]
    sets = [isinstance(spec, _DistinctRows) or spec.distinct for spec in counted]

    def new_group(rows=0):
        return _Group(rows, [set() if distinct else 0 for distinct in sets])

    groups: dict[tuple, _Group] = {}
    if rel.cols is not None and not rel.preds and len(key_cols) <= 1:
        groups = _columnar_groups(rel, key_cols, counted, new_group)
    else:
        row_sets = [k for k, spec in enumerate(counted) if isinstance(spec, _DistinctRows)]
        column_tallies = [
            (k, spec.col, spec.distinct)
            for k, spec in enumerate(counted)
            if isinstance(spec, _ColumnCount)
        ]
        for row in relation._code_rows(rel):
            key = tuple(row[i] for i in key_cols)
            group = groups.get(key)
            if group is None:
                group = groups[key] = new_group()
            group.rows += 1
            tallies = group.tallies
            for k in row_sets:
                tallies[k].add(row)
            for k, i, distinct in column_tallies:
                code = row[i]
                if code is not None:
                    if distinct:
                        tallies[k].add(code)
                    else:
                        tallies[k] += 1

    if not groups:
        if implicit_group:
            return iter([FrozenBindings(ctx, {res: Literal(0) for res, _, _ in counts})])
        return iter([FrozenBindings(ctx)])

    # Merge groups whose decoded keys are equal terms; distinct counts likewise
    # count equal terms once (see `_alias_codes`).
    codes = {c for key in groups for c in key if c is not None}
    for group in groups.values():
        for spec, tally in zip(counted, group.tallies, strict=True):
            if isinstance(spec, _DistinctRows):
                codes.update(c for row in tally for c in row if c is not None)
            elif spec.distinct:
                codes.update(tally)
    foreign = store._terms().foreign
    aliases: dict = {}
    aliasing._alias_codes(store, codes, aliases)

    merged: dict[tuple, tuple[tuple, _Group]] = {}
    for key, group in groups.items():
        tkey = tuple(aliases.get(c, c) for c in key)
        first = merged.get(tkey)
        if first is None:
            merged[tkey] = (key, group)
            continue
        base = first[1]
        base.rows += group.rows
        for k, distinct in enumerate(sets):
            if distinct:
                base.tallies[k] |= group.tallies[k]
            else:
                base.tallies[k] += group.tallies[k]

    key_index = {v: i for i, v in enumerate(v for v in group_vars if v in col)}
    cached = store._prime_decode_cache(
        [c for key, _ in merged.values() for c in key if c is not None and c >= 0]
    )

    def count(spec, tally) -> int:
        if isinstance(spec, _DistinctRows):
            if aliases:
                return len({tuple(aliases.get(c, c) for c in row) for row in tally})
            return len(tally)
        if spec.distinct and aliases:
            return len({aliases.get(c, c) for c in tally})
        return len(tally) if spec.distinct else tally

    fixed = [spec for spec in specs if isinstance(spec, _Fixed)]

    def solutions():
        for key, group in merged.values():
            row: dict = {}
            for spec in fixed:
                row[spec.res] = Literal(group.rows if spec.of_rows else spec.value)
            for spec, tally in zip(counted, group.tallies, strict=True):
                row[spec.res] = Literal(count(spec, tally))
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


def _columnar_groups(rel: Relation, key_cols, counted, new_group) -> dict:
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
    for k, spec in enumerate(counted):
        if isinstance(spec, _DistinctRows):
            rows = set(zip(*(col.tolist() for col in cols), strict=True))
            for row in rows:
                groups[tuple(row[i] for i in key_cols)].tallies[k].add(row)
        elif not spec.distinct:
            for group in groups.values():
                group.tallies[k] = group.rows
        else:
            column = relation._native_column(cols[spec.col])
            if key_col is None:
                groups[()].tallies[k] = set(relation._distinct_codes(column))
            elif len(groups) <= SETTINGS.native_groups_max:
                if members is None:
                    members = {key: key_col.join_indices(U64Column([key[0]]))[0] for key in groups}
                for key, rows in members.items():
                    groups[key].tallies[k] = set(relation._distinct_codes(column.take(rows)))
            else:
                if keys is None:
                    keys = memoryview(key_col).cast("Q").tolist()
                for key, code in set(zip(keys, memoryview(column).cast("Q").tolist(), strict=True)):
                    groups[(key,)].tallies[k].add(code)
    return groups
