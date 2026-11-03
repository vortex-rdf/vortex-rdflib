"""``COUNT`` aggregates over a block (:func:`_eval_aggregate`): counted over
the code columns, a column at a time by the native kernels where the body
is columnar, with only the group keys decoded.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

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
        cases = _count_cases(ctx, {v: i for i, v in enumerate(pat.varpos)}, counts)
        if cases.of_rows:
            n = Literal(matching._exact_count(store, pat))
            return iter([FrozenBindings(ctx, {res: n for res, _, _ in counts})])

    rel = blocks._solve_block(ctx, store, block, blocks._Frame(scope))
    return _aggregate_rows(ctx, store, rel, group_vars or [], counts, samples, group.expr is None)


@dataclass(frozen=True, slots=True)
class _RowCount:
    """``COUNT(*)``, or ``COUNT(?v)`` of a variable the context binds, in
    every row: a group's rows."""

    res: Any
    #: Whether the COUNT of one pattern's match — every column bound — is
    #: the match's row count.
    of_rows: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class _Constant:
    """A COUNT a group's rows leave as it is: ``COUNT(DISTINCT ?v)`` of a
    variable the context binds — one term, ``value`` 1 — or a COUNT of a
    variable bound nowhere — ``value`` 0."""

    res: Any
    value: int
    of_rows: ClassVar[bool] = False


@dataclass(frozen=True, slots=True)
class _ColumnCount:
    """``COUNT(?v)`` — its bound values — or ``COUNT(DISTINCT ?v)`` — its
    distinct terms — of a variable the block binds, column ``col``. A
    group's tally is the count, or the set of codes seen."""

    res: Any
    col: int
    distinct: bool

    @property
    def of_rows(self) -> bool:
        """``COUNT(?v)`` of one pattern's match, every column bound, is its
        row count."""
        return not self.distinct

    @property
    def new_tally(self) -> type:
        """What makes a group's empty tally: ``set``, or ``int`` (0)."""
        return set if self.distinct else int

    def merge(self, tally, other):
        """The tally of two groups made one."""
        return tally | other if self.distinct else tally + other

    def add_codes(self, codes: set, tallies) -> None:
        """Add to ``codes`` the codes ``tallies`` — one per group — count
        as terms, which aliases may merge."""
        if self.distinct:
            codes.update(*tallies)

    def count(self, tally, aliases: dict) -> int:
        """The COUNT of a group's ``tally``, equal terms (``aliases``) once."""
        if not self.distinct:
            return tally
        if aliases:
            return len({aliases.get(c, c) for c in tally})
        return len(tally)


@dataclass(frozen=True, slots=True)
class _DistinctRows:
    """``COUNT(DISTINCT *)``: a group's distinct rows, its tally the set of
    them."""

    res: Any
    of_rows: ClassVar[bool] = False
    #: What makes a group's empty tally.
    new_tally: ClassVar[type] = set

    def merge(self, tally, other):
        """The tally of two groups made one."""
        return tally | other

    def add_codes(self, codes: set, tallies) -> None:
        """Add to ``codes`` the codes ``tallies`` — one per group — count
        as terms, which aliases may merge."""
        codes.update({c for tally in tallies for row in tally for c in row if c is not None})

    def count(self, tally, aliases: dict) -> int:
        """The COUNT of a group's ``tally``, rows of equal terms
        (``aliases``) once."""
        if aliases:
            return len({tuple(aliases.get(c, c) for c in row) for row in tally})
        return len(tally)


@dataclass(slots=True)
class _Counts:
    """An aggregate's COUNTs, each as the case that answers it
    (:func:`_count_cases`): those a group's row count answers
    (``row_counts``), the ``constants``, and those ``tallied`` over a
    group's rows — with the row loop's index lists of the tallied ones: the
    positions of the ``COUNT(DISTINCT *)``s (``row_sets``) and ``(position,
    column, distinct)`` of each column's (``column_tallies``)."""

    row_counts: list = field(default_factory=list)
    constants: list = field(default_factory=list)
    tallied: list = field(default_factory=list)
    row_sets: list = field(default_factory=list)
    column_tallies: list = field(default_factory=list)

    @property
    def of_rows(self) -> bool:
        """Whether every COUNT of one pattern's match is its row count."""
        return all(case.of_rows for case in self.row_counts + self.constants + self.tallied)


def _count_cases(ctx, col: dict, counts) -> _Counts:
    """Each ``(res, target, distinct)`` COUNT as the case that answers it,
    over a body whose columns ``col`` maps by variable."""
    cases = _Counts()
    for res, target, distinct in counts:
        if target == "*":
            if distinct:
                cases.row_sets.append(len(cases.tallied))
                cases.tallied.append(_DistinctRows(res))
            else:
                cases.row_counts.append(_RowCount(res))
        elif target in col:
            cases.column_tallies.append((len(cases.tallied), col[target], distinct))
            cases.tallied.append(_ColumnCount(res, col[target], distinct))
        elif ctx[target] is None:
            cases.constants.append(_Constant(res, 0))
        elif distinct:
            cases.constants.append(_Constant(res, 1))
        else:
            cases.row_counts.append(_RowCount(res))
    return cases


@dataclass(slots=True)
class _Group:
    """One group's rows, and a tally per tallied COUNT (a
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
    cases = _count_cases(ctx, col, counts)
    tallied = cases.tallied
    new_tallies = [case.new_tally for case in tallied]

    def new_group(rows=0):
        return _Group(rows, [new_tally() for new_tally in new_tallies])

    groups: dict[tuple, _Group] = {}
    if rel.cols is not None and not rel.preds and len(key_cols) <= 1:
        groups = _columnar_groups(rel, key_cols, cases, new_group)
    else:
        row_sets, column_tallies = cases.row_sets, cases.column_tallies
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
    for k, case in enumerate(tallied):
        case.add_codes(codes, [group.tallies[k] for group in groups.values()])
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
        base.tallies = [
            case.merge(tally, other)
            for case, tally, other in zip(tallied, base.tallies, group.tallies, strict=True)
        ]

    key_index = {v: i for i, v in enumerate(v for v in group_vars if v in col)}
    cached = store._prime_decode_cache(
        [c for key, _ in merged.values() for c in key if c is not None and c >= 0]
    )

    constants = {case.res: Literal(case.value) for case in cases.constants}
    row_counts = [case.res for case in cases.row_counts]

    def solutions():
        for key, group in merged.values():
            row: dict = dict(constants)
            if row_counts:
                n = Literal(group.rows)
                for res in row_counts:
                    row[res] = n
            for case, tally in zip(tallied, group.tallies, strict=True):
                row[case.res] = Literal(case.count(tally, aliases))
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


def _columnar_groups(rel: Relation, key_cols, cases: _Counts, new_group) -> dict:
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
    for k in cases.row_sets:
        rows = set(zip(*(col.tolist() for col in cols), strict=True))
        for row in rows:
            groups[tuple(row[i] for i in key_cols)].tallies[k].add(row)
    members = keys = None
    for k, i, distinct in cases.column_tallies:
        if not distinct:
            for group in groups.values():
                group.tallies[k] = group.rows
            continue
        column = relation._native_column(cols[i])
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
