import os
from collections.abc import Callable
from pathlib import Path

from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, Dataset, Graph
from rdflib.store import NO_STORE, VALID_STORE, Store
from rdflib.term import BNode, IdentifiedNode, Node, URIRef
from vortex_rdf import VortexRdfStore

from . import query_scope
from .pushdown import _CHUNK_MAX, _CHUNK_START, register_sparql_pushdown
from .query_scope import QueryTerms
from .terms import (
    BLANK,
    IRI,
    LITERAL,
    _term_builder,
    canonical_spelling,
    kind_bounds,
    unparseable_iri,
)

#: Sentinel for a cached lookup whose answer may legitimately be ``None``.
_UNRESOLVED = object()

#: How the native layer spells the default graph: the fourth column of a quad
#: no ``GRAPH`` names is the empty string, and ``""`` is also how a pattern
#: selects those rows — ``None`` being the wildcard over every graph.
DEFAULT_GRAPH = ""


class _NoGraph(NotImplementedError):
    """A context names no graph the file can hold (see
    :meth:`VortexRdflibStore._graph_n3`). As a ``NotImplementedError`` it makes
    the pushdown leave the block to rdflib, whose ``triples()`` answers it with
    nothing."""


#: The layout names the constructor accepts: the layouts' own, ``typed_object``
#: and the ``cottas-native*`` names of COTTAS benchmark scripts. The layout is
#: only a label here: VortexRdfStore auto-detects the actual layout from the
#: file.
_LAYOUT_ALIASES = {
    "default": "default",
    "cottas-native-strings": "default",
    "typed-object": "typed-object",
    "typed_object": "typed-object",
    "dictionary": "dictionary",
    "cottas-native-ids": "dictionary",
    "cottas-native": "dictionary",
}


def _term_debug(t):
    if t is None:
        return "None"
    try:
        return t.n3()
    except Exception:
        return repr(t)


class VortexRdflibStore(Store):
    """Read-only rdflib Store over a `.vortex` file.

    SPARQL evaluation is rdflib's engine; this store serves quad patterns.
    Use as ``Graph(store=VortexRdflibStore("data.vortex"))`` for a view over the
    whole file, or ``Dataset(store=VortexRdflibStore("data.vortex"))`` for the named
    graphs it holds — see :meth:`_graph_n3` for how a context selects rows.
    """

    context_aware = True
    formula_aware = False
    transaction_aware = False
    # rdflib requires a graph-aware store to back a Dataset; the graphs
    # themselves come from the file (see `add_graph`/`remove_graph`).
    graph_aware = True

    def __init__(
        self,
        configuration=None,
        identifier=None,
        path: str | None = None,
        layout: str | None = None,
        backend: str = "native",
        *,
        in_memory: bool | None = None,
        **kwargs,
    ):
        # IMPORTANT:
        # RDFLib Store.__init__ may call self.open(configuration).
        # Therefore all attributes used by open() must exist BEFORE super().__init__.
        if "max_resident_bytes" in kwargs:
            raise TypeError(
                "max_resident_bytes was removed in vortex-rdflib 0.3.0: file-backed stores are "
                "memory-mapped and the OS page cache decides what stays in RAM"
            )
        if path is None:
            path = configuration

        self.path = str(Path(path)) if path is not None else None
        self.layout = _LAYOUT_ALIASES.get(layout, layout) if layout else None
        self.backend = backend
        # File-backed stores are memory-mapped: a query reads only the pages it
        # touches. vortex-rdf keeps a bounded cache of the dictionary windows
        # it reads; the OS page cache holds the rest. in_memory loads the whole
        # store up front instead. Env override for benchmark sweeps.
        if in_memory is None:
            in_memory = os.environ.get("VORTEX_RDF_IN_MEMORY") == "1"
        self.in_memory = in_memory
        self._use_codes = os.environ.get("VORTEX_RDF_DISABLE_CODE_PATH") != "1"
        self._native: VortexRdfStore | None = None
        self._dict = None
        # Everything a query learns about terms lives in its scope
        # (query_scope.QueryTerms, through `_terms()`); the store keeps
        # fixed-size structure only: the kind ranges, the default graph's
        # code, the indexes and one context per graph of the file.
        self._kind_bounds: tuple[int, int, int] | None = None
        self._ranges: dict | None = None
        self._indexes: tuple = ()
        self._default_code: int | None | object = _UNRESOLVED
        # One rdflib graph object per graph of the file, so a match yields
        # contexts without allocating one per row; keyed both ways a match
        # names a graph (its spelling, its dictionary code).
        self._contexts_by_spelling: dict[str, tuple] = {}
        self._contexts_by_code: dict[int, tuple] = {}
        self._all_contexts: list | None = None

        # Whole-BGP pushdown into code space (no-op for non-Vortex graphs;
        # VORTEX_RDF_DISABLE_PUSHDOWN=1 keeps rdflib's default evaluator).
        register_sparql_pushdown()

        # Do not pass configuration here, otherwise RDFLib calls open()
        # before our initialization logic is fully under control.
        super().__init__(configuration=None, identifier=identifier)

        # Explicitly open after initialization.
        if self.path is not None:
            self.open(self.path)

    def open(self, configuration, create=False):
        """RDFLib Store API: configuration is the Vortex file path."""
        if self.backend != "native":
            raise ValueError(
                f"Unsupported Vortex backend: {self.backend!r} (only 'native' is available)"
            )

        if configuration is not None:
            self.path = str(Path(configuration))

        if self.path is None:
            return NO_STORE

        self._native = VortexRdfStore(self.path, in_memory=self.in_memory)
        # Record what the file actually is, whatever label the caller passed.
        self.layout = self._native.layout()
        self._indexes = tuple(self._native.indexes())
        # Dictionary-layout stores serve matches as u64 code columns, the
        # dictionary memory-mapped with the file (or loaded with the store);
        # each distinct code is decoded to an rdflib term once per query. None
        # on other layouts -> string fallback path.
        self._dict = self._native.term_dict() if self._use_codes else None
        self._kind_bounds = None
        self._ranges = None
        self._default_code = _UNRESOLVED
        self._contexts_by_spelling = {}
        self._contexts_by_code = {}
        self._all_contexts = None
        return VALID_STORE

    def close(self, commit_pending_transaction=False):
        self._native = None
        self._dict = None
        self._kind_bounds = None
        self._ranges = None
        self._indexes = ()
        self._default_code = _UNRESOLVED
        self._contexts_by_spelling = {}
        self._contexts_by_code = {}
        self._all_contexts = None

    def _store(self) -> VortexRdfStore:
        if self._native is None:
            if self.path is None:
                raise ValueError("VortexRdflibStore has no path; pass one or call open()")
            self.open(self.path)
        assert self._native is not None  # open() either sets it or raises
        return self._native

    def _graph_n3(self, context) -> str | None:
        """The native graph spelling a context selects, ``None`` for the union.

        ``None`` and any view whose default graph is the union of the others
        select every graph; rdflib's default-graph identifier (a ``Dataset``'s
        default graph) selects the unnamed rows; any other identified graph
        selects itself.

        A bare ``Graph(store=VortexRdflibStore(path))`` is the union view over
        the whole file: rdflib gives a graph constructed without an identifier
        a blank node, and that blank node names nothing the file holds — see
        :meth:`_blank_graph_n3`, which tells it apart from a real blank-node
        graph name.

        An identifier that names no graph the file can hold raises
        :class:`_NoGraph`: a term other than an IRI or a blank node, or an IRI
        vortex-rdf's parser rejects, which is what rdflib's ``get_context``
        builds from a literal a ``GRAPH`` variable is bound to (``"zz"``
        becomes ``URIRef("zz")``). A count capped at zero rows parses the name
        without reading a row; a graph the file is known to hold skips it.
        """
        if context is None:
            return None
        if isinstance(context, Dataset):
            # A dataset resolves the active graph itself before it calls the
            # store, so this is the direct-call path — and `ctx.graph` under
            # the SPARQL pushdown, which asks the same question. Without union
            # its default graph is rdflib's, by definition of `Dataset`.
            return None if context.default_union else DEFAULT_GRAPH
        if getattr(context, "default_union", False):
            # Any other view whose default graph is the union of the graphs it
            # holds — rdflib's deprecated `ConjunctiveGraph` is the one that
            # exists today. Keyed on the property rather than the class, so
            # this neither imports a deprecated name nor breaks when it goes:
            # a plain `Graph` carries `default_union = False`.
            return None
        identifier = getattr(context, "identifier", context)
        if identifier == DATASET_DEFAULT_GRAPH_ID:
            return DEFAULT_GRAPH
        if isinstance(identifier, BNode):
            return self._blank_graph_n3(identifier)
        if not isinstance(identifier, URIRef):
            raise _NoGraph
        spelling = canonical_spelling(identifier)
        if spelling not in self._contexts_by_spelling:
            try:
                self._store().count_quads(None, None, None, spelling, limit=0)
            except ValueError:
                raise _NoGraph from None
        return spelling

    def _blank_graph_n3(self, identifier: BNode) -> str | None:
        """A blank-node-identified graph: itself if the file holds a graph by
        that name, otherwise the union over every graph.

        Both readings are real. ``_:b0`` is a legal graph name in N-Quads, and
        rdflib also labels a ``Graph()`` built without an identifier with a
        blank node — which is what ``Graph(store=VortexRdflibStore(path))`` is,
        the view over the whole file. One ``count_quads`` off the row
        selection settles it exactly (a blank node the file uses only as a
        *subject* names no graph, and answers zero), and the answer is kept
        for the query (the store is read-only).
        """
        spelling = canonical_spelling(identifier)
        known = self._terms().blank_graphs
        names_a_graph = known.get(spelling)
        if names_a_graph is None:
            names_a_graph = known[spelling] = (
                self._store().count_quads(None, None, None, spelling) > 0
            )
        return spelling if names_a_graph else None

    def _context_tuple(self, spelling: str, by_spelling: dict | None = None) -> tuple:
        """The one-graph context tuple ``triples()`` yields for a graph of the
        file.

        Kept per graph: rdflib only ever iterates it, so one tuple per graph
        keeps a match from allocating a context per row. Only graphs the file
        holds are asked for: a name read from the graph column, or a name a
        caller asks for once a matched row proves the file holds it.
        ``by_spelling`` is the cache to read and fill: the store's, unless a
        call bound the one of the file it started on.
        """
        if by_spelling is None:
            by_spelling = self._contexts_by_spelling
        contexts = by_spelling.get(spelling)
        if contexts is None:
            identifier: IdentifiedNode = DATASET_DEFAULT_GRAPH_ID
            if spelling != DEFAULT_GRAPH:
                name = self._from_n3_safe(spelling)
                if not isinstance(name, IdentifiedNode):
                    # Only an IRI or a blank node can name a graph; anything
                    # else in the graph column is a malformed file.
                    raise ValueError(f"graph name is not an IRI or blank node: {spelling!r}")
                identifier = name
            contexts = by_spelling[spelling] = (Graph(store=self, identifier=identifier),)
        return contexts

    def _graph_contexts(self) -> Callable[[int], tuple]:
        """The context tuple of a graph column's code, resolved through the
        dictionary and context caches the store holds now: a call that keeps
        this resolves against the file it started on, whatever ``close()``
        or ``open()`` does meanwhile (each replaces those, never clears
        them)."""
        term_dict = self._dict
        if term_dict is None:
            raise ValueError("store has no term dictionary (code path inactive)")
        by_code, by_spelling = self._contexts_by_code, self._contexts_by_spelling

        def context_of(code: int) -> tuple:
            contexts = by_code.get(code)
            if contexts is None:
                spelling = term_dict.decode(code)
                if spelling is None:
                    raise ValueError(f"term code {code} is not in the store dictionary")
                contexts = by_code[code] = self._context_tuple(spelling, by_spelling)
            return contexts

        return context_of

    def triples(self, triple_pattern, context=None):
        """RDFLib Store API: yields ``((s, p, o), contexts)`` for a pattern.

        ``context`` restricts the match to one graph (:meth:`_graph_n3` maps
        it to a graph name); ``None`` — and a bare ``Graph`` over this store —
        is the union over every graph, where a triple several graphs hold is
        yielded once per graph; a context that names no graph the file can
        hold yields nothing. The store streams the quads it holds rather than
        building the RDF merge of the union, which would mean a set the size
        of the result, and decodes them a chunk at a time, the first of 64
        rows, so a consumer that stops early decodes only the chunks it
        reached.
        """
        if self.path is None:
            return

        s, p, o = triple_pattern

        # RDFLib can propagate an object binding into subject or predicate
        # position during joins. That pattern is unsatisfiable, not an error.
        if s is not None and not isinstance(s, (URIRef, BNode)):
            return
        if p is not None and not isinstance(p, URIRef):
            return
        # So is one holding an IRI vortex-rdf's parser rejects, which is no
        # term of the file.
        if unparseable_iri(s) or unparseable_iri(p) or unparseable_iri(o):
            return

        s_n3 = self._node_to_n3(s)
        p_n3 = self._node_to_n3(p)
        o_n3 = self._node_to_n3(o)
        try:
            g_n3 = self._graph_n3(context)
        except _NoGraph:
            return
        # One named graph: every matched row is in it, so the contexts are one
        # tuple and the graph column is never read. The tuple is taken once
        # the match has a row, which proves the file holds the graph; a name
        # that matches nothing leaves no state behind.
        fixed = None

        if os.environ.get("VORTEX_RDF_TRACE_TRIPLES") == "1":
            print(
                "[VortexRdflibStore.triples:start] "
                f"layout={self.layout} "
                f"s={_term_debug(s)} p={_term_debug(p)} o={_term_debug(o)} "
                f"s_n3={s_n3} p_n3={p_n3} o_n3={o_n3} g_n3={g_n3!r}",
                flush=True,
            )

        store = self._store()

        # Fully-ground pattern: no term needs materializing. In one graph it
        # is an existence check — counting from the row selection preserves
        # multiplicity (the quad repeated in the graph) by yielding once per
        # match. Under the union only the graph column is read: one row per
        # graph holding the triple, each with that graph's context.
        if s_n3 is not None and p_n3 is not None and o_n3 is not None:
            triple = (s, p, o)
            if g_n3 is not None:
                count = store.count_quads(s_n3, p_n3, o_n3, g_n3)
                if count:
                    fixed = self._context_tuple(g_n3)
                    for _ in range(count):
                        yield triple, fixed
                return
            if self._dict is not None:
                cols = store.match_codes(s_n3, p_n3, o_n3, None)
                if cols is not None:
                    context_of = self._graph_contexts()
                    for code in memoryview(cols[3]).cast("Q").tolist():
                        yield triple, context_of(code)
                    return
            *_, graphs = store.match_columns(s_n3, p_n3, o_n3, None)
            by_spelling = self._contexts_by_spelling
            for g_raw in graphs:
                yield triple, self._context_tuple(g_raw, by_spelling)
            return

        # The dictionary, and the graph contexts below, are bound once: rows
        # read after a close(), or an open() of another file, still come from
        # the file the call started on.
        term_dict = self._dict
        if term_dict is not None:
            cols = store.match_codes(s_n3, p_n3, o_n3, g_n3)
            if cols is not None:
                rows = len(cols[0])
                # The columns of one native match are equally long; reading
                # them by the first one's length would hide a native bug.
                if not len(cols[1]) == len(cols[2]) == len(cols[3]) == rows:
                    lengths = [len(column) for column in cols]
                    raise ValueError(f"match columns of unequal lengths: {lengths}")
                if not rows:
                    return
                if g_n3 is not None:
                    fixed = self._context_tuple(g_n3)
                context_of = self._graph_contexts()
                # Zero-copy u64 views over the Rust column buffers, read a
                # chunk at a time: 64 rows, then four times more, 4096 at
                # most. Each chunk's codes become Python ints, and those the
                # call's term cache (the query's, inside one) lacks are
                # decoded in one GIL-released decode_many call, so a consumer
                # that stops early (rdflib's LIMIT, ASK) decodes only the
                # chunks it reached.
                views = [memoryview(column).cast("Q") for column in cols]
                cache = self._terms().decode
                contexts: dict[int, tuple] = {}
                start, size = 0, _CHUNK_START
                while start < rows:
                    stop = min(start + size, rows)
                    # A match that is one chunk is read from the views whole.
                    chunk = views if stop - start == rows else [v[start:stop] for v in views]
                    s_codes = chunk[0].tolist()
                    p_codes = chunk[1].tolist()
                    o_codes = chunk[2].tolist()
                    self._decode_into(term_dict, cache, (s_codes, p_codes, o_codes))
                    shared = fixed
                    if shared is None:
                        # Union: the fourth column names each row's graph. A
                        # chunk whose rows all live in one graph — every chunk
                        # of a single-graph file — shares that graph's context
                        # and pays no per-row lookup.
                        g_codes = chunk[3].tolist()
                        graphs = set(g_codes)
                        for code in graphs.difference(contexts):
                            contexts[code] = context_of(code)
                        if len(graphs) == 1:
                            shared = contexts[g_codes[0]]
                        else:
                            for row in zip(s_codes, p_codes, o_codes, g_codes, strict=True):
                                yield (
                                    (cache[row[0]], cache[row[1]], cache[row[2]]),
                                    contexts[row[3]],
                                )
                    if shared is not None:
                        for row in zip(s_codes, p_codes, o_codes, strict=True):
                            yield (cache[row[0]], cache[row[1]], cache[row[2]]), shared
                    start, size = stop, min(size * 4, _CHUNK_MAX)
                return

        # String fallback (non-dictionary layouts, code path disabled):
        # match_columns shares one Python string object across repeats of a
        # term, so memoizing the parse by string turns per-row term
        # construction into per-distinct-term construction.
        subjects, predicates, objects, graphs = store.match_columns(s_n3, p_n3, o_n3, g_n3)
        by_spelling = self._contexts_by_spelling
        if g_n3 is not None and subjects:
            fixed = self._context_tuple(g_n3, by_spelling)
        parse = self._from_n3_safe
        memo: dict[str, Node] = {}
        for s_raw, p_raw, o_raw, g_raw in zip(subjects, predicates, objects, graphs, strict=True):
            row = []
            for raw in (s_raw, p_raw, o_raw):
                node = memo.get(raw)
                if node is None:
                    node = memo[raw] = parse(raw)
                row.append(node)
            yield (
                (row[0], row[1], row[2]),
                (fixed if fixed is not None else self._context_tuple(g_raw, by_spelling)),
            )

    def contexts(self, triple=None):
        """RDFLib Store API: the graphs holding ``triple``, or every graph.

        There is no graph index, so the names come from the fourth column of
        a match — a whole-store call walks the store once. The store is
        read-only, so that walk is cached for its lifetime.
        """
        if self.path is None:
            return iter(())
        if triple is None or triple == (None, None, None):
            if self._all_contexts is None:
                self._all_contexts = self._distinct_graphs(None, None, None)
            return iter(self._all_contexts)
        s, p, o = triple
        if (
            (s is not None and not isinstance(s, (URIRef, BNode)))
            or (p is not None and not isinstance(p, URIRef))
            or any(unparseable_iri(term) for term in triple)
        ):
            return iter(())
        return iter(
            self._distinct_graphs(self._node_to_n3(s), self._node_to_n3(p), self._node_to_n3(o))
        )

    def _distinct_graphs(self, s_n3, p_n3, o_n3) -> list:
        """The distinct graphs of a pattern's match, in first-seen order."""
        store = self._store()
        if self._dict is not None:
            cols = store.match_codes(s_n3, p_n3, o_n3, None)
            if cols is not None:
                # Native distinct, in first-seen order.
                codes = memoryview(cols[3].distinct()).cast("Q").tolist()
                context_of = self._graph_contexts()
                return [context_of(code)[0] for code in codes]
        *_spo, graphs = store.match_columns(s_n3, p_n3, o_n3, None)
        return [self._context_tuple(name)[0] for name in dict.fromkeys(graphs)]

    def _terms(self) -> QueryTerms:
        """The term state of the query being evaluated (:mod:`.query_scope`),
        or a fresh one for a call outside any query."""
        return query_scope.terms_for(self)

    def _prime_decode_cache(self, *code_columns) -> dict:
        """Decode every code the query's term cache lacks in one native call
        (:meth:`_decode_into`), and return that cache (code -> term)."""
        cache = self._terms().decode
        self._decode_into(self._dict, cache, code_columns)
        return cache

    def _decode_into(self, term_dict, cache: dict, code_columns) -> None:
        """Decode into ``cache``, through ``term_dict``, every code of
        ``code_columns`` it lacks.

        ``decode_many`` releases the GIL for the whole batch and returns one
        shared string per distinct code, so the per-term cost collapses from
        one FFI round trip each to one bulk call per batch.
        """
        if term_dict is None:
            raise ValueError("store has no term dictionary (code path inactive)")
        distinct: set[int] = set()
        for column in code_columns:
            distinct.update(column)
        # The difference walks the smaller side: a cache holding fewer codes
        # than the batch (the first chunks of a call outside a query) is
        # walked once, rather than each code looked up in it.
        if len(cache) < len(distinct):
            distinct.difference_update(cache)
            missing = list(distinct)
        else:
            missing = list(distinct.difference(cache))
        if missing:
            build = self._from_n3_safe
            for code, raw in zip(missing, term_dict.decode_many(missing), strict=True):
                if raw is None:
                    raise ValueError(f"term code {code} is not in the store dictionary")
                cache[code] = build(raw)

    def _default_graph_code(self) -> int | None:
        """The dictionary code of the default graph's empty name, or ``None``
        when the file holds no default-graph rows (the dictionary is
        immutable, so this is looked up once)."""
        if self._default_code is _UNRESOLVED:
            if self._dict is None:
                raise ValueError("store has no term dictionary (code path inactive)")
            self._default_code = self._dict.encode(DEFAULT_GRAPH)
        return self._default_code  # ty: ignore[invalid-return-type]

    def _term_kind_bounds(self) -> tuple[int, int, int]:
        """The first code of each term kind (literal, IRI, blank node); the
        dictionary is immutable, so three binary searches, once."""
        if self._kind_bounds is None:
            if self._dict is None:
                raise ValueError("store has no term dictionary (code path inactive)")
            self._kind_bounds = kind_bounds(self._dict)
        return self._kind_bounds

    def _kind_ranges(self) -> dict:
        """Each term kind's half-open code range (``TermDict.prefix_range``
        of ``"``, ``<`` and ``_:``): codes rank spellings in byte order, so a
        kind is one range — the keep a kind test pushes into a match."""
        if self._ranges is None:
            if self._dict is None:
                raise ValueError("store has no term dictionary (code path inactive)")
            self._ranges = {
                LITERAL: tuple(self._dict.prefix_range('"')),
                IRI: tuple(self._dict.prefix_range("<")),
                BLANK: tuple(self._dict.prefix_range("_:")),
            }
        return self._ranges

    def _foreign_code(self, term: Node) -> int:
        """The negative code standing for a term outside the dictionary, for
        the rest of the query."""
        terms = self._terms()
        code = terms.foreign_codes.get(term)
        if code is None:
            code = terms.foreign_codes[term] = -1 - len(terms.foreign_codes)
            terms.foreign[code] = term
        return code

    def _decode_term(self, code: int) -> Node:
        terms = self._terms()
        if code < 0:
            return terms.foreign[code]
        node = terms.decode.get(code)
        if node is None:
            if self._dict is None:
                raise ValueError("store has no term dictionary (code path inactive)")
            raw = self._dict.decode(code)
            if raw is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            node = terms.decode[code] = self._from_n3_safe(raw)
        return node

    def __len__(self, context=None):
        """Quads in ``context``, or in the whole store for the union view.

        The union counts quads, not distinct triples: a triple several graphs
        hold counts once per graph, exactly as ``triples()`` yields it. A
        context that names no graph the file can hold has none.
        """
        if self.path is None:
            return 0
        try:
            g_n3 = self._graph_n3(context)
        except _NoGraph:
            return 0
        if g_n3 is None:
            return len(self._store())
        return self._store().count_quads(None, None, None, g_n3)

    def add(self, triple, context=None, quoted=False):
        raise NotImplementedError("VortexRdflibStore is read-only")

    def addN(self, quads):
        raise NotImplementedError("VortexRdflibStore is read-only")

    def remove(self, triple, context=None):
        raise NotImplementedError("VortexRdflibStore is read-only")

    def add_graph(self, graph):
        """No-op: the file's graphs are the store's graphs.

        A ``Dataset`` calls this whenever it hands out a named graph
        (``ds.graph(name)``), so it cannot raise. Nothing is added — a
        read-only store cannot gain a graph, and a named graph with no quads
        has nothing to hold; the view it returns is simply empty.
        """

    def remove_graph(self, graph):
        raise NotImplementedError("VortexRdflibStore is read-only")

    def bind(self, prefix, namespace, override=True):
        return None

    def namespace(self, prefix):
        return None

    def namespaces(self):
        return iter(())

    def prefix(self, namespace):
        return None

    @staticmethod
    def _node_to_n3(node: Node | None) -> str | None:
        """A pattern term in the N-Triples spelling the native pattern parser
        accepts (rdflib's ``n3()`` spells a multi-line literal with triple
        quotes, which N-Triples does not have)."""
        if node is None:
            return None
        return canonical_spelling(node)

    #: The rdflib term for a stored N-Triples spelling: built directly where
    #: the import-time check finds that equal to rdflib's constructors
    #: (`_term_builder`), by the constructors otherwise.
    _from_n3_safe = staticmethod(_term_builder())
