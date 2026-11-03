"""The file a store has open (:class:`OpenFile`): its native handle, its
term codes (:class:`Codes`), what kind of file it is and what the store
learns about it once. ``VortexRdflibStore.open()`` makes one and
``close()`` drops it, so every fact about a file goes with it.
"""

from dataclasses import dataclass
from functools import cached_property

from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, Graph
from rdflib.term import IdentifiedNode
from vortex_rdf import VortexRdfStore

from . import boundary
from .terms import KindRanges, _term_builder

#: How the native layer spells the default graph: the fourth column of a quad
#: no ``GRAPH`` names is the empty string, and ``""`` is also how a pattern
#: selects those rows — ``None`` being the wildcard over every graph.
DEFAULT_GRAPH = ""


@dataclass(frozen=True, slots=True)
class UnionFacts:
    """The union over every graph of a file: whether some triple is in more
    than one graph, so a match over every graph can return it twice
    (``has_repeats``), and how many distinct triples it holds, once a pass
    over them has counted them (``size``, else ``None``)."""

    has_repeats: bool
    size: int | None


class Codes:
    """An open file's term codes: its term dictionary, and what the
    dictionary answers once — each term kind's code range and the default
    graph's code. Its decode is checked (:meth:`spelling`,
    :meth:`spellings`): a code read from the file's own columns is in its
    dictionary, so one that decodes to nothing is an error, never a missing
    term. Decodes go through the native boundary
    (:func:`~vortex_rdflib.boundary.dictionary`), which counts them while
    the counters are on."""

    def __init__(self, term_dict):
        self.term_dict = term_dict

    @cached_property
    def kind_ranges(self) -> KindRanges:
        """Each term kind's code range."""
        return KindRanges.from_dictionary(self.term_dict)

    @cached_property
    def default_graph_code(self) -> int | None:
        """The dictionary code of the default graph's empty name; ``None``
        when the file holds no default-graph rows."""
        return self.term_dict.encode(DEFAULT_GRAPH)

    def spelling(self, code: int) -> str:
        """The N-Triples spelling of ``code``; a code the dictionary does
        not hold raises ``ValueError``."""
        found = boundary.dictionary(self).decode(code)
        if found is None:
            raise ValueError(f"term code {code} is not in the store dictionary")
        return found

    def spellings(self, codes) -> list[str]:
        """:meth:`spelling` of each of ``codes``, decoded in one batch; the
        first code the dictionary does not hold raises ``ValueError``."""
        found = boundary.dictionary(self).decode_many(codes)
        if None in found:
            raise ValueError(f"term code {codes[found.index(None)]} is not in the store dictionary")
        return found


#: The rdflib term for a stored N-Triples spelling (:func:`.terms._term_builder`).
_term = _term_builder()


class OpenFile:
    """One open ``.vortex`` file of ``store``: its native handle
    (``native``), its term codes (``codes``, ``None`` while the store reads
    none: a layout without a dictionary, or the code path off), layout,
    indexes and residency, one rdflib context per graph a match has named
    (:meth:`context_tuple`, :meth:`context_of_code`), the file's graphs once
    listed (``all_contexts``) and its union facts once read (``union``,
    filled by the store)."""

    def __init__(self, store, native: VortexRdfStore, in_memory: bool, use_codes: bool):
        self.store = store
        self.native = native
        term_dict = native.term_dict() if use_codes else None
        self.codes: Codes | None = None if term_dict is None else Codes(term_dict)
        self.layout: str = native.layout()
        self.indexes: tuple[str, ...] = tuple(native.indexes())
        self.in_memory = in_memory
        self.contexts_by_spelling: dict[str, tuple] = {}
        self.contexts_by_code: dict[int, tuple] = {}
        self.all_contexts: list | None = None
        self.union: UnionFacts | None = None

    def point_lookup(self, n3) -> bool:
        """Whether the native layer answers the quad pattern ``n3`` without a
        scan: its subject is bound (a binary search in the subject-ordered
        base), or a secondary index serves its bound predicate or object."""
        return n3[0] is not None or bool(self.indexes) and (n3[1] is not None or n3[2] is not None)

    def context_tuple(self, spelling: str) -> tuple:
        """The one-graph context tuple ``triples()`` yields for a graph of
        this file.

        Kept per graph: rdflib only ever iterates it, so one tuple per graph
        keeps a match from allocating a context per row. Only graphs the file
        holds are asked for: a name read from the graph column, or a name a
        caller asks for once a matched row proves the file holds it.
        """
        contexts = self.contexts_by_spelling.get(spelling)
        if contexts is None:
            identifier: IdentifiedNode = DATASET_DEFAULT_GRAPH_ID
            if spelling != DEFAULT_GRAPH:
                name = _term(spelling)
                if not isinstance(name, IdentifiedNode):
                    # Only an IRI or a blank node can name a graph; anything
                    # else in the graph column is a malformed file.
                    raise ValueError(f"graph name is not an IRI or blank node: {spelling!r}")
                identifier = name
            contexts = (Graph(store=self.store, identifier=identifier),)
            self.contexts_by_spelling[spelling] = contexts
        return contexts

    def context_of_code(self, code: int) -> tuple:
        """The context tuple of a graph column's code, its name decoded
        through this file's dictionary: a ``triples()`` call keeps resolving
        against the file it started on, whatever ``close()`` or ``open()``
        does meanwhile."""
        contexts = self.contexts_by_code.get(code)
        if contexts is None:
            assert self.codes is not None, "a graph column's codes come from a dictionary"
            contexts = self.contexts_by_code[code] = self.context_tuple(self.codes.spelling(code))
        return contexts
