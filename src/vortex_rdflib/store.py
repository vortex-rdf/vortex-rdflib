from collections.abc import Iterable
from pathlib import Path

from rdflib.graph import DATASET_DEFAULT_GRAPH_ID, Dataset
from rdflib.store import NO_STORE, VALID_STORE, Store
from rdflib.term import BNode, Node, URIRef
from vortex_rdf import VortexRdfStore

from . import boundary, query_scope, settings, union
from .open_file import DEFAULT_GRAPH, Codes, OpenFile, UnionFacts
from .pushdown import register_sparql_pushdown
from .query_scope import QueryTerms
from .settings import SETTINGS
from .terms import _term_builder, canonical_spelling


class _NoGraph(NotImplementedError):
    """A context names no graph the file can hold (see
    :meth:`VortexRdflibStore._graph_n3`). As a ``NotImplementedError`` it makes
    the pushdown leave the block to rdflib, whose ``triples()`` answers it with
    nothing."""


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
        *,
        in_memory: bool | None = None,
    ):
        """``path`` (or rdflib's ``configuration``) is the ``.vortex`` file,
        opened at once. A file-backed store is memory-mapped: a query reads
        only the pages it touches, vortex-rdf keeps a bounded cache of the
        dictionary windows it reads and the OS page cache holds the rest.
        ``in_memory=True`` loads the whole file up front instead (default:
        ``VORTEX_RDF_IN_MEMORY``). Any other argument raises ``TypeError``."""
        # rdflib's Store.__init__ opens a configuration it is given, so it is
        # given none, and every attribute open() reads is set first.
        if path is None:
            path = configuration
        self.path = str(Path(path)) if path is not None else None
        # Every switch is read here, once per store, the pushdown's too when
        # it is off (settings.apply_switches); and the whole-BGP pushdown into
        # code space is installed (a no-op for non-Vortex graphs;
        # VORTEX_RDF_DISABLE_PUSHDOWN=1 keeps rdflib's default evaluator).
        register_sparql_pushdown()
        if in_memory is None:
            in_memory = settings.in_memory_by_default()
        self.in_memory = in_memory
        self._use_codes = settings.code_path_enabled()
        # The open file: its native handle, its term codes (None while the
        # store reads none) and what the store learns about it
        # (open_file.OpenFile), read through `_opened()`. Everything a query
        # learns about terms lives in its scope (query_scope.QueryTerms,
        # through `_terms()`).
        self._file: OpenFile | None = None

        super().__init__(configuration=None, identifier=identifier)
        if self.path is not None:
            self.open(self.path)

    @property
    def layout(self) -> str | None:
        """The open file's layout, as vortex-rdf reads it from the file;
        ``None`` while the store is closed."""
        return None if self._file is None else self._file.layout

    def open(self, configuration, create=False):
        """RDFLib Store API: ``configuration`` is the ``.vortex`` file's path.
        The file's record — its handle, its term codes, its facts — is made
        whole, then set. A dictionary-layout file serves matches as u64 code
        columns, the dictionary memory-mapped with the file (or loaded with
        the store), each distinct code decoded to an rdflib term once per
        query; another layout, or the code path off, reads spellings."""
        if configuration is not None:
            self.path = str(Path(configuration))
        if self.path is None:
            return NO_STORE
        native = VortexRdfStore(self.path, in_memory=self.in_memory)
        self._file = OpenFile(native, self.in_memory, self._use_codes)
        return VALID_STORE

    def close(self, commit_pending_transaction=False):
        """Drop the open file: its handle, its codes and every fact the store
        learnt about it. A later call opens the file again."""
        self._file = None

    def _opened(self) -> OpenFile:
        """The open file's record, the file opened again if the store is
        closed."""
        if self._file is None:
            self._reopen()
        assert self._file is not None  # open() either sets it or raises
        return self._file

    def _reopen(self) -> None:
        if self.path is None:
            raise ValueError("VortexRdflibStore has no path; pass one or call open()")
        self.open(self.path)

    def _union_has_repeats(self) -> bool:
        """Whether a match over every graph can return a triple more than
        once: some triple is in more than one graph. Where none is, the union
        is read as the quads are and pays nothing for being a set: a file of
        one graph (every N-Triples file), and graphs that partition the data."""
        return self._union_facts().has_repeats

    def _union_facts(self) -> UnionFacts:
        """The open file's union facts, read once per open file
        (:meth:`_read_union`)."""
        opened = self._opened()
        if opened.union is None:
            opened.union = self._read_union()
        return opened.union

    def _read_union(self) -> UnionFacts:
        """:meth:`_union_facts`, from the file's own codes, which the code
        path's switch does not change. A file whose quads are all in its
        first row's graph has no repeats: a match of one row and a count of
        that graph tell. A file of several graphs has them when it holds
        fewer distinct triples than quads, counted in one pass over its
        triple codes (:func:`union.distinct_chunks`), which is the union's
        size. A layout without codes has no such pass: a file whose quads
        are not all in the default graph is taken to have them."""
        native = boundary.native(self)
        quads = native.count_quads()
        if not quads:
            return UnionFacts(has_repeats=False, size=None)
        first = native.match_codes(limit=1)
        codes = self._opened().codes
        if codes is None:
            term_dict = native.term_dict()
            codes = None if term_dict is None else Codes(term_dict)
        if first is None or codes is None:
            in_default = native.count_quads(None, None, None, DEFAULT_GRAPH)
            return UnionFacts(has_repeats=in_default != quads, size=None)
        graph = codes.spelling(memoryview(first[3]).cast("Q")[0])
        if native.count_quads(None, None, None, graph) == quads:
            return UnionFacts(has_repeats=False, size=None)
        chunks = native.match_code_chunks(
            columns=("s", "p", "o"), batch_rows=SETTINGS.triples_chunk_rows
        )
        if chunks is None:
            return UnionFacts(has_repeats=True, size=None)
        size = sum(len(cols[0]) for cols in union.distinct_chunks(chunks))
        return UnionFacts(has_repeats=size != quads, size=size)

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
        if spelling not in self._opened().contexts_by_spelling:
            try:
                boundary.native(self).count_quads(None, None, None, spelling, limit=0)
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
            pattern = (None, None, None, spelling)
            try:
                count = boundary.native(self).count_quads(*pattern)
            except ValueError:
                if boundary.refused(self, spelling):
                    return None  # a label vortex-rdf refuses names no graph of the file
                raise
            names_a_graph = known[spelling] = count > 0
        return spelling if names_a_graph else None

    def triples(self, triple_pattern, context=None):
        """RDFLib Store API: yields ``((s, p, o), contexts)`` for a pattern.

        ``context`` restricts the match to one graph (:meth:`_graph_n3` maps
        it to a graph name); ``None`` — and a bare ``Graph`` over this store —
        is the union over every graph, a set: a triple several graphs hold is
        yielded once, with the contexts of all of them
        (:func:`union.one_per_triple`, over the rows :meth:`_quad_rows` streams,
        where a triple's rows are adjacent); a context that names no graph the
        file can hold yields nothing.
        """
        if self.path is None:
            return
        try:
            g_n3 = self._graph_n3(context)
        except _NoGraph:
            return
        # The union facts first, then the pattern's reads, as a lazy reader
        # would (rows are read once iterated).
        repeats = g_n3 is None and self._union_has_repeats()
        rows = self._quad_rows(triple_pattern, g_n3)
        yield from union.one_per_triple(rows) if repeats else rows

    def _quad_rows(self, triple_pattern, g_n3: str | None) -> Iterable[tuple]:
        """``((s, p, o), contexts)`` for each quad a pattern matches in the
        graph named ``g_n3`` (``None``: every graph), a triple several graphs
        hold once per graph under the union. The store streams the quads it holds rather than
        building the RDF merge of the union, which would mean a set the size
        of the result. A pattern of more than ``SETTINGS.triples_chunk_rows`` rows is
        read from the native layer in chunks of at most that many rows
        (:meth:`_code_matches`), so a scan of a large store holds one chunk of
        codes at a time. A match or chunk is decoded a slice at a time, the
        first of 64 rows, so a consumer that stops early decodes only the
        slices, and pulls only the chunks, it reached. The query's cancel
        token (``cancel.cancel_scope``) is read once, when the iteration
        starts, and the native boundary (:mod:`.boundary`) applies it: a
        one-shot native match or count races it where the token can stop
        that call — a scan of a file-backed store — and checks it before the
        call otherwise, and every pull of a chunk races it, the string
        fallback's (``iter_quads``) included.

        Each term it yields is one object per code for the whole call, and on
        the string fallback one object per spelling within a chunk and across
        consecutive rows: the union's rows of one triple, which are adjacent,
        are told apart by identity (:func:`union.one_per_triple`).
        """
        s, p, o = triple_pattern
        # RDFLib can propagate an object binding into subject or predicate
        # position during joins: that pattern is unsatisfiable, not an error.
        if (s is not None and not isinstance(s, (URIRef, BNode))) or (
            p is not None and not isinstance(p, URIRef)
        ):
            return ()
        pattern = (self._node_to_n3(s), self._node_to_n3(p), self._node_to_n3(o), g_n3)
        if SETTINGS.trace_triples:
            print(
                "[VortexRdflibStore.triples:start] "
                f"layout={self.layout} "
                f"s={_term_debug(s)} p={_term_debug(p)} o={_term_debug(o)} "
                f"s_n3={pattern[0]} p_n3={pattern[1]} o_n3={pattern[2]} g_n3={g_n3!r}",
                flush=True,
            )
        try:
            return self._read_rows(triple_pattern, pattern)
        except ValueError:
            # So is one naming a term vortex-rdf's parser refuses: no file
            # holds it. vortex-rdf refuses it before it reads a row, so the
            # check runs only then, and any other error stands (g_n3 is a
            # graph name the file can hold, _graph_n3).
            if boundary.any_refused(self, pattern[:3]):
                return ()
            raise

    def _read_rows(self, triple_pattern, pattern: tuple) -> Iterable[tuple]:
        """:meth:`_quad_rows`' read of the quad pattern ``pattern``: its first
        native call made here, the rest as the rows are pulled."""
        s, p, o = triple_pattern
        g_n3 = pattern[3]
        # The handle and the file's record (its codes, its graph contexts)
        # are bound once: rows read after a close(), or an open() of another
        # file, still come from the file the call started on.
        native = boundary.native(self)
        opened = self._opened()
        if None not in pattern[:3]:
            # Fully ground: no term needs materializing. A store's quads are
            # unique, so in one graph the triple is there or not: one count.
            # Under the union it is one row, with the context of each graph
            # that holds it (only the graph column is read).
            if g_n3 is None:
                graphs = self._distinct_graphs(*pattern[:3])
                return [((s, p, o), tuple(graphs))] if graphs else ()
            if not native.count_quads(*pattern):
                return ()
            return [((s, p, o), opened.context_tuple(self, g_n3))]
        codes = opened.codes
        matches = None if codes is None else self._code_matches(native, pattern)
        if codes is not None and matches is not None:
            return self._rows_from_codes(opened, codes, matches, g_n3)
        quads = native.iter_quads(*pattern, batch_rows=SETTINGS.triples_chunk_rows)
        return self._rows_from_strings(opened, quads, g_n3)

    def _rows_from_codes(self, opened: OpenFile, codes: Codes, matches, g_n3: str | None):
        """The rows of ``matches`` — the code columns of one native match, or
        its chunks — each decoded a slice at a time through the call's term
        cache, with its graph's one context tuple: the named graph's for
        ``g_n3``, else the graph column's, a slice of one graph sharing its
        tuple."""
        # One named graph: every matched row is in it, so the contexts are one
        # tuple and the graph column is never read. The tuple is taken once
        # the match has a row, which proves the file holds the graph; a name
        # that matches nothing leaves no state behind.
        fixed = None
        cache = None
        contexts: dict[int, tuple] = {}
        try:
            for cols in matches:
                rows = len(cols[0])
                # The columns of one native match, or chunk, are equally long;
                # reading them by the first one's length would hide a native
                # bug.
                if {len(column) for column in cols} != {rows}:
                    raise ValueError(f"match columns of unequal lengths: {list(map(len, cols))}")
                if not rows:
                    continue
                if cache is None:
                    # The match's first row: the call's term cache, and the
                    # named graph's one context tuple (see `fixed`).
                    cache = self._terms().decode
                    if g_n3 is not None:
                        fixed = opened.context_tuple(self, g_n3)
                # Zero-copy u64 views over the Rust column buffers, read a
                # slice at a time: 64 rows, then four times more, 4096 at most.
                # Each slice's codes become Python ints, and those the call's
                # term cache (the query's, inside one) lacks are decoded in one
                # GIL-released decode_many call, so a consumer that stops early
                # (rdflib's LIMIT, ASK) decodes only the slices it reached.
                views = [memoryview(column).cast("Q") for column in cols]
                start, size = 0, SETTINGS.chunk_start
                while start < rows:
                    stop = min(start + size, rows)
                    # A match or chunk that is one slice is read from the
                    # views whole.
                    part = views if stop - start == rows else [v[start:stop] for v in views]
                    s_codes = part[0].tolist()
                    p_codes = part[1].tolist()
                    o_codes = part[2].tolist()
                    self._decode_into(codes, cache, (s_codes, p_codes, o_codes))
                    shared = fixed
                    if shared is None:
                        # Union: the fourth column names each row's graph. A
                        # slice whose rows all live in one graph — every slice
                        # of a single-graph file — shares that graph's context
                        # and pays no per-row lookup.
                        g_codes = part[3].tolist()
                        graphs = set(g_codes)
                        for code in graphs.difference(contexts):
                            contexts[code] = opened.context_of_code(self, code)
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
                    start, size = stop, min(size * 4, SETTINGS.chunk_max)
                # Let go of this chunk's columns before the next pull.
                del cols, views, part
        finally:
            # Dropping a chunk iterator ends its native scan (a bare
            # CodeChunks has no close()): once the rows run out, the consumer
            # stops or a pull fails.
            del matches

    def _rows_from_strings(self, opened: OpenFile, quads, g_n3: str | None):
        """The rows of a quad pattern in the graph named ``g_n3`` as
        spellings — a layout without codes, or the code path off — from
        ``quads``, the pattern's ``iter_quads``, which streams
        ``SETTINGS.triples_chunk_rows`` rows at a time, so a read holds about
        one chunk and a consumer that stops early stops the read; the
        boundary races the query's token on the call and on every chunk.
        Each term is built once per distinct spelling of a chunk: the memo
        starts over every ``triples_chunk_rows`` rows, so it never holds more
        than a chunk's terms, and keeps the last row's, so a triple whose
        rows straddle the restart is still the same objects — the union's
        rows of one triple are adjacent, told apart by identity."""
        chunk_rows = SETTINGS.triples_chunk_rows
        fixed = None
        parse = self._from_n3_safe
        memo: dict[str, Node] = {}
        left = chunk_rows
        previous: tuple = ()
        for quad in quads:
            if not left:
                memo = {raw: memo[raw] for raw in previous[:3]}
                left = chunk_rows
            left -= 1
            s_raw, p_raw, o_raw, g_raw = previous = quad
            if g_n3 is not None and fixed is None:
                fixed = opened.context_tuple(self, g_n3)
            row = []
            for raw in (s_raw, p_raw, o_raw):
                node = memo.get(raw)
                if node is None:
                    node = memo[raw] = parse(raw)
                row.append(node)
            yield (
                (row[0], row[1], row[2]),
                (fixed if fixed is not None else opened.context_tuple(self, g_raw)),
            )

    def _code_matches(
        self, native: boundary.NativeHandle, pattern: tuple
    ) -> Iterable[tuple] | None:
        """The code columns :meth:`triples` reads for the quad pattern
        ``pattern``, as matches to read in turn: ``match_codes``'s one match of
        a pattern of at most ``SETTINGS.triples_chunk_rows`` rows, or, for a pattern of
        more, ``match_code_chunks``'s chunks of at most that many rows, of the
        columns ``s``, ``p``, ``o`` and, under the union, ``g``. ``None`` when
        the store has no term codes.

        The match is asked for one row more than ``SETTINGS.triples_chunk_rows``: one
        that holds at most that many is the whole pattern, and a full one is
        dropped before the chunks are read. ``native`` is the store's handle
        through the boundary (:mod:`.boundary`), which gives each call the
        query's token as its policy says.
        """
        chunk_rows = SETTINGS.triples_chunk_rows
        cols = native.match_codes(*pattern, limit=chunk_rows + 1)
        if cols is None:
            return None
        if len(cols[0]) <= chunk_rows:
            return (cols,)
        del cols
        columns = ("s", "p", "o", "g") if pattern[3] is None else ("s", "p", "o")
        return native.match_code_chunks(*pattern, columns=columns, batch_rows=chunk_rows)

    def contexts(self, triple=None):
        """RDFLib Store API: the graphs holding ``triple``, or every graph.

        There is no graph index, so the names come from the fourth column of
        a match — a whole-store call walks the store once. The store is
        read-only, so that walk is cached for its lifetime.
        """
        if self.path is None:
            return iter(())
        if triple is None or triple == (None, None, None):
            opened = self._opened()
            if opened.all_contexts is None:
                opened.all_contexts = self._distinct_graphs(None, None, None)
            return iter(opened.all_contexts)
        s, p, o = triple
        if (s is not None and not isinstance(s, (URIRef, BNode))) or (
            p is not None and not isinstance(p, URIRef)
        ):
            return iter(())
        spellings = (self._node_to_n3(s), self._node_to_n3(p), self._node_to_n3(o))
        try:
            return iter(self._distinct_graphs(*spellings))
        except ValueError:
            # A term vortex-rdf's parser refuses names nothing (_quad_rows).
            if boundary.any_refused(self, spellings):
                return iter(())
            raise

    def _distinct_graphs(self, s_n3, p_n3, o_n3) -> list:
        """The distinct graphs of a pattern's match, in first-seen order."""
        native = boundary.native(self)
        opened = self._opened()
        if opened.codes is not None:
            cols = native.match_codes(s_n3, p_n3, o_n3, None)
            if cols is not None:
                # Native distinct, in first-seen order.
                codes = memoryview(cols[3].distinct()).cast("Q").tolist()
                return [opened.context_of_code(self, code)[0] for code in codes]
        # The string fallback: the match streamed, its graph names kept once.
        quads = native.iter_quads(s_n3, p_n3, o_n3, None, batch_rows=SETTINGS.triples_chunk_rows)
        names = dict.fromkeys(quad[3] for quad in quads)
        return [opened.context_tuple(self, name)[0] for name in names]

    def _terms(self) -> QueryTerms:
        """The term state of the query being evaluated (:mod:`.query_scope`),
        or a fresh one for a call outside any query."""
        return query_scope.terms_for(self)

    def _prime_decode_cache(self, *code_columns) -> dict:
        """Decode every code the query's term cache lacks in one native call
        (:meth:`_decode_into`), and return that cache (code -> term)."""
        cache = self._terms().decode
        codes = self._opened().codes
        assert codes is not None, "only a store that reads codes decodes them"
        self._decode_into(codes, cache, code_columns)
        return cache

    def _decode_into(self, codes: Codes, cache: dict, code_columns) -> None:
        """Decode into ``cache``, through the file's ``codes``, every code of
        ``code_columns`` it lacks.

        ``decode_many`` releases the GIL for the whole batch and returns one
        shared string per distinct code, so the per-term cost collapses from
        one FFI round trip each to one bulk call per batch.
        """
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
            for code, raw in zip(missing, codes.spellings(missing), strict=True):
                cache[code] = build(raw)

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
            codes = self._opened().codes
            assert codes is not None, "only a store that reads codes decodes them"
            node = terms.decode[code] = self._from_n3_safe(codes.spelling(code))
        return node

    def __len__(self, context=None):
        """Triples in ``context``: the quads of its graph, or the distinct
        triples of the union view, as ``triples()`` yields them — the store's
        quads when no triple is in two graphs, else the union's size
        :meth:`_read_union` counted, or, in a layout without codes, the count
        :meth:`_union_size_by_spelling` makes, read once. A context that names
        no graph the file can hold has none.
        """
        if self.path is None:
            return 0
        try:
            g_n3 = self._graph_n3(context)
        except _NoGraph:
            return 0
        if g_n3 is None:
            facts = self._union_facts()
            if not facts.has_repeats:
                return len(self._opened().native)
            if facts.size is None:
                size = self._union_size_by_spelling()
                facts = self._opened().union = UnionFacts(has_repeats=True, size=size)
            return facts.size
        return boundary.native(self).count_quads(None, None, None, g_n3)

    def _union_size_by_spelling(self) -> int:
        """The union's distinct triples, in a layout without codes: every quad,
        as spellings, streamed a chunk at a time, whose rows of one triple are
        adjacent (every sort order a store holds ends in the graph), counted
        by runs of equal ``(s, p, o)`` spellings across chunks — no term is
        built."""
        quads = boundary.native(self).iter_quads(
            None, None, None, None, batch_rows=SETTINGS.triples_chunk_rows
        )
        size, last = 0, None
        for s, p, o, _ in quads:
            key = (s, p, o)
            if key != last:
                size += 1
                last = key
        return size

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
