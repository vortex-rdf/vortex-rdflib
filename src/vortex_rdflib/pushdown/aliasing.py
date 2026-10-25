"""Term aliasing: the codes of a query whose decoded terms another code
represents. rdflib compares decoded terms, and two spellings of a typed
literal can be one term (``"042"^^xsd:integer`` and ``"42"^^xsd:integer``
are both ``42``), so DISTINCT, grouping and distinct counts merge them
(:func:`_alias_codes`).
"""

import re
from collections.abc import Iterator

import rdflib.term
from rdflib.term import Literal

from ..terms import CANONICAL_INTEGER, INTEGER_SUFFIX, canonical_spelling


def _alias_codes(store, codes, foreign: dict, aliases: dict) -> None:
    """Record in ``aliases`` each code of ``codes`` whose decoded term another
    code represents, mapped to that code — rdflib compares decoded terms,
    and two spellings of a typed literal can be one term
    (``"042"^^xsd:integer`` and ``"42"^^xsd:integer`` are both ``42``).

    IRIs, blank nodes and untyped literals are one term per spelling, so
    only typed literals can alias, and only through rdflib's normalization:
    a typed spelling rdflib keeps as written is its term's canonical one
    (``canonical_spelling``), and every other spelling of that term is one
    rdflib rewrites. So only the typed spellings rdflib may rewrite are
    built (`_rewritable`), and each term built is represented by its
    canonical spelling's code when the dictionary holds it
    (`_canonical_twins`), else by the first code that carried it. Whether a
    code aliases is a property of the store, not of the query: each literal
    code is classified once per query, and later calls in the query only
    look their codes up. A query constant outside the dictionary (a negative
    code, stable for the query too) takes part through its own term; its
    canonical spelling is not in the dictionary, which is why its code is
    negative (`_constant_codes`), so no twin is looked up for it. For most
    stores no code aliases another and ``aliases`` stays empty.
    """
    literal_lo, iri_lo, _ = store._term_kind_bounds()
    terms = store._terms()
    classified = terms.alias_classified
    pending = [
        c
        for c in codes
        if c is not None and (c < 0 or literal_lo <= c < iri_lo) and c not in classified
    ]
    if pending:
        typed = []
        local = [c for c in pending if c >= 0]
        if local:
            cache = terms.decode
            for code, spelling in _rewritable(local, store._dict.decode_many(local)):
                term = cache.get(code)
                if term is None:
                    term = cache[code] = store._from_n3_safe(spelling)
                typed.append((code, term, spelling))
        for code in pending:
            if code < 0:
                term = foreign[code]
                if isinstance(term, Literal) and term.datatype is not None:
                    typed.append((code, term, None))
        firsts, reps = terms.alias_firsts, terms.alias_reps
        twins = _canonical_twins(
            store, [(t, s) for _, t, s in typed if s is not None and t not in firsts]
        )
        for code, term, _ in typed:
            representative = firsts.get(term)
            if representative is None:
                representative = firsts[term] = twins.get(term, code)
            if representative != code:
                reps[code] = representative
        classified.update(pending)
    reps = terms.alias_reps
    if reps:
        for code in codes:
            representative = reps.get(code)
            if representative is not None:
                aliases[code] = representative


#: rdflib's datatype converters (``Literal`` normalizes the lexical forms of
#: their datatypes); ``None`` in an rdflib release without them, and then the
#: alias pass builds every typed literal.
_toPythonMapping = getattr(rdflib.term, "_toPythonMapping", None)


#: A canonical xsd:integer's whole spelling, which rdflib keeps as written.
_CANONICAL_INTEGER_SPELLING = re.compile(
    f'"(?:{CANONICAL_INTEGER.pattern})"{re.escape(INTEGER_SUFFIX)}'
)


def _rewritable(codes, spellings) -> Iterator[tuple[int, str]]:
    """The ``(code, spelling)`` pairs whose typed literal rdflib's
    normalization may rewrite: every typed spelling but a canonical
    xsd:integer and one of a datatype rdflib has no converter for, which
    ``Literal`` keeps as written. Without rdflib's converter table every
    typed spelling. Assumes rdflib's own xsd:integer binding, as the native
    filters do."""
    mapping = _toPythonMapping
    if mapping is None:
        for code, spelling in zip(codes, spellings, strict=True):
            if spelling is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            if spelling.endswith(">"):
                yield code, spelling
        return
    canonical = _CANONICAL_INTEGER_SPELLING.fullmatch
    converted = None
    for code, spelling in zip(codes, spellings, strict=True):
        if spelling is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        if not spelling.endswith(">") or canonical(spelling) is not None:
            continue
        if converted is None:
            # Read per call, so a datatype bound at runtime counts.
            converted = {str(datatype) for datatype in mapping if datatype is not None}
        cut = spelling.rfind('"^^<')
        if cut > 0 and spelling[cut + 4 : -1] not in converted:
            continue
        yield code, spelling


def _canonical_twins(store, read: list) -> dict:
    """The code of each term's canonical spelling, for the ``(term,
    spelling)`` pairs of ``read`` whose term was read from another spelling
    and whose canonical spelling the dictionary holds: one ``encode_many``
    call, and the codes it finds decoded to confirm the term."""
    wanted: dict = {}
    for term, spelling in read:
        if term not in wanted:
            canonical = canonical_spelling(term)
            if canonical != spelling:
                wanted[term] = canonical
    if not wanted:
        return {}
    spellings = list(wanted.values())
    try:
        found = store._dict.encode_many(spellings)
    except ValueError:
        # A spelling the native parser rejects fails the whole batch; alone,
        # it names no term of the file.
        found = []
        for spelling in spellings:
            try:
                found.append(store._dict.encode(spelling))
            except ValueError:
                found.append(None)
    hits = [code for code in found if code is not None]
    if not hits:
        return {}
    # The lookup canonicalizes a spelling as the native parser does, so a
    # code is a twin only if it decodes to the same term.
    cache = store._prime_decode_cache(hits)
    return {
        term: code
        for term, code in zip(wanted, found, strict=True)
        if code is not None and cache[code] == term
    }
