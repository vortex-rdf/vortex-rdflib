"""The head above a block — ``Project``, ``Distinct``, ``OrderBy`` and
``Slice`` — applied to its code-space relation (:func:`_eval_head`).
"""

import heapq
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from operator import itemgetter

from rdflib.plugins.sparql.evalutils import _val
from rdflib.term import Variable

from ..settings import SETTINGS
from . import algebra, aliasing, blocks, relation, solutions, tracing
from .relation import Relation
from .tracing import _TRACE


@dataclass(slots=True)
class _Head:
    block: object
    pv: tuple | None
    start: int
    stop: int | None
    distinct: bool
    order: list  # (variable, descending) per ORDER BY condition


def _plan_head(part) -> _Head:
    """Recognize a ``Slice? -> Distinct? -> Project -> OrderBy? -> block``
    head (or a bare ``OrderBy -> block``), eagerly.

    Anything else is rdflib's: its evaluator runs the node and re-enters the
    hook for the nodes below.
    """
    node, start, stop, distinct, order = part, 0, None, False, []
    if node.name == "Slice":
        start = int(node.start or 0)
        # The key is absent without LIMIT; CompValue then answers None.
        length = node.length
        stop = None if length is None else start + int(length)
        node = node.p
    if node.name == "Distinct":
        distinct = True
        node = node.p
    pv = None
    if node.name == "Project":
        pv = node.PV
        pv = (pv,) if isinstance(pv, Variable) else tuple(pv)
        node = node.p
    elif distinct or start or stop is not None:
        raise NotImplementedError
    if node.name == "OrderBy":
        for condition in node.expr:
            if not isinstance(condition.expr, Variable):
                raise NotImplementedError
            order.append((condition.expr, condition.order == "DESC"))
        node = node.p
    if pv is None and not order:
        raise NotImplementedError
    algebra._check_block(node)
    return _Head(node, pv, start, stop, distinct, order)


def _eval_head(ctx, store, part):
    started = tracing._stopwatch()
    head = _plan_head(part)
    tracing._trace_event(
        "head_planned",
        entry_node=part.name,
        project_variable_count=0 if head.pv is None else len(head.pv),
        order_key_count=len(head.order),
        descending_key_count=sum(descending for _, descending in head.order),
        distinct=head.distinct,
        offset=head.start,
        limit=None if head.stop is None else head.stop - head.start,
    )
    # Without an ORDER BY or a DISTINCT between them, a LIMIT bounds the
    # block's rows themselves; a block that is one native match takes it.
    # The relation then goes straight to the emitter (``stream``).
    stream = not head.order and not head.distinct
    limit = head.stop if stream else None
    rel = blocks._solve_block(ctx, store, head.block, limit=limit, stream=stream)
    tracing._trace_event(
        "block_solve_complete",
        rows=relation._relation_rows(rel),
        columns=len(rel.schema),
        nullable_columns=len(rel.nullable),
        foreign_terms=len(rel.foreign),
        elapsed_ns=started(),
    )
    if head.order:
        # DISTINCT narrows the rows after the sort, so only a plain LIMIT
        # can stop the sort at the top k.
        rows = _order_rows(ctx, store, rel, head.order, None if head.distinct else head.stop)
        rel = rel.with_rows(rows)
    if head.distinct:
        return _eval_distinct(ctx, store, rel, head)
    return solutions._yield_solutions(ctx, store, rel, head.pv, head.start, head.stop)


def _order_rows(ctx, store, rel: Relation, order, stop) -> list:
    """The relation's rows in ORDER BY order.

    Each sort variable's distinct codes are ranked once (``_rank_codes``);
    a row's key is the tuple of its ranks, negated for DESC, so one stable
    sort reproduces rdflib's chain of stable sorts, and a following LIMIT
    keeps only the top ``stop`` rows. A variable the block does not bind
    ties every row, as it does for rdflib.
    """
    started = tracing._stopwatch()
    rows = relation._rows_of(rel)
    tracing._trace_event(
        "order_start",
        input_rows=len(rows),
        order_key_count=len(order),
        descending_key_count=sum(descending for _, descending in order),
        limit=stop,
    )
    keys = []
    for key_index, (var, descending) in enumerate(order):
        if var not in rel.schema:
            continue
        index = rel.schema.index(var)
        distinct_codes = {row[index] for row in rows}
        key_started = tracing._stopwatch()
        ranks = _rank_codes(store, rel.foreign, distinct_codes)
        tracing._trace_event(
            "order_key_ranked",
            key_index=key_index,
            descending=descending,
            input_rows=len(rows),
            distinct_codes=len(distinct_codes),
            elapsed_ns=key_started(),
        )
        keys.append((index, ranks, -1 if descending else 1))
    if not keys:
        return rows

    def sort_key(row):
        return tuple(sign * ranks[row[i]] for i, ranks, sign in keys)

    if stop is not None and stop < len(rows):
        output = heapq.nsmallest(stop, rows, key=sort_key)
        algorithm = "bounded_heap_nsmallest"
    else:
        output = sorted(rows, key=sort_key)
        algorithm = "full_stable_sort"
    tracing._trace_event(
        "order_complete",
        algorithm=algorithm,
        input_rows=len(rows),
        output_rows=len(output),
        limit=stop,
        limit_applied_during_order=algorithm == "bounded_heap_nsmallest",
        elapsed_ns=started(),
    )
    return output


def _rank_codes(store, foreign: dict, codes) -> dict:
    """Ranks reproducing rdflib's ORDER BY comparison (``_val``): unbound
    first, then blank nodes, IRIs and literals.

    Blank nodes rank by code: the spelling ``_:label`` sorts as the label.
    IRIs rank by their spelling without the brackets, the string rdflib
    compares, read in one ``decode_many`` with no term built. Their codes
    are not in that order: the closing ``>`` of ``<iri>`` sorts an IRI after
    the IRIs it is a prefix of whenever the next character is below ``>``
    (``<…/a>`` after ``<…/a/b>``). Literals are decoded once per query, in
    one batch through the query's term cache, and ranked by rdflib's own
    comparator. Adjacent keys that compare equal share a rank, so the stable
    sort keeps their input order. With a query constant outside the
    dictionary among the codes, every code ranks as the literals do."""
    elapsed = tracing._stopwatch()
    codes = set(codes)
    ranks = {None: -1}
    literal_lo, iri_lo, blank_lo = store._term_kind_bounds()
    blanks, iris, others = [], [], []
    by_kind = all(code is None or code >= 0 for code in codes)
    for code in codes:
        if code is None:
            continue
        if by_kind and code >= blank_lo:
            blanks.append(code)
        elif by_kind and code >= iri_lo:
            iris.append(code)
        else:
            others.append(code)
    rank = 0
    for code in sorted(blanks):
        ranks[code] = rank
        rank += 1
    if iris:
        # ``<iri>`` decodes to ``URIRef(iri)``, so the string rdflib compares
        # is the spelling without its brackets.
        spellings = store._dict.decode_many(iris)
        if None in spellings:
            code = iris[spellings.index(None)]
            raise ValueError(f"term code {code} is not in the store dictionary")
        keyed = [(spelling[1:-1], code) for spelling, code in zip(spellings, iris, strict=True)]
        rank = _rank_keyed(ranks, keyed, rank)
    local = [code for code in others if code >= 0]
    traced = _TRACE.get() is not None
    # For the trace: the literals the query's term cache holds already.
    hits = sum(code in store._terms().decode for code in local) if traced else 0
    if others:
        cached = store._prime_decode_cache(local)
        keyed = [(_val(cached[code] if code >= 0 else foreign[code]), code) for code in others]
        _rank_keyed(ranks, keyed, rank)
    if traced:
        tracing._trace_event(
            "rank_codes_complete",
            input_codes=len(codes),
            unique_codes=len(codes),
            null_codes=int(None in codes),
            blank_codes=len(blanks),
            iri_codes=len(iris),
            other_codes=len(others),
            foreign_codes=sum(code < 0 for code in others),
            decoded_codes=len(local),
            decode_cache_hits=hits,
            decode_cache_misses=len(local) - hits,
            elapsed_ns=elapsed(),
        )
    return ranks


def _rank_keyed(ranks: dict, keyed: list, rank: int) -> int:
    """Rank the codes of ``keyed``, ``(key, code)`` pairs, in key order from
    ``rank`` on, adjacent keys that compare equal sharing a rank. Returns one
    more than the last rank given, or ``rank + 1`` for an empty ``keyed``: a
    gap, which leaves the order of the ranks unchanged."""
    keyed.sort(key=itemgetter(0))
    previous = None
    for key, code in keyed:
        if previous is not None and previous < key:
            rank += 1
        ranks[code] = rank
        previous = key
    return rank + 1


def _eval_distinct(ctx, store, rel: Relation, head: _Head):
    """DISTINCT over the projected variables, without decoding the
    duplicates: code-distinct tuples first, then a term-level pass over the
    survivors (rdflib compares decoded terms, and two spellings of a typed
    literal can be one term), then LIMIT/OFFSET."""
    pv = head.pv or ()
    keys = tuple(v for v in pv if v in rel.schema)
    idx = [rel.schema.index(v) for v in keys]
    rows = _term_distinct(store, _code_distinct(rel, idx), rel.foreign)
    return solutions._yield_rows(ctx, store, keys, rows, rel.foreign, pv, head.start, head.stop)


def _code_distinct(rel: Relation, idx: list) -> Iterator[tuple]:
    """The relation's rows projected to ``idx``, one per distinct code
    tuple, in first-seen order."""
    if rel.cols is not None and not rel.preds:
        if len(idx) == 1:
            # The native kernel keeps first-seen order, as dict.fromkeys did.
            return ((code,) for code in relation._distinct_codes(rel.cols[idx[0]]))
        columns = [rel.cols[i].tolist() for i in idx]
        return iter(dict.fromkeys(zip(*columns, strict=True)))

    def rows():
        seen: set = set()
        for row in relation._code_rows(rel):
            key = tuple(row[i] for i in idx)
            if key not in seen:
                seen.add(key)
                yield key

    return rows()


def _term_distinct(store, tuples: Iterator[tuple], foreign: dict) -> Iterator[tuple]:
    """Drop the code tuples whose decoded terms equal an earlier tuple's."""
    seen: set = set()
    aliases: dict = {}
    size = SETTINGS.chunk_start
    while True:
        chunk = list(islice(tuples, size))
        if not chunk:
            return
        aliasing._alias_codes(store, {c for row in chunk for c in row}, foreign, aliases)
        for row in chunk:
            key = tuple(aliases.get(c, c) for c in row) if aliases else row
            if key not in seen:
                seen.add(key)
                yield row
        size = min(size * 4, SETTINGS.chunk_max)
