"""Code-space relations (:class:`Relation`): their bodies — columns, rows or
streamed pieces — read as rows, and the join kernels over them.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from operator import itemgetter

from vortex_rdf import JoinTooLarge, U64Column

from ..cancel import check_cancelled
from ..settings import SETTINGS


def _relation_rows(rel: "Relation") -> int | None:
    """The relation's row count where it is known without reading the
    rows: ``None`` for a columnar body still to be filtered, for a streamed
    one, and for one rdflib still finishes (``rest``)."""
    if rel.rest:
        return None
    if rel.rows is not None:
        return len(rel.rows)
    if rel.cols is not None and not rel.preds:
        return rel.nrows
    return None


@dataclass(slots=True)
class Relation:
    """A code-space relation.

    ``schema`` names the variable-like terms. The body is exactly one of
    ``u64`` columns (``cols``, one per schema entry: zero-copy views over
    native ``U64Column`` buffers — a single matched pattern's, or a hash
    join's gathered columns), materialized ``rows`` of ``int`` codes, or
    ``pieces``: an iterator of column tuples in schema order, a join step
    gathered a piece at a time as it is pulled (:func:`_streamed_step`),
    consumed once. ``None`` in a row is an unbound variable and
    ``nullable`` lists the variables that may hold it; ``foreign`` maps
    negative codes to terms outside the dictionary; ``preds`` are row
    predicates still to be applied while a columnar or streamed body streams
    (``nrows`` counts a columnar body's rows before them).

    ``rest`` holds the triple patterns of a BGP that rdflib's ``evalBGP``
    still has to join into each row (tier 3, :func:`_finish_with_rdflib`):
    the body is then the rows to finish, not the block's solutions, and the
    schema lacks the variables only those patterns bind. Such a relation
    goes to :func:`_yield_solutions` alone.
    """

    schema: tuple
    cols: tuple | None  # a u64 memoryview per entry (slice, tolist, iterate)
    rows: list[tuple] | None
    nrows: int
    nullable: frozenset = frozenset()
    foreign: dict = field(default_factory=dict)
    preds: tuple[Callable[[tuple], bool], ...] = ()
    pieces: Iterator[tuple] | None = None
    rest: tuple = ()

    @classmethod
    def from_rows(cls, schema, rows):
        return cls(schema, None, rows, len(rows))

    @classmethod
    def from_cols(cls, schema, cols):
        """A columnar relation: ``cols``, one non-empty tuple of columns."""
        return cls(schema, cols, None, len(cols[0]))

    def with_rows(self, rows: list) -> "Relation":
        """The relation over ``rows`` instead of its body: the same schema,
        nullable variables, foreign terms and ``rest``."""
        return Relation(
            self.schema, None, rows, len(rows), self.nullable, self.foreign, rest=self.rest
        )

    def row_list(self) -> list[tuple]:
        """A columnar or row body's rows, a columnar one zipped in one pass."""
        if self.cols is not None:
            return _cols_to_rows(self.cols)
        assert self.rows is not None, "a streamed body has no rows to list"
        return self.rows

    def native_column(self, index: int) -> U64Column:
        """Column ``index`` of a columnar or row body as a native column: the
        one behind a columnar body's view, or one built from the rows."""
        if self.cols is not None:
            return _native_column(self.cols[index])
        assert self.rows is not None, "a streamed body has no columns"
        return U64Column([row[index] for row in self.rows])


def _rows_of(rel: Relation) -> list:
    """The relation's rows, materialized. A streamed body is drained into
    rows, its ``preds`` applied, and the relation keeps them as its body, so
    a second call returns the same list."""
    assert not rel.rest, "rdflib finishes this relation's rows: only the emitter reads them"
    if rel.rows is not None:
        return rel.rows
    rows = list(_code_rows(rel))
    if rel.pieces is not None:
        rel.rows, rel.nrows, rel.preds, rel.pieces = rows, len(rows), (), None
    return rows


def _shared_key_ok(left: Relation, right: Relation, shared) -> None:
    # A join key that may be unbound needs rdflib's compatibility semantics
    # (unbound matches anything); a hash join cannot give them.
    if any(v in left.nullable or v in right.nullable for v in shared):
        raise NotImplementedError


def _left_join_rows(schema_a, rows_a, schema_b, rows_b, preds) -> list:
    """Hash left join: each left row extended by every compatible right row
    that passes ``preds`` (over the combined row), or padded with None."""
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    ia = [schema_a.index(v) for v in shared]
    ib = [schema_b.index(v) for v in shared]
    table: dict = {}
    for rb in rows_b:
        table.setdefault(tuple(rb[i] for i in ib), []).append(tuple(rb[i] for i in keep_b))
    pad = (None,) * len(keep_b)
    out = []
    for ra in rows_a:
        matched = False
        for tail in table.get(tuple(ra[i] for i in ia), ()):
            row = ra + tail
            if all(pred(row) for pred in preds):
                out.append(row)
                matched = True
        if not matched:
            out.append(ra + pad)
    return out


def _filter_relation(rel: Relation, preds) -> Relation:
    """Apply row predicates: streamed over a columnar or streamed body,
    eagerly over rows. The patterns rdflib still finishes the rows with
    (``rest``) stay with them."""
    if rel.rows is None:
        return replace(rel, preds=rel.preds + tuple(preds))
    rows = [row for row in rel.rows if all(pred(row) for pred in preds)]
    return rel.with_rows(rows)


class _OverBudget(Exception):
    """A join step of a BGP over the pair budget (``SETTINGS.pair_budget``:
    counted by ``join_indices`` capped at the budget in :func:`_join_columns`,
    by :func:`_pair_count` for a join of rows), raised before the step
    is built. ``pairs`` is the step's pair count, ``None`` for a probe step
    or a keep-scan, which stop on their rows. ``calls`` are the native calls
    the step made before it stopped — a probe step's probes, a keep-scan's
    match — and ``native_ns`` the keep-scan's time when tracing, for the
    step's statistics. :func:`_join_incremental_bgp` catches it."""

    def __init__(self, pairs: int | None, calls: int = 0, native_ns: int = 0):
        super().__init__(pairs)
        self.pairs = pairs
        self.calls = calls
        self.native_ns = native_ns


def _native_column(col) -> U64Column:
    """A relation column as the native column behind it: a view over a
    ``U64Column`` is that column, anything else (an ``array('Q')``) is
    copied into one."""
    if isinstance(col, U64Column):
        return col
    owner = getattr(col, "obj", None)
    if isinstance(owner, U64Column) and len(owner) == len(col):
        return owner
    return U64Column(col)


def _distinct_codes(col) -> list:
    """A column's distinct codes in first-seen order, by the native kernel."""
    return memoryview(_native_column(col).distinct()).cast("Q").tolist()


def _cols_to_rows(cols) -> list:
    """Columnar body to row tuples, in one zip."""
    return list(zip(*(col.tolist() for col in cols), strict=True))


def _pair_count(left: U64Column, right: U64Column) -> int | None:
    """The row pairs a join of the two key columns makes, counted natively
    (``join_count``, no pair built) — or ``None``, uncounted, when the
    columns' product is within ``SETTINGS.pair_budget``: the count a join of
    rows makes before it is built (:func:`_row_step_pairs`)."""
    if len(left) * len(right) <= SETTINGS.pair_budget:
        return None
    return left.join_count(right)


def _row_step_pairs(schema_a, rows_a, schema_b, rows_b) -> int | None:
    """The pairs :func:`_join` makes of two row relations, as
    :func:`_pair_count` counts them: ``None`` while their product is within
    ``SETTINGS.pair_budget``; past it, the pairs of the first shared variable's
    codes, copied into native columns (with several shared variables an
    upper bound of the joined rows), or the product when none is shared."""
    product = len(rows_a) * len(rows_b)
    if product <= SETTINGS.pair_budget:
        return None
    shared = [v for v in schema_b if v in schema_a]
    if not shared:
        return product
    ia, ib = schema_a.index(shared[0]), schema_b.index(shared[0])
    return _pair_count(
        U64Column([row[ia] for row in rows_a]), U64Column([row[ib] for row in rows_b])
    )


def _join_columns(schema_a, cols_a, schema_b, cols_b):
    """Hash join of two columnar relations sharing exactly one variable,
    columns out — the rows exist only when a consumer materializes them:
    the joined schema, its columns and the step's pair count, ``None`` while
    it is uncounted. ``None`` when the shape is not its case (several or no
    shared variables); the row join handles those.

    Native kernels, GIL released: ``join_indices`` pairs the rows of the two
    key columns in nested-loop order (the left rows in order, each with its
    right matches in theirs) and ``take`` gathers every output column by
    those indices. Within the product bound the join is built uncounted.
    Past it, ``join_indices`` has ``SETTINGS.pair_budget`` as its cap, so it
    counts the pairs before it builds any: a join over the budget is refused
    (``JoinTooLarge``) and raises :class:`_OverBudget` with that count, and
    one within it is built, its pair count the pairs it made.
    """
    shared = [v for v in schema_b if v in schema_a]
    if len(shared) != 1:
        return None
    ia = schema_a.index(shared[0])
    ib = schema_b.index(shared[0])
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)
    left = [_native_column(col) for col in cols_a]
    key = _native_column(cols_b[ib])
    pairs = None
    if len(left[ia]) * len(key) <= SETTINGS.pair_budget:
        a_idx, b_idx = left[ia].join_indices(key)
    else:
        try:
            a_idx, b_idx = left[ia].join_indices(key, max_pairs=SETTINGS.pair_budget)
        except JoinTooLarge as refused:
            raise _OverBudget(refused.pairs) from None
        pairs = len(a_idx)
    out = [col.take(a_idx) for col in left]
    out += [_native_column(cols_b[i]).take(b_idx) for i in keep_b]
    return schema, tuple(memoryview(col).cast("Q") for col in out), pairs


def _slices(cols: Sequence[U64Column], nrows: int) -> Iterator[tuple[U64Column, ...]]:
    """The columns in consecutive zero-copy slices (``U64Column.slice``) of
    at most ``SETTINGS.piece_rows`` rows."""
    for start in range(0, nrows, SETTINGS.piece_rows):
        stop = min(start + SETTINGS.piece_rows, nrows)
        yield tuple(col.slice(start, stop) for col in cols)


def _join(schema_a, rows_a, schema_b, rows_b):
    """Hash join of two code-space relations on their shared variables.

    Keys come from :func:`~operator.itemgetter`: a bare ``int`` code for one
    shared variable (the common case — no per-row key tuple), a tuple for
    several.
    """
    shared = [v for v in schema_b if v in schema_a]
    keep_b = [i for i, v in enumerate(schema_b) if v not in schema_a]
    schema = schema_a + tuple(schema_b[i] for i in keep_b)

    if not shared:
        return schema, [ra + rb for ra in rows_a for rb in rows_b]

    key_a = itemgetter(*(schema_a.index(v) for v in shared))
    key_b = itemgetter(*(schema_b.index(v) for v in shared))
    # The build-side encoding of _join_columns, inlined once per tail shape.
    table: dict = {}
    if not keep_b:
        for rb in rows_b:
            key = key_b(rb)
            previous = table.get(key)
            if previous is None:
                table[key] = ()
            elif isinstance(previous, list):
                previous.append(())
            else:
                table[key] = [previous, ()]
    elif len(keep_b) == 1:
        tail_of = itemgetter(keep_b[0])
        for rb in rows_b:
            key = key_b(rb)
            tail = (tail_of(rb),)
            previous = table.get(key)
            if previous is None:
                table[key] = tail
            elif isinstance(previous, list):
                previous.append(tail)
            else:
                table[key] = [previous, tail]
    else:
        tail_of = itemgetter(*keep_b)
        for rb in rows_b:
            key = key_b(rb)
            tail = tail_of(rb)
            previous = table.get(key)
            if previous is None:
                table[key] = tail
            elif isinstance(previous, list):
                previous.append(tail)
            else:
                table[key] = [previous, tail]
    out: list = []
    extend = out.extend
    get = table.get
    for ra in rows_a:
        tails = get(key_a(ra))
        if tails is not None:
            if isinstance(tails, list):
                extend(ra + tail for tail in tails)
            else:
                out.append(ra + tails)
    return schema, out


def _code_rows(rel: Relation) -> Iterator[tuple]:
    """The relation's rows as code tuples, lazily for a columnar or streamed
    body."""
    assert not rel.rest, "rdflib finishes this relation's rows: only the emitter reads them"
    return _body_rows(rel)


def _body_rows(rel: Relation) -> Iterator[tuple]:
    """The code tuples of the relation's body, its ``preds`` applied: its
    rows, or the rows rdflib still finishes when it has a ``rest``."""
    if rel.rows is not None:
        rows = iter(rel.rows)
    elif rel.pieces is not None:
        rows = _piece_rows(rel.pieces)
    else:
        rows = _column_rows(rel.cols, rel.nrows)
    if rel.preds:
        preds = rel.preds
        rows = (row for row in rows if all(pred(row) for pred in preds))
    return rows


def _piece_rows(pieces) -> Iterator[tuple]:
    """The rows of a streamed body, each piece's through
    :func:`_column_rows`, a piece let go before the next is pulled. The
    pieces are closed when the rows end, fail or are closed, which releases
    what feeds them."""
    try:
        for piece in pieces:
            yield from _column_rows(piece, len(piece[0]))
            del piece
    finally:
        close = getattr(pieces, "close", None)
        if close is not None:
            close()


def _column_rows(cols, nrows) -> Iterator[tuple]:
    """Zip column views into row tuples a slice at a time, so a consumer that
    stops early never converts the whole match into Python objects. Each
    slice ends with a check of the query's cancel token
    (:func:`check_cancelled`), so a cancel stops a drain within one slice."""
    i, size = 0, SETTINGS.chunk_start
    while i < nrows:
        j = min(i + size, nrows)
        yield from zip(*(view[i:j].tolist() for view in cols), strict=True)
        i, size = j, min(size * 4, SETTINGS.chunk_max)
        check_cancelled()
