"""The file a store has open (:class:`OpenFile`): what kind of file it is and
what the store learns about it once. ``VortexRdflibStore.open()`` makes one
and ``close()`` drops it, so every fact about a file goes with it.

Also the checked decode (:func:`spelling`, :func:`spellings`): a code read
from the file's own columns is in its dictionary, so one that decodes to
nothing is an error, never a missing term.
"""

from dataclasses import dataclass
from functools import cached_property

from vortex_rdf import VortexRdfStore

from .terms import KindRanges

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


class OpenFile:
    """One open ``.vortex`` file: its term dictionary (``None`` while the
    store reads no codes), layout, indexes and residency, one rdflib context
    per graph a match has named (by spelling and by dictionary code), the
    file's graphs once listed (``all_contexts``), its union facts once read
    (``union``, filled by the store), and the facts its dictionary answers
    once: the kind ranges and the default graph's code."""

    def __init__(self, native: VortexRdfStore, term_dict, in_memory: bool):
        self.term_dict = term_dict
        self.layout: str = native.layout()
        self.indexes: tuple[str, ...] = tuple(native.indexes())
        self.in_memory = in_memory
        self.contexts_by_spelling: dict[str, tuple] = {}
        self.contexts_by_code: dict[int, tuple] = {}
        self.all_contexts: list | None = None
        self.union: UnionFacts | None = None

    @cached_property
    def kind_ranges(self) -> KindRanges | None:
        """Each term kind's code range, ``None`` without a dictionary."""
        return None if self.term_dict is None else KindRanges.from_dictionary(self.term_dict)

    @cached_property
    def default_graph_code(self) -> int | None:
        """The dictionary code of the default graph's empty name; ``None``
        when the file holds no default-graph rows, or there is no
        dictionary."""
        return None if self.term_dict is None else self.term_dict.encode(DEFAULT_GRAPH)

    def point_lookup(self, n3) -> bool:
        """Whether the native layer answers the quad pattern ``n3`` without a
        scan: its subject is bound (a binary search in the subject-ordered
        base), or a secondary index serves its bound predicate or object."""
        return n3[0] is not None or bool(self.indexes) and (n3[1] is not None or n3[2] is not None)


def spelling(term_dict, code: int) -> str:
    """The N-Triples spelling ``term_dict`` holds for ``code``; a code it
    does not hold raises ``ValueError``."""
    found = term_dict.decode(code)
    if found is None:
        raise ValueError(f"term code {code} is not in the store dictionary")
    return found


def spellings(term_dict, codes) -> list[str]:
    """:func:`spelling` of each of ``codes``, decoded in one batch; the first
    code ``term_dict`` does not hold raises ``ValueError``."""
    found = term_dict.decode_many(codes)
    if None in found:
        raise ValueError(f"term code {codes[found.index(None)]} is not in the store dictionary")
    return found
