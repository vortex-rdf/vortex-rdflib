"""The FILTER conjuncts a FILTER hands down into the block it filters
(:class:`Pushed`): the contract between a FILTER and the scans, VALUES
tables and joins below it."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType


@dataclass(frozen=True, slots=True, eq=False)
class Pushed:
    """The filter conjuncts a FILTER hands down into its block.

    ``per_var`` maps each variable to its single-variable conjuncts, for the
    scans and VALUES tables that bind it. ``tuples`` are the fast conjuncts
    over several variables, which a BGP that binds all of theirs may prune
    with mid-join (a pre-filter: the FILTER still applies them) or turn into
    restrictions of one pattern (:func:`.planner._specialize_tuple_conjuncts`): those
    every row the BGP yields passes, and its relation reports them
    (``Relation.applied``). ``views`` are the terms parsed for the tuple
    conjuncts, shared with the FILTER so none is decoded twice — the
    FILTER's own, held no longer than it runs.

    They travel wherever the block solvers pass them — a group join's
    sides, an OPTIONAL's or a MINUS's left side, a GRAPH block — and never
    an OPTIONAL's or a MINUS's right side, where dropping a row would change
    the answer: that side gets :data:`NOTHING`.
    """

    per_var: Mapping = MappingProxyType({})
    tuples: tuple = ()
    views: dict | None = None

    def without(self, applied: frozenset) -> "Pushed":
        """These conjuncts but the tuple conjuncts ``applied`` holds: another
        block of the group has applied them already."""
        if not applied or not self.tuples:
            return self
        return replace(self, tuples=tuple(c for c in self.tuples if c not in applied))


#: No conjunct pushed.
NOTHING = Pushed()
