# SPARQL pushdown

`vortex-rdflib` runs SPARQL queries with rdflib's engine. In addition, a
[`VortexRdflibStore`](../src/vortex_rdflib/store.py#L61) installs a hook that
answers the parts of a query it supports itself. The hook works on the store's
integer term codes instead of rdflib terms. Everything it does not support is
left to rdflib.

This document explains how the hook works, what it does for each SPARQL
feature, and where in the code each step happens.

## Source code

| File | Role |
| --- | --- |
| [`pushdown.py`](../src/vortex_rdflib/pushdown.py) | The hook and the planner: patterns, joins, `OPTIONAL`, `MINUS`, `EXISTS`, `DISTINCT`, `ORDER BY`, `COUNT`, `VALUES`, named graphs, and turning results into rdflib solutions. |
| [`filters.py`](../src/vortex_rdflib/filters.py) | `FILTER` expressions: splitting them into conjuncts and evaluating them, in Python or inside vortex-rdf. |
| [`terms.py`](../src/vortex_rdflib/terms.py) | Term spellings: parsing the dictionary's N-Triples strings, and spelling rdflib terms the way the dictionary stores them. |
| [`store.py`](../src/vortex_rdflib/store.py) | The rdflib store: opening the file, decoding codes to terms, choosing which graph a query reads, and the caches the hook relies on. |

## How the hook works

rdflib turns a query into a tree of algebra nodes (`Project`, `Filter`, `BGP`,
`LeftJoin`, ...). Before it evaluates a node, it offers the node to every
function registered in `rdflib.plugins.sparql.CUSTOM_EVALS`.

1. Creating a `VortexRdflibStore` calls
   [`register_sparql_pushdown`](../src/vortex_rdflib/pushdown.py#L341), which
   registers [`_eval_part`](../src/vortex_rdflib/pushdown.py#L374) as such a
   function.
2. For each node, `_eval_part` checks that the node is one it handles
   ([`_HANDLER_NAMES`](../src/vortex_rdflib/pushdown.py#L124)) and has not
   been switched off (see [Switches](#switches)), and that the
   query's store is a `VortexRdflibStore` with a term dictionary (a
   Dictionary-layout).
3. If both hold, the matching handler answers the node and everything below it.
4. If not, the hook raises `NotImplementedError`. rdflib then evaluates the node
   itself, and offers the nodes below it to the hook again.

So rdflib runs the parts the hook does not support, and the hook still runs
every supported subtree below them.

| Algebra node | Handler |
| --- | --- |
| `BGP`, `Filter`, `Join`, `LeftJoin`, `Minus`, `ToMultiSet` (`VALUES`), `Graph` | [`_eval_block_node`](../src/vortex_rdflib/pushdown.py#L1174) |
| `Project`, `Slice`, `Distinct`, `OrderBy` | [`_eval_head`](../src/vortex_rdflib/pushdown.py#L670) |
| `AggregateJoin` | [`_eval_aggregate`](../src/vortex_rdflib/pushdown.py#L927) |
| `AskQuery` | [`_eval_ask`](../src/vortex_rdflib/pushdown.py#L1179) |

**Plan first, decode later.** rdflib only catches `NotImplementedError` while
the hook is being called. So every handler makes all its checks and all its
calls into vortex-rdf before it returns. What it returns is a generator that
only decodes results; it never hands work back to rdflib halfway through.

## Working on codes

In the Dictionary layout, each term is stored once in a sorted dictionary, and
the quads refer to terms by a `u32` **code**: the term's position in that
dictionary.

- Matching a pattern (`VortexRdfStore.match_codes`) returns four code columns
  (subject, predicate, object, graph) without copying them.
- Joins, filters, `DISTINCT`, grouping and sorting all work on these integers.
- A code becomes an rdflib term only when a solution is returned. Each code is
  decoded once and then cached while the store is open
  ([`_prime_decode_cache`](../src/vortex_rdflib/store.py#L464)).

Codes follow the byte order of the term spellings, so each kind of term fills
one contiguous range of codes: first the default graph's empty name, then
literals (`"…`), then IRIs (`<…>`), then blank nodes (`_:…`). Asking whether a
code is an IRI is a range check, with no decoding
([`kind_bounds`](../src/vortex_rdflib/terms.py#L146),
[`_kind_ranges`](../src/vortex_rdflib/store.py#L504)).

Much of the integer work runs inside vortex-rdf ("natively") instead of in
Python:

- A **keep** is a constraint passed along with a match or a count
  (`match_codes(..., keep=...)`): a code range or a code set per position. Rows
  outside it are dropped inside vortex-rdf and never reach Python.
- Joins use `U32Column.join_indices` and `take`.
- `DISTINCT` uses `U32Column.distinct`, and group counts use
  `U32Column.value_counts`.

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

[`_check_block`](../src/vortex_rdflib/pushdown.py#L575) checks a subtree
against this grammar before any work starts.
[`_solve_block`](../src/vortex_rdflib/pushdown.py#L1250) then solves it, with
one solver per node type. The result is a
[`Relation`](../src/vortex_rdflib/pushdown.py#L502): the variables it binds
(its schema) and a body of codes, which is either

- **columns**: zero-copy views of a native match or a native join, or
- **rows**: Python tuples of codes, where `None` means the variable is unbound.

The head is what rdflib puts above a block:

- `Slice? → Distinct? → Project → OrderBy? → block`, read by
  [`_plan_head`](../src/vortex_rdflib/pushdown.py#L539) and run by `_eval_head`;
- `AggregateJoin(Group(block))`, run by `_eval_aggregate`;
- `AskQuery(Project(block))`, run by `_eval_ask`.

## Returning solutions

[`_yield_rows`](../src/vortex_rdflib/pushdown.py#L3122) turns code rows into
rdflib solutions, lazily:

- It reads the rows in chunks that grow from 64 to 4096 rows
  ([`_CHUNK_START`](../src/vortex_rdflib/pushdown.py#L145)).
- For each chunk, it decodes the codes the cache does not hold yet in one
  `TermDict.decode_many` call.
- It builds each solution as a
  [`_Solution`](../src/vortex_rdflib/pushdown.py#L306), an rdflib
  `FrozenBindings` that takes its bindings without copying them.

A consumer that stops early, such as a `LIMIT` or an `EXISTS` that found a
match, never decodes the rows it did not read. Columns are turned into Python
tuples one slice at a time, for the same reason
([`_column_rows`](../src/vortex_rdflib/pushdown.py#L3086)).

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
SelectQuery                    ← offered to the hook, declined
└─ Slice(0, 5)                 ┐
   └─ Distinct                 │  head   (_plan_head)
      └─ Project(?o)           ┘
         └─ Filter(isIRI(?o))  ┐  block  (_solve_block → Relation)
            └─ BGP(?m <p> ?o,  │
                   ?s <p> ?m)  ┘
```

The hook does not handle `SelectQuery`, so it declines it. rdflib evaluates
that node and offers `Slice` next. The hook takes `Slice` and everything below
it, in one call:

1. `_plan_head` splits off `Slice → Distinct → Project` and passes the
   `Filter` to `_solve_block`.
2. `isIRI(?o)` uses a single variable, and vortex-rdf can decide it: all IRIs
   are one code range. It becomes a keep on the pattern that binds `?o`
   ([`_push_native_restrictions`](../src/vortex_rdflib/pushdown.py#L2651)).
3. Both patterns are counted. They differ only in their variable names, so
   they are the same quad pattern and one count serves both.
4. `?m <p> ?o` is matched with its keep: only rows whose object is an IRI come
   back, and no term is decoded to find them. `?s <p> ?m` has no keep, so it is
   matched separately.
5. The two sides are about the same size, so they are joined in one go rather
   than row by row: a native join on `?m`
   ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L2829)). The block's
   relation has the schema `(?m, ?o, ?s)`.
6. The head works on codes: `Project` keeps the `?o` column, `Distinct` drops
   duplicate codes natively, and `Slice` takes the first five rows.
7. Only now is anything decoded: one `decode_many` call for five codes, then
   five solutions.

## Basic graph patterns

**Shape:** `BGP(triples)`. Terms can be variables, blank nodes (which act as
variables), IRIs and literals.

```sparql
SELECT ?s ?o WHERE { 
  ?s <p> ?m . 
  ?m <p> ?o . 
}
```

[`_solve_bgp`](../src/vortex_rdflib/pushdown.py#L1821) matches a single
pattern directly. Its columns become the relation without being copied into
Python ([`_rel_from_pattern`](../src/vortex_rdflib/pushdown.py#L2748)).

Several patterns go to
[`_join_incremental_bgp`](../src/vortex_rdflib/pushdown.py#L2234), which plans
the join from the pattern sizes:

1. **Resolve.** [`_pattern_terms`](../src/vortex_rdflib/pushdown.py#L2580)
   turns each triple pattern into a quad pattern: the bound terms as N-Triples
   spellings, `None` for variables, and the graph of the current scope (see
   [Named graphs](#named-graphs)).
2. **Count.** [`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2014) asks
   vortex-rdf how many quads each pattern selects (`count_quads`). A count is
   much cheaper than a match, since no columns come back. Patterns with more
   bound terms are counted first, and a count of zero ends the block before
   anything is matched. When enough patterns need a scan, they are counted
   together in one `count_quads_many` call, which runs them concurrently.
3. **Share.** Patterns that resolve to the same quad pattern share one count,
   and one match too unless filters narrow them differently
   ([`_pattern_key`](../src/vortex_rdflib/pushdown.py#L1971)).
4. **Seed.** The smallest pattern is matched first and becomes the running
   relation.
5. **Extend.** Each next step takes the smallest remaining pattern that shares
   a variable with the relation (or the smallest one overall, for a cross
   product) and joins it in one of two ways:
   - **Probe join** ([`_probe_join`](../src/vortex_rdflib/pushdown.py#L2891)),
     when the relation is at least
     [`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L1804) (100) times
     smaller than the pattern's count. The pattern is never matched as a
     whole. Instead, each row's codes are filled into the pattern, and that
     narrower pattern (a **probe**) is matched. Probes that need a scan are
     sent in batches through `match_codes_many`; cheap ones, such as probes
     with a bound subject, are sent one by one
     ([`_run_probes`](../src/vortex_rdflib/pushdown.py#L2132)).
   - **Hash join** otherwise: the pattern is matched once and joined. When
     both sides are columns and share exactly one variable,
     [`_join_columns`](../src/vortex_rdflib/pushdown.py#L2829) pairs the rows
     with the native `join_indices` and gathers the columns with `take`, so
     the result stays columnar. Other cases (several shared variables, a
     repeated variable as in `?x <p> ?x`, a filter still to apply in Python, a
     cross product) use the Python hash join
     [`_join`](../src/vortex_rdflib/pushdown.py#L3012).

A literal that a join puts in subject or predicate position is not an error:
the pattern simply matches nothing, as with `Store.triples()`.

**Left to rdflib:** property paths and RDF-star quoted triples, which
`_pattern_terms` declines.

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

[`_solve_filter`](../src/vortex_rdflib/pushdown.py#L1364) calls
[`analyze_filter`](../src/vortex_rdflib/filters.py#L1137), which splits the
expression at its top-level `&&`. A row passes only when every part (a
**conjunct**) is true, so each conjunct can be handled on its own. Conjuncts
are grouped by how many of the block's variables they use
([`FilterPlan`](../src/vortex_rdflib/filters.py#L1113)):

- **No variable:** evaluated once
  ([`evaluate_constant`](../src/vortex_rdflib/filters.py#L1198)). If it is
  false, the block is empty and nothing is matched.
- **One variable:** pushed down to the pattern that binds the variable, so rows
  are dropped before any join.
  - If vortex-rdf can decide it, it becomes a keep on the match (see
    [Native predicates](#native-predicates)).
  - Otherwise it is evaluated once per distinct code of the variable: over the
    distinct codes of the matched column
    ([`_restrict_pattern`](../src/vortex_rdflib/pushdown.py#L2700)), or, for a
    probed pattern, over the codes the probes return (in `_probe_join`).
  - A variable that an `OPTIONAL` may leave unbound is not pushed down; its
    conjunct is applied after the join.
- **Several variables:** applied to the joined rows, with each answer
  remembered per distinct tuple of codes
  ([`tuple_predicate`](../src/vortex_rdflib/filters.py#L1280)). Over columns,
  the test runs while the rows stream out
  ([`_filter_relation`](../src/vortex_rdflib/pushdown.py#L1782)), so a `LIMIT`
  above stops it early.
- **`EXISTS` / `NOT EXISTS`:** a semi-join or anti-join, see
  [FILTER (NOT) EXISTS](#filter-not-exists).

Pushed-down conjuncts travel in a
[`_Pushed`](../src/vortex_rdflib/pushdown.py#L1226). They reach both sides of
a group join and the left side of an `OPTIONAL` or `MINUS`, but never the right
side of an `OPTIONAL` or `MINUS`, where dropping a row would change the answer.

During a BGP join, a conjunct over several variables (one the fast route below
can compile) gets two more chances to act early:

- **Early pruning**
  ([`_prune_early`](../src/vortex_rdflib/pushdown.py#L2489)): once the relation
  binds all of the conjunct's variables, the conjunct may be applied mid-join.
  This only happens when a smaller relation would let a later pattern be probed
  instead of matched whole, and it stops as soon as too many rows pass.
- **Specialization**
  ([`_specialize_tuple_conjuncts`](../src/vortex_rdflib/pushdown.py#L2422)):
  when the relation holds a single value for every variable of the conjunct
  but one, the conjunct becomes a test of that one variable, and possibly a
  keep. For example, once `?ref` is bound to a single value, `?v < ?ref + 10`
  becomes a numeric bound on the match that binds `?v`.

### Evaluating in Python

Python evaluates a conjunct once per distinct value (or tuple of values), never
once per row. It has two routes
([`evaluate_column`](../src/vortex_rdflib/filters.py#L1207)):

- **Fast route.** [`compile_fast`](../src/vortex_rdflib/filters.py#L715)
  compiles a known set of expression shapes into a Python function that reads
  the term's spelling ([`parse_spelling`](../src/vortex_rdflib/terms.py#L74))
  instead of building an rdflib term. It covers comparisons, `IN`/`NOT IN`,
  `sameTerm`, `datatype`, `lang`, `langMatches`, `isIRI`/`isBlank`/`isLiteral`
  (code range checks), `isNumeric`, `bound`, `str`, `regex`, `strstarts`,
  `strends`, `contains`, `+` and `-` on integers, and `&&`, `||`, `!`. Each
  piece copies rdflib's behaviour and answers true, false, error (treated as
  false) or *unknown*: a value whose rdflib result it cannot reproduce exactly,
  such as an ill-typed number.
- **Generic route.** rdflib's own evaluator (`_ebv`) on the decoded term
  ([`Conjunct.generic`](../src/vortex_rdflib/filters.py#L1097)). It handles the
  *unknown* values and every expression the fast route does not cover, and
  gives rdflib's answer by construction.

Because the generic route accepts any expression, the hook takes almost every
`Filter` above a block (see the exceptions below).

The fast route only computes `+` and `-` when every operand is a well-formed
integer literal: that is the one case where it can spell the result exactly as
rdflib does. Any other operand makes the value *unknown*, so that value goes to
the generic route.

### Native predicates

Some single-variable conjuncts are decided by vortex-rdf, over the whole
dictionary at once ([`native_shape`](../src/vortex_rdflib/filters.py#L804)):

- `isIRI(?v)`, `isBlank(?v)`, `isLiteral(?v)`
- `datatype(?v) = <iri>` and `lang(?v) = "tag"`
- `langMatches(lang(?v), "range")`
- `strstarts(str(?v), "prefix")`
- `?v` compared with a numeric constant (`<`, `<=`, `>`, `>=`, `=`, `!=`)

`TermDict.filter_codes(kind, arg)` scans the dictionary and returns two code
sets: the codes for which the test is surely true, and the codes it cannot
decide the way rdflib would
([`native_verdicts`](../src/vortex_rdflib/filters.py#L922)). The store
remembers recent answers
([`_native_verdicts`](../src/vortex_rdflib/store.py#L518)). The kind tests need
no scan at all: they are the kind's code range.

[`native_restriction`](../src/vortex_rdflib/filters.py#L968) turns these
answers into a keep: the code range for a kind test, the set of true codes
otherwise. Several keeps on the same position are intersected
([`intersect_keeps`](../src/vortex_rdflib/filters.py#L1003)). A keep is only
used when no code that can appear at the pattern's position is undecided
(subjects and graph names are IRIs or blank nodes, predicates are IRIs). Then:

- rejected rows never leave vortex-rdf;
- an `ASK` or `COUNT` over the pattern counts under the keep;
- a probe carries a range keep into vortex-rdf, and tests a code-set keep on
  the rows it gets back.

When undecided codes can appear, the conjunct stays on the Python routes, which
then only evaluate the undecided codes.

On a file-backed store, vortex-rdf applies a mid-sized code set slowly inside a
file scan. Such a set is sent as its bounding code range instead, and the exact
set is tested in Python
([`_split_keeps`](../src/vortex_rdflib/pushdown.py#L2060)).

Where vortex-rdf and rdflib could disagree, rdflib's answer wins:

- `!=` never becomes a keep: rdflib lets every IRI and blank node pass it,
  while the native answer only covers literals.
- `langMatches` is only native for `*` and for ranges made of ASCII letters and
  digits.
- `xsd:long` and `xsd:unsignedLong` values beyond 64 bits are marked undecided
  ([`wide_integer_codes`](../src/vortex_rdflib/filters.py#L945)), because
  rdflib does not bound them.
- A typed constant outside its datatype's range, such as `"300"^^xsd:byte`, is
  never decided natively.

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
  it ([`_reject_env_references`](../src/vortex_rdflib/pushdown.py#L1545)).

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
  ([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2216)): nothing
  is left for Python to filter out, and the store has no secondary indexes, so
  that the limited match returns the same first rows as a full match.
- **`ASK`** ([`_block_nonempty`](../src/vortex_rdflib/pushdown.py#L1187)) over
  a single pattern, whose `FILTER` (if any) is fully native, is
  `count_quads(..., limit=1)`: it stops at the first match, without matching
  rows or decoding terms. Any other `ASK` solves its block with a one-row limit
  and checks for a first row.

## DISTINCT

**Shape:** `Distinct(Project(block))`, possibly under `Slice` and above
`OrderBy`.

```sparql
SELECT DISTINCT ?p WHERE { ?s ?p ?o }
```

[`_eval_distinct`](../src/vortex_rdflib/pushdown.py#L821) removes duplicates in
two passes, and decodes only what survives:

1. **By code** ([`_code_distinct`](../src/vortex_rdflib/pushdown.py#L833)):
   duplicate code tuples are dropped. For a single column this is the native
   `U32Column.distinct`, which keeps the first-seen order.
2. **By term** ([`_term_distinct`](../src/vortex_rdflib/pushdown.py#L854)):
   rdflib compares terms, and two spellings can be the same typed literal
   (`"042"^^xsd:integer` and `"42"^^xsd:integer` are both 42).
   [`_alias_codes`](../src/vortex_rdflib/pushdown.py#L872) maps each such code
   to the first code of the same term. Only typed literals can alias, so only
   they are decoded. Each code is checked once per store and the result is
   kept; in most stores no code aliases another.

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

[`_eval_aggregate`](../src/vortex_rdflib/pushdown.py#L927) has two paths:

- **One pattern, no `GROUP BY`:** a `COUNT(*)` or `COUNT(?v)` is a single
  `count_quads` call ([`_count_pattern`](../src/vortex_rdflib/pushdown.py#L1987)),
  and no row is matched. This also works under a `FILTER` that vortex-rdf
  decides fully: the count uses its keep.
- **Otherwise** the block is solved and counted on codes
  ([`_aggregate_rows`](../src/vortex_rdflib/pushdown.py#L996)). A columnar body
  grouped by at most one variable is counted one column at a time
  ([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1117)): the native
  `value_counts` of the key column gives every group and its size. For
  `COUNT(DISTINCT ?v)`, each group's rows are gathered natively
  (`join_indices`, `take`, `distinct`) when there are few groups
  ([`_NATIVE_GROUPS_MAX`](../src/vortex_rdflib/pushdown.py#L1114)), and read
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
  ([`_solve_left_join`](../src/vortex_rdflib/pushdown.py#L1622)): a hash left
  join ([`_left_join_rows`](../src/vortex_rdflib/pushdown.py#L1735)). The
  optional group's `FILTER` is checked on each candidate pair. A left row with
  no passing match is kept, with the right side's variables unbound.
- **`MINUS`** ([`_solve_minus`](../src/vortex_rdflib/pushdown.py#L1759)): an
  anti-join. When the sides share no variable, it follows rdflib's rule: with
  no outer bindings (the usual top-level case) nothing is removed; with outer
  bindings, everything is removed if the right side has any row.
- **Group join** ([`_solve_join`](../src/vortex_rdflib/pushdown.py#L1573)): a
  hash join (`_join`). When rdflib would deduplicate the right side (a join it
  does not evaluate lazily), so does the hook.

When the right side of an `OPTIONAL` or of a lazy group join is a single
pattern, [`_plan_pattern_side`](../src/vortex_rdflib/pushdown.py#L2547) counts
it first. If the left relation is at least `_PROBE_FANOUT` times smaller than
that count, the pattern is probed once per left row (with `_probe_join`, which
also pads unmatched `OPTIONAL` rows) and never matched whole. Otherwise it is
matched once and hash-joined.

**Left to rdflib.** These shapes fall back so that the answers stay identical:

- a join key that an `OPTIONAL` may leave unbound: for rdflib an unbound value
  matches anything, which a hash join cannot express
  ([`_shared_key_ok`](../src/vortex_rdflib/pushdown.py#L1566));
- an `OPTIONAL` whose right side uses a variable that the left side's `_vars`
  leave out (one bound by the outer context, an enclosing join or a `VALUES`
  table): rdflib checks unmatched rows a second time with only those `_vars`
  bound, which can change the answer (a similar check covers the `OPTIONAL`'s
  filter);
- a `FILTER` inside a side that, in rdflib, would see a binding from an
  enclosing join;
- an `OPTIONAL` whose filter contains `EXISTS`.

## FILTER (NOT) EXISTS

**Shape:** a `FILTER` conjunct that is `EXISTS { body }` or
`NOT EXISTS { body }`, possibly under one `!`, whose body is a block
([`exists_shape`](../src/vortex_rdflib/filters.py#L1120)).

```sparql
SELECT ?s WHERE {
  ?s <p> ?o .
  FILTER NOT EXISTS { ?s <q> ?x }
}
```

rdflib evaluates the body once per row. Instead,
[`_apply_exists`](../src/vortex_rdflib/pushdown.py#L1422) runs a semi-join
(`EXISTS`) or an anti-join (`NOT EXISTS`) on the variables the body shares with
the block:

- Usually the body is solved once, and each row is kept or dropped by looking
  up its key among the body's keys.
- When the body is a single pattern and the block is much smaller than the
  pattern's count, each row is probed instead, with `count_quads(..., limit=1)`
  sent in batches ([`_probe_exists`](../src/vortex_rdflib/pushdown.py#L1500)).
- When the body shares no variable with the block, it is a single existence
  check, solved with a one-row limit.

The body is read from rdflib's translated algebra, because rdflib removes the
body's own `FILTER`s from the parse tree
([`expr_vars`](../src/vortex_rdflib/filters.py#L1041)). rdflib also wraps the
body in a `Join` with an empty `BGP`, which the hook skips
([`_unwrap_empty_joins`](../src/vortex_rdflib/pushdown.py#L1486)).

**Left to rdflib.** rdflib's evaluator decides the conjunct, once per distinct
tuple of values, when a shared variable may be unbound, when the body is not a
block, or when a `FILTER` in the body sees the block's bindings. The same goes
for an `EXISTS` inside `||` or another operator. Under `GRAPH ?g`, the whole
block goes to rdflib (see [Named graphs](#named-graphs)).

## ORDER BY

**Shape:** `Project(OrderBy(block))`, possibly under `Slice` and `Distinct`,
when every sort key is a plain variable.

```sparql
SELECT ?s ?v WHERE { ?s <p> ?v } ORDER BY DESC(?v) LIMIT 10
```

[`_order_rows`](../src/vortex_rdflib/pushdown.py#L705) sorts the code rows:

1. Each sort variable's distinct codes are ranked once
   ([`_rank_codes`](../src/vortex_rdflib/pushdown.py#L764)), in rdflib's order:
   unbound first, then blank nodes, then IRIs, then literals. Blank nodes and
   IRIs sort like their spellings, so their codes are already in order.
   Literals are decoded and sorted with rdflib's own comparison; literals that
   compare equal share a rank.
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

[`_solve_values`](../src/vortex_rdflib/pushdown.py#L1307) turns the table into
a relation, which then joins like any other:

- All constants are looked up in one `TermDict.encode_many` call
  ([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1333)), spelled the
  way the dictionary stores terms
  ([`canonical_spelling`](../src/vortex_rdflib/terms.py#L127)).
- `UNDEF` becomes an unbound value.
- A constant the dictionary does not hold gets a private negative code
  ([`_foreign_code`](../src/vortex_rdflib/store.py#L540)). It joins nothing,
  but is still returned as written.
- Rows that contradict an outer binding are dropped, as in rdflib.

**Left to rdflib:** a constant whose rdflib term differs from the dictionary's
term, such as `"042"^^xsd:integer` (rdflib would return the query's own
spelling); a constant vortex-rdf cannot parse; and a `VALUES` variable that an
`OPTIONAL` re-checks (see above).

## Named graphs

Every pattern is matched as a quad pattern. Its graph position comes from the
current [`_GraphScope`](../src/vortex_rdflib/pushdown.py#L454):

- Outside any `GRAPH`, it is the graph the query runs on
  ([`_active_scope`](../src/vortex_rdflib/pushdown.py#L468), which asks
  [`_graph_n3`](../src/vortex_rdflib/store.py#L224)): `None` (all graphs) for
  a union default graph, `""` for the default graph of a `Dataset` without
  union, or a graph's name.
- Under `GRAPH <iri>`, it is that graph's name.
- Under `GRAPH ?g`, it is a wildcard, and `?g` is bound from the graph column
  of the match.

```sparql
SELECT ?g ?s WHERE { GRAPH ?g { ?s <p> ?o } }
```

A `Graph` node matches nothing itself. `_solve_block` solves the block below it
with a new scope ([`_graph_scope`](../src/vortex_rdflib/pushdown.py#L476)), so
a nested `GRAPH` simply replaces the outer one. Under `GRAPH ?g`,
`_pattern_terms` maps `?g` to the fourth column, which makes it an ordinary
variable: it is joined, filtered, grouped, sorted and decoded like any other.
Two patterns under the same `GRAPH ?g` join on `?g`, which is exactly the
requirement that they come from the same graph.

`GRAPH ?g` only covers named graphs, so default-graph rows have to be dropped.
The default graph's name is the empty string, which sorts before every term:
its code is 0, and every named graph has a higher code.
[`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L2623) adds a keep
with that range on the graph position, so default-graph rows never leave
vortex-rdf.

For `ASK` and `COUNT`, [`_peel_graph`](../src/vortex_rdflib/pushdown.py#L490)
looks through `GRAPH <iri>`, so their single-pattern shortcuts also work inside
a named graph.

**Left to rdflib:**

- `FILTER (NOT) EXISTS` under `GRAPH ?g`: rdflib evaluates the body in the
  active graph, and under a graph variable there is no single active graph;
- a `GRAPH` variable bound to something that cannot name a graph, such as a
  literal.

When rdflib evaluates a `Graph` node itself (with
`VORTEX_RDF_PUSHDOWN_OPS=bgp`, for example), it changes `solution.ctx.graph`
while it yields rows. The hook then gives each solution its own context, so
that change cannot leak into later rows
([`_pushed_graph`](../src/vortex_rdflib/pushdown.py#L3108)).

## vortex-rdf primitives

| Primitive | Where it is used |
| --- | --- |
| `match_codes`, `count_quads` | Every pattern match and count. |
| `count_quads_many`, `match_codes_many` | Counting several scanning patterns at once ([`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2014)); batched probes for joins, `OPTIONAL` and `EXISTS` ([`_run_probes`](../src/vortex_rdflib/pushdown.py#L2132)). |
| `keep=` on a match or count | Native FILTER conjuncts ([`native_restriction`](../src/vortex_rdflib/filters.py#L968)), specialized conjuncts ([`_specialize_tuple_conjuncts`](../src/vortex_rdflib/pushdown.py#L2422)), dropping the default graph under `GRAPH ?g` ([`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L2623)), exact `ASK` and `COUNT` answers. |
| `limit=` on a match or count | `LIMIT` over a single pattern ([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2216)), `ASK`, `EXISTS` probes. `offset=` is not used: an `OFFSET` is applied to the code rows. |
| `TermDict.filter_codes` | Native FILTER predicates ([`native_verdicts`](../src/vortex_rdflib/filters.py#L922)). |
| `TermDict.encode_many` | `VALUES` constants ([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1333)). |
| `TermDict.decode_many` | Decoding results ([`_prime_decode_cache`](../src/vortex_rdflib/store.py#L464)) and the spellings that probes fill in ([`_probe_spellings`](../src/vortex_rdflib/pushdown.py#L2998)). |
| `TermDict.prefix_range`, `lower_bound` | The code range of each term kind ([`kind_bounds`](../src/vortex_rdflib/terms.py#L146), [`_kind_ranges`](../src/vortex_rdflib/store.py#L504)). |
| `U32Column.distinct`, `value_counts` | `DISTINCT` over one variable ([`_distinct_codes`](../src/vortex_rdflib/pushdown.py#L2695)), `GROUP BY` counts ([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1117)). |
| `U32Column.join_indices`, `take` | The columnar join ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L2829)), a group's rows for `COUNT(DISTINCT)`, intersecting keeps ([`intersect_keeps`](../src/vortex_rdflib/filters.py#L1003)). |

## Switches

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Do not register the hook: rdflib evaluates every node. |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only take the listed algebra nodes (`BGP,Filter,Project,...`); `bgp` means basic graph patterns only. |
| `VORTEX_RDF_FILTER_FAST=0` | Send every FILTER value through rdflib's evaluator (still once per distinct value). |
| `VORTEX_RDF_NATIVE_FILTERS=0` | Decide no FILTER conjunct in vortex-rdf: no keeps from filters, every value goes through the Python routes. |
| `VORTEX_RDF_TRACE_QUERY=1` | Print the plan of every query the hook takes to stderr, one JSON line per step, prefixed `VORTEX_RDF_QUERY_TRACE`: patterns counted, matched or probed, join strategies, filter keeps and prunes, every native call, rows decoded. `VORTEX_RDF_TRACE_QUERY_ID` labels the lines of one query. |

`register_sparql_pushdown` reads the first four when a store is created. The
trace switch is read for each query
([`_QueryTrace`](../src/vortex_rdflib/pushdown.py#L155)). From Python,
`register_sparql_pushdown()` and
[`unregister_sparql_pushdown()`](../src/vortex_rdflib/pushdown.py#L356) turn
the hook on and off.

## Tests

The tests check that the hook never changes an answer:

- [`tests/test_pushdown.py`](../tests/test_pushdown.py) runs every query shape
  twice, with the hook and with rdflib's default evaluator, and compares the
  results. It does so on file-backed and in-memory stores, on stores with
  secondary indexes or with the dictionary left in the file, and with each
  code path forced: probe joins only, batched probes only, basic graph patterns
  only, the generic FILTER route only, and no native filters.
- [`tests/test_filters.py`](../tests/test_filters.py) checks each piece of the
  fast FILTER route against rdflib's evaluator, over many literal spellings
  (ill-typed numbers, NaN, out-of-range values, language tags, custom
  datatypes, unbound values).
- [`tests/test_native_filters.py`](../tests/test_native_filters.py) does the
  same for each native predicate.

## What stays with rdflib

- `UNION`, `BIND`, sub-queries and `SERVICE`. rdflib evaluates these nodes and
  offers the nodes below them to the hook, so each `UNION` branch is still
  pushed down.
- Property paths and RDF-star patterns.
- `REDUCED`, aggregates other than `COUNT`, and `GROUP BY` or `ORDER BY` on an
  expression.
- Parsing. Every query goes through rdflib's `parseQuery` and `translateQuery`;
  use `prepareQuery` and reuse its result when the same query runs many times.

## Possible improvements

In vortex-rdflib:

- Find out once per store whether any typed literal can alias another, and skip
  the alias pass of `DISTINCT` and `COUNT` when none can.
- Offer an API that returns decoded tuples or code columns, for callers that do
  not need rdflib's per-row result objects.
- Rank numeric literals for `ORDER BY` by their value, keeping rdflib's
  comparison for the rest.
- Turn more FILTER shapes into keeps: `?v IN (...)`, `sameTerm(?v, <iri>)`,
  `?v = <iri>`, a `regex` that is a plain anchored prefix, and `strstarts` over
  IRIs (a `prefix_range`, no scan).
- Let `LIMIT` and `ASK` stop a probe join once it has enough rows.
- Re-tune `_PROBE_FANOUT` for batched probes, per kind of store.
- Decode in larger chunks when the dictionary is read from the file.

In vortex-rdf:

- Apply mid-sized code-set keeps quickly in file scans, so that `_split_keeps`
  can go.
- Apply keeps and counts on in-memory stores column-wise instead of row by row.
- Make small batches cheaper, for example by running them on the calling
  thread.
- Accept codes instead of spellings in patterns, so that probes skip a decode
  and re-encode.
- Report `xsd:long` and `xsd:unsignedLong` values beyond 64 bits as undecided,
  so that `wide_integer_codes` can go.
- Allow keeps made of several ranges or of a complement, for
  `isIRI(?v) || isBlank(?v)`, `!isLiteral(?v)`, `?v != 5` and
  `lang(?v) != "en"`.
- Return only the columns a pattern needs from `match_codes`.
- Add `distinct` and `value_counts` over several columns, and a semi-join
  kernel, for multi-variable `DISTINCT` and `GROUP BY`, `EXISTS` and `MINUS`.
- Give each typed literal a value key, so that `ORDER BY` ranking and literal
  aliasing work without decoding.
