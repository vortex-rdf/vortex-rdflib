# SPARQL pushdown

`vortex-rdflib` runs SPARQL queries with rdflib's engine. In addition, a
[`VortexRdflibStore`](../src/vortex_rdflib/store.py#L76) installs a hook that
answers the parts of a query it supports itself. The hook works on the store's
integer term codes instead of rdflib terms. Everything it does not support is
left to rdflib.

This document explains how the hook works, what it does for each SPARQL
feature, and where in the code each step happens.

## Source code

| File | Role |
| --- | --- |
| [`pushdown.py`](../src/vortex_rdflib/pushdown.py) | The hook and the planner: patterns, joins, `OPTIONAL`, `MINUS`, `EXISTS`, `DISTINCT`, `ORDER BY`, `COUNT`, `VALUES`, named graphs, the query's scope, and turning results into rdflib solutions. |
| [`filters.py`](../src/vortex_rdflib/filters.py) | `FILTER` expressions: splitting them into conjuncts, recognizing the shapes vortex-rdf evaluates, and evaluating the rest in Python. |
| [`candidates.py`](../src/vortex_rdflib/candidates.py) | `FILTER` predicates evaluated by vortex-rdf over the codes a query produced, and the keeps kind tests and IRI equality become. |
| [`query_scope.py`](../src/vortex_rdflib/query_scope.py) | What a query learns about terms — decoded terms, alias classes, the codes of constants outside the dictionary — kept for that query alone. |
| [`terms.py`](../src/vortex_rdflib/terms.py) | Term spellings: parsing the dictionary's N-Triples strings, building the rdflib terms they stand for, and spelling rdflib terms the way the dictionary stores them. |
| [`store.py`](../src/vortex_rdflib/store.py) | The rdflib store: opening the file (memory-mapped, or loaded in memory), decoding codes to terms, choosing which graph a query reads, and the store's fixed-size bookkeeping. |

## How the hook works

rdflib turns a query into a tree of algebra nodes (`Project`, `Filter`, `BGP`,
`LeftJoin`, ...). Before it evaluates a node, it offers the node to every
function registered in `rdflib.plugins.sparql.CUSTOM_EVALS`.

1. Creating a `VortexRdflibStore` calls
   [`register_sparql_pushdown`](../src/vortex_rdflib/pushdown.py#L578), which
   registers [`_eval_part`](../src/vortex_rdflib/pushdown.py#L611) as such a
   function.
2. For each node, `_eval_part` checks that the node is one it handles
   ([`_HANDLER_NAMES`](../src/vortex_rdflib/pushdown.py#L172)) and has not
   been switched off (see [Switches](#switches)), and that the
   query's store is a `VortexRdflibStore` with a term dictionary (a
   Dictionary-layout).
3. If both hold, the matching handler answers the node and everything below it.
4. If not, the hook raises `NotImplementedError`. rdflib then evaluates the node
   itself, and offers the nodes below it to the hook again.

So rdflib runs the parts the hook does not support, and the hook still runs
every supported subtree below them.

**One scope per query.** rdflib offers the hook the query node itself first.
The hook opens the query's scope there
([`_eval_scoped`](../src/vortex_rdflib/pushdown.py#L671)) and hands the node
back to rdflib's `evalPart`, so every node below runs inside it. With a scope
current, the hook answers a node only through a handler: `AskQuery` has one,
`SelectQuery` none, so rdflib evaluates it. A lazy result keeps the scope
current around each of its steps
([`_scoped_iterator`](../src/vortex_rdflib/pushdown.py#L708)). See
[Caches and lifetimes](#caches-and-lifetimes).

| Algebra node | Handler |
| --- | --- |
| `SelectQuery`, `AskQuery`, `ConstructQuery`, `DescribeQuery`, offered first, with no scope current | [`_eval_scoped`](../src/vortex_rdflib/pushdown.py#L671): opens the query's scope, then hands the node back to rdflib's `evalPart`, which offers it again |
| `BGP`, `Filter`, `Join`, `LeftJoin`, `Minus`, `ToMultiSet` (`VALUES`), `Graph` | [`_eval_block_node`](../src/vortex_rdflib/pushdown.py#L1661) |
| `Project`, `Slice`, `Distinct`, `OrderBy` | [`_eval_head`](../src/vortex_rdflib/pushdown.py#L1042) |
| `AggregateJoin` | [`_eval_aggregate`](../src/vortex_rdflib/pushdown.py#L1415) |
| `AskQuery`, offered again inside the scope | [`_eval_ask`](../src/vortex_rdflib/pushdown.py#L1666) |

**Plan first, decode later.** rdflib only catches `NotImplementedError` while
the hook is being called. So every handler makes all its checks, and every call
into vortex-rdf that can decline, before it returns. What it returns is a
generator that decodes results, pulls the iterators the plan created (the
pieces of a streamed join) and runs kernels; it never raises
`NotImplementedError` halfway through. What the plan leaves to rdflib is
decided before the generator exists too: the patterns of a block that a join
could not take within the [pair budget](#the-pair-budget), which rdflib joins
into each row as the generator yields it ([tier 3](#tier-3-and-the-emitter)).

**Cancellation.** [`cancel_scope(token)`](../src/vortex_rdflib/pushdown.py#L248)
gives the queries started inside it a `vortex_rdf.CancelToken`. The query's
scope keeps it, so each step of a lazy result runs under it, the steps after
the block included. A call into vortex-rdf that the token can stop (a batch, a
chunked match, a one-shot call that scans a file-backed store) is given it
(`cancel=`) and races it
([`_cancel_kw_for`](../src/vortex_rdflib/pushdown.py#L290)). The one-shot calls
that finish without waiting (a point lookup, a call capped at one row, any call
on an in-memory store) check it as they are made
([`_check_cancelled`](../src/vortex_rdflib/pushdown.py#L280)), as do the loops
between calls: the pieces of a streamed join, the rows rdflib finishes, the
per-row probes. `token.cancel()`, from any thread, then raises
`vortex_rdf.Cancelled` at the next of these. It reaches the caller; it is never
handed back to rdflib.

## Working on codes

In the Dictionary layout, each term is stored once in a sorted dictionary, and
the quads refer to terms by a `u64` **code**: the term's position in that
dictionary.

- Matching a pattern (`VortexRdfStore.match_codes`) returns four code columns
  (subject, predicate, object, graph) without copying them.
- Joins, filters, `DISTINCT`, grouping and sorting all work on these integers.
- A code becomes an rdflib term only when a solution is returned. Each code is
  decoded once per query and kept until the query ends
  ([`_prime_decode_cache`](../src/vortex_rdflib/store.py#L592)). IRIs, blank
  nodes, and plain, language-tagged and `xsd:integer` literals without
  escapes (an integer `int()` reads, a non-canonical one only while
  `rdflib.NORMALIZE_LITERALS` holds), are made without rdflib's constructors,
  with the attributes those set; every other spelling reaches the
  constructors split once. At import, a fixed sample is built both ways, and
  if any term differs the constructors build every term
  ([`_term_builder`](../src/vortex_rdflib/terms.py#L348)).

Codes follow the byte order of the term spellings, so each kind of term fills
one contiguous range of codes: first the default graph's empty name, then
literals (`"…`), then IRIs (`<…>`), then blank nodes (`_:…`). Asking whether a
code is an IRI is a range check, with no decoding
([`kind_bounds`](../src/vortex_rdflib/terms.py#L161),
[`_kind_ranges`](../src/vortex_rdflib/store.py#L646)).

Much of the integer work runs inside vortex-rdf ("natively") instead of in
Python:

- A **keep** is a constraint passed along with a match or a count
  (`match_codes(..., keep=...)`): a code range or a code set per position. Rows
  outside it are dropped inside vortex-rdf and never reach Python.
- Joins use `U64Column.join_indices` and `take`.
- `DISTINCT` uses `U64Column.distinct`, and group counts use
  `U64Column.value_counts`.

[vortex-rdf primitives](#vortex-rdf-primitives) lists where each one is used.

## Blocks, relations and heads

The hook reads a supported part of a query as a **head** on top of a **block**.

A block is a subtree made of these nodes:

```text
block := BGP
       | Filter(block)
       | Join(block, block)
       | LeftJoin(block, block, expr)
       | Minus(block, block)
       | ToMultiSet(values)
       | Graph(block)
```

[`_check_block`](../src/vortex_rdflib/pushdown.py#L904) checks a subtree
against this grammar before any work starts.
[`_solve_block`](../src/vortex_rdflib/pushdown.py#L1745) then solves it, with
one solver per node type. The result is a
[`Relation`](../src/vortex_rdflib/pushdown.py#L821): the variables it binds
(its schema) and a body of codes, which is either

- **columns**: zero-copy views of a native match or a native join, or
- **rows**: Python tuples of codes, where `None` means the variable is unbound.

The head is what rdflib puts above a block:

- `Slice? → Distinct? → Project → OrderBy? → block`, read by
  [`_plan_head`](../src/vortex_rdflib/pushdown.py#L868) and run by `_eval_head`;
- `AggregateJoin(Group(block))`, run by `_eval_aggregate`;
- `AskQuery(Project(block))`, run by `_eval_ask`.

## Returning solutions

[`_yield_rows`](../src/vortex_rdflib/pushdown.py#L4526) turns code rows into
rdflib solutions, lazily:

- It reads the rows in chunks that grow from 64 to 4096 rows
  ([`_CHUNK_START`](../src/vortex_rdflib/pushdown.py#L193)).
- For each chunk, it decodes the codes the query's cache does not hold yet in
  one `TermDict.decode_many` call.
- It builds each solution as a
  [`_Solution`](../src/vortex_rdflib/pushdown.py#L538), an rdflib
  `FrozenBindings` that takes its bindings without copying them.

A consumer that stops early, such as a `LIMIT` or an `EXISTS` that found a
match, never decodes the rows it did not read. Columns are turned into Python
tuples one slice at a time, for the same reason
([`_column_rows`](../src/vortex_rdflib/pushdown.py#L4440)).
[`Store.triples`](../src/vortex_rdflib/store.py#L318), which rdflib's own
evaluator calls for the nodes the hook leaves to it, reads and decodes its
match in the same chunks.

## A worked example

```sparql
SELECT DISTINCT ?o WHERE {
  ?s <p> ?m .
  ?m <p> ?o .
  FILTER(isIRI(?o))
}
LIMIT 5
```

rdflib translates this into the tree below, which the hook reads as a head
over a block:

```text
SelectQuery                    ← opens the query's scope, then rdflib evaluates it
└─ Slice(0, 5)                 ┐
   └─ Distinct                 │  head   (_plan_head)
      └─ Project(?o)           ┘
         └─ Filter(isIRI(?o))  ┐  block  (_solve_block → Relation)
            └─ BGP(?m <p> ?o,  │
                   ?s <p> ?m)  ┘
```

The hook opens the query's scope at `SelectQuery` and hands that node back to
rdflib's `evalPart`. The hook has no handler for it, so rdflib evaluates it and
offers `Slice` next. The hook takes `Slice` and everything below it, in one
call:

1. `_plan_head` splits off `Slice → Distinct → Project` and passes the
   `Filter` to `_solve_block`.
2. `isIRI(?o)` uses a single variable, and vortex-rdf can decide it: all IRIs
   are one code range. It becomes a keep on the pattern that binds `?o`
   ([`_push_native_restrictions`](../src/vortex_rdflib/pushdown.py#L3585)).
3. Both patterns are counted. They differ only in their variable names, so
   they are the same quad pattern and one count serves both.
4. `?m <p> ?o` is matched with its keep: only rows whose object is an IRI come
   back, and no term is decoded to find them. `?s <p> ?m` has no keep, so it is
   matched separately.
5. The two sides are about the same size, so they are joined in one go rather
   than row by row: a native join on `?m`
   ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L3810)). The block's
   relation has the schema `(?m, ?o, ?s)`.
6. The head works on codes: `Project` keeps the `?o` column, `Distinct` drops
   duplicate codes natively, and `Slice` takes the first five rows.
7. Decoding comes last: one `decode_many` call for five codes, then five
   solutions.

## Basic graph patterns

**Shape:** `BGP(triples)`. Terms can be variables, blank nodes (which act as
variables), IRIs and literals.

```sparql
SELECT ?s ?o WHERE { 
  ?s <p> ?m . 
  ?m <p> ?o . 
}
```

[`_solve_bgp`](../src/vortex_rdflib/pushdown.py#L2487) matches a single
pattern directly. Its columns become the relation without being copied into
Python ([`_rel_from_pattern`](../src/vortex_rdflib/pushdown.py#L3724)).

Several patterns go to
[`_join_incremental_bgp`](../src/vortex_rdflib/pushdown.py#L2991), which plans
the join from the pattern sizes:

1. **Resolve.** [`_pattern_terms`](../src/vortex_rdflib/pushdown.py#L3507)
   turns each triple pattern into a quad pattern: the bound terms as N-Triples
   spellings, `None` for variables, and the graph of the current scope (see
   [Named graphs](#named-graphs)).
2. **Count.** [`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2694) asks
   vortex-rdf how many quads each pattern selects (`count_quads`), without the
   pattern's keep. A count is much cheaper than a match, since no columns come
   back. Patterns with more bound terms are counted first — the point patterns
   (a bound subject, or a pattern a secondary index serves) one at a time —
   and a count of zero ends the block before anything is matched. A pattern
   that counts at most [`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L2421)
   (100) rows marks every pattern not yet counted that shares a variable with
   it: those join after it, so planning does not count them. When one's turn
   comes, it is counted only as far as the probe-or-match decision needs,
   capped at `running × 100 + 1`, `running` being the rows of the relation so
   far ([`_count_pattern`](../src/vortex_rdflib/pushdown.py#L2667)): a capped
   answer means probe. When enough patterns need a scan, they are counted
   together in one `count_quads_many` call, which runs them concurrently.
3. **Share.** Patterns that resolve to the same quad pattern share one count,
   and one match too unless filters narrow them differently
   ([`_pattern_key`](../src/vortex_rdflib/pushdown.py#L2649)).
4. **Seed.** The smallest counted pattern is matched first and becomes the
   running relation.
5. **Extend.** Each next step takes the smallest remaining pattern that shares
   a variable with the relation (or the smallest one overall, for a cross
   product; an uncounted pattern ranks last) and joins it in one of three ways:
   - **Probe join** ([`_probe_join`](../src/vortex_rdflib/pushdown.py#L4188)),
     when the relation is at least
     [`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L2421) (100) times
     smaller than the pattern's count and each probe is a point lookup (a
     bound subject, or a pattern a secondary index serves:
     [`_cheap_probe`](../src/vortex_rdflib/pushdown.py#L2791)). The pattern is
     never matched as a whole. Instead, each row's codes are filled into the
     pattern, and that narrower pattern (a **probe**) is matched. Probes are
     sent one by one, except on a file-backed store from 256 at a time, which
     go through `match_codes_many` in one batch whose answers are pulled as the
     join reaches them
     ([`_run_probes`](../src/vortex_rdflib/pushdown.py#L2821)). A probe with
     no free variable is a count, and its row stands once per quad counted:
     at most once in a single graph, once per graph that holds the triple
     under a union default graph.
   - **Keep-scan** ([`_keep_scan`](../src/vortex_rdflib/pushdown.py#L4095)),
     when the relation is that much smaller but each probe would scan the
     store: the pattern is matched once, narrowed to the codes the relation
     binds (see [Keep-scans](#keep-scans)).
   - **Hash join** otherwise: the pattern is matched once and joined. When
     both sides are columns and share exactly one variable,
     [`_join_columns`](../src/vortex_rdflib/pushdown.py#L3810) pairs the rows
     with the native `join_indices` and gathers the columns with `take`, so
     the result stays columnar. Other cases (several shared variables, a
     repeated variable as in `?x <p> ?x`, a filter still to apply in Python, a
     cross product) use the Python hash join
     [`_join`](../src/vortex_rdflib/pushdown.py#L4338).

A literal that a join puts in subject or predicate position is not an error:
the pattern simply matches nothing, as with `Store.triples()`. The same holds
for an IRI vortex-rdf's parser rejects, such as a relative one
([`unparseable_iri`](../src/vortex_rdflib/terms.py#L154)), in any position:
it is no term of the file.

**Left to rdflib:** property paths and RDF-star quoted triples, which
`_pattern_terms` declines.

### The pair budget

A join can make as many rows as the product of its sides. The pair budget,
[`_PAIR_BUDGET`](../src/vortex_rdflib/pushdown.py#L2448), bounds the pairs one
join step of a BGP builds at once: 16,777,216 unless `VORTEX_RDF_PAIR_BUDGET`
sets another value (see [Switches](#switches)). A step is guarded before it is
built:

- **Hash join.** A step whose sides' rows multiply to no more than the budget
  cannot pass it, and is built as before. Past that product bound,
  [`_pair_count`](../src/vortex_rdflib/pushdown.py#L3783) counts the pairs
  natively first (`U64Column.join_count`, no pair built). A step that counts
  more than the budget raises
  [`_OverBudget`](../src/vortex_rdflib/pushdown.py#L2451) before any pair
  exists; one within it is built with the budget as the `max_pairs` of
  `join_indices`
  ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L3810)). A join of
  Python rows counts the pairs of its first shared variable the same way
  ([`_row_step_pairs`](../src/vortex_rdflib/pushdown.py#L3792)), which is an
  upper bound when it shares several.
- **Probe join.** It has no pairs to count, so it stops once its rows pass a
  quarter of the budget. It drops the rows it made and the batch it is pulling,
  which aborts the probes still in flight, and the step is planned again as a
  keep-scan.
- **Keep-scan.** It stops once its match passes the budget.

A step over the budget is traced as `bgp_over_budget` and is not built.

### Over the budget

A step over the budget goes to the first of three tiers that takes it.

1. **Stream** (tier 1,
   [`_streamed_step`](../src/vortex_rdflib/pushdown.py#L3920)). The last step,
   when it shares exactly one variable with the relation, is gathered a piece
   at a time. The side with fewer rows is grouped on the shared variable
   (`U64Column.groups`), and the other side is fed through the groups in slices
   of at most [`_PIECE_ROWS`](../src/vortex_rdflib/pushdown.py#L2470) rows
   ([`_slices`](../src/vortex_rdflib/pushdown.py#L3845), `U64Column.slice`). A
   pattern that is not read yet and counts more rows than both the budget and
   the relation, or whose keep-scan came back over the budget, is not matched
   whole. The relation is grouped, and the pattern is fed in chunks of at most
   `_PIECE_ROWS` rows from `match_code_chunks`, narrowed by its native keep.
   That needs a pattern with no deferred restriction and no repeated variable
   ([`_read_in_chunks`](../src/vortex_rdflib/pushdown.py#L3978),
   [`_chunkable`](../src/vortex_rdflib/pushdown.py#L3986)).
   [`_gather_pieces`](../src/vortex_rdflib/pushdown.py#L3853) gathers a piece
   when it is pulled: `CodeGroups.count` gives the pairs of a slice or chunk,
   one over the budget is halved (a single row is gathered whole, its pairs
   being rows of the grouped side, which is held already), and
   `CodeGroups.gather` fills the columns of the pairs.
2. **Reorder** (tier 2,
   [`_fits`](../src/vortex_rdflib/pushdown.py#L3996)). Any other step is put
   back ([`_put_back`](../src/vortex_rdflib/pushdown.py#L4034)): its pattern is
   unread again, with its match left in the memo, and the next step takes the
   first other pattern that shares a variable with the relation and provably
   fits. A pattern fits when the relation's rows times its count (a count that
   did not stop at its cap) are within the budget, or when the step would
   hash-join it, its match is held already, and the pairs `join_count` counts
   between that match and the relation are within the budget. Nothing is
   counted or matched to decide. The patterns it was reordered around are tried
   again after it.
3. **Let rdflib finish** (tier 3,
   [`_finish_with_rdflib`](../src/vortex_rdflib/pushdown.py#L4472)). When none
   of the patterns still to join fits, they stay with rdflib: for each row of
   the relation so far, rdflib's `evalBGP` joins them, the pattern with the
   fewest unbound terms first
   ([`_most_bound_first`](../src/vortex_rdflib/pushdown.py#L4492)). It reads
   them through `Store.triples`, which reads a pattern of more than
   [`_TRIPLES_CHUNK_ROWS`](../src/vortex_rdflib/store.py#L42) (65,536) rows
   from vortex-rdf in chunks of that many. Tier 3 has conditions, below.

### Streamed rows

The relation of a streamed step has neither columns nor rows. Its body is
**pieces** ([`Relation`](../src/vortex_rdflib/pushdown.py#L821)): an iterator
of column tuples, consumed once. A consumer that reads its rows as they come
(the emitter, `DISTINCT`, `COUNT`, `ASK`) pulls a piece at a time and lets each
go before the next is gathered
([`_piece_rows`](../src/vortex_rdflib/pushdown.py#L4425)), so a streamed join
holds one piece, besides its grouped side. A `LIMIT` or an `ASK` stops pulling
after the piece it needs, and closing the rows closes the pieces, which
releases what feeds them. A consumer that holds rows (`ORDER BY`, group joins,
`OPTIONAL`, `MINUS`, `EXISTS`) drains the pieces into rows first
([`_rows_of`](../src/vortex_rdflib/pushdown.py#L2135)), and the relation keeps
them as its body.

### Keep-scans

When the relation is far smaller than a pattern but each probe would scan the
store, probing per row would scan it once per row.
[`_keep_scan`](../src/vortex_rdflib/pushdown.py#L4095) matches the pattern once
instead. For each variable the pattern shares with the relation, the
relation's distinct codes, ascending, become a native keep at every position
the variable takes
([`_set_native_keep`](../src/vortex_rdflib/pushdown.py#L3574)), on top of any
keep the pattern has. The match leaves out only the rows that no row of the
relation can join, and the step joins it as a hash join does, under the same
budget. A pattern whose count is over the budget, or was stopped at its cap, is
matched only up to one row past the budget: a match that reaches that is over
the budget, and is dropped.

`OPTIONAL`, a lazy group join and `EXISTS` keep their probes. A batch of probes
that each scan is given a lookahead of
[`_SCAN_LOOKAHEAD`](../src/vortex_rdflib/pushdown.py#L2788) (one per core,
eight at most): the native layer runs at most twice that many ahead of the
pulls ([`_probe_lookahead`](../src/vortex_rdflib/pushdown.py#L2796)). A batch
of point lookups runs all at once.

### Tier 3 and the emitter

rdflib extends a row only where the rows go straight to the emitter
([`_yield_solutions`](../src/vortex_rdflib/pushdown.py#L4450)). The `stream`
flag says so, and [`_solve_block`](../src/vortex_rdflib/pushdown.py#L1745)
hands it down to the BGP.
[`_eval_block_node`](../src/vortex_rdflib/pushdown.py#L1661) sets it;
[`_eval_head`](../src/vortex_rdflib/pushdown.py#L1042) sets it unless the head
has an `ORDER BY` or a `DISTINCT`. A `FILTER` passes it on unless it holds an
`EXISTS` ([`_solve_filter`](../src/vortex_rdflib/pushdown.py#L1877)), and
`GRAPH` passes it on. A group join, `OPTIONAL`, `MINUS`, an aggregate and an
`ASK` do not. Tier 3 also needs the block to be matched in the graph the
context has made active, which is the graph `evalBGP` reads, and no `FILTER`
conjunct to have been pushed into the patterns, which `evalBGP` would not
apply. A `FILTER` above a block rdflib still finishes tests its remaining
conjuncts on the rows before rdflib extends them; a conjunct over a variable
only the finished patterns bind sends the `FILTER` back to rdflib.

**The residual hand-back.** Where tier 3 is refused, a step over the budget
that no tier takes raises `NotImplementedError` while the query is planned.
rdflib evaluates the node and offers the nodes below it to the hook again, so
the BGP arrives alone, as a block that goes straight to the emitter, and tier 3
applies. Each node offered again re-plans up to the over-budget step.

## FILTER

**Shape:** `Filter(expr, block)`. rdflib merges all `FILTER`s of a group into
one node above the whole group, joined with `&&`.

```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?s ?v WHERE {
  ?s <p> ?v .
  FILTER(datatype(?v) = xsd:integer && ?v < 100)
}
```

### Splitting into conjuncts

[`_solve_filter`](../src/vortex_rdflib/pushdown.py#L1877) calls
[`analyze_filter`](../src/vortex_rdflib/filters.py#L1213), which splits the
expression at its top-level `&&`. A row passes only when every part (a
**conjunct**) is true, so each conjunct can be handled on its own. Conjuncts
are grouped by how many of the block's variables they use
([`FilterPlan`](../src/vortex_rdflib/filters.py#L1189)):

- **No variable:** evaluated once
  ([`evaluate_constant`](../src/vortex_rdflib/filters.py#L1306)). If it is
  false, the block is empty and nothing is matched.
- **One variable:** pushed down to the patterns and `VALUES` tables that bind
  the variable, so rows are dropped before any join.
  - A kind test or an IRI equality becomes a keep on the match (see
    [Native predicates](#native-predicates)).
  - Otherwise it is evaluated once per distinct code of the variable — by
    vortex-rdf when it has a native shape, in Python otherwise: over the
    distinct codes of the matched column
    ([`_restrict_pattern`](../src/vortex_rdflib/pushdown.py#L3654)), the match
    then compacted to the rows that pass with the native `join_indices` and
    `take` ([`_compact_pattern`](../src/vortex_rdflib/pushdown.py#L3699)), or,
    for a probed pattern, over the codes the probes return (in `_probe_join`).
  - A variable that an `OPTIONAL` may leave unbound is not pushed down; its
    conjunct is applied after the join.
- **Several variables:** applied to the joined rows, with each answer
  remembered per distinct tuple of codes
  ([`tuple_predicate`](../src/vortex_rdflib/filters.py#L1390)). Over columns,
  the test runs while the rows stream out
  ([`_filter_relation`](../src/vortex_rdflib/pushdown.py#L2396)), so a `LIMIT`
  above stops it early.
- **`EXISTS` / `NOT EXISTS`:** a semi-join or anti-join, see
  [FILTER (NOT) EXISTS](#filter-not-exists).

Pushed-down conjuncts travel in a
[`_Pushed`](../src/vortex_rdflib/pushdown.py#L1720). They reach both sides of
a group join and the left side of an `OPTIONAL` or `MINUS`, but never the right
side of an `OPTIONAL` or `MINUS`, where dropping a row would change the answer.

During a BGP join, a conjunct over several variables (one the fast route below
can compile) gets two more chances to act early:

- **Early pruning**
  ([`_prune_early`](../src/vortex_rdflib/pushdown.py#L3409)): once the relation
  binds all of the conjunct's variables, the conjunct may be applied mid-join.
  This only happens when a smaller relation would let a later pattern be probed
  instead of matched whole, and it stops as soon as too many rows pass.
- **Specialization**
  ([`_specialize_tuple_conjuncts`](../src/vortex_rdflib/pushdown.py#L3341)):
  when the relation holds a single value for every variable of the conjunct
  but one, the conjunct becomes a test of that one variable
  ([`specialize`](../src/vortex_rdflib/filters.py#L1290)), evaluated by
  vortex-rdf over the codes the pattern binding it brings. For example, once
  `?ref` is bound to a single value, `?v < ?ref + 10` is a numeric comparison
  over the `?v` codes of that match or of those probes.

### Evaluating in Python

Python evaluates a conjunct once per distinct value (or tuple of values), never
once per row. It has two routes
([`evaluate_column`](../src/vortex_rdflib/filters.py#L1315)):

- **Fast route.** [`compile_fast`](../src/vortex_rdflib/filters.py#L738)
  compiles a known set of expression shapes into a Python function that reads
  the term's spelling ([`parse_spelling`](../src/vortex_rdflib/terms.py#L77))
  instead of building an rdflib term. It covers comparisons, `IN`/`NOT IN`,
  `sameTerm`, `datatype`, `lang`, `langMatches`, `isIRI`/`isBlank`/`isLiteral`
  (code range checks), `isNumeric`, `bound`, `str`, `regex`, `strstarts`,
  `strends`, `contains`, `+` and `-` on integers, and `&&`, `||`, `!`. Each
  piece copies rdflib's behaviour and answers true, false, error (treated as
  false) or *unknown*: a value whose rdflib result it cannot reproduce exactly,
  such as an ill-typed number.
- **Generic route.** rdflib's own evaluator (`_ebv`) on the decoded term
  ([`Conjunct.generic`](../src/vortex_rdflib/filters.py#L1173)). It handles the
  *unknown* values and every expression the fast route does not cover, and
  gives rdflib's answer by construction. For a conjunct of one variable, the
  values it handles are decoded in one `decode_many` call.

Because the generic route accepts any expression, the hook takes almost every
`Filter` above a block (see the exceptions below).

The fast route only computes `+` and `-` when every operand is a well-formed
integer literal: that is the one case where it can spell the result exactly as
rdflib does. Any other operand makes the value *unknown*, so that value goes to
the generic route.

### Native predicates

Some single-variable conjuncts are decided by vortex-rdf
([`native_shape`](../src/vortex_rdflib/filters.py#L935)), never over the whole
dictionary: a keep narrows the scan itself, and every other shape is evaluated
over the codes the query produced. Nothing is remembered between calls.

- **Keeps.** `isIRI(?v)`, `isBlank(?v)` and `isLiteral(?v)` are their kind's
  code range. `?v = <iri>`, `<iri> = ?v`, `sameTerm(?v, <iri>)` and
  `?v IN (<iri>, ...)` are the IRIs' codes, from one `encode_many`
  ([`keep_of`](../src/vortex_rdflib/candidates.py#L69)). An IRI the dictionary
  lacks has no code, and a pattern left with no code matches nothing: IRIs the
  dictionary lacks, `IN ()` and lists with no IRI in common all leave none.
  Both kinds become a keep on the match of every pattern binding the variable
  ([`native_restriction`](../src/vortex_rdflib/filters.py#L1028)), intersected
  per position ([`intersect_keeps`](../src/vortex_rdflib/filters.py#L1051)).
  Rejected rows never leave vortex-rdf, with one exception: a probe carries a
  range keep into vortex-rdf and tests a code-set keep on the rows it gets
  back. An `ASK` or `COUNT` over the pattern counts under the keep.
- **Candidate evaluation.** These are decided over candidate codes:
  - `datatype(?v) = <iri>` and `lang(?v) = "tag"`;
  - `langMatches(lang(?v), "range")`;
  - `?v` compared with a numeric constant (`<`, `<=`, `>`, `>=`, `=`, `!=`);
  - `strstarts(str(?v), "prefix")`;
  - `REGEX`, `CONTAINS`, `STRSTARTS` and `STRENDS` with the text `?v` or
    `STR(?v)`, optionally under `LCASE` or `UCASE`.

  The candidates are the codes the variable first has: the distinct codes of
  its matched column, of the join step that binds it, or of the rows the
  probes reached. They go to `TermDict.filter_codes(kind, arg, codes)`, which
  answers each code passed, failed or undecided
  ([`evaluate`](../src/vortex_rdflib/candidates.py#L97)). The undecided codes
  go on to the Python routes.

Only the codes in a predicate's domain are sent:

- the literal predicates fail every IRI and blank node, and `!=` passes them
  all;
- the string functions fail them too, unless the text is `STR(?v)`: then IRIs
  are sent, and blank nodes, whose `STR` is their label, stay with the Python
  routes.

A code that is no term's, such as the empty graph name or the negative code of
a constant outside the dictionary, is left undecided under every shape.

Where vortex-rdf and rdflib could disagree, rdflib's answer wins:

- `langMatches` is only native for `*` and for ranges made of ASCII letters
  and digits.
- A typed constant outside its datatype's range, such as `"300"^^xsd:byte`, is
  never decided natively. `xsd:long` and `xsd:unsignedLong` values beyond 64
  bits come back undecided.
- A string constant whose language tag is not lower-case stays with the
  Python routes, because rdflib compares tags as written and the dictionary
  stores them lower-cased. So does a `STR(?v)` text with a language-tagged
  constant.
- `REGEX` is native only for patterns Python itself compiles. vortex-rdf
  decides a pattern only when it is in its allow-list grammar. It leaves
  Unicode-sensitive constructs (`\w`, `\b`, flag `i`, ...) undecided on
  non-ASCII text, and `$` without flag `m` undecided on text ending in a
  newline. Only the flags rdflib maps (`i`, `s`, `m`) are passed.
- `LCASE` and `UCASE` are decided on ASCII text only.

### Visibility and fallbacks

As in rdflib's `evalFilter`, a filter sees the block's variables, the outer
bindings named in the node's `_vars` or in the query's `initBindings`, and all
outer bindings when it sits directly inside an `EXISTS` body (see
`analyze_filter`).

**Left to rdflib:**

- a `FILTER` that uses an impure function (`RAND`, `UUID`, `STRUUID`,
  `BNODE()`), since remembering its result per value would change the answer;
- a conjunct that, in rdflib, could see a variable an enclosing join binds row
  by row; the hook solves both sides separately, so the conjunct could not see
  it ([`_reject_env_references`](../src/vortex_rdflib/pushdown.py#L2092));
- a conjunct on a variable both the block and such a row bind, where rdflib's
  filter forgets the row's binding (a `VALUES` table's, outside the node's
  `_vars`, or any in a group of an `EXISTS` body), so sees it unbound;
- a conjunct holding an `EXISTS` that names an outer binding the filter does
  not see (an `OPTIONAL`'s filter sees only the query's `initBindings`), where
  rdflib evaluates the filter once per row and calls the hook with the row
  bound: the `EXISTS` would see the binding
  ([`_reject_forgotten_exists`](../src/vortex_rdflib/pushdown.py#L2121)).

## Projection, LIMIT/OFFSET and ASK

**Shape:** `Slice(Project(block))`, `Project(block)`, `AskQuery(Project(block))`.

```sparql
SELECT ?s WHERE { ?s <p> ?o } LIMIT 10
ASK { ?s <p> "42" }
```

- **Projection** decodes only the projected variables (in `_yield_rows`).
- **`LIMIT`/`OFFSET`** is applied to the code rows, before anything is
  decoded. Without `ORDER BY` or `DISTINCT`, `_eval_head` also passes the limit
  down to the block. A block that is a single pattern then calls
  `match_codes(..., limit=k)`, and the scan stops after k rows. This needs two
  conditions
  ([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2974)): nothing
  is left for Python to filter out, and the store has no secondary indexes, so
  that the limited match returns the same first rows as a full match.
- **`ASK`** ([`_block_nonempty`](../src/vortex_rdflib/pushdown.py#L1674)) over
  a single pattern, whose `FILTER` (if any) is made of keeps only (see
  [Native predicates](#native-predicates)), is `count_quads(..., limit=1)`: it
  stops at the first match, without matching rows or decoding terms. Any other
  `ASK` solves its block with a one-row limit and checks for a first row.

## DISTINCT

**Shape:** `Distinct(Project(block))`, possibly under `Slice` and above
`OrderBy`.

```sparql
SELECT DISTINCT ?p WHERE { ?s ?p ?o }
```

[`_eval_distinct`](../src/vortex_rdflib/pushdown.py#L1220) removes duplicates in
two passes, and decodes only what survives:

1. **By code** ([`_code_distinct`](../src/vortex_rdflib/pushdown.py#L1232)):
   duplicate code tuples are dropped. For a single column this is the native
   `U64Column.distinct`, which keeps the first-seen order.
2. **By term** ([`_term_distinct`](../src/vortex_rdflib/pushdown.py#L1253)):
   rdflib compares terms, and two spellings can be the same typed literal
   (`"042"^^xsd:integer` and `"42"^^xsd:integer` are both 42).
   [`_alias_codes`](../src/vortex_rdflib/pushdown.py#L1271) maps each such code
   to one code of the same term: its canonical spelling's when the dictionary
   holds it, else the first one seen. Only typed literals rdflib rewrites can
   alias. A canonical `xsd:integer` and a literal of a datatype rdflib cannot
   convert are kept as written, so they are classified by their spelling
   ([`_rewritable`](../src/vortex_rdflib/pushdown.py#L1346)); the other typed
   literals are turned into terms, and look their canonical spellings up in
   one `TermDict.encode_many` call
   ([`_canonical_twins`](../src/vortex_rdflib/pushdown.py#L1377)). Each
   literal code is checked once per query, and the result lives in the
   query's scope; in most stores no code aliases another.

`LIMIT` applies after both passes, lazily.

**Left to rdflib:** `REDUCED`.

## COUNT and GROUP BY

**Shape:** `AggregateJoin(Group(block))` with `COUNT(*)`, `COUNT(?v)` or
`COUNT(DISTINCT ?v)`, and the `SAMPLE` that rdflib adds for each `GROUP BY`
variable. The nodes above it (the `Extend` that names the results, and any
`HAVING`) stay with rdflib and run on the rows the hook returns.

```sparql
SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p
```

[`_eval_aggregate`](../src/vortex_rdflib/pushdown.py#L1415) has two paths:

- **One pattern, no `GROUP BY`:** a `COUNT(*)` or `COUNT(?v)` is a single
  `count_quads` call ([`_count_pattern`](../src/vortex_rdflib/pushdown.py#L2667)),
  and no row is matched. This also works under a `FILTER` made of keeps only:
  the count uses them.
- **Otherwise** the block is solved and counted on codes
  ([`_aggregate_rows`](../src/vortex_rdflib/pushdown.py#L1484)). A columnar body
  grouped by at most one variable is counted one column at a time
  ([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1604)): the native
  `value_counts` of the key column gives every group and its size. For
  `COUNT(DISTINCT ?v)`, each group's rows are gathered natively
  (`join_indices`, `take`, `distinct`) when there are few groups
  ([`_NATIVE_GROUPS_MAX`](../src/vortex_rdflib/pushdown.py#L1601)), and read
  from the distinct (key, value) pairs otherwise. Other bodies are counted in a
  Python loop.

Only the group keys are decoded. Groups whose keys are the same term are
merged, and distinct counts compare terms, through the same aliasing as
`DISTINCT`.

**Left to rdflib:** `SUM`, `MIN`, `MAX`, `AVG`, `GROUP_CONCAT`, `SAMPLE` of a
variable that is not a group key, `GROUP BY` on an expression, and `COUNT` of
an expression.

## OPTIONAL, MINUS and group joins

**Shape:** `LeftJoin(p1, p2, expr)` for `OPTIONAL`, where `expr` is the
`FILTER` inside the optional group (rdflib moves it onto the node);
`Minus(p1, p2)`; and `Join(p1, p2)` for nested groups.

```sparql
SELECT ?s ?o ?x WHERE {
  ?s <p> ?o .
  OPTIONAL { ?s <q> ?x }
}
```

rdflib evaluates the right side of an `OPTIONAL`, and of most group joins, once
for every left row. The hook usually solves each side once instead, and
combines them on their shared variables:

- **`OPTIONAL`**
  ([`_solve_left_join`](../src/vortex_rdflib/pushdown.py#L2204)): a hash left
  join ([`_left_join_rows`](../src/vortex_rdflib/pushdown.py#L2329)). The
  optional group's `FILTER` is checked on each candidate pair. A left row with
  no passing match is kept, with the right side's variables unbound.
- **`MINUS`** ([`_solve_minus`](../src/vortex_rdflib/pushdown.py#L2353)): an
  anti-join. When the sides share no variable (under `GRAPH ?g`, the graph
  column alone does not count), it follows rdflib's rule: with no outer
  bindings (the usual top-level case) nothing is removed; with outer bindings,
  everything is removed if the right side has any row (under `GRAPH ?g`, any
  row in the same graph).
- **Group join** ([`_solve_join`](../src/vortex_rdflib/pushdown.py#L2155)): a
  hash join (`_join`). When rdflib would deduplicate the right side (a join it
  does not evaluate lazily), so does the hook.

When the right side of an `OPTIONAL` or of a lazy group join is a single
pattern, [`_plan_pattern_side`](../src/vortex_rdflib/pushdown.py#L3471) counts
it first, only as far as the decision needs: up to the left relation's rows ×
`_PROBE_FANOUT` + 1. If the left relation is at least `_PROBE_FANOUT` times
smaller than that count, the pattern is probed once per left row (with
`_probe_join`, which also pads unmatched `OPTIONAL` rows) and never matched
whole. Otherwise it is matched once and hash-joined.

**Left to rdflib.** These shapes fall back so that the answers stay identical:

- a join key that an `OPTIONAL` may leave unbound: for rdflib an unbound value
  matches anything, which a hash join cannot express
  ([`_shared_key_ok`](../src/vortex_rdflib/pushdown.py#L2148));
- an `OPTIONAL` whose right side uses a variable that the left side's `_vars`
  leave out (one bound by the outer context, an enclosing join or a `VALUES`
  table): rdflib checks unmatched rows a second time with only those `_vars`
  bound, which can change the answer (a similar check covers the `OPTIONAL`'s
  filter);
- a `FILTER` inside a side that would see a binding of an enclosing join's row
  that rdflib's filter forgets, or miss one that rdflib's filter keeps, in an
  `EXISTS` body too (see [Visibility and fallbacks](#visibility-and-fallbacks));
- an `OPTIONAL` whose filter contains `EXISTS`;
- a `MINUS` that rdflib solves once per row of an enclosing lazy join,
  `OPTIONAL` or `EXISTS`, when its right side binds a variable of that row
  that its left side does not (rdflib matches it with the row's value), or
  when its sides share no variable (whether the row binds anything decides).

## FILTER (NOT) EXISTS

**Shape:** a `FILTER` conjunct that is `EXISTS { body }` or
`NOT EXISTS { body }`, possibly under one `!`, whose body is a block
([`exists_shape`](../src/vortex_rdflib/filters.py#L1196)).

```sparql
SELECT ?s WHERE {
  ?s <p> ?o .
  FILTER NOT EXISTS { ?s <q> ?x }
}
```

rdflib evaluates the body once per row. Instead,
[`_apply_exists`](../src/vortex_rdflib/pushdown.py#L1952) runs a semi-join
(`EXISTS`) or an anti-join (`NOT EXISTS`) on the variables the body shares with
the block:

- Usually the body is solved once, and each row is kept or dropped by looking
  up its key among the body's keys.
- When the body is a single pattern and the block is much smaller than the
  pattern's count (counted only up to the block's rows × `_PROBE_FANOUT` + 1),
  each row is probed instead, with `count_quads(..., limit=1)` sent in batches
  ([`_probe_exists`](../src/vortex_rdflib/pushdown.py#L2045)).
- When the body shares no variable with the block, it is a single existence
  check, solved with a one-row limit.

The body is read from rdflib's translated algebra, because rdflib removes the
body's own `FILTER`s from the parse tree
([`expr_vars`](../src/vortex_rdflib/filters.py#L1089)). rdflib also wraps the
body in a `Join` with an empty `BGP`, which the hook skips
([`_unwrap_empty_joins`](../src/vortex_rdflib/pushdown.py#L2031)).

**Left to rdflib.** rdflib's evaluator decides the conjunct, once per distinct
tuple of values, when a shared variable may be unbound in the block or in the
body's solutions, when the body is not a block, or when a `FILTER` in the body
sees the block's bindings. Where such a top-level `EXISTS` conjunct's body
holds a `MINUS`, and the `EXISTS` names no variable of the block, or one the
block may leave unbound, the whole `FILTER` goes to rdflib: rdflib binds the
whole row in the body, so the `MINUS` always shares a binding. An `EXISTS`
inside `||` or another operator is decided by rdflib's evaluator too, and its
`FILTER` goes to rdflib whole when its body holds a `MINUS`. Under `GRAPH ?g`,
the whole block goes to rdflib (see [Named graphs](#named-graphs)).

## ORDER BY

**Shape:** `Project(OrderBy(block))`, possibly under `Slice` and `Distinct`,
when every sort key is a plain variable.

```sparql
SELECT ?s ?v WHERE { ?s <p> ?v } ORDER BY DESC(?v) LIMIT 10
```

[`_order_rows`](../src/vortex_rdflib/pushdown.py#L1079) sorts the code rows:

1. Each sort variable's distinct codes are ranked once
   ([`_rank_codes`](../src/vortex_rdflib/pushdown.py#L1138)), in rdflib's order:
   unbound first, then blank nodes, then IRIs, then literals. Blank nodes rank
   by code: the spelling `_:label` sorts as the label. IRIs rank by their
   spelling without the brackets, which is the string rdflib compares; no
   rdflib term is built. Their codes are not in that order: the closing `>` of
   `<iri>` puts `<…/a>` after `<…/a/b>`. Literals are decoded once per query
   and ranked by rdflib's own comparison. Terms that compare equal share a
   rank.
2. A row's sort key is its tuple of ranks, negated for `DESC`. One stable sort
   gives the same order as rdflib.
3. With a `LIMIT` and no `DISTINCT`, only the top k rows are kept
   (`heapq.nsmallest`).

**Left to rdflib:** `ORDER BY` on an expression.

## VALUES

**Shape:** `ToMultiSet(values)`, an inline table used as a block leaf, usually
the left side of a group join.

```sparql
SELECT ?s ?o WHERE {
  VALUES ?s { <s1> <s2> <s3> }
  ?s <p> ?o .
}
```

[`_solve_values`](../src/vortex_rdflib/pushdown.py#L1809) turns the table into
a relation, which then joins like any other:

- All constants are looked up in one `TermDict.encode_many` call
  ([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1841)), spelled the
  way the dictionary stores terms
  ([`canonical_spelling`](../src/vortex_rdflib/terms.py#L130)). An IRI
  vortex-rdf's parser rejects is not looked up: no file holds it.
- `UNDEF` becomes an unbound value.
- A constant the dictionary does not hold, or that is not looked up, gets a
  private negative code
  ([`_foreign_code`](../src/vortex_rdflib/store.py#L660)). It joins nothing,
  but is still returned as written.
- Rows that contradict an outer binding are dropped, as in rdflib.
- Rows whose term fails a `FILTER` conjunct pushed down to the table are
  dropped, even where a pattern that binds the variable tests it too. Each
  distinct term is tested once, as the query writes it, which is the term
  rdflib binds
  ([`constant_predicate`](../src/vortex_rdflib/filters.py#L1421)).

**Left to rdflib:** a constant whose rdflib term differs from the dictionary's
term, such as `"042"^^xsd:integer` (rdflib would return the query's own
spelling); any other constant vortex-rdf cannot parse; and a `VALUES` variable
that an `OPTIONAL` re-checks (see above).

## Named graphs

Every pattern is matched as a quad pattern. Its graph position comes from the
current [`_GraphScope`](../src/vortex_rdflib/pushdown.py#L771):

- Outside any `GRAPH`, it is the graph the query runs on
  ([`_active_scope`](../src/vortex_rdflib/pushdown.py#L785), which asks
  [`_graph_n3`](../src/vortex_rdflib/store.py#L203)): `None` (all graphs) for
  a union default graph, `""` for the default graph of a `Dataset` without
  union, or a graph's name.
- Under `GRAPH <iri>`, it is that graph's name.
- Under `GRAPH ?g`, it is a wildcard, and `?g` is bound from the graph column
  of the match.

A graph name vortex-rdf's parser rejects names no graph of the file: a relative
IRI (`GRAPH <zz>` without a BASE), or the IRI rdflib builds from a literal it
binds a `GRAPH` variable to. The pushdown leaves its block to rdflib, and the
store matches nothing for it.

```sparql
SELECT ?g ?s WHERE { GRAPH ?g { ?s <p> ?o } }
```

A `Graph` node matches nothing itself. `_solve_block` solves the block below it
with a new scope ([`_graph_scope`](../src/vortex_rdflib/pushdown.py#L793)), so
a nested `GRAPH` simply replaces the outer one. Under `GRAPH ?g`,
`_pattern_terms` maps `?g` to the fourth column, which makes it an ordinary
variable: it is joined, filtered, grouped, sorted and decoded like any other.
Two patterns under the same `GRAPH ?g` join on `?g`, which is exactly the
requirement that they come from the same graph.

`GRAPH ?g` only covers named graphs, so default-graph rows have to be dropped.
The default graph's name is the empty string, which sorts before every term:
its code is 0, and every named graph has a higher code.
[`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L3555) adds a keep
with that range on the graph position, so default-graph rows never leave
vortex-rdf.

For `ASK` and `COUNT`, [`_peel_graph`](../src/vortex_rdflib/pushdown.py#L809)
looks through `GRAPH <iri>`, so their single-pattern shortcuts also work inside
a named graph.

**Left to rdflib:**

- `FILTER (NOT) EXISTS` under `GRAPH ?g`: rdflib evaluates the body in the
  active graph, and under a graph variable there is no single active graph;
- under any `GRAPH`, an `EXISTS` that rdflib's evaluator decides (one under
  another operator, in an `OPTIONAL`'s condition, or on the generic route):
  rdflib would match its body in the query's graph
  ([`_reject_graph_exists`](../src/vortex_rdflib/pushdown.py#L2113));
- a `GRAPH` variable bound to something that cannot name a graph, such as a
  literal;
- a `GRAPH ?g` group none of whose patterns is matched in its scope (an empty
  group, a `VALUES` table, a `GRAPH` of another graph), or with an `OPTIONAL`
  or `MINUS` that has one on its right side only: rdflib repeats such a part
  once per named graph, which a join with a part matched in the scope also
  does here ([`_carries_graph`](../src/vortex_rdflib/pushdown.py#L983));
- an `OPTIONAL` under `GRAPH ?g` whose right side alone binds `?g`, and a
  `MINUS` likewise where its two sides share a binding (otherwise it removes
  nothing and stays in the pushdown): rdflib matches that `?g` against any
  term and joins the graph in only after the group is solved.

When rdflib evaluates a `Graph` node itself (with
`VORTEX_RDF_PUSHDOWN_OPS=bgp`, for example), it changes `solution.ctx.graph`
while it yields rows. The hook then gives each solution its own context, so
that change cannot leak into later rows
([`_pushed_graph`](../src/vortex_rdflib/pushdown.py#L4512)).

## Caches and lifetimes

Nothing a query computes about terms outlives it
([`query_scope`](../src/vortex_rdflib/query_scope.py)). The decoded terms,
the literal alias classes of `DISTINCT` and `COUNT`, the negative codes of
constants outside the dictionary, and whether a blank node names a graph all
live in the query's scope. That scope is a `ContextVar`:
[`_eval_scoped`](../src/vortex_rdflib/pushdown.py#L671) opens it when rdflib
offers the query node, and it stays current around each step of a lazy result
([`_scoped_iterator`](../src/vortex_rdflib/pushdown.py#L708)). It is dropped
when the last solution is produced, or when the result is closed or
garbage-collected. The scope holds the query's cancel token too
(`QueryScope.cancel`), copied from the token current when the query starts, so
a lazy result keeps the token of the query that made it, and the token goes
with the scope. `Store.triples()` called outside a query (rdflib's non-SPARQL
API) keeps its decodes for that call alone, and reads the cancel token current
when its iteration starts.

The store itself keeps fixed-size structure only: each term kind's code range,
the default graph's code, the indexes the file holds, and one context per
graph of the file. vortex-rdf keeps a bounded cache of the dictionary windows
a mapped store reads; the operating system's page cache holds the rest.

## vortex-rdf primitives

vortex-rdf opens a file-backed store memory-mapped.

| Primitive | Where it is used |
| --- | --- |
| `match_codes`, `count_quads` | Every pattern match and count. |
| `count_quads_many`, `match_codes_many` | Counting several scanning patterns at once ([`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2694)); batched probes for joins, `OPTIONAL` and `EXISTS` ([`_run_probes`](../src/vortex_rdflib/pushdown.py#L2821)), whose answers are pulled one at a time as the join reaches them ([`_probe_matches`](../src/vortex_rdflib/pushdown.py#L2877)). `lookahead=` sets how far ahead of the pulls a batch's probes run ([`_probe_lookahead`](../src/vortex_rdflib/pushdown.py#L2796)). |
| `match_code_chunks` | The chunks a streamed join step is fed from ([`_streamed_step`](../src/vortex_rdflib/pushdown.py#L3920)), and the chunks `Store.triples` reads a large pattern in ([`_code_matches`](../src/vortex_rdflib/store.py#L517)). |
| `keep=` on a match or count | Kind tests and IRI equality ([`native_restriction`](../src/vortex_rdflib/filters.py#L1028)), dropping the default graph under `GRAPH ?g` ([`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L3555)), the codes a keep-scan joins on ([`_keep_scan`](../src/vortex_rdflib/pushdown.py#L4095)), exact `ASK` and `COUNT` answers. |
| `limit=` on a match or count | `LIMIT` over a single pattern ([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2974)), `ASK`, `EXISTS` probes, the existence probes of a join in a fixed graph, the counts that only decide probe or match ([`_count_pattern`](../src/vortex_rdflib/pushdown.py#L2667)), a keep-scan's match (one row past the budget) and `Store.triples`' first match of a pattern (one row past a chunk). `offset=` is not used: an `OFFSET` is applied to the code rows. |
| `cancel=` on a match, count, batch or chunk call (`CancelToken`) | The token the query runs under: every batch and chunk call is given it, and so is each one-shot call the token can stop ([`_cancel_kw_for`](../src/vortex_rdflib/pushdown.py#L290)). |
| `TermDict.filter_codes` | FILTER predicates over the codes a query produced ([`evaluate`](../src/vortex_rdflib/candidates.py#L97)). |
| `TermDict.encode_many` | `VALUES` constants ([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1841)), the IRIs of an IRI equality ([`keep_of`](../src/vortex_rdflib/candidates.py#L69)) and the canonical spellings of the typed literals `DISTINCT` and `COUNT` alias ([`_canonical_twins`](../src/vortex_rdflib/pushdown.py#L1377)). |
| `TermDict.decode_many` | Decoding results ([`_prime_decode_cache`](../src/vortex_rdflib/store.py#L592)), the spellings that probes fill in ([`_probe_spellings`](../src/vortex_rdflib/pushdown.py#L4317)) and the IRIs `ORDER BY` ranks ([`_rank_codes`](../src/vortex_rdflib/pushdown.py#L1138)). |
| `TermDict.prefix_range`, `lower_bound` | The code range of each term kind ([`kind_bounds`](../src/vortex_rdflib/terms.py#L161), [`_kind_ranges`](../src/vortex_rdflib/store.py#L646)). |
| `U64Column.distinct`, `value_counts` | `DISTINCT` over one variable and a variable's candidate codes ([`_distinct_codes`](../src/vortex_rdflib/pushdown.py#L3635)), `GROUP BY` counts ([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1604)). |
| `U64Column.join_indices`, `take` | The columnar join ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L3810)), which passes the pair budget as `max_pairs=` once the pairs are counted; compacting a match to the rows a FILTER keeps ([`_compact_pattern`](../src/vortex_rdflib/pushdown.py#L3699)), a group's rows for `COUNT(DISTINCT)`, intersecting keeps ([`intersect_keeps`](../src/vortex_rdflib/filters.py#L1051)). |
| `U64Column.join_count` | The pairs of a join, counted before it is built ([`_pair_count`](../src/vortex_rdflib/pushdown.py#L3783)), and whether a pattern fits when a step is reordered ([`_fits`](../src/vortex_rdflib/pushdown.py#L3996)). |
| `U64Column.groups`, `CodeGroups.count` and `gather`, `U64Column.slice` | A streamed join step: the grouped side, the pairs and the columns of each slice or chunk, and the slices of the fed side ([`_streamed_step`](../src/vortex_rdflib/pushdown.py#L3920), [`_gather_pieces`](../src/vortex_rdflib/pushdown.py#L3853), [`_slices`](../src/vortex_rdflib/pushdown.py#L3845)). |

## Switches

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Do not register the hook: rdflib evaluates every node. |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only take the listed algebra nodes (`BGP,Filter,Project,...`); `bgp` means basic graph patterns only. |
| `VORTEX_RDF_FILTER_FAST=0` | Send every FILTER value through rdflib's evaluator (still once per distinct value). |
| `VORTEX_RDF_NATIVE_FILTERS=0` | Decide no FILTER conjunct in vortex-rdf: no `filter_codes` calls and no keeps from filters, every value goes through the Python routes. |
| `VORTEX_RDF_PAIR_BUDGET=<n>` | The most row pairs one join step of a basic graph pattern builds at once, an integer of at least 0 (default 16,777,216). A step over it is streamed, reordered around or left to rdflib (see [Basic graph patterns](#basic-graph-patterns)); `0` puts every step that makes a row over it. |
| `VORTEX_RDF_TRACE_QUERY=1` | Print the plan of every query the hook takes to stderr, one JSON line per step, prefixed `VORTEX_RDF_QUERY_TRACE`: patterns counted, matched or probed, join strategies, filter keeps and prunes, every native call, rows decoded. `VORTEX_RDF_TRACE_QUERY_ID` labels the lines of one query. |

`register_sparql_pushdown` reads the first four when a store is created. The
pair budget is read once, when `pushdown.py` is imported
([`_pair_budget`](../src/vortex_rdflib/pushdown.py#L2426)); a value that is not
an integer of at least 0 raises `ValueError` naming the variable. The trace
switch is read for each query
([`_QueryTrace`](../src/vortex_rdflib/pushdown.py#L203)). From Python,
`register_sparql_pushdown()` and
[`unregister_sparql_pushdown`](../src/vortex_rdflib/pushdown.py#L593) turn
the hook on and off.

## Tests

The tests check that the hook never changes an answer:

- [`tests/test_pushdown.py`](../tests/test_pushdown.py) runs every query shape
  twice, with the hook and with rdflib's default evaluator, and compares the
  results. It covers file-backed and in-memory stores, stores with each
  secondary index, and every code path forced on its own (file-backed): probe
  joins only, batched probes only, basic graph patterns only, the generic
  FILTER route only, and no native filters. File-backed and in memory, it also
  forces a pair budget of 0, which puts every join step that makes a row over
  it; the same with pieces of one and of three rows, which streams every last
  step that shares one variable; and keep-scans for every join step that
  shares a variable. A test checks in the trace that each of these forcings
  reaches its path.
- [`tests/test_filters.py`](../tests/test_filters.py) checks each piece of the
  fast FILTER route against rdflib's evaluator, over many literal spellings
  (ill-typed numbers, NaN, out-of-range values, language tags, custom
  datatypes, unbound values).
- [`tests/test_native_filters.py`](../tests/test_native_filters.py) does the
  same for each native predicate, with every spelling of the matrix sent as
  one set of candidate codes.
- [`tests/test_native_strings.py`](../tests/test_native_strings.py) checks
  the string shapes against rdflib and Python's `re`: random patterns from
  vortex-rdf's regex grammar, case folding outside ASCII, trailing newlines,
  and typed literals under `STR()`. With `VORTEX_RDFLIB_BSBM_STORE` set, it
  also runs over a BSBM store's literals.
- [`tests/test_candidate_filters.py`](../tests/test_candidate_filters.py)
  checks which codes reach vortex-rdf.
- [`tests/test_filter_scope.py`](../tests/test_filter_scope.py) checks which
  bindings a `FILTER` sees and a group's solutions carry, against rdflib's
  evaluator: across enclosing joins, `OPTIONAL`, `GRAPH ?g`, `MINUS`, `EXISTS`
  bodies and `VALUES` tables.
- [`tests/test_order_by.py`](../tests/test_order_by.py) checks that `ORDER BY`
  ranks IRIs by their bare string and blank nodes by their label, as rdflib
  does.
- [`tests/test_term_aliases.py`](../tests/test_term_aliases.py) checks which
  typed literals `DISTINCT` and `COUNT` treat as one term: the by-spelling
  check against rdflib's normalization over generated lexical forms, and the
  alias classes against rdflib's term equality over a generated store.
- [`tests/test_query_scope.py`](../tests/test_query_scope.py) checks that
  nothing a query learns outlives it, its cancel token included.
- [`tests/test_native_contract.py`](../tests/test_native_contract.py) pins the
  vortex-rdf API this package relies on: how stores open, what `filter_codes`
  answers and leaves undecided, how keeps and limits narrow a count, and the
  0.13 surface: the batch calls' answer iterators with `lookahead` and
  `cancel`, `JoinTooLarge`, `groups` and `gather`, chunks and `slice`.
- [`tests/test_join_budget.py`](../tests/test_join_budget.py) checks the pair
  budget and what it sets off: the product bound and the native count, the
  three tiers (streamed pieces, a pattern joined first, rdflib finishing the
  rest, and the hand-back where it cannot), keep-scans, the probe tally and the
  probe lookahead. The answers are compared with rdflib's.
- [`tests/test_join_memory.py`](../tests/test_join_memory.py) checks that a
  streamed join holds a fraction of the memory the one-shot join does. Each
  join runs in a subprocess of its own, and its peak RSS is read from
  `/proc/self/status`, so the test is Linux only.
- [`tests/test_cancel.py`](../tests/test_cancel.py) checks cancellation: which
  calls are given the token and which check it, that a cancel from another
  thread stops a streamed join, tier 3, the probes and a chunked `triples()`
  read, that a query keeps its own token, and that without a token no native
  call gains a keyword.
- [`tests/test_store.py`](../tests/test_store.py) checks the rdflib store API,
  `Store.triples` among it: a pattern of more than `_TRIPLES_CHUNK_ROWS` rows is
  read in chunks, with one chunk of codes held at a time, and a smaller one as
  one match.

## What stays with rdflib

- `UNION`, `BIND`, sub-queries and `SERVICE`. rdflib evaluates these nodes and
  offers the nodes below them to the hook, so each `UNION` branch is still
  pushed down.
- Property paths and RDF-star patterns.
- The patterns of a BGP that a join over the pair budget leaves (tier 3):
  rdflib's `evalBGP` joins them into each row of the relation so far, where
  the relation goes straight to the emitter.
- A node whose BGP has a step over the pair budget that no tier takes (the
  residual hand-back): tier 3 is refused there, so rdflib evaluates the node
  and offers the BGP below it to the hook alone, where tier 3 applies.
- `REDUCED`, aggregates other than `COUNT`, and `GROUP BY` or `ORDER BY` on an
  expression.
- Parsing. Every query goes through rdflib's `parseQuery` and `translateQuery`;
  use `prepareQuery` and reuse its result when the same query runs many times.

## Possible improvements

In vortex-rdflib:

- Offer an API that returns decoded tuples or code columns, for callers that
  do not need rdflib's per-row result objects.
- Rank numeric literals for `ORDER BY` by their value, keeping rdflib's
  comparison for the rest.
- Let `LIMIT` and `ASK` stop a probe join once it has enough rows: they stop a
  streamed join, and a chunked `Store.triples`, at the piece or chunk they have
  reached, but not a probe join.
- Re-tune `_PROBE_FANOUT` for batched probes, per kind of store.
- Keep a variable's candidate codes native end to end (sorted and unique
  without a Python set), given a sort kernel on `U64Column`.
- Read a block that is one pattern in chunks, as `Store.triples` does, rather
  than as one match.
- Guard the joins above a BGP (group joins, `OPTIONAL`, `MINUS`) with the pair
  budget: they hash-join their sides in Python and build the result whole.
- Keep a top-k over the pieces of a streamed join for `ORDER BY ... LIMIT`,
  rather than draining them into rows before sorting.
- Give `OPTIONAL` a left-join keep-scan, so that a right side whose probes
  would scan is matched once, narrowed to the codes of the left rows, instead
  of being probed once per left row.

In vortex-rdf:

- Apply keeps and counts on in-memory stores column-wise instead of row by
  row.
- Make small batches cheaper, for example by running them on the calling
  thread.
- Accept codes instead of spellings in patterns, so that probes skip a decode
  and re-encode.
- Allow keeps made of several ranges or of a complement, for
  `isIRI(?v) || isBlank(?v)`, `!isLiteral(?v)`, `?v != 5` and
  `lang(?v) != "en"`.
- Return only the columns a pattern needs from `match_codes`.
- Add `distinct` and `value_counts` over several columns, and a semi-join
  kernel, for multi-variable `DISTINCT` and `GROUP BY`, `EXISTS` and `MINUS`.
- Give each typed literal a value key, so that `ORDER BY` ranking and literal
  aliasing work without decoding, and so that `STR()` of a typed literal can be
  decided by rdflib's normalized lexical form.
- Report from the file whether any typed literal can alias another (computed
  at build time), so that `DISTINCT` and `COUNT` can skip the alias pass.
- Evaluate string predicates directly on FSST-compressed bytes.
- Bound a `regex` call's matching work and leave the remaining candidates
  undecided, so a pattern whose automaton explodes (`^(?:a|b)*a(?:a|b){60}$`)
  falls back to Python's `re` instead of costing more natively.
- Serialize code chunks natively: SPARQL results written straight from the
  codes and the dictionary, so that a large answer needs no rdflib solution
  per row.
- Make a call that carries a `cancel=` token cheaper when it finishes without
  waiting (a point lookup, any call on an in-memory store), so that the Python
  side need not check the token itself instead.
