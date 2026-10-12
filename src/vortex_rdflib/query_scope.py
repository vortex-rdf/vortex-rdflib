"""What a query learns about terms, kept for that query alone.

Evaluating a query over a :class:`~vortex_rdflib.store.VortexRdflibStore`
decodes term codes to rdflib terms, classifies typed literals for term
aliasing, gives the query's constants outside the dictionary negative codes,
and asks whether a blank node names a graph of the file. Each answer is
reused for the rest of the query and dropped with it, so a long-running
process does not grow with the number of queries it answers.

The scope is a :class:`~contextvars.ContextVar`. The pushdown hook opens one
at the top of every query — the query node rdflib offers it first, see
``pushdown._eval_scoped`` — and makes it current around every step of a lazy
result, so it covers the iteration too; it is cleared once the last solution
is produced or the result is closed. A call outside any query (rdflib's
``Store.triples()`` API, ``len()``) gets fresh state for that call alone.
"""

from contextvars import ContextVar, Token

from rdflib.term import Node


class QueryTerms:
    """One query's term state over one store.

    ``decode`` maps codes to the rdflib terms they decode to; ``alias_*`` are
    the literal codes classified for term aliasing (``pushdown._alias_codes``):
    every code looked at, each typed term's first code, and the codes whose
    term an earlier code already carries; ``foreign`` and ``foreign_codes``
    map the negative codes of query constants the dictionary lacks to their
    terms and back; ``blank_graphs`` says, per blank-node spelling, whether
    the file holds a graph by that name.
    """

    __slots__ = (
        "decode",
        "alias_classified",
        "alias_firsts",
        "alias_reps",
        "foreign",
        "foreign_codes",
        "blank_graphs",
    )

    def __init__(self) -> None:
        self.decode: dict[int, Node] = {}
        self.alias_classified: set[int] = set()
        self.alias_firsts: dict[Node, int] = {}
        self.alias_reps: dict[int, int] = {}
        self.foreign: dict[int, Node] = {}
        self.foreign_codes: dict[Node, int] = {}
        self.blank_graphs: dict[str, bool] = {}


class QueryScope:
    """The term state of one query, per store it reads (one, in practice)."""

    __slots__ = ("_by_store", "__weakref__")

    def __init__(self) -> None:
        self._by_store: dict[int, tuple[object, QueryTerms]] = {}

    def terms(self, store) -> QueryTerms:
        entry = self._by_store.get(id(store))
        if entry is None or entry[0] is not store:
            entry = self._by_store[id(store)] = (store, QueryTerms())
        return entry[1]

    def clear(self) -> None:
        """Drop everything the query learned."""
        self._by_store.clear()


_CURRENT: ContextVar[QueryScope | None] = ContextVar("vortex_rdflib_query_scope", default=None)


def current() -> QueryScope | None:
    """The scope of the query being evaluated, or ``None`` outside one."""
    return _CURRENT.get()


def enter(scope: QueryScope) -> Token:
    return _CURRENT.set(scope)


def leave(token: Token) -> None:
    _CURRENT.reset(token)


def terms_for(store) -> QueryTerms:
    """``store``'s term state in the current query, or fresh state for a
    call outside any query."""
    scope = _CURRENT.get()
    return QueryTerms() if scope is None else scope.terms(store)
