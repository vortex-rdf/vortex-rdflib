"""SPARQL pushdown for :class:`~vortex_rdflib.store.VortexRdflibStore`.

rdflib evaluates a query as a tree of algebra operators and offers every node
to the ``CUSTOM_EVALS`` hooks before running its own evaluator. This module's
hook answers the nodes it understands in **code space** — over the ``u64``
term codes of a Dictionary-layout store — and hands everything else back to
rdflib by raising ``NotImplementedError``; rdflib then evaluates that node
itself and re-enters the hook for the nodes below it, so every supported
subtree of a query still runs here.

What runs in code space:

- a basic graph pattern is solved in one pass: its point patterns are
  *counted* natively first, one at a time, the scans after them in one
  concurrent batch (a count is a fraction of a match's cost and moves no
  columns); a pattern sharing a variable with one that counts at most
  ``SETTINGS.probe_fanout`` rows is not counted until its join step, and then only as
  far as the probe-or-match decision needs; the smallest counted pattern is
  matched and seeds the relation, and each further pattern — smallest first
  among those sharing a variable with the relation — is joined per row of
  the relation when the relation is far smaller than the pattern's count
  (re-probed natively where each probe is a point lookup, ``_probe_join``;
  matched once, narrowed to the codes the relation binds, where each would
  scan, ``_keep_scan``) or matched once and hash-joined by the native
  ``join_indices``/``take`` kernels; a pattern that counts to nothing empties
  the block before anything is matched, and the join itself never decodes a
  term. A join step whose pairs would pass ``SETTINGS.pair_budget``
  (``VORTEX_RDF_PAIR_BUDGET``) is not built whole: a hash step has its pairs
  counted natively once its sides' product passes the budget, a probe step
  stops once its rows pass a quarter of it, and a keep-scan once its match
  passes it. When it is the last step and shares one variable it streams
  instead (tier 1): the smaller side is grouped, the other is fed through the
  groups, and the pairs are gathered a piece at a time as the rows are read,
  so a consumer that reads the rows as they come holds one piece of the join
  at a time, besides the grouped side and, unless it is a pattern fed in
  chunks, the fed side; a consumer that holds rows drains the pieces into
  rows first. Any other step is put back and a pattern that provably fits
  joins first (tier 2); when none does, rdflib finishes the patterns left per
  row of the relation so far (tier 3) where the relation goes straight to the
  emitter, and elsewhere the node goes back to rdflib, which offers the BGP
  below it alone, where tier 3 applies;
- a ``Filter`` over a block is split into conjuncts (:mod:`vortex_rdflib.filters`):
  those over one variable that are a kind test or an IRI equality (``=``,
  ``sameTerm``, ``IN``) become a ``keep`` on the match of every pattern
  binding the variable, so the rows that fail never leave it (a probed
  pattern tests a code set on the rows each probe returns); the others over
  one variable are evaluated once per distinct code of the variable, on the
  pattern that binds it — over the pattern's whole column when it is
  matched, over the codes the probes reach when it is probed — by
  vortex-rdf when they have a native shape
  (``TermDict.filter_codes`` over exactly those codes, :mod:`vortex_rdflib.filters.native`),
  and the match is compacted natively to the rows that pass; the rest per
  distinct code tuple, after the join (mid-join when that lets a later
  pattern be probed, and as a test of one variable when the relation binds
  all but one of its variables to single values); a whitelist of expression
  shapes runs over the stored spellings, anything else (and any value
  vortex-rdf or the whitelist leaves undecided) goes to rdflib's own
  evaluator, per distinct value, not per row;
- group joins (``Join``), ``OPTIONAL`` (``LeftJoin``), ``MINUS`` and
  ``FILTER (NOT) EXISTS`` over blocks run as hash joins, left joins, anti-
  and semi-joins over code tuples, with a one-pattern inner side counted
  first and re-probed per outer row when the outer relation is small —
  instead of rdflib re-entering the store once per outer solution, and
  without the inner side's complete match; an inline ``VALUES`` table
  is a code-space relation too, its constants encoded in one batch (a
  constant the dictionary does not hold gets a private negative code and
  joins nothing);
- ``Project``, ``Distinct``, ``OrderBy`` (on variables) and ``Slice``
  (LIMIT/OFFSET) heads are applied to the code-space relation — a LIMIT over
  one pattern stops the native match itself — and an ``AskQuery`` over one
  pattern is a count capped at one row, so only the projected variables of
  the rows that are actually consumed are ever decoded — a ``LIMIT 10``
  decodes a few dozen codes, an ASK none, a ``DISTINCT ?p`` over the whole
  store only its distinct predicates (found by the native ``distinct``); an
  ORDER BY ranks each distinct code once (blank nodes by code, as the
  spelling ``_:label`` sorts as the label; IRIs by their spelling without
  the brackets, the string rdflib compares, with no term built; literals
  decoded once per query and ranked by rdflib's comparison) and sorts the
  code rows, a top-k when a LIMIT follows;
- ``COUNT`` aggregates (``COUNT(*)``, ``COUNT(?v)``, ``COUNT(DISTINCT ?v)``,
  with or without ``GROUP BY`` variables) are computed over the code
  columns — a column at a time by the native ``value_counts`` and
  ``distinct`` where the body is columnar — decoding only the group keys,
  and a ``COUNT(*)`` over one pattern (under a FILTER the native match
  decides, too) is answered by ``count_quads`` without matching a row;
- solutions are decoded lazily in growing chunks, each distinct code once
  per query through the query's term cache (:mod:`vortex_rdflib.query_scope`), and built
  directly as ``FrozenBindings``, the row shape every rdflib operator above
  expects.

Named graphs: a ``Graph`` node sets the scope of the block below it rather
than matching anything itself. A bound name restricts every pattern to that
graph (as does the graph the context is active in, for a block under no
``GRAPH`` at all); an unbound ``GRAPH ?g`` binds the graph from the match's
fourth column instead, so ``?g`` is an ordinary variable of the relation and
one match answers what rdflib would walk the store's graphs for. A FILTER in
the block still reads ``?g`` unbound unless a pattern of its group binds it:
rdflib joins the graph in after the group is solved. Where no pattern of the
group is matched in that scope, or an OPTIONAL or MINUS has one on its right
side only, or an OPTIONAL binds ``?g`` on its right side only, or a MINUS does
and its two sides share a binding, the ``Graph`` node is left to rdflib, which
solves the group once per named graph. The rows of the default graph are
dropped from a variable's match, which is what makes ``GRAPH`` range over the
named graphs only.

Cancellation: ``cancel_scope(token)`` gives the queries started inside it a
``vortex_rdf.CancelToken``. A query keeps it in its scope
(:mod:`vortex_rdflib.query_scope`), so every step of its lazy result runs under it, the
steps after the block included. A native call the token can stop — a batch,
a chunked match, a one-shot call that scans a file-backed store — is given
it (``cancel=``) and races it; the other one-shot calls — a point lookup, a
call capped at one row, any call on an in-memory store — check it as they
are made (a join's probes are calls of either kind), as do the loops between
calls: a streamed join's pieces, the rows rdflib finishes, and the slices
and decode chunks a result's rows are read in. ``token.cancel()``, from any
thread, stops the query at the next of these with ``vortex_rdf.Cancelled``,
which reaches the caller and is never a hand-back; a result whose rows are
decoded as they are read stops within one decode chunk. A query started
outside a block has no token, and without one no native call gains a keyword.

rdflib only catches ``NotImplementedError`` at hook-call time, so every
capability check and every native call that can decline happens before a
generator is handed back; the generators decode, pull iterators created
while planning, run kernels and, where a plan leaves patterns to rdflib
(tier 3), run rdflib's own evaluation of them, which makes new
``triples()`` calls. The hook applies when the active graph's
store is a VortexRdflibStore with the code path available (Dictionary layout,
its dictionary memory-mapped from the file or loaded with the store);
behaviour is identical to rdflib's default evaluation, only faster, and the
equivalence tests compare both paths on every query shape.

Registration happens automatically when the first ``VortexRdflibStore`` is
constructed. ``VORTEX_RDF_DISABLE_PUSHDOWN=1`` keeps rdflib's evaluator
entirely (the equivalence tests' oracle); ``VORTEX_RDF_PUSHDOWN_OPS`` narrows
the intercepted algebra nodes to a comma-separated list (``bgp`` = basic
graph patterns only), ``VORTEX_RDF_NATIVE_FILTERS=0`` keeps the conjuncts
the native layer decides on the Python fast route and
``VORTEX_RDF_FILTER_FAST=0`` routes every FILTER value through rdflib's
evaluator, for bisecting and A/B measurements.
``VORTEX_RDF_TRACE_QUERY=1`` prints one JSON line per planning step to
stderr (prefixed ``VORTEX_RDF_QUERY_TRACE``): what was counted, matched,
probed, restricted and joined, with row counts and timings.

The modules, from the hook down:

- :mod:`.hook` — registration, the query scope, dispatch to the handlers;
- :mod:`.head` — the head above a block: projection, DISTINCT, ORDER BY, LIMIT;
- :mod:`.aggregate` — ``COUNT`` aggregates;
- :mod:`.blocks` — FILTER, joins, OPTIONAL, MINUS, EXISTS, VALUES, GRAPH;
- :mod:`.bgp` — basic graph patterns and the incremental join planner;
- :mod:`.streaming` — a last join step over the pair budget, streamed;
- :mod:`.probes` — probe joins and batched native probes;
- :mod:`.matching` — one pattern: resolved, narrowed, counted, matched;
- :mod:`.algebra` — what a block holds and binds, and its graph scope;
- :mod:`.relation` — code-space relations, their rows, the join kernels;
- :mod:`.solutions` — decoding rows into rdflib solutions;
- :mod:`.aliasing` — codes whose terms another code represents;
- :mod:`.tracing` — the query trace.

Within the package a module calls another's functions through it
(``matching._rel_from_pattern``), so a test that patches a function on its
module reaches every caller.
"""

from . import (
    aggregate,
    algebra,
    aliasing,
    bgp,
    blocks,
    head,
    hook,
    matching,
    probes,
    relation,
    solutions,
    streaming,
    tracing,
)
from .hook import register_sparql_pushdown, unregister_sparql_pushdown

__all__ = [
    "aggregate",
    "algebra",
    "aliasing",
    "bgp",
    "blocks",
    "head",
    "hook",
    "matching",
    "probes",
    "register_sparql_pushdown",
    "relation",
    "solutions",
    "streaming",
    "tracing",
    "unregister_sparql_pushdown",
]
