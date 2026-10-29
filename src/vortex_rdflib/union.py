"""The union default graph as a set of triples.

A store's quads are unique, but one triple may be in several graphs, and a
match over every graph returns it once per graph. The union of the graphs is
an RDF graph — a set — so it holds the triple once (SPARQL 1.1 §13; the RDF
merge of a ``FROM`` list). Every sort order a store holds ends in the graph,
so in any match over every graph the rows of one triple are adjacent: keeping
the first row of each run of equal ``(s, p, o)`` codes (``U64Column.run_starts``)
makes the match a set, in one pass and no memory beyond its rows.
"""

from collections.abc import Iterable, Iterator

#: The positions of a quad's triple: the key a union match is made distinct on.
TRIPLE = (0, 1, 2)


def distinct_rows(cols: tuple, key=TRIPLE) -> tuple:
    """``cols`` (equally long ``U64Column``s) with one row of each run of rows
    equal on the columns at ``key``."""
    keys = [cols[i] for i in key]
    rows = len(keys[0])
    if rows < 2:
        return cols
    starts = keys[0].run_starts(*keys[1:])
    if len(starts) == rows:
        return cols
    return tuple(None if col is None else col.take(starts) for col in cols)


def distinct_chunks(chunks: Iterable[tuple], key=TRIPLE) -> Iterator[tuple]:
    """:func:`distinct_rows` of each chunk of one match, read in order: a run
    that crosses a chunk boundary is kept in the chunk it starts in."""
    last = None
    for cols in chunks:
        cols = distinct_rows(cols, key)
        rows = len(cols[0])
        if rows and last is not None and all(_at(cols[i], 0) == v for i, v in zip(key, last, strict=True)):
            cols = tuple(col.slice(1, rows) for col in cols)
            rows -= 1
        if rows:
            last = [_at(cols[i], rows - 1) for i in key]
        yield cols


def one_per_triple(rows: Iterable[tuple]) -> Iterator[tuple]:
    """``(triple, contexts)`` rows whose equal triples are adjacent, as one row
    per triple carrying the contexts of all its rows — what an rdflib store's
    ``triples()`` yields for the union."""
    triple, first, merged = None, (), None
    for row_triple, row_contexts in rows:
        if row_triple == triple:
            if merged is None:
                merged = list(first)
            merged.extend(row_contexts)
            continue
        if triple is not None:
            yield triple, first if merged is None else tuple(merged)
        triple, first, merged = row_triple, row_contexts, None
    if triple is not None:
        yield triple, first if merged is None else tuple(merged)


def _at(col, row: int) -> int:
    return memoryview(col).cast("Q")[row]
