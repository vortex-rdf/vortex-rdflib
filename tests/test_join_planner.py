"""The join planner's parts, each on its own: the readers of a relation's
body, the probes' batches, a pattern's match and counts, and the union's
distinct rows."""

from dataclasses import replace

import pytest
from rdflib.term import Variable

from vortex_rdflib.pushdown.relation import Relation

V = Variable("v")


def test_row_and_column_readers_refuse_pending_predicates_and_rest():
    """``row_list`` and ``native_column`` read a body as it is held: a
    relation with row predicates still to apply, or with patterns rdflib
    still finishes per row, is not theirs to read."""
    rel = Relation.from_rows((V,), [(1,), (2,)])
    assert rel.row_list() == [(1,), (2,)] and len(rel.native_column(0)) == 2
    for pending in (replace(rel, preds=(lambda row: True,)), replace(rel, rest=((V, V, V),))):
        with pytest.raises(AssertionError):
            pending.row_list()
        with pytest.raises(AssertionError):
            pending.native_column(0)


class _Answers:
    """A batch's native iterator: ``answers`` in turn, ``closed`` once
    closed."""

    def __init__(self, answers):
        self._answers = iter(answers)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._answers)

    def close(self):
        self.closed = True


def test_a_checked_batch_answers_each_probe_once():
    """A batch call's answers, one per probe given: one short raises at the
    first probe left without an answer, one over raises with its last probe,
    and the batch's iterator is closed however its answers end."""
    from vortex_rdflib.pushdown.probes import _checked_batch

    exact = _Answers("abc")
    assert list(_checked_batch(exact, 3)) == ["a", "b", "c"] and exact.closed
    short = _Answers("ab")
    pulled = []
    with pytest.raises(ValueError, match="fewer than its 3 probes"):
        pulled.extend(_checked_batch(short, 3))
    assert pulled == ["a", "b"] and short.closed
    over = _Answers("abcd")
    pulled = []
    with pytest.raises(ValueError, match="more than its 3 probes"):
        pulled.extend(_checked_batch(over, 3))
    assert pulled == ["a", "b"] and over.closed
    stopped = _Answers("abc")
    checked = _checked_batch(stopped, 3)
    assert next(checked) == "a"
    checked.close()
    assert stopped.closed


def test_a_left_probe_join_takes_no_deferred_restriction():
    """A left join keeps the rows a probe does not extend, padded: deferred
    restrictions, evaluated over the joined rows, would drop them, so a
    pattern with any is never probed as a left join."""
    from vortex_rdflib.pushdown import probes
    from vortex_rdflib.pushdown.matching import Pattern

    pat = Pattern((None, "<http://ex.org/p>", None, None), {V: [0]}, deferred={V: ["x"]})
    with pytest.raises(AssertionError):
        probes._probe_join(None, (), [()], pat, keep_unmatched=True)


def test_probe_join_drops_union_repeats_per_answer(tmp_path, monkeypatch):
    """A probe of a pattern over the union of a store where some triple is
    in two graphs is made a set once per answer (``union.distinct_rows``),
    not row by row: each subject's triple joins once."""
    from vortex_rdf import serialize_rdf

    from vortex_rdflib import VortexRdflibStore, union
    from vortex_rdflib.pushdown import probes
    from vortex_rdflib.pushdown.matching import Pattern

    source = tmp_path / "repeats.nq"
    source.write_text(
        "<http://ex.org/s1> <http://ex.org/p> <http://ex.org/o> <http://ex.org/g1> .\n"
        "<http://ex.org/s1> <http://ex.org/p> <http://ex.org/o> <http://ex.org/g2> .\n"
        "<http://ex.org/s2> <http://ex.org/p> <http://ex.org/o2> <http://ex.org/g1> .\n",
        encoding="utf-8",
    )
    out = tmp_path / "repeats.vortex"
    serialize_rdf(str(source), str(out), layout="dictionary", format="nquads")
    store = VortexRdflibStore(str(out))
    assert store._union_has_repeats()
    term_dict = store._dict
    assert term_dict is not None
    s1, s2 = (term_dict.encode(f"<http://ex.org/{name}>") for name in ("s1", "s2"))
    calls = []
    distinct_rows = union.distinct_rows
    monkeypatch.setattr(union, "distinct_rows", lambda cols: calls.append(1) or distinct_rows(cols))
    o = Variable("o")
    pat = Pattern((None, "<http://ex.org/p>", None, None), {V: [0], o: [2]}, distinct=True)
    schema, rows = probes._probe_join(store, (V,), [(s1,), (s2,)], pat)
    assert schema == (V, o) and len(rows) == 2 and {row[0] for row in rows} == {s1, s2}
    assert len(calls) == 2


def test_distinct_chunks_closes_its_source():
    """Closing a match's distinct chunks after the first closes the chunks
    they read, which ends the native scan and emits a traced query's chunk
    event."""
    from vortex_rdf import U64Column

    from vortex_rdflib import union

    def column(*codes):
        return U64Column(list(codes))

    source = _Answers(
        [
            (column(1, 1), column(2, 2), column(3, 3)),
            (column(4), column(5), column(6)),
        ]
    )
    chunks = union.distinct_chunks(source)
    assert len(next(chunks)[0]) == 1
    chunks.close()
    assert source.closed
