"""What a block of the algebra is, read without matching anything: whether
the block solver takes it (:func:`_check_block`), the variables it
mentions, binds and may leave unbound, and the graph scope its patterns
are matched in (:class:`_GraphScope`).
"""

from dataclasses import dataclass

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


def _graph_scope(ctx, node) -> _GraphScope:
    """The scope a ``Graph`` node establishes, mirroring rdflib's ``evalGraph``:
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


def _peel_graph(ctx, node, scope) -> tuple:
    """Look through ``GRAPH`` nodes that name one graph, so the single-pattern
    fast paths below still apply to a block inside a named graph."""
    while getattr(node, "name", None) == "Graph":
        inner = _graph_scope(ctx, node)
        if inner.var is not None:
            break
        node, scope = node.p, inner
    return node, scope


def _check_block(node) -> None:
    """Eagerly reject anything the block solver does not handle.

    A *block* is a subtree solved into one code-space relation; this walk and
    `_solve_block`'s dispatch are the two statements of what that subtree may
    contain.
    """
    name = getattr(node, "name", None)
    if name == "BGP":
        return
    if name in ("Filter", "Graph"):
        _check_block(node.p)
        return
    if name in ("Join", "LeftJoin", "Minus"):
        _check_block(node.p1)
        _check_block(node.p2)
        return
    if name == "ToMultiSet" and getattr(node.p, "name", None) == "values":
        return
    raise NotImplementedError


def _node_vars(node, out: list | None = None, *, bound: bool = False) -> list:
    """Every variable-like term a block mentions, in first-seen order
    (static: what rdflib's ``_vars`` is made of). With ``bound``, only those
    its solutions can bind: a MINUS's right side removes rows, it binds
    nothing."""
    if out is None:
        out = []
    name = node.name
    if name == "BGP":
        for triple in node.triples:
            for term in triple:
                if isinstance(term, _VAR_LIKE) and term not in out:
                    out.append(term)
    elif name == "Filter":
        _node_vars(node.p, out, bound=bound)
    elif name == "ToMultiSet":
        for row in node.p.res:
            for term in row:
                if term not in out:
                    out.append(term)
    elif name == "Graph":
        # The graph term binds in every row of the block below it, and comes
        # last, as it does in a matched pattern's schema.
        _node_vars(node.p, out, bound=bound)
        if isinstance(node.term, _VAR_LIKE) and node.term not in out:
            out.append(node.term)
    else:
        _node_vars(node.p1, out, bound=bound)
        if not (bound and name == "Minus"):
            _node_vars(node.p2, out, bound=bound)
    return out


def _scoped_vars(node, scope: _GraphScope) -> list:
    """A block's variable-like terms, plus the graph variable an enclosing
    ``GRAPH ?g`` binds in every row of it."""
    out = _node_vars(node)
    if scope.var is not None and scope.var not in out:
        out.append(scope.var)
    return out


def _block_vars(ctx, node, scope: _GraphScope) -> list:
    """The variables a block binds: its variable-like terms the context has
    not bound, in first-seen order (static, no matching)."""
    return [term for term in _scoped_vars(node, scope) if ctx[term] is None]


def _group_vars(ctx, node) -> list:
    """The variables a block's own solutions bind in rdflib's evaluation: its
    variable-like terms the context has not bound, none from a MINUS's right
    side, and an enclosing ``GRAPH ?g``'s variable only where the block binds
    it itself — rdflib's ``evalGraph`` joins the graph in after the group is
    solved."""
    return [var for var in _node_vars(node, bound=True) if ctx[var] is None]


def _carries_graph(node, var) -> bool:
    """Whether the rows of a block under ``GRAPH var`` carry its graph
    column: a pattern of the block is matched in that scope, so the column
    names the graph each row was solved in.

    A part without the column (an empty group, a VALUES table, a ``GRAPH`` of
    another graph) is the same in every named graph. A join with a part that
    has it repeats it per graph, as rdflib does; an OPTIONAL or a MINUS whose
    right side alone has it cannot, and raises ``NotImplementedError``.
    """
    name = node.name
    if name == "BGP":
        return bool(node.triples)
    if name == "Filter":
        return _carries_graph(node.p, var)
    if name == "Graph":
        return node.term == var and _carries_graph(node.p, var)
    if name in ("Join", "LeftJoin", "Minus"):
        left, right = _carries_graph(node.p1, var), _carries_graph(node.p2, var)
        if name == "Join":
            return left or right
        if right and not left:
            raise NotImplementedError
        return left
    return False


def _block_nullable(node) -> frozenset:
    """The variables a block may leave unbound: those an OPTIONAL side
    introduces, every one its left side does not bind.

    A ``GRAPH ?g``'s variable is among them only where an OPTIONAL's right
    side names it and its left side does not bind it, or a VALUES table
    leaves it UNDEF; the solver leaves both to rdflib (see
    :func:`_solve_left_join`, :func:`_solve_minus` and :func:`_shared_key_ok`).
    Otherwise the graph column is bound in every row below the ``GRAPH``,
    and when both sides of an OPTIONAL are under the same one, the join on it
    keeps them in the same graph.
    """
    name = node.name
    if name == "BGP":
        return frozenset()
    if name == "Graph":
        return _block_nullable(node.p)
    if name == "ToMultiSet":
        rows = node.p.res
        return frozenset(
            v for v in _node_vars(node) if any(row.get(v, "UNDEF") == "UNDEF" for row in rows)
        )
    if name == "Filter":
        return _block_nullable(node.p)
    if name == "Minus":
        return _block_nullable(node.p1)
    nullable = _block_nullable(node.p1) | _block_nullable(node.p2)
    if name == "LeftJoin":
        nullable |= frozenset(_node_vars(node.p2)) - frozenset(_node_vars(node.p1, bound=True))
    return nullable


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
