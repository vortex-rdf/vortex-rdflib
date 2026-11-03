"""What a block of the algebra is, read without matching anything: the
typed block the solver takes (:func:`block`, which refuses any other), the
variables it mentions, binds and may leave unbound, and the graph scope its
patterns are matched in (:class:`_GraphScope`).
"""

from dataclasses import dataclass
from typing import Any

from rdflib.term import BNode, URIRef, Variable

from ..terms import canonical_spelling, unparseable_iri

# Query bnodes act as variables, exactly as rdflib's evalBGP treats them.
_VAR_LIKE = (Variable, BNode)


@dataclass(frozen=True, slots=True)
class _GraphScope:
    """How the block below sees the graph column of a match.

    ``n3`` is the graph the patterns are restricted to — ``None`` is the
    wildcard over every graph, ``""`` the default graph, a name its own graph.
    ``var`` is the variable a ``GRAPH ?g`` binds *from* the column instead, in
    which case ``n3`` is the wildcard and the default graph's rows are dropped
    (a ``GRAPH`` clause ranges over the named graphs only).
    """

    n3: str | None = None
    var: Variable | None = None


def _active_scope(ctx, store) -> _GraphScope:
    """The scope of a block that is not under a ``GRAPH`` node of its own: the
    graph the context has made active (the union default graph, a dataset's
    default graph, or the graph rdflib pushed for a ``GRAPH`` it evaluated
    itself)."""
    return _GraphScope(store._graph_n3(getattr(ctx, "graph", None)))


def _graph_scope(ctx, node: "Graph") -> _GraphScope:
    """The scope a ``GRAPH`` block establishes, mirroring rdflib's ``evalGraph``:
    a bound term names one graph, an unbound variable ranges over the named
    graphs. A term that cannot name a graph is left to rdflib rather than
    guessed at: one other than an IRI or a blank node, and an IRI vortex-rdf's
    parser rejects (:func:`~vortex_rdflib.terms.unparseable_iri`), which the
    store answers with nothing."""
    term = node.term
    value = ctx[term]
    if value is None:
        return _GraphScope(None, term)
    if not isinstance(value, (URIRef, BNode)) or unparseable_iri(value):
        raise NotImplementedError
    return _GraphScope(canonical_spelling(value), None)


def _peel_graph(ctx, node: "Block", scope) -> tuple:
    """Look through ``GRAPH`` blocks that name one graph, so the
    single-pattern fast paths below still apply to a block inside a named
    graph."""
    while isinstance(node, Graph):
        inner = _graph_scope(ctx, node)
        if inner.var is not None:
            break
        node, scope = node.inner, inner
    return node, scope


@dataclass(frozen=True, slots=True, eq=False)
class Block:
    """A block — a subtree of the algebra the solver takes into one
    code-space relation — converted from its algebra node by :func:`block`,
    the one statement of what a block may hold.

    ``source`` is the algebra node. ``vars`` are the variable-like terms the
    block mentions, in first-seen order (static: what rdflib's ``_vars`` is
    made of); ``bound_vars`` those its solutions can bind (a MINUS's right
    side removes rows, it binds nothing); ``nullable`` those it may leave
    unbound — those an OPTIONAL's right side introduces, every one its left
    side does not bind, and a VALUES table's UNDEF.

    A ``GRAPH ?g``'s variable is nullable only where an OPTIONAL's right side
    names it and its left side does not bind it, or a VALUES table leaves it
    UNDEF; the solver leaves both to rdflib (see ``blocks._solve_left_join``,
    ``blocks._solve_minus`` and :func:`relation._shared_key_ok`). Otherwise
    the graph column is bound in every row below the ``GRAPH``, and when both
    sides of an OPTIONAL are under the same one, the join on it keeps them in
    the same graph.
    """

    source: Any
    vars: tuple
    bound_vars: tuple
    nullable: frozenset

    @property
    def name(self) -> str:
        """rdflib's name for the algebra node."""
        return self.source.name

    @property
    def single(self) -> tuple | None:
        """The one triple pattern the block is, if it is a BGP of one."""
        return None

    def carries_graph(self, var) -> bool:
        """Whether the rows of the block under ``GRAPH var`` carry its graph
        column: a pattern of the block is matched in that scope, so the
        column names the graph each row was solved in.

        A part without the column (an empty group, a VALUES table, a
        ``GRAPH`` of another graph) is the same in every named graph. A join
        with a part that has it repeats it per graph, as rdflib does; an
        OPTIONAL or a MINUS whose right side alone has it cannot, and raises
        ``NotImplementedError``."""
        return False


@dataclass(frozen=True, slots=True, eq=False)
class Bgp(Block):
    """A basic graph pattern: its ``triples``."""

    triples: tuple

    @property
    def single(self) -> tuple | None:
        return self.triples[0] if len(self.triples) == 1 else None

    def carries_graph(self, var) -> bool:
        return bool(self.triples)


@dataclass(frozen=True, slots=True, eq=False)
class Filter(Block):
    """A FILTER over the ``inner`` block; its expression is the source's."""

    inner: Block

    def carries_graph(self, var) -> bool:
        return self.inner.carries_graph(var)


@dataclass(frozen=True, slots=True, eq=False)
class Join(Block):
    """A group join of ``left`` and ``right``, ``lazy`` where rdflib solves
    the right side per left row."""

    left: Block
    right: Block
    lazy: bool

    def carries_graph(self, var) -> bool:
        left, right = self.left.carries_graph(var), self.right.carries_graph(var)
        return left or right


@dataclass(frozen=True, slots=True, eq=False)
class LeftJoin(Block):
    """An OPTIONAL of ``right`` on ``left``; its condition is the source's
    ``expr``."""

    left: Block
    right: Block

    def carries_graph(self, var) -> bool:
        return _left_carries_graph(self.left, self.right, var)


@dataclass(frozen=True, slots=True, eq=False)
class Minus(Block):
    """``left`` MINUS ``right``."""

    left: Block
    right: Block

    def carries_graph(self, var) -> bool:
        return _left_carries_graph(self.left, self.right, var)


@dataclass(frozen=True, slots=True, eq=False)
class Values(Block):
    """An inline VALUES table: its ``rows``, each a tuple of its terms in
    ``vars`` order, ``None`` for an UNDEF."""

    rows: tuple


@dataclass(frozen=True, slots=True, eq=False)
class Graph(Block):
    """A GRAPH ``term`` over the ``inner`` block. The graph term binds in
    every row of the block below it, and comes last, as it does in a
    matched pattern's schema."""

    term: Any
    inner: Block

    def carries_graph(self, var) -> bool:
        return self.term == var and self.inner.carries_graph(var)


def _left_carries_graph(left: Block, right: Block, var) -> bool:
    """An OPTIONAL's or a MINUS's :meth:`Block.carries_graph`: its left
    side's, which a right side that alone carries it cannot be."""
    left_carries, right_carries = left.carries_graph(var), right.carries_graph(var)
    if right_carries and not left_carries:
        raise NotImplementedError
    return left_carries


def _merged(first: tuple, second) -> tuple:
    """``first``, then the terms of ``second`` it lacks, in order."""
    return first + tuple(dict.fromkeys(term for term in second if term not in first))


def block(node) -> Block:
    """``node`` as the block the solver takes; ``NotImplementedError`` for
    anything it does not — the capability check, made eagerly, before
    anything is matched."""
    name = getattr(node, "name", None)
    if name == "BGP":
        triples = tuple(node.triples)
        found = _merged((), (t for triple in triples for t in triple if isinstance(t, _VAR_LIKE)))
        return Bgp(node, found, found, frozenset(), triples)
    if name == "Filter":
        inner = block(node.p)
        return Filter(node, inner.vars, inner.bound_vars, inner.nullable, inner)
    if name == "Graph":
        inner = block(node.p)
        term = node.term
        own = (term,) if isinstance(term, _VAR_LIKE) else ()
        return Graph(
            node,
            _merged(inner.vars, own),
            _merged(inner.bound_vars, own),
            inner.nullable,
            term,
            inner,
        )
    if name in ("Join", "LeftJoin", "Minus"):
        left, right = block(node.p1), block(node.p2)
        mentioned = _merged(left.vars, right.vars)
        if name == "Minus":
            return Minus(node, mentioned, left.bound_vars, left.nullable, left, right)
        bound = _merged(left.bound_vars, right.bound_vars)
        nullable = left.nullable | right.nullable
        if name == "Join":
            return Join(node, mentioned, bound, nullable, left, right, bool(node.lazy))
        nullable |= frozenset(right.vars) - frozenset(left.bound_vars)
        return LeftJoin(node, mentioned, bound, nullable, left, right)
    if name == "ToMultiSet" and getattr(node.p, "name", None) == "values":
        found, rows, nullable = _values_table(node.p.res)
        return Values(node, found, found, nullable, rows)
    raise NotImplementedError


def _values_table(rows) -> tuple[tuple, tuple, frozenset]:
    """A VALUES table as ``(variables, rows, nullable)``: its variables, in
    first-seen order; each row a tuple of its terms in that order
    (:func:`_cell`); and the variables some row leaves unbound."""
    found: dict = {}
    for row in rows:
        for var in row:
            if var not in found:
                found[var] = None
    variables = tuple(found)
    table = tuple([tuple([_cell(row.get(var)) for var in variables]) for row in rows])
    nullable = frozenset(
        [var for i, var in enumerate(variables) if any([row[i] is None for row in table])]
    )
    return variables, table, nullable


def _cell(value):
    """A VALUES row's term for a variable: ``None`` for ``UNDEF`` — rdflib's
    plain string, where a bound value is a term — or for no entry at all."""
    return None if value.__class__ is str and value == "UNDEF" else value


def _scoped_vars(node: Block, scope: _GraphScope) -> tuple:
    """A block's variable-like terms, plus the graph variable an enclosing
    ``GRAPH ?g`` binds in every row of it."""
    if scope.var is not None and scope.var not in node.vars:
        return (*node.vars, scope.var)
    return node.vars


def _block_vars(ctx, node: Block, scope: _GraphScope) -> list:
    """The variables a block binds: its variable-like terms the context has
    not bound, in first-seen order (static, no matching)."""
    return [term for term in _scoped_vars(node, scope) if ctx[term] is None]


def _group_vars(ctx, node: Block) -> list:
    """The variables a block's own solutions bind in rdflib's evaluation: its
    variable-like terms the context has not bound, none from a MINUS's right
    side, and an enclosing ``GRAPH ?g``'s variable only where the block binds
    it itself — rdflib's ``evalGraph`` joins the graph in after the group is
    solved."""
    return [var for var in node.bound_vars if ctx[var] is None]


def _unwrap_empty_joins(node):
    """rdflib never simplifies an EXISTS body, so a group translates to a
    Join with an empty BGP on one side; look through those."""
    while getattr(node, "name", None) == "Join":
        p1, p2 = node.p1, node.p2
        if getattr(p1, "name", None) == "BGP" and not p1.triples:
            node = p2
        elif getattr(p2, "name", None) == "BGP" and not p2.triples:
            node = p1
        else:
            break
    return node
