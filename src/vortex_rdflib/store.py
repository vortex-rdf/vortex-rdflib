import os
from pathlib import Path

from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, Dataset, Graph
from rdflib.store import NO_STORE, VALID_STORE, Store
from rdflib.term import BNode, IdentifiedNode, Literal, Node, URIRef
from rdflib.util import from_n3
from vortex_rdf import VortexRdfStore

from .pushdown import register_sparql_pushdown
from .terms import canonical_spelling, kind_bounds

# The cottas-bench branch's layout names are accepted as aliases so its
# benchmark scripts keep working. The layout is only a label here: VortexRdfStore
# auto-detects the actual layout from the file.
#: Sentinel for a cached lookup whose answer may legitimately be ``None``.
_UNRESOLVED = object()

#: How the native layer spells the default graph: the fourth column of a quad
#: no ``GRAPH`` names is the empty string, and ``""`` is also how a pattern
#: selects those rows — ``None`` being the wildcard over every graph.
DEFAULT_GRAPH = ""

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
        max_resident_bytes: int | None = None,
        in_memory: bool | None = None,
        **kwargs,
    ):
        # IMPORTANT:
        # RDFLib Store.__init__ may call self.open(configuration).
        # Therefore all attributes used by open() must exist BEFORE super().__init__.
        if path is None:
            path = configuration

        self.path = str(Path(path)) if path is not None else None
        self.layout = _LAYOUT_ALIASES.get(layout, layout) if layout else None
        self.backend = backend
        self.max_resident_bytes = max_resident_bytes
        # File-backed lazy open by default; in-memory drops the ~1 ms per-call
        # file-scan floor (decisive for rdflib joins) at the cost of loading
        # the store up front. Env override for benchmark sweeps.
        if in_memory is None:
            in_memory = os.environ.get("VORTEX_RDF_IN_MEMORY") == "1"
        self.in_memory = in_memory
        self._native: VortexRdfStore | None = None
        self._dict = None
        self._decode_cache: dict = {}
        self._kind_bounds: tuple[int, int, int] | None = None
        self._default_code: int | None | object = _UNRESOLVED
        # Query constants the dictionary does not hold get negative codes, so
        # a VALUES row can be joined in code space and still yield its term.
        self._foreign: dict[int, Node] = {}
        self._foreign_codes: dict[Node, int] = {}
        # One rdflib graph object per graph name, so a match yields contexts
        # without allocating one per row; keyed both ways a match names a
        # graph (its spelling, its dictionary code).
        self._contexts_by_spelling: dict[str, tuple] = {}
        self._contexts_by_code: dict[int, tuple] = {}
        self._all_contexts: list | None = None
        # Whether a blank node names a graph of this file (see _blank_graph_n3).
        self._blank_graphs: dict[str, bool] = {}
        self._use_codes = os.environ.get("VORTEX_RDF_DISABLE_CODE_PATH") != "1"

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

        self._native = VortexRdfStore(
            self.path,
            max_resident_bytes=self.max_resident_bytes,
            in_memory=self.in_memory,
        )
        # Record what the file actually is, whatever label the caller passed.
        self.layout = self._native.layout()
        # Dictionary-layout stores with a resident dictionary serve matches as
        # u32 code columns; each distinct code is decoded to an rdflib term
        # once and cached. None on other layouts -> string fallback path.
        self._dict = self._native.term_dict() if self._use_codes else None
        self._decode_cache = {}
        self._kind_bounds = None
        self._default_code = _UNRESOLVED
        self._foreign = {}
        self._foreign_codes = {}
        self._contexts_by_spelling = {}
        self._contexts_by_code = {}
        self._all_contexts = None
        self._blank_graphs = {}
        return VALID_STORE

    def close(self, commit_pending_transaction=False):
        self._native = None
        self._dict = None
        self._decode_cache = {}
        self._kind_bounds = None
        self._default_code = _UNRESOLVED
        self._foreign = {}
        self._foreign_codes = {}
        self._contexts_by_spelling = {}
        self._contexts_by_code = {}
        self._all_contexts = None
        self._blank_graphs = {}

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
        return canonical_spelling(identifier)

    def _blank_graph_n3(self, identifier: BNode) -> str | None:
        """A blank-node-identified graph: itself if the file holds a graph by
        that name, otherwise the union over every graph.

        Both readings are real. ``_:b0`` is a legal graph name in N-Quads, and
        rdflib also labels a ``Graph()`` built without an identifier with a
        blank node — which is what ``Graph(store=VortexRdflibStore(path))`` is,
        the view over the whole file. One ``count_quads`` off the row
        selection settles it exactly (a blank node the file uses only as a
        *subject* names no graph, and answers zero), and the store is
        read-only, so the answer is cached.
        """
        spelling = canonical_spelling(identifier)
        names_a_graph = self._blank_graphs.get(spelling)
        if names_a_graph is None:
            names_a_graph = self._blank_graphs[spelling] = (
                self._store().count_quads(None, None, None, spelling) > 0
            )
        return spelling if names_a_graph else None

    def _context_tuple(self, spelling: str) -> tuple:
        """The one-graph context tuple ``triples()`` yields for a graph name.

        Cached per name: rdflib only ever iterates it, so one tuple per graph
        keeps a match from allocating a context per row.
        """
        contexts = self._contexts_by_spelling.get(spelling)
        if contexts is None:
            identifier: IdentifiedNode = DATASET_DEFAULT_GRAPH_ID
            if spelling != DEFAULT_GRAPH:
                name = self._from_n3_safe(spelling)
                if not isinstance(name, IdentifiedNode):
                    # Only an IRI or a blank node can name a graph; anything
                    # else in the graph column is a malformed file.
                    raise ValueError(f"graph name is not an IRI or blank node: {spelling!r}")
                identifier = name
            contexts = self._contexts_by_spelling[spelling] = (
                Graph(store=self, identifier=identifier),
            )
        return contexts

    def _context_of_code(self, code: int) -> tuple:
        """The context tuple for a graph column's term code."""
        contexts = self._contexts_by_code.get(code)
        if contexts is None:
            if self._dict is None:
                raise ValueError("store has no resident term dictionary (code path inactive)")
            spelling = self._dict.decode(code)
            if spelling is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            contexts = self._contexts_by_code[code] = self._context_tuple(spelling)
        return contexts

    def triples(self, triple_pattern, context=None):
        """RDFLib Store API: yields ``((s, p, o), contexts)`` for a pattern.

        ``context`` restricts the match to one graph (:meth:`_graph_n3` maps
        it to a graph name); ``None`` — and a bare ``Graph`` over this store —
        is the union over every graph, where a triple several graphs hold is
        yielded once per graph. The store streams the quads it holds rather
        than building the RDF merge of the union, which would mean a set the
        size of the result.
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

        s_n3 = self._node_to_n3(s)
        p_n3 = self._node_to_n3(p)
        o_n3 = self._node_to_n3(o)
        g_n3 = self._graph_n3(context)
        # One named graph: every matched row is in it, so the contexts are one
        # cached tuple and the graph column is never read.
        fixed = None if g_n3 is None else self._context_tuple(g_n3)

        if os.environ.get("VORTEX_RDF_TRACE_TRIPLES") == "1":
            print(
                "[VortexRdflibStore.triples:start] "
                f"layout={self.layout} "
                f"s={_term_debug(s)} p={_term_debug(p)} o={_term_debug(o)} "
                f"s_n3={s_n3} p_n3={p_n3} o_n3={o_n3} g_n3={g_n3!r}",
                flush=True,
            )

        store = self._store()

        # Fully-ground pattern in one graph: an existence check. Counting from
        # the row selection materializes no term at all; multiplicity (the
        # quad repeated in the graph) is preserved by yielding once per match.
        if fixed is not None and s_n3 is not None and p_n3 is not None and o_n3 is not None:
            for _ in range(store.count_quads(s_n3, p_n3, o_n3, g_n3)):
                yield (s, p, o), fixed
            return

        if self._dict is not None:
            cols = store.match_codes(s_n3, p_n3, o_n3, g_n3)
            if cols is not None:
                # Zero-copy u32 views over the Rust column buffers; every
                # distinct code not yet in the store-lifetime cache is decoded
                # in one GIL-released decode_many call (codes are stable in a
                # read-only store).
                s_codes = memoryview(cols[0]).cast("I").tolist()
                p_codes = memoryview(cols[1]).cast("I").tolist()
                o_codes = memoryview(cols[2]).cast("I").tolist()
                self._prime_decode_cache(s_codes, p_codes, o_codes)
                cached = self._decode_cache
                # strict: the columns come from one native match and must be
                # equally long; truncation would hide a native bug.
                if fixed is not None:
                    for row in zip(s_codes, p_codes, o_codes, strict=True):
                        yield (cached[row[0]], cached[row[1]], cached[row[2]]), fixed
                    return
                # Union: the fourth column names each row's graph, and each
                # distinct graph code maps to one cached context tuple.
                g_codes = memoryview(cols[3]).cast("I").tolist()
                contexts = self._contexts_by_code
                for row in zip(s_codes, p_codes, o_codes, g_codes, strict=True):
                    ctx = contexts.get(row[3])
                    if ctx is None:
                        ctx = self._context_of_code(row[3])
                    yield (cached[row[0]], cached[row[1]], cached[row[2]]), ctx
                return

        # String fallback (non-dictionary layouts, non-resident dictionary):
        # match_columns shares one Python string object across repeats of a
        # term, so memoizing the parse by string turns per-row term
        # construction into per-distinct-term construction.
        subjects, predicates, objects, graphs = store.match_columns(s_n3, p_n3, o_n3, g_n3)
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
                (fixed if fixed is not None else self._context_tuple(g_raw)),
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
        if (s is not None and not isinstance(s, (URIRef, BNode))) or (
            p is not None and not isinstance(p, URIRef)
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
                codes = dict.fromkeys(memoryview(cols[3]).cast("I"))
                return [self._context_of_code(code)[0] for code in codes]
        *_spo, graphs = store.match_columns(s_n3, p_n3, o_n3, None)
        return [self._context_tuple(name)[0] for name in dict.fromkeys(graphs)]

    def _prime_decode_cache(self, *code_columns) -> None:
        """Decode every code not yet in the cache in one native call.

        ``decode_many`` releases the GIL for the whole batch and returns one
        shared string per distinct code, so the per-term cost collapses from
        one FFI round trip each to one bulk call per match.
        """
        if self._dict is None:
            raise ValueError("store has no resident term dictionary (code path inactive)")
        cache = self._decode_cache
        distinct: set[int] = set()
        for column in code_columns:
            distinct.update(column)
        missing = list(distinct.difference(cache))
        if not missing:
            return
        for code, raw in zip(missing, self._dict.decode_many(missing), strict=True):
            if raw is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            cache[code] = self._from_n3_safe(raw)

    def _default_graph_code(self) -> int | None:
        """The dictionary code of the default graph's empty name, or ``None``
        when the file holds no default-graph rows (the dictionary is
        immutable, so this is looked up once)."""
        if self._default_code is _UNRESOLVED:
            if self._dict is None:
                raise ValueError("store has no resident term dictionary (code path inactive)")
            self._default_code = self._dict.encode(DEFAULT_GRAPH)
        return self._default_code  # ty: ignore[invalid-return-type]

    def _term_kind_bounds(self) -> tuple[int, int, int]:
        """The first code of each term kind (literal, IRI, blank node); the
        dictionary is immutable, so three binary searches, once."""
        if self._kind_bounds is None:
            if self._dict is None:
                raise ValueError("store has no resident term dictionary (code path inactive)")
            self._kind_bounds = kind_bounds(self._dict)
        return self._kind_bounds

    def _foreign_code(self, term: Node) -> int:
        """The negative code standing for a term outside the dictionary."""
        code = self._foreign_codes.get(term)
        if code is None:
            code = self._foreign_codes[term] = -1 - len(self._foreign_codes)
            self._foreign[code] = term
        return code

    def _decode_term(self, code: int) -> Node:
        if code < 0:
            return self._foreign[code]
        node = self._decode_cache.get(code)
        if node is None:
            if self._dict is None:
                raise ValueError("store has no resident term dictionary (code path inactive)")
            raw = self._dict.decode(code)
            if raw is None:
                raise ValueError(f"term code {code} is not in the store dictionary")
            node = self._from_n3_safe(raw)
            self._decode_cache[code] = node
        return node

    def __len__(self, context=None):
        """Quads in ``context``, or in the whole store for the union view.

        The union counts quads, not distinct triples: a triple several graphs
        hold counts once per graph, exactly as ``triples()`` yields it.
        """
        if self.path is None:
            return 0
        g_n3 = self._graph_n3(context)
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

    @staticmethod
    def _from_n3_safe(value: str) -> Node:
        # URIRefs and blank nodes have unambiguous standalone N3 forms.  Build
        # them directly rather than routing every dictionary term through
        # RDFLib's generic from_n3 parser.  Besides avoiding parser overhead,
        # this accepts valid absolute IRIs that from_n3 rejects in some RDFLib
        # versions/configurations when no namespace manager is supplied.
        if len(value) >= 2 and value[0] == "<" and value[-1] == ">":
            return URIRef(value[1:-1])
        if value.startswith("_:"):
            return BNode(value[2:])

        try:
            term = from_n3(value)
        except Exception as error:
            # DBpedia contains a small number of language-tagged literals with
            # an escaped apostrophe (\') even though the literal itself is
            # double quoted.  RDFLib rejects that non-canonical escape.  The
            # fallback is deliberately restricted to simple, language-tagged,
            # double-quoted literals; typed and multiline literals still fail
            # loudly through the generic error below.
            split = value.rfind('"@')
            if value.startswith('"') and split > 0:
                lexical = value[1:split]
                language = value[split + 2 :]
                if language and all(
                    character.isalnum() or character == "-" for character in language
                ):
                    lexical = lexical.replace("\\'", "'")
                    try:
                        parsed = from_n3(f'"{lexical}"')
                    except Exception:
                        parsed = None
                    if isinstance(parsed, Literal):
                        return Literal(parsed.value, lang=language)
            raise ValueError(f"Could not parse returned RDF term as N3: {value!r}") from error

        # from_n3 may also return None or a plain str (e.g. for prefixed names
        # with no namespace manager); the native store only emits full
        # N-Triples forms, so anything but a Node is a bug worth surfacing.
        if not isinstance(term, Node):
            raise ValueError(f"RDFLib returned no RDF term for N3 value: {value!r}")
        return term
