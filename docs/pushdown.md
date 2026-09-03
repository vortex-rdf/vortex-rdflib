# SPARQL pushdown

`vortex-rdflib` answers SPARQL with rdflib's engine, but a
`VortexRdflibStore` can do more than serve quad patterns: it hooks into rdflib's algebra evaluation
and answers the operators it understands in **code space**, i.e., over the `u32`
term codes of a Dictionary-layout store, handing everything else back to
rdflib node by node. This document explains, for each pushdown, the algebra
shape it intercepts, what runs in code space, what stays in rdflib and how
it makes it faster.

**Measurement numbers.** Unless marked *end-to-end*, every timing below is
the best of 5–7 runs of a *prepared* query (`prepareQuery`; rdflib's parse
and algebra translation would otherwise dominate the cheap queries),
measured on 2026-08-30 on an Intel Core Ultra 7 155H with Python 3.13.7,
vortex-rdf 0.10.0 and rdflib 7.6.0, over a single-graph 50,000-statement
dataset from the benchmark generator ([`bench/dataset.py`](../bench/dataset.py): 5,000 subjects, 33
predicates, 27,034 distinct terms) in a Dictionary-layout store loaded in memory
(`VortexRdflibStore(path, in_memory=True)`). "rdflib" is the same store under
rdflib's default evaluator (`VORTEX_RDF_DISABLE_PUSHDOWN=1`), which is what
the dashboard's *pushdown off* row runs; the [benchmark
dashboard](https://vortex-rdf.github.io/vortex-rdflib/) is the live
reference, at 250,000 quads over 8 graphs and across the other stores.

## How the hook works

rdflib evaluates a query as a tree of algebra operators (`Project`, `Filter`,
`LeftJoin`, `BGP`, ...) and, for **every** node, offers it to the functions
registered in `rdflib.plugins.sparql.CUSTOM_EVALS` before running its own
evaluator. Constructing a `VortexRdflibStore` registers one such hook
([`pushdown.register_sparql_pushdown`](../src/vortex_rdflib/pushdown.py#L125)).
The hook looks at the node's name and for:

- a node it handles, over a graph whose store is a `VortexRdflibStore` with the
  code path available (Dictionary layout, resident term dictionary), is
  answered in code space;
- anything else raises `NotImplementedError`, which rdflib takes as
  "fallback to default behavior": it evaluates that node itself and,
  for the nodes below it, offers them to the hook again.

rdflib only catches that `NotImplementedError` while the hook is being
*called*, so the hook does all its planning and all its native calls before
handing a generator back; the generators only decode. This is the
workflow every handler follows: eager plan and execute, then lazy decode.

**Code space.** In the Dictionary layout every term has a `u32` code — its
position in the lexicographically sorted dictionary — and a matched pattern
comes back from the native layer as four zero-copy code columns
(`VortexRdfStore.match_codes`). Joins, filters, distinct, grouping and
sorting all work on those integers; a term is decoded (to an rdflib term,
once per distinct code, cached for the store's lifetime) only when a
solution is finally yielded. And because codes follow the byte order of the
spellings, a term's *kind* (IRI, literal, blank node) is a range test on its
code — no decode needed.

**Blocks and heads.** The hook solves a *block* — a subtree of the grammar
`BGP` | `Filter(block)` | `Join(block, block)` | `LeftJoin(block, block,
expr)` | `Minus(block, block)` | `ToMultiSet(values)` | `Graph(block)` —
into a [`Relation`](../src/vortex_rdflib/pushdown.py#L220): a schema of
variables and a body of `u32` columns (a matched pattern's zero-copy views,
or a join's gathered columns) or of materialized code-tuple rows (an
unbound variable is `None`). Above a block it recognizes the *heads* rdflib
puts there: `Slice? -> Distinct? -> Project -> OrderBy? -> block`,
`AggregateJoin(Group(block))` and `AskQuery(Project(block))`.

**Yielding.** Solutions are built directly as rdflib `FrozenBindings`, in
chunks that grow from 64 to 4096 rows, one batch `TermDict.decode_many` per
chunk for the codes the cache does not hold yet. A consumer that stops early
— `LIMIT`, `ASK`, the first match of an `EXISTS` — never decodes what it
does not consume.

### Example query

One query, end to end, through all of the above:

```sparql
SELECT DISTINCT ?o WHERE {
  ?s <p> ?m .
  ?m <p> ?o .
  FILTER(isIRI(?o))
}
LIMIT 5
```

rdflib translates that into an algebra tree, which the hook reads as a head
over a block:

```
SelectQuery                    ← offered to the hook, declined
└─ Slice(0, 5)                 ┐
   └─ Distinct                 │  head   (_plan_head)
      └─ Project(?o)           ┘
         └─ Filter(isIRI(?o))  ┐  block  (_solve_block → Relation)
            └─ BGP(?m <p> ?o,  │
                   ?s <p> ?m)  ┘
```

`SelectQuery` is offered to the hook first and raises `NotImplementedError`;
rdflib evaluates it itself and offers `Slice`, which the hook takes — and
with it everything below, which rdflib never sees again. In that one call:

1. [`_plan_head`](../src/vortex_rdflib/pushdown.py#L256) splits
   `Slice -> Distinct -> Project` and hands the `Filter` below it to
   [`_solve_block`](../src/vortex_rdflib/pushdown.py#L759).
2. `isIRI(?o)` references a single block variable, so it becomes a
   *per-variable predicate*
   [pushed into the scans](../src/vortex_rdflib/pushdown.py#L872-L879) rather
   than a test applied to returned solutions.
3. Both triple patterns resolve to the same quad pattern, scoped to the
   active graph — a variable is `None` in a resolved pattern, so only the
   names differ — and one native match serves both: 1,516 rows, four
   zero-copy `u32` columns.
   `isIRI` is then evaluated over the object column's 1,516 distinct codes —
   a code-range test, no term decoded — and the 909 codes that pass become
   that scan's allowed set.
4. The two sides of the join are the same size, so neither is worth probing
   per binding: a hash join on `?m`, dropping the rows outside the allowed
   set as it builds. The block's `Relation` is schema `(?m, ?o, ?s)` over
   166 rows (181 without the filter).
5. The head runs on those codes: `Project` keeps the `?o` column, `Distinct`
   deduplicates it in one pass (166 codes, already distinct here), `Slice`
   takes the first five rows.
6. Only then is anything decoded: one `decode_many` of five codes, five
   `FrozenBindings`. The other 161 rows never become terms.

Prepared and without the `LIMIT`, this is 2.9 ms against rdflib's 61 ms.
With it, rdflib's nested loop also stops after five solutions and wins,
1.9 ms to 2.3 ms: the up-front matching is what the pushdown pays to learn
the sizes, and what it earns back on every shape that cannot stop early.

## Basic graph patterns

**Shape.** `BGP(triples)`; the pattern's terms may be variables, blank nodes
(query blank nodes are variables), IRIs and literals. Property paths and
RDF-star quoted triples are rdflib's. Every pattern is matched as a quad,
scoped to the active graph (see *Named graphs* below).

```sparql
SELECT ?s ?o WHERE {
  ?s <p> ?m .
  ?m <p> ?o .
}
```

**In code space.** Every triple pattern is matched natively once, up front —
a match is near-constant cost (45–67 µs in memory, ≈1 ms file-backed,
regardless of how many rows it selects), so the actual row counts are known
before any join. Patterns that resolve to the same quad pattern share that one match:
a variable is `None` in a resolved pattern, so the hops of a self-join
(`?s <p> ?m . ?m <p> ?o`) differ only in names the match never sees, and each
keeps its own reading of the shared columns. Patterns join smallest first,
greedily preferring one that shares a variable with the relation so far. Each
join gathers the matching rows into fresh `u32` columns — an index-gather,
[`_join_columns`](../src/vortex_rdflib/pushdown.py#L1425) — so the running
relation stays columnar and row tuples exist only where a consumer
materializes them; a restricted or repeated-variable pattern, several
shared variables, or a cross product drop to the row hash join. Either way,
when the running relation is at least `_PROBE_FANOUT` (100) times smaller
than the next pattern's match, that pattern is re-matched natively per
binding of the relation
(`_probe_join`), so an anchored star — a subject fixed by one selective
pattern, joined to two unselective ones — never materializes the unselective
legs. A block that is a single pattern is not even turned into tuples: its
column views are the relation, and rows are zipped a chunk at a time as they
are consumed.

**Why it is faster.** rdflib's `evalBGP` is a nested loop: one
`Store.triples()` call per candidate binding, each paying the native match
floor and decoding its rows. A two-hop chain over a predicate
(`?s <p> ?m . ?m <p> ?o`, 1,516 matches per pattern, 181 solutions) is
55 ms under rdflib and 1.8 ms here. The anchored shapes are where rdflib's
nested loop is already near-optimal — it issues one selective match then
probes — and the pushdown pays two extra up-front matches to learn the
sizes: 0.36 ms against rdflib's 0.17 ms for a three-leg anchored star,
invisible behind the ≈1.2 ms parse. The columnar kernel is what the wide
shapes gain: an unanchored two-leg star (1,516 rows a side, 1,213 solutions)
9.2 ms -> 8.0 ms, and under a `LIMIT 10` the join's columns are gathered but
barely tupled or decoded, 1.1 ms end to end.

**Steps aside.** A literal propagated into subject or predicate position
makes the pattern unsatisfiable (empty, not an error), exactly as
`Store.triples()` treats it. A store whose code path declines
(`match_codes` returns `None`: another layout, a dictionary over the
residency budget) raises while rdflib is still listening, and the default
evaluator runs over the string path.

## FILTER

**Shape.** `Filter(expr, block)`. rdflib folds every `FILTER` of a group into
one node whose expression is a `ConditionalAndExpression`, and places it
above the whole group's pattern.

```sparql
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?s ?v WHERE {
  ?s <p> ?v .
  FILTER(datatype(?v) = xsd:integer && ?v < 100)
}
```

**In code space.** The expression is split into its top-level conjuncts (a
row passes iff every conjunct is true, so the split is exact) and each
conjunct is classified by the block variables it references:

- **none** — evaluated once; a false constant conjunct empties the block
  before anything is matched;
- **one** — a *per-variable predicate*, evaluated once per distinct code of
  the variable and applied to every pattern scan that binds it *before* the
  join, so filter selectivity drives the join order and the probe decision;
- **several** — applied to the joined rows, memoized per distinct code tuple.

Every conjunct is evaluated **per distinct value, never per row**, through
two routes:

- the **fast route** compiles a whitelist of expression shapes into a
  predicate over the term's parsed spelling (`terms.parse_spelling`), each
  leaf mirroring the exact rdflib code path: numeric comparisons (`< <= > >=
  = !=` with rdflib's numeric fast path and its datatype-IRI ordering
  otherwise), `IN`/`NOT IN` and `sameTerm` (term equality), `datatype`,
  `lang`, `langMatches` (rdflib's own `_lang_range_check`), `isIRI`,
  `isBlank`, `isLiteral` (pure code-range tests, no decode at all),
  `isNumeric`, `bound`, `str`, `regex` (Python `re`, as rdflib), `strstarts`,
  `strends`, `contains`, `+` and `-` over integer-derived literals, and
  `&&`, `||`, `!` with rdflib's three-valued short-circuit rules. A leaf
  answers true, false, *error* (rdflib would raise, which a filter turns
  into false) or **unknown** — the value is outside the domain the fast path
  reproduces exactly: an ill-typed number, a datatype rdflib orders by its
  own rules, a normalized lexical form under `str()`, a NaN against a
  decimal. Unknown values, alone, go to
- the **generic route**: rdflib's own evaluator (`_ebv`) on a
  `FrozenBindings` holding the decoded term. Semantics-exact by
  construction, at rdflib's cost per evaluation (≈24 µs), but paid once per
  distinct value instead of once per row. Expressions outside the whitelist
  take this route for every value, so *every* `Filter` over a block is
  intercepted and none is slower than rdflib's per-row evaluation.

Visibility follows rdflib's `evalFilter`: a filter sees the block's own
variables, the context's bindings that its node's `_vars` or the query's
`initBindings` keep (the rest was forgotten), and everything when it sits
directly inside an `EXISTS` body.

**Arithmetic.** `+` and `-` compile only when every operand is an
integer-derived literal rdflib itself considers well-formed. That is the
one case reproducible without building a term: rdflib's `type_promotion`
sends every integer-derived datatype to `xsd:integer`, and
`Literal(int, datatype=xsd:integer)` spells its value with `str`, so the sum
matches rdflib's own result in both value and lexical form. A decimal,
double, `dateTime` or ill-typed operand is unknown and defers — including
the ones rdflib does not merely order differently but *raises* on, such as
`"abc"^^xsd:integer`, which its `numeric()` computes with as a string. An
unreproducible operand therefore outranks an unbound one when a comparison
routes its answer: rdflib evaluates the operands in order and can raise on
the first, outside the `SPARQLError` catch that turns an unbound variable
into false.

**Why it is faster.** rdflib evaluates the expression tree per row through
its `CompValue`/`Literal` machinery, ≈33 µs per row; the fast predicate is
≈0.5 µs per distinct value, and single-variable conjuncts also shrink the
pattern before it is joined or decoded. `filter-range`
(`FILTER(datatype(?v) = xsd:integer && ?v < N)` over a 1,516-row predicate
scan, 38 rows kept): 48.7 ms → 3.8 ms. `isIRI(?o)` over the same scan:
26 ms → 6.6 ms; a `regex(str(?o), ...)`: 36 ms → 4.3 ms; `lang(?o) = "fr"`:
34 ms → 4.7 ms.

A conjunct over *several* variables is memoized per distinct code tuple
instead, which is what makes arithmetic worth compiling: BSBM Explore Q5
brackets two numeric properties against a reference product's
(`?v < ?ref + N && ?v > ?ref - N`), so each band is one integer add per
distinct pair. Over a 374,911-triple store, prepared, median of 30 runs:
52.8 ms → 35.0 ms once those bands compile instead of taking the generic
route. `filter-arith` in the benchmark query set is that shape.

**Steps aside.** Impure builtins (`RAND`, `UUID`, `STRUUID`, `BNODE()`) —
memoizing them per value would change observable behaviour — and a conjunct
that could see a variable an enclosing lazy join binds row by row (see
*OPTIONAL, MINUS and group joins*) hand the whole `Filter` to rdflib.
`VORTEX_RDF_FILTER_FAST=0` forces the generic route everywhere; the
equivalence tests run the matrix both ways, and `tests/test_filters.py`
checks every fast leaf against rdflib's evaluator over a matrix of literal
spellings (ill-typed integers, NaN and INF doubles, out-of-range bytes,
escaped quotes, language tags, custom datatypes, unbound).

## Projection, LIMIT/OFFSET and ASK

**Shape.** `Slice(Project(block))`, `Project(block)`, `AskQuery(Project(block))`.

```sparql
SELECT ?s WHERE { ?s <p> ?o } LIMIT 10
ASK { ?s <p> "42" }
```

**In code space.** The projection decodes only the projected variables; the
slice is applied to the code rows before any decoding; an `ASK` over one
pattern is answered by `count_quads` from the row selection — no row
matched, no term decoded — and otherwise by the first row of the solved
block.

**Why it is faster.** Before the lazy chunked yield, a `LIMIT 10` primed the
decode cache with every code of the match: `SELECT * WHERE { ?s ?p ?o }
LIMIT 10` over 50,000 triples was 11.9 ms, slower than rdflib's 4.8 ms; it
is 0.7 ms end-to-end now. An `ASK` over a variable pattern (1,516 matches):
0.20 ms → 0.03 ms. A predicate scan (1,516 rows, two projected variables):
14.6 ms → 4.6 ms — what remains is rdflib's own `ResultRow` construction,
≈2 µs per row.

## DISTINCT

**Shape.** `Distinct(Project(block))`, optionally under `Slice` and over
`OrderBy`.

```sparql
SELECT DISTINCT ?p WHERE { ?s ?p ?o }
```

**In code space.** The projected code tuples are deduplicated first — one
`dict.fromkeys` over a column view for the common single-variable case —
and only the survivors are decoded. rdflib compares *decoded* terms, and
two spellings of a typed literal can be one term (`"042"^^xsd:integer` and
`"42"^^xsd:integer` are both `42`), so a second, term-level pass runs over
the code-distinct survivors: IRIs, blank nodes and untyped literals are one
term per spelling and keep their code as key, typed literals are keyed by
the decoded term. `LIMIT` applies after both passes, lazily.

**Why it is faster.** `SELECT DISTINCT ?p WHERE { ?s ?p ?o }` over 50,000
triples asks rdflib to build 50,000 solutions and hash each: 487 ms. The
column view deduplicates in 1 ms and 33 codes are decoded: 1.3 ms.

**Steps aside.** `REDUCED` stays rdflib's (its MRU-1 pass over our lazy
projection is cheap and order-defined).

## COUNT and GROUP BY

**Shape.** `AggregateJoin(Group(block))` with `Aggregate_Count` items
(`COUNT(*)`, `COUNT(?v)`, `COUNT(DISTINCT ?v)`) and the `Aggregate_Sample`
rdflib synthesizes for each `GROUP BY` variable; the `Extend` nodes that
rename `__agg_N__` to the query's variables and a `HAVING` filter sit above
and stay rdflib's, over the rows the hook yields.

```sparql
SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p
```

**In code space.** A `COUNT(*)` over one pattern without grouping is
`count_quads` — no row matched. Otherwise the counts are taken over the
code columns: a `Counter` over the key column for the common
`GROUP BY ?v COUNT(*)`, a row loop for several keys or column counts,
per-group code sets for distinct counts. Only the group keys are decoded.
Groups whose decoded keys are equal terms are merged and distinct counts
are taken over key terms (the same normalization concern as `DISTINCT`).
An empty input yields the zero row without `GROUP BY` and rdflib's empty
binding with it.

**Why it is faster.** rdflib's `evalAggregateJoin` consumes one
`FrozenBindings` per row and evaluates the group expression per row.
`SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }`: 235 ms → 0.04 ms.
`SELECT ?p (COUNT(*) AS ?n) ... GROUP BY ?p`: 311 ms → 2.8 ms.
`COUNT(DISTINCT ?o)` per predicate: 390 ms → 61 ms — the term-level pass
decodes every distinct typed literal; exactness is kept over speed here.

**Steps aside.** `SUM`, `MIN`, `MAX`, `AVG`, `GROUP_CONCAT`, a `SAMPLE` of a
variable that is not a group key, `GROUP BY` on an expression, and a
`COUNT` over an expression are rdflib's.

## OPTIONAL, MINUS and group joins

**Shape.** `LeftJoin(p1, p2, expr)` (an `OPTIONAL`; `expr` is the inner
group's `FILTER`, which rdflib hoists onto the node), `Minus(p1, p2)`, and
`Join(p1, p2)` (nested groups; rdflib marks it `lazy` unless a side
contains a `Join`, `Slice` or `Distinct`).

```sparql
SELECT ?s ?o ?x WHERE {
  ?s <p> ?o .
  OPTIONAL { ?s <q> ?x }
}
```

**In code space.** Both sides are solved into relations and combined on
their shared variables: a hash left join whose unmatched rows are padded
with unbound variables (the hoisted condition is applied to candidate
pairs before a row counts as matched), an anti-join, or a hash join (the
right side deduplicated for a non-lazy join, as rdflib's `set(b)` does).
When the inner side is one pattern and the outer relation is small, the
inner pattern is re-probed per outer row instead — today's probe rule — so
an anchored `OPTIONAL` still costs one native match. A `MINUS` without
shared block variables follows rdflib's compatibility test on the
context's own bindings: nothing is removed at top level, everything is
when the right side is non-empty inside a lazy join.

**Why it is faster.** rdflib's `evalLeftJoin` and lazy `evalJoin` evaluate
the inner side *once per outer solution* — through the BGP hook, ≈85 µs
each, or through `triples()` — and its `_minus` compares every left row
with every right row. An `OPTIONAL` whose outer side is a whole predicate
scan (1,516 rows): 77 ms → 12.5 ms; a `MINUS` of the filtered range against
an object-kind-filtered scan: 113 ms → 3.5 ms; two nested groups joined on
their subject: 57 ms → 6.6 ms. The anchored `OPTIONAL` of the benchmark
(one outer row) costs one extra match and probe: 0.44 ms against rdflib's
0.22 ms, behind the parse.

**Steps aside — exactness guards, all raised while rdflib is listening.**
A join key an `OPTIONAL` may leave unbound needs rdflib's compatibility
semantics (unbound matches anything), which a hash join cannot give: the
shape falls back. rdflib re-evaluates an unmatched `OPTIONAL` with only
p1's `_vars` bound (its "cheated scope" check), which can differ from the
first pass when a variable that pass drops — bound by the context, by an
enclosing join, or by a `VALUES` table (rdflib's `_vars` exclude those) —
reaches the inner side: those shapes fall back. And because the sides are
solved independently rather than row by row, a `FILTER` inside a side that
rdflib would let see an enclosing join's binding falls back too.

## FILTER (NOT) EXISTS

**Shape.** A `FILTER` conjunct that is `EXISTS { body }`, `NOT EXISTS
{ body }` or either under one `!`, with a body in the block grammar.

```sparql
SELECT ?s WHERE {
  ?s <p> ?o .
  FILTER NOT EXISTS { ?s <q> ?x }
}
```

**In code space.** A semi-join (or anti-join) on the variables the body
shares with the block: the body is solved once and the block's rows kept
or dropped by key; for a small block and a one-pattern body, each row is
probed with `count_quads` instead; a body without shared variables is a
global existence test. rdflib pulls the body's own `FILTER`s out of the
parse tree and never simplifies the translated body, so the conjunct's
variables are read from the translated body and an empty-`BGP` `Join` is
looked through.

**Why it is faster.** rdflib evaluates the body once per row (re-entering
the BGP hook each time). `FILTER NOT EXISTS { ?s <q> ?x }` over a 1,516-row
scan: 126 ms → 5.5 ms.

**Steps aside.** A nullable shared variable, a body outside the grammar, or
a body `FILTER` that sees the block's bindings send the conjunct down the
generic route — rdflib's evaluator, once per distinct tuple. An `EXISTS`
inside `||` or another operator is part of that conjunct's expression and
takes the generic route as well.

## ORDER BY

**Shape.** `Project(OrderBy(block))`, optionally under `Slice`/`Distinct`,
when every condition is a plain variable.

```sparql
SELECT ?s ?v WHERE { ?s <p> ?v } ORDER BY DESC(?v) LIMIT 10
```

**In code space.** Each sort variable's distinct codes are ranked once,
reproducing rdflib's `_val` order: unbound first, then blank nodes and IRIs
— whose codes already order as their spellings, so their codes are their
ranks — then literals, decoded and sorted with rdflib's own comparator,
adjacent terms that compare equal sharing a rank so the stable sort keeps
their input order exactly as rdflib's chain of stable sorts does. A row's
key is its tuple of ranks (negated for `DESC`), one sort replaces rdflib's
per-condition sorts over decoded rows, and a following `LIMIT` keeps only
the top k (`heapq.nsmallest`).

**Why it is faster.** rdflib sorts decoded `FrozenBindings`, evaluating the
condition per row per comparison. `ORDER BY ?o` over a 1,516-row scan:
24.7 ms → 8.0 ms; the benchmark's filtered `ORDER BY DESC(?v) LIMIT 10`:
47 ms → 6.6 ms.

**Steps aside.** `ORDER BY` on an expression is rdflib's. rdflib's own
comparator raises `decimal.InvalidOperation` on a NaN double against a
decimal, on either path.

## VALUES

**Shape.** `ToMultiSet(values)` — an inline data table — as a block leaf,
typically the left side of the lazy `Join` rdflib builds around it.

```sparql
SELECT ?s ?o WHERE {
  VALUES ?s { <s1> <s2> <s3> }
  ?s <p> ?o .
}
```

**In code space.** Every constant is looked up by its canonical dictionary
spelling (`terms.canonical_spelling`: the store's escapes, lowercase
language tags, no `^^xsd:string`) and becomes a code, `UNDEF` an unbound
variable; the table joins like any relation. A constant the dictionary does
not hold gets a private negative code (it joins nothing but is still
yielded verbatim), after a spelling-tolerant `count_quads` check that the
store really lacks the term — a mismatch between our spelling and the
store's is never guessed.

**Why it is faster.** rdflib joins a `VALUES` table lazily, one `triples()`
call per row. Sixty-four subjects joined to a predicate scan: 3.8 ms →
2.1 ms.

**Steps aside.** A constant whose rdflib object is not the decoded
dictionary term — a query literal rdflib's parser leaves unnormalized,
`"042"^^xsd:integer` — is left to rdflib, whose rows would carry the query's
own object; a `VALUES` variable that an `OPTIONAL` re-checks falls back as
described above.

## Named graphs

**Shape.** Not an algebra node of its own: every pattern above carries the
graph the query is active in. `_pattern_terms` builds a **quad** pattern,
whose fourth position is `VortexRdflibStore._graph_n3(ctx.graph)` — `None`
(the wildcard over every graph) for a union default graph, `""` for the
default graph of a `Dataset` without union, the graph's own name inside a
`GRAPH` block. Every `match_codes` and `count_quads` in this document takes
that position, so a pushed-down block reads exactly the rows rdflib's own
evaluator would have asked `ctx.graph` for, and an absent graph selects
nothing without materializing a row.

```sparql
SELECT ?g ?s WHERE { GRAPH ?g { ?s <p> ?o } }
```

**In code space.** A `Graph` node is a block node like any other: it does not
match anything itself, it sets the scope of the block below it (`_solve_block`
recurses into `node.p` with the new scope, so a nested `GRAPH` simply wins).
A bound name becomes the constant above. An **unbound variable becomes a
column**: the pattern is matched with the graph wildcard and `?g` takes
position 3 in `varpos`, which makes it an ordinary variable of the relation —
joined, filtered, grouped, ordered and decoded like any other, out of the
fourth column the native match already returns. Two patterns under the same
`GRAPH ?g` therefore join on `?g`, which is exactly the requirement that they
come from the same graph.

`GRAPH` ranges over the *named* graphs, so the default graph's rows are
dropped from a variable-scoped match. There is no "any named graph" native
pattern, so `_exclude_default_graph` restricts the wildcard match afterwards:
the graph column's distinct codes minus the default graph's, as a `keep` set,
which every row path already applies (and which then narrows the values a
`FILTER` on `?g` is evaluated over, so the default graph's empty name — which
is no RDF term — never reaches a predicate).

**Why it is faster.** rdflib's `evalGraph` walks the dataset's graphs and
evaluates the block once per graph, joining `{?g: <name>}` onto every
solution in Python. One match replaces all of it. Over the dashboard's
250,000 quads in 8 graphs: `SELECT DISTINCT ?g` over the whole store 2,678 ms
-> 117 ms, a `COUNT(*)` per graph 1,593 ms -> 148 ms, a predicate scan under
`GRAPH ?g` 87 ms -> 28 ms. A *bound* graph gains too, because the head above
it is now intercepted at the same time as the block: a scan inside one named
graph 13.3 ms -> 5.6 ms.

**Steps aside.** `FILTER (NOT) EXISTS` under a graph *variable*: rdflib
evaluates an EXISTS body against the active graph, and under a variable there
is no single active graph — the row's graph is a column — so the block goes
back to rdflib, which walks the graphs itself. A term bound to something that
cannot name a graph (a literal) is rdflib's too. And when rdflib does evaluate
a `Graph` node — in `bgp` mode, or above a block it declined — it writes
`solution.ctx.graph` back as it yields, so the solutions of a block below it
each carry their own context rather than sharing the caller's
(`_pushed_graph`); otherwise that write would reach the rows still to come and
send an OPTIONAL's right side to the wrong graph.

## Switches and the test oracle

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Never register the hook: rdflib's default evaluator for every operator. |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Intercept only the listed algebra nodes (`BGP,Filter,Project,...`); `bgp` is basic graph patterns only, the behaviour of the first releases. |
| `VORTEX_RDF_FILTER_FAST=0` | Route every FILTER value through rdflib's evaluator (still once per distinct value). |

The switches are read when a `VortexRdflibStore` is constructed. The default
evaluator is the oracle of the test suite: `tests/test_pushdown.py` runs
every query shape (≈280) with the pushdown and without, on a file-backed
and an in-memory store, in four modes — the shipped configuration, the
per-binding probe path forced (`_PROBE_FANOUT = 0`), basic graph patterns
only, and the generic FILTER route — and compares the answers as multisets
(as sequences for `ORDER BY` queries with a total order).
`tests/test_filters.py` is the differential matrix for the fast FILTER
route.

## Measuring

The dashboard's *pushdown off* row (`vortex-rdflib (dict · in-mem · pushdown
off)`) is the same store with `VORTEX_RDF_DISABLE_PUSHDOWN=1`, so the
pushdown's own contribution is the difference between two rows. In-process,
the pattern used for the numbers above toggles the hook on one graph with a
prepared query:

```python
from rdflib.plugins.sparql import prepareQuery
from vortex_rdflib import register_sparql_pushdown, unregister_sparql_pushdown

query = prepareQuery(sparql)
register_sparql_pushdown()
rows_on = list(graph.query(query))
unregister_sparql_pushdown()
rows_off = list(graph.query(query))
register_sparql_pushdown()
```

The benchmark's query set (`bench/queries.py`) has a query per pushdown —
`ask-var`, `limit-scan`, `filter-range`, `filter-arith`, `filter-class`,
`distinct`,
`distinct-p`, `count-all`, `count-distinct`, `agg-count`, `optional-wide`,
`not-exists`, `minus`, `order-var`, `order-limit`, `values-64` and the six
`graph-*` queries — next to the lookups and joins.

## What stays with rdflib

- `UNION` (each branch is a block of its own and is ours), `BIND`/`Extend`,
  sub-selects, `SERVICE`: rdflib evaluates the node and re-enters the hook
  below it.
- Property paths and RDF-star patterns.
- `REDUCED`, aggregates other than `COUNT`, `GROUP BY` and `ORDER BY` on
  expressions.
- Parsing. rdflib's `parseQuery` + `translateQuery` is ≈1.2 ms per query on
  the machine above — most of a point lookup's time — and is paid by every
  store alike; `prepareQuery` once and reuse the `Query` object when a query
  string repeats.

## Planned vortex-rdf 0.11 primitives

Everything above uses the published 0.10 API. A few thin *data-access*
primitives in the native layer — no join or planning logic — would make
some pushdowns cheaper at scale; the Python side would detect them with
`hasattr` and keep the 0.10 path. In order of value:

1. **`VortexRdfStore.match_codes_many(patterns)` / `count_quads_many(patterns)`**
   — element-wise `match_codes`/`count_quads` in one call: all patterns
   parsed first (any malformed one → `ValueError`, nothing evaluated), one
   GIL release, results in input order, file-backed scans free to run
   concurrently. Used by every probe (`_probe_join`, `_probe_exists`, the
   OPTIONAL probe): today 1,516 probes cost 1,516 × 45 µs in memory and
   1,516 × ≈1 ms file-backed.
2. **`TermDict.filter_codes(kind, arg) -> (true_codes, unknown_codes)`** —
   one scan of the dictionary (the literal range for literal predicates)
   returning the ascending codes for which the predicate is definitely true
   under the same rules as the Python fast path, and the codes outside its
   exact domain (which Python resolves with rdflib). Kinds: `is_literal`,
   `is_iri`, `is_blank`, `datatype`, `lang`, `lang_matches`, `num_lt`…`num_ne`
   (numeric datatypes, lexical parsed with the same rules), `str_prefix`.
   Cached per dictionary by `(kind, arg)`. Turns the per-query
   "decode + predicate per distinct value" into a one-time scan.
3. **A `keep` constraint on `match_codes` (per position: a code set or a
   `(lo, hi)` range)** — the per-variable restriction applied inside the
   native scan instead of over the gathered rows, so rows whose code falls
   outside it never cross the FFI. Composes with `filter_codes`, which
   produces the very set it consumes; kind and prefix predicates are
   contiguous code ranges, the shape a file-backed scan can prune whole
   chunks by. Mostly a file-backed win: the numeric `filter-range` shape
   today gathers 30,303 rows to keep 757.
4. **Spelling-tolerant `TermDict.encode`** (parse with the pattern parser,
   canonicalize, look up) and **`encode_many(terms)`** — removes
   `canonical_spelling` and the `count_quads` guard from the `VALUES` path.
5. **`match_codes(..., limit, offset)` and `count_quads(..., limit)`** — the
   first rows of a match in base order, an existence test that stops at
   the first hit: `LIMIT` over a single pattern and `ASK` on file-backed
   stores, where a match gathers every row today.
6. **`TermDict.prefix_range(prefix) -> (lo, hi)`** — the code range of a
   spelling prefix: kind bounds without the bisection, IRI namespaces as
   ranges for `strstarts(str(?s), ...)`.
7. **`U32Column.distinct()` / `value_counts()`** — native distinct and
   counts for `DISTINCT`, `GROUP BY` and `COUNT DISTINCT` over columns of
   millions of rows (Python's `set`/`Counter` cost 15–37 ns per element).
8. **Native equi-join over code columns** — the matching row pairs of two
   matches' key columns, gathered into result columns in one GIL-released
   call: it would replace `_join_columns`' Python kernel. numpy was
   prototyped for that kernel and deliberately not shipped — the stdlib
   index-gather already captures most of the win (1M-row wide star: 130 ms
   against numpy's 118 ms) and this primitive obsoletes both.
