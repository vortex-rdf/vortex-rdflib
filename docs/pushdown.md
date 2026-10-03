# SPARQL pushdown

`vortex-rdflib` answers SPARQL with rdflib's engine, but a
[`VortexRdflibStore`](../src/vortex_rdflib/store.py#L61) can do more than
serve quad patterns: it hooks into rdflib's algebra evaluation and answers
the operators it understands in **code space**, i.e., over the `u32` term
codes of a Dictionary-layout store, handing everything else back to rdflib
node by node. This document explains, for each pushdown, the algebra shape
it intercepts, what runs in code space, what stays in rdflib and how it
makes it faster.

**Measurement numbers.** Unless marked *end-to-end*, every timing below is
the best of 5–7 runs of a *prepared* query (`prepareQuery`; rdflib's parse
and algebra translation would otherwise dominate the cheap queries),
measured on 2026-08-30 on an Intel Core Ultra 7 155H with Python 3.13.7,
vortex-rdf 0.10.0 and rdflib 7.6.0, over a single-graph 50,000-statement
dataset from the benchmark generator ([`bench/dataset.py`](../bench/dataset.py): 5,000 subjects, 33
predicates, 27,034 distinct terms) in a Dictionary-layout store loaded in memory
(`VortexRdflibStore(path, in_memory=True)`). "rdflib" is the same store under
rdflib's default evaluator (`VORTEX_RDF_DISABLE_PUSHDOWN=1`), which is what
the dashboard's *pushdown off* rows run; the [benchmark
dashboard](https://vortex-rdf.github.io/vortex-rdflib/) is the live
reference, at 250,000 quads over 8 graphs and across the other stores.

**vortex-rdf 0.11.** Most pushdowns now run on primitives vortex-rdf 0.11
added — batched probes, native filter predicates and `keep` constraints,
`LIMIT` inside a match, native distinct, counts and joins over code columns;
[the last sections](#vortex-rdf-011-primitives-in-use) map each primitive to
where it is used. Numbers marked *0.11* compare this package before
(vortex-rdf 0.10.0) and after (0.11.0) that rework, over the benchmark's
dashboard dataset (250,000 quads in 8 graphs), best of at least 5 prepared
runs across interleaved rounds, measured on 2026-10-03 on an Intel Xeon @
2.10 GHz (4 vCPUs) with Python 3.11.15 and rdflib 7.6.0. They are not
comparable with the 50,000-statement numbers, which stay as measured on 0.10
unless stated otherwise.

## How the hook works

rdflib evaluates a query as a tree of algebra operators (`Project`,
`Filter`, `LeftJoin`, `BGP`, ...) and, for **every** node, offers it to the
functions registered in `rdflib.plugins.sparql.CUSTOM_EVALS` before running
its own evaluator. Constructing a
[`VortexRdflibStore`](../src/vortex_rdflib/store.py#L61) registers one such
hook
([`pushdown.register_sparql_pushdown`](../src/vortex_rdflib/pushdown.py#L341)).
The hook looks at the node's name and for:

- a node it handles, over a graph whose store is a `VortexRdflibStore` with the
  code path available (Dictionary layout; the term dictionary resident, or
  read from the file on demand once it outgrows the residency budget — see
  the README), is answered in code space;
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
code — no decode needed; `TermDict.prefix_range` gives the three ranges.
Since vortex-rdf 0.11 much of that integer work runs natively, GIL released:
a FILTER the native layer can decide becomes a `keep` constraint the match
applies before any row crosses the FFI, a join pairs its key columns with
`U32Column.join_indices` and gathers them with `take`, and `DISTINCT` and
`GROUP BY` call `U32Column.distinct` and `value_counts`.

**Blocks and heads.** The hook solves a *block* — a subtree of the grammar
`BGP` | `Filter(block)` | `Join(block, block)` | `LeftJoin(block, block,
expr)` | `Minus(block, block)` | `ToMultiSet(values)` | `Graph(block)` —
into a [`Relation`](../src/vortex_rdflib/pushdown.py#L502): a schema of
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

1. [`_plan_head`](../src/vortex_rdflib/pushdown.py#L539) splits
   `Slice -> Distinct -> Project` and hands the `Filter` below it to
   [`_solve_block`](../src/vortex_rdflib/pushdown.py#L1250).
2. `isIRI(?o)` references a single block variable, so it becomes a
   *per-variable predicate* — one the native layer answers: the IRI codes
   are one contiguous range, which goes into the scan of the pattern that
   binds `?o` as a `keep` constraint
   ([`_push_native_restrictions`](../src/vortex_rdflib/pushdown.py#L2651))
   rather than a test applied to returned solutions.
3. Both triple patterns resolve to the same quad pattern, scoped to the
   active graph — a variable is `None` in a resolved pattern, so only the
   names differ — and one native count serves both: 1,516 rows. `?m <p> ?o`
   is matched under its keep: only the 909 rows whose object is an IRI
   cross the FFI, as four zero-copy `u32` columns, and no term is decoded
   to find them. `?s <p> ?m`, which nothing narrows, is a match of its own.
4. The two sides of the join are about the same size, so neither is worth
   probing per binding: an equi-join on `?m`, its matching row pairs found
   by `U32Column.join_indices` and its columns gathered by `take`, both
   native. The block's `Relation` is schema `(?m, ?o, ?s)` over 166 rows
   (181 without the filter).
5. The head runs on those codes: `Project` keeps the `?o` column, `Distinct`
   deduplicates it natively (`U32Column.distinct`; 166 codes, already
   distinct here), `Slice` takes the first five rows.
6. Only then is anything decoded: one `decode_many` of five codes, five
   `FrozenBindings`. The other 161 rows never become terms.

Prepared and without the `LIMIT`, this was 2.9 ms against rdflib's 61 ms
on vortex-rdf 0.10. With the `LIMIT`, rdflib's nested loop also stops after
five solutions, and it won: 1.9 ms to 2.3 ms — the up-front counting and
matching is what the pushdown pays to learn the sizes. *0.11*, over the same
50,000 statements: 1.0 ms against rdflib's 88 ms without the `LIMIT` (3.1 ms
before the rework), and 0.42 ms against 2.8 ms with it (2.5 ms before) — the
narrowed scan, the native join and the native distinct leave little to pay
for learning the sizes.

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

**In code space.** Every triple pattern is *counted* natively before
anything is matched (`count_quads`: the row selection's size, no columns — a
fraction of a match, which costs 9 µs for a point lookup but 60 µs for a
predicate scan of 1,024 rows and 580 µs for one of 7,576). Patterns that
resolve to the same quad pattern share one count and one match: a variable
is `None` in a resolved pattern, so the hops of a self-join
(`?s <p> ?m . ?m <p> ?o`) differ only in names the match never sees, and
each keeps its own reading of the shared columns (two patterns a FILTER
narrows differently share the count, not the match). The patterns are
counted in shape order, most bound positions first, so an anchor that
selects nothing ends the block before a scan is even counted — and when
enough of them *scan* (no subject bound, no secondary index serving them),
those are counted last and together, in one `count_quads_many` call
([`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2014)): every probe
parsed before any runs, all of them concurrently on the native runtime, one
GIL release. A batch costs a fixed 60–150 µs (the upper end once the
runtime's workers have parked, as they do between the steps of a query),
which two scans of a file-backed store repay — they overlap their reads, two
predicate scans ≈1.3x — and two in-memory scans, pure CPU, do not: in memory
a batch takes three
([`_MEMORY_SCAN_BATCH_MIN`](../src/vortex_rdflib/pushdown.py#L2109)), and
fewer are counted one by one, stopping at a zero like the rest. The
smallest pattern is matched and seeds the relation
([`_join_incremental_bgp`](../src/vortex_rdflib/pushdown.py#L2234)). Each
further step takes the smallest pattern sharing a variable with the relation
so far (the smallest of all for a cross product) and decides by the counts:
when the relation is at least
[`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L1804) (100) times
smaller than the pattern's count, the pattern is re-matched natively per row
of the relation ([`_probe_join`](../src/vortex_rdflib/pushdown.py#L2891))
and is never matched whole — an anchored star, a subject fixed by one
selective pattern joined to two unselective legs, costs one match and one
probe per leg. Probes that scan go through `match_codes_many`, up to
[`_PROBE_BATCH`](../src/vortex_rdflib/pushdown.py#L2888) (4,096) per call;
point probes — a bound subject is a binary search in the subject-ordered
base — go through plain calls, which answer one in a few µs, sooner than a
batch is scheduled
([`_batched_probes`](../src/vortex_rdflib/pushdown.py#L2117)). A relation
not that much smaller has the pattern matched once and joined instead:
`U32Column.join_indices` pairs the
matching rows of the two key columns and `take` gathers every column by
those indices, both native and GIL released
([`_join_columns`](../src/vortex_rdflib/pushdown.py#L2829)), so the running
relation stays columnar and row tuples exist only where a consumer
materializes them (a pattern Python still restricts, a repeated variable,
several shared variables, or a cross product drop to the row hash join). A
block that is a single pattern is not even turned into tuples: its column
views are the relation, and rows are zipped a chunk at a time as they are
consumed.

**Why it is faster.** rdflib's `evalBGP` is a nested loop: one
`Store.triples()` call per candidate binding, each paying the native match
floor and decoding its rows. A two-hop chain over a predicate
(`?s <p> ?m . ?m <p> ?o`, 1,516 matches per pattern, 181 solutions) is
55 ms under rdflib and 1.8 ms here. The anchored shapes are where rdflib's
nested loop is already near-optimal — it issues one selective match then
probes — and the pushdown now does the same, at the price of one count per
pattern instead of one match: a three-leg anchored star over 32,768 quads
issues 3 counts, 1 match and 2 probes, and selects 3 rows in total where
matching every leg up front selected 2,051. The columnar kernel is what
the wide shapes gain: an unanchored two-leg star (1,516 rows a side, 1,213
solutions) 9.2 ms -> 8.0 ms, and under a `LIMIT 10` the join's columns are
gathered but barely tupled or decoded, 1.1 ms end to end. *0.11:* the
benchmark's two-hop chain (`chain-2`, 908 solutions) 7.6 ms → 4.3 ms in
memory and 7.8 ms → 6.3 ms file-backed, its three-leg anchored star
(`star-3`) 1.21 ms → 0.87 ms in memory and 1.61 ms → 1.32 ms file-backed.

**Steps aside.** A literal propagated into subject or predicate position
makes the pattern unsatisfiable (empty, not an error), exactly as
`Store.triples()` treats it. A store whose code path declines
(`match_codes` returns `None`: another layout) raises while rdflib is still
listening, and the default evaluator runs over the string path.

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
- **one** — a *per-variable predicate*, on the pattern that binds the
  variable. A shape the native layer decides becomes a constraint of the
  match itself (*Native predicates* below). Otherwise it is evaluated once
  per distinct code of the variable: a pattern that is matched whole
  evaluates it over its column's distinct codes and is compacted to the
  rows that pass before it joins, so the filter's selectivity is what the
  join sees; a pattern the relation *probes* evaluates it over the codes
  the probes actually reach — BSBM Q8's `langMatches(lang(?text), "EN")`
  runs over one product's reviews, not over every review text in the
  store. A variable the relation already binds is not restricted again:
  the pattern that bound it did;
- **several** — applied to the joined rows, memoized per distinct code
  tuple, and streamed over a columnar body, so a `LIMIT` above stops it
  early. A fast conjunct is also tried mid-join, in any BGP of the block
  that binds all its variables (wherever the single-variable ones reach: a
  GRAPH block, a group join, an OPTIONAL's or a MINUS's left side), when
  pruning could let a remaining pattern be probed rather than matched
  whole — the `_PROBE_FANOUT` test again. BSBM Explore Q5 is the case: its
  similarity band prunes the candidate products, and the next numeric
  property is then probed per survivor instead of matched over every
  product. Evaluation stops as soon as too many rows pass for that,
  leaving the conjunct to the joined rows, which reuse the terms it
  decoded. And when the relation binds every variable of a conjunct but
  one to a single value — Q5's reference product, once the anchor is
  matched — the conjunct is a predicate of that one variable on the
  pattern about to bind it, and a native one narrows that pattern's match
  ([`_specialize_tuple_conjuncts`](../src/vortex_rdflib/pushdown.py#L2422)):
  the band becomes two numeric bounds on the scan.

Every conjunct is evaluated **per distinct value, never per row**, through
two routes:

- the **fast route** compiles a whitelist of expression shapes into a
  predicate over the term's parsed spelling
  ([`terms.parse_spelling`](../src/vortex_rdflib/terms.py#L74)), each leaf
  mirroring the exact rdflib code path: numeric comparisons
  (`< <= > >= = !=` with rdflib's numeric fast path and its datatype-IRI
  ordering otherwise), `IN`/`NOT IN` and `sameTerm` (term equality),
  `datatype`, `lang`, `langMatches` (rdflib's own `_lang_range_check`),
  `isIRI`, `isBlank`, `isLiteral` (pure code-range tests, no decode at all),
  `isNumeric`, `bound`, `str`, `regex` (Python `re`, as rdflib),
  `strstarts`, `strends`, `contains`, `+` and `-` over integer-derived
  literals, and `&&`, `||`, `!` with rdflib's three-valued short-circuit
  rules. A leaf answers true, false, *error* (rdflib would raise, which a
  filter turns into false) or **unknown** — the value is outside the domain
  the fast path reproduces exactly: an ill-typed number, a datatype rdflib
  orders by its own rules, a normalized lexical form under `str()`, a NaN
  against a decimal. Unknown values, alone, go to
- the **generic route**: rdflib's own evaluator (`_ebv`) on a
  `FrozenBindings` holding the decoded term. Semantics-exact by
  construction, at rdflib's cost per evaluation (≈24 µs), but paid once per
  distinct value instead of once per row. Expressions outside the whitelist
  take this route for every value, so *every* `Filter` over a block is
  intercepted and none is slower than rdflib's per-row evaluation.

**Native predicates.** A single-variable conjunct of a shape vortex-rdf
0.11 decides — `isIRI`, `isBlank`, `isLiteral`; `datatype(?v) = <iri>` and
`lang(?v) = "tag"`, either way round; `langMatches(lang(?v), "range")`;
`strstarts(str(?v), "prefix")`; and `?v` against a numeric constant by any
of `< <= > >= = !=`, either way round
([`native_shape`](../src/vortex_rdflib/filters.py#L804)) — is answered for
the whole dictionary at once: `TermDict.filter_codes(kind, arg)` scans it
and returns the codes for which the predicate is definitely true and the
codes outside the domain it decides exactly, memoized natively and by the
store (the last
[`_VERDICT_MEMO_SIZE`](../src/vortex_rdflib/store.py#L39), 128, verdicts).
The kind tests need no scan: they are the code ranges of
`TermDict.prefix_range`. When no code the pattern's position can hold is
undecided — subjects and graph names are IRIs or blank nodes, predicates
IRIs — the verdict goes into the native match as a `keep` constraint
([`native_restriction`](../src/vortex_rdflib/filters.py#L968)): a code range
for a kind test, the true codes otherwise, intersected when several
conjuncts narrow one position. The rows it rejects never cross the FFI;
an `ASK` or `COUNT` over the pattern counts under it, and a probe carries a
range natively and tests a code set on the rows it returns. (Planning
counts stay unnarrowed: in memory, a narrowed count costs a pass over the
rows it selects, more than an estimate is worth.) With undecided codes in
reach, the conjunct stays with the two routes above, which then evaluate
only the undecided values
([`evaluate_column`](../src/vortex_rdflib/filters.py#L1207)). Where rdflib
and the native scan part ways, rdflib decides:

- `!=` is never a keep: rdflib passes every IRI and blank node through it,
  and the native verdict covers literals only;
- `langMatches` maps only for `*` and ranges of ASCII letters and digits,
  where rdflib's basic filtering and the native one agree;
- rdflib bounds neither `xsd:long` nor `xsd:unsignedLong`, the native parser
  does: their values beyond 64 bits are made undecided
  ([`wide_integer_codes`](../src/vortex_rdflib/filters.py#L945));
- rdflib's parser leaves a typed constant outside its datatype's range
  (`"300"^^xsd:byte`) well-typed and compares it by value: never mapped —
  and the fast route, which used to answer false, defers it too;
- on a file-backed store vortex-rdf evaluates a keep of 33 to 4,096 codes as
  one `list_contains`, slower than the scan it narrows: such a set goes
  natively as its bounding code range, and its codes are tested on the rows
  the range admits ([`_split_keeps`](../src/vortex_rdflib/pushdown.py#L2060)).

`VORTEX_RDF_NATIVE_FILTERS=0` keeps every conjunct on the two routes above.

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
34 ms → 4.7 ms. *0.11:* with the predicate inside the match, `filter-range`
(189 rows kept of a 7,576-row scan) is 17.5 ms → 1.18 ms in memory and
15.7 ms → 2.76 ms file-backed; `filter-class` (`isIRI(?o)`, 4,544 rows
kept) 21.3 ms → 11.9 ms, most of what remains being the rows' yield.

A conjunct over *several* variables is memoized per distinct code tuple
instead, which is what makes arithmetic worth compiling: BSBM Explore Q5
brackets two numeric properties against a reference product's
(`?v < ?ref + N && ?v > ?ref - N`), so each band is one integer add per
distinct pair. Over a 374,911-triple store, prepared, median of 30 runs:
52.8 ms → 35.0 ms once those bands compile instead of taking the generic
route. `filter-arith` in the benchmark query set is that shape. *0.11:*
once the anchor binds `?ref`, its band is two native bounds on the scan of
`?v` — 32.8 ms → 2.1 ms in memory, 33.2 ms → 3.8 ms file-backed — and
`filter-band-probe`, which then probes a third pattern per survivor,
72 ms → 18 ms.

**Steps aside.** Impure builtins (`RAND`, `UUID`, `STRUUID`, `BNODE()`) —
memoizing them per value would change observable behaviour — and a conjunct
that could see a variable an enclosing lazy join binds row by row (see
*OPTIONAL, MINUS and group joins*) hand the whole `Filter` to rdflib.
`VORTEX_RDF_FILTER_FAST=0` forces the generic route everywhere and
`VORTEX_RDF_NATIVE_FILTERS=0` the Python routes; the equivalence tests run
the matrix each way, and
[`tests/test_filters.py`](../tests/test_filters.py) checks every fast leaf
against rdflib's evaluator over a matrix of literal spellings (ill-typed
integers, NaN and INF doubles, out-of-range bytes, escaped quotes, language
tags, custom datatypes, unbound).
[`tests/test_native_filters.py`](../tests/test_native_filters.py) holds
every native verdict to rdflib's answer over the same matrix and the shapes
the native value model treats apart (decimals against doubles, integers past
2^53, 64-bit bounds, language subtags), then runs each mapped shape end to
end against the default evaluator.

## Projection, LIMIT/OFFSET and ASK

**Shape.** `Slice(Project(block))`, `Project(block)`, `AskQuery(Project(block))`.

```sparql
SELECT ?s WHERE { ?s <p> ?o } LIMIT 10
ASK { ?s <p> "42" }
```

**In code space.** The projection decodes only the projected variables; the
slice is applied to the code rows before any decoding, and without
`ORDER BY` or `DISTINCT` its end travels down into the block: a single
pattern that leaves Python nothing to drop is matched with
`match_codes(..., limit=k)`, and the native scan stops at the k-th row
([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2216); on a store
without secondary indexes, whose base order — the order the native layer
windows — is the order rdflib reads). An `ASK` over one pattern, narrowed by
any native FILTER keep, is `count_quads(..., limit=1)`, which stops at the
first hit — no row matched, no term decoded — and any other `ASK` solves its
block under a one-row limit and answers by its first row.

**Why it is faster.** Before the lazy chunked yield, a `LIMIT 10` primed the
decode cache with every code of the match: `SELECT * WHERE { ?s ?p ?o }
LIMIT 10` over 50,000 triples was 11.9 ms, slower than rdflib's 4.8 ms; it
is 0.7 ms end-to-end now. An `ASK` over a variable pattern (1,516 matches):
0.20 ms → 0.03 ms. A predicate scan (1,516 rows, two projected variables):
14.6 ms → 4.6 ms — what remains is rdflib's own `ResultRow` construction,
≈2 µs per row. *0.11:* `limit-scan` stops inside the match, 0.86 ms →
0.08 ms in memory and 1.30 ms → 0.10 ms file-backed; and the solutions are
`FrozenBindings` built around their bindings without copying them
([`_Solution`](../src/vortex_rdflib/pushdown.py#L306)): `p-scan` (7,576 rows)
31.8 ms → 18.3 ms in memory, 32.8 ms → 19.4 ms file-backed.

## DISTINCT

**Shape.** `Distinct(Project(block))`, optionally under `Slice` and over
`OrderBy`.

```sparql
SELECT DISTINCT ?p WHERE { ?s ?p ?o }
```

**In code space.** The projected code tuples are deduplicated first — for
the common single-variable case natively, `U32Column.distinct` keeping
first-seen order — and only the survivors are decoded. rdflib compares
*decoded* terms, and two spellings of a typed literal can be one term
(`"042"^^xsd:integer` and `"42"^^xsd:integer` are both `42`), so a second,
term-level pass runs over the code-distinct survivors: IRIs, blank nodes and
untyped literals are one term per spelling, so only the typed literals are
decoded, and a code whose term equals an earlier code's becomes that code's
alias ([`_alias_codes`](../src/vortex_rdflib/pushdown.py#L872)) — for most
stores there is none, and the codes themselves are the keys. Whether a code
aliases is the store's, not the query's: each literal code is classified
once for the store's lifetime, and later queries only look their codes up.
`LIMIT` applies after both passes, lazily.

**Why it is faster.** `SELECT DISTINCT ?p WHERE { ?s ?p ?o }` over 50,000
triples asks rdflib to build 50,000 solutions and hash each: 487 ms. The
column view deduplicates in 1 ms and 33 codes are decoded: 1.3 ms. *0.11:*
`distinct-p`, the same query over 250,000 quads, 5.7 ms → 0.89 ms in memory
and 7.3 ms → 1.8 ms file-backed; `distinct` (7,576 distinct objects of a
predicate scan) 25.5 ms → 18.1 ms.

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
`count_quads` — no row matched — and so is one whose FILTER the native match
decides entirely: the count takes the keep. Otherwise the counts are taken
over the code columns. A columnar body grouped by at most one variable is
counted a column at a time
([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1117)): the key
column's native `value_counts` gives every group and its size, which is
also its `COUNT(?v)` (a columnar body holds no unbound value), and a
`COUNT(DISTINCT ?v)` gathers each group's rows natively (`join_indices`
against the key, `take`, `distinct`) up to
[`_NATIVE_GROUPS_MAX`](../src/vortex_rdflib/pushdown.py#L1114) (64) groups,
or reads the distinct (key, value) pairs beyond. Several keys, or a body of
rows, take a row loop. Only the group keys are decoded. Groups whose decoded
keys are equal terms are merged and distinct counts are taken over key
terms, through the same alias map as `DISTINCT`. An empty input yields the
zero row without `GROUP BY` and rdflib's empty binding with it.

**Why it is faster.** rdflib's `evalAggregateJoin` consumes one
`FrozenBindings` per row and evaluates the group expression per row.
`SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }`: 235 ms → 0.04 ms.
`SELECT ?p (COUNT(*) AS ?n) ... GROUP BY ?p`: 311 ms → 2.8 ms.
`COUNT(DISTINCT ?o)` per predicate: 390 ms → 61 ms — the term-level pass
decodes every distinct typed literal; exactness is kept over speed here.
*0.11:* `agg-count` (`GROUP BY ?p`, 250,000 quads) 10.1 ms → 1.9 ms in
memory and 13.0 ms → 3.2 ms file-backed; `count-distinct`, its literals
classified once per store, 276 ms → 38 ms; `graph-count` (see *Named
graphs*) 160 ms → 15.5 ms in memory and 195 ms → 2.7 ms file-backed.

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
with unbound variables (the hoisted condition is applied to candidate pairs
before a row counts as matched), an anti-join, or a hash join (the right
side deduplicated for a non-lazy join, as rdflib's `set(b)` does). When the
inner side is one pattern, it is counted before it is matched
([`_plan_pattern_side`](../src/vortex_rdflib/pushdown.py#L2547)): an outer
relation at least [`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L1804)
times smaller than the count re-probes the pattern per outer row — unmatched
rows padded, the hoisted condition applied per candidate — and the inner
side's complete match is never made; otherwise it is matched once for the
hash join. An anchored `OPTIONAL` thus costs one count and one probe per
outer row, and the filter conjuncts a lazy join pushes into its inner
pattern travel with the probe — as a native keep when the native layer
decides them, else over the codes the probe reaches. A `MINUS` without
shared block variables follows rdflib's compatibility test on the context's
own bindings: nothing is removed at top level, everything is when the right
side is non-empty inside a lazy join.

**Why it is faster.** rdflib's `evalLeftJoin` and lazy `evalJoin` evaluate
the inner side *once per outer solution* — through the BGP hook, ≈85 µs
each, or through `triples()` — and its `_minus` compares every left row
with every right row. An `OPTIONAL` whose outer side is a whole predicate
scan (1,516 rows): 77 ms → 12.5 ms; a `MINUS` of the filtered range against
an object-kind-filtered scan: 113 ms → 3.5 ms; two nested groups joined on
their subject: 57 ms → 6.6 ms. The anchored `OPTIONAL` of the benchmark
(one outer row) costs one extra match and probe: 0.44 ms against rdflib's
0.22 ms, behind the parse. *0.11:* the benchmark's `MINUS`, both of whose
sides carry a FILTER the native layer decides (`datatype(?v) = xsd:integer
&& ?v < N` and `isIRI(?x)`), 26.7 ms → 3.3 ms in memory and 23.1 ms →
6.5 ms file-backed; `optional-wide` 42.7 ms → 36.2 ms in memory.

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
probed instead, with a count capped at one row
([`_probe_exists`](../src/vortex_rdflib/pushdown.py#L1500):
`count_quads(..., limit=1)`, the probes that scan batched through
`count_quads_many`); a body without shared variables is a global existence
test, solved under a one-row limit. rdflib pulls the body's own `FILTER`s
out of the parse tree and never simplifies the translated body, so the
conjunct's variables are read from the translated body and an empty-`BGP`
`Join` is looked through.

**Why it is faster.** rdflib evaluates the body once per row (re-entering
the BGP hook each time). `FILTER NOT EXISTS { ?s <q> ?x }` over a 1,516-row
scan: 126 ms → 5.5 ms. *0.11:* the benchmark's `not-exists` (7,576 rows
in, 3,788 out) 19.1 ms → 16.1 ms in memory and 21.5 ms → 18.0 ms
file-backed.

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
47 ms → 6.6 ms. *0.11:* that `order-limit`, its FILTER now a keep on the
scan, 23.3 ms → 16.0 ms in memory and 30.1 ms → 15.1 ms file-backed;
`order-var`, which ranks 7,576 literals with rdflib's comparator, 63.6 ms →
58.0 ms.

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

**In code space.** Every constant of the table is encoded in one
`TermDict.encode_many` call
([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1333)) and becomes a
code, `UNDEF` an unbound variable; the table joins like any relation. The
encode is tolerant of spelling: the native layer parses each term with its
pattern parser and canonicalizes it, as it does a pattern's constants, so
the spelling sent
([`terms.canonical_spelling`](../src/vortex_rdflib/terms.py#L127): the
store's escapes, lowercase language tags, no `^^xsd:string`) is never
matched byte for byte. A constant the dictionary does not hold gets a
private negative code (it joins nothing but is still yielded verbatim) with
no further check: a term the encode misses is one no pattern could match.

**Why it is faster.** rdflib joins a `VALUES` table lazily, one `triples()`
call per row. Sixty-four subjects joined to a predicate scan: 3.8 ms →
2.1 ms. *0.11:* `values-64` stays at about 1 ms on every store — one
`encode_many` replaces 64 lookups, but its 64 probes on a bound subject were
point lookups already.

**Steps aside.** A constant whose rdflib object is not the decoded
dictionary term — a query literal rdflib's parser leaves unnormalized,
`"042"^^xsd:integer` — is left to rdflib, whose rows would carry the query's
own object; a `VALUES` variable that an `OPTIONAL` re-checks falls back as
described above.

## Named graphs

**Shape.** Not an algebra node of its own: every pattern above carries the
graph the query is active in.
[`_pattern_terms`](../src/vortex_rdflib/pushdown.py#L2580) builds a **quad**
pattern, whose fourth position is
[`VortexRdflibStore._graph_n3(ctx.graph)`](../src/vortex_rdflib/store.py#L224)
— `None` (the wildcard over every graph) for a union default graph, `""` for
the default graph of a `Dataset` without union, the graph's own name inside
a `GRAPH` block. Every `match_codes` and `count_quads` in this document
takes that position, so a pushed-down block reads exactly the rows rdflib's
own evaluator would have asked `ctx.graph` for, and an absent graph selects
nothing without materializing a row.

```sparql
SELECT ?g ?s WHERE { GRAPH ?g { ?s <p> ?o } }
```

**In code space.** A `Graph` node is a block node like any other: it does
not match anything itself, it sets the scope of the block below it
([`_solve_block`](../src/vortex_rdflib/pushdown.py#L1250) recurses into
`node.p` with the new scope, so a nested `GRAPH` simply wins). A bound name
becomes the constant above. An **unbound variable becomes a column**: the
pattern is matched with the graph wildcard and `?g` takes position 3 in
`varpos`, which makes it an ordinary variable of the relation — joined,
filtered, grouped, ordered and decoded like any other, out of the fourth
column the native match already returns. Two patterns under the same
`GRAPH ?g` therefore join on `?g`, which is exactly the requirement that
they come from the same graph.

`GRAPH` ranges over the *named* graphs, so the default graph's rows are
dropped from a variable-scoped match. There is no "any named graph" native
pattern, but the default graph's empty name sorts before every term — its
code is 0 — so the named graphs are the code range above it, and
[`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L2623) narrows
the wildcard match by that range, a native `keep` on the graph position:
the default graph's rows never cross the FFI, and its empty name, which is
no RDF term, never reaches a `FILTER` on `?g`.

**Why it is faster.** rdflib's `evalGraph` walks the dataset's graphs and
evaluates the block once per graph, joining `{?g: <name>}` onto every
solution in Python. One match replaces all of it. Over the dashboard's
250,000 quads in 8 graphs: `SELECT DISTINCT ?g` over the whole store 2,678 ms
-> 117 ms, a `COUNT(*)` per graph 1,593 ms -> 148 ms, a predicate scan under
`GRAPH ?g` 87 ms -> 28 ms. A *bound* graph gains too, because the head above
it is now intercepted at the same time as the block: a scan inside one named
graph 13.3 ms -> 5.6 ms. *0.11:* with the default graph cut inside the
match, `graph-names` (`SELECT DISTINCT ?g`) 129 ms → 15.1 ms in memory and
132 ms → 2.2 ms file-backed, `graph-count` 160 ms → 15.5 ms and 195 ms →
2.7 ms, `graph-var` 30 ms → 20 ms in memory. A file scan evaluates the
range over whole encoded chunks; an in-memory match tests it row by row
(≈60 ns a row, over 250,000 rows here), which is why memory trails.

**Steps aside.** `FILTER (NOT) EXISTS` under a graph *variable*: rdflib
evaluates an EXISTS body against the active graph, and under a variable
there is no single active graph — the row's graph is a column — so the block
goes back to rdflib, which walks the graphs itself. A term bound to
something that cannot name a graph (a literal) is rdflib's too. And when
rdflib does evaluate a `Graph` node — in `bgp` mode, or above a block it
declined — it writes `solution.ctx.graph` back as it yields, so the
solutions of a block below it each carry their own context rather than
sharing the caller's
([`_pushed_graph`](../src/vortex_rdflib/pushdown.py#L3108)); otherwise that
write would reach the rows still to come and send an OPTIONAL's right side
to the wrong graph.

## Switches and the test oracle

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Never register the hook: rdflib's default evaluator for every operator. |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Intercept only the listed algebra nodes (`BGP,Filter,Project,...`); `bgp` is basic graph patterns only, the behaviour of the first releases. |
| `VORTEX_RDF_FILTER_FAST=0` | Route every FILTER value through rdflib's evaluator (still once per distinct value). |
| `VORTEX_RDF_NATIVE_FILTERS=0` | Decide no FILTER conjunct natively: no `filter_codes` verdicts, no `keep` constraints from a FILTER, every value on the Python routes. |
| `VORTEX_RDF_TRACE_QUERY=1` | Print the plan of every intercepted query as JSON lines on stderr, one per step — each prefixed `VORTEX_RDF_QUERY_TRACE ` and carrying `schema: vortex-rdf-query-trace-v1`, the event name and a sequence number: the head planned, each pattern counted/matched/probed/restricted, each join step and its strategy, each mid-join FILTER prune (`bgp_prune_complete`, whose input and output rows link the steps around it), each FILTER conjunct pushed into a match as a native `keep` (`bgp_native_restriction`, `bgp_tuple_specialized`), every native call, probes included (`native_call_complete`, with its keep and limit; a batch call is one `native_batch_complete` carrying its probe count), the ORDER BY ranking, the rows decoded. The lines are written once the planning they describe is done, so no timing an event reports includes the trace's own output. `VORTEX_RDF_TRACE_QUERY_ID` labels the lines of one query. Read at query time, not at construction. |

The switches are read when a
[`VortexRdflibStore`](../src/vortex_rdflib/store.py#L61) is constructed. The
default evaluator is the oracle of the test suite:
[`tests/test_pushdown.py`](../tests/test_pushdown.py) runs every query shape
(≈270) with the pushdown and without, on a file-backed and an in-memory
store, in six modes — the shipped configuration, the per-binding probe path
forced (`_PROBE_FANOUT = 0`), every probe through the native batch calls,
basic graph patterns only, the generic FILTER route and the Python FILTER
routes (no native predicate) — and over the store configurations the native
layer serves differently (a dictionary left in the file, each secondary
index), and compares the answers as multisets (as sequences for `ORDER BY`
queries with a total order).
[`tests/test_filters.py`](../tests/test_filters.py) and
[`tests/test_native_filters.py`](../tests/test_native_filters.py) are the
differential matrices for the fast FILTER route and the native predicates.

## Measuring

The dashboard's *pushdown off* rows (`vortex-rdflib (dict · in-mem · pushdown
off)` and `vortex-rdflib (dict · file · pushdown off)`) are the in-memory and
file-backed stores with `VORTEX_RDF_DISABLE_PUSHDOWN=1`, so the pushdown's own
contribution is the difference between two rows of the same residency. In-process,
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

The benchmark's query set ([`bench/queries.py`](../bench/queries.py)) has a
query per pushdown — `ask-var`, `limit-scan`, `filter-range`,
`filter-arith`, `filter-class`, `filter-probe`, `distinct`, `distinct-p`,
`count-all`, `count-distinct`, `agg-count`, `optional`, `optional-wide`,
`not-exists`, `minus`, `order-var`, `order-limit`, `values-64` and the six
`graph-*` queries — next to the lookups and joins. The CodSpeed suite
([`bench/test_codspeed.py`](../bench/test_codspeed.py)) runs the same set
under instruction counting on every pull request, with the join queries —
the anchored stars, the chain, the anchored `OPTIONAL` and `filter-probe` —
also run with the hook unregistered, so a planning regression shows as a
change in a tracked number rather than as a hunch.

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

## vortex-rdf 0.11 primitives in use

vortex-rdf 0.11 shipped every data-access primitive this section used to
plan, and vortex-rdflib requires it (`vortex-rdf>=0.11,<0.12`). Where each
one is used:

| Primitive | Used for |
| --- | --- |
| `count_quads_many` / `match_codes_many` | The planning counts of the patterns that scan ([`_count_patterns`](../src/vortex_rdflib/pushdown.py#L2014)); the scanning probes of probe joins, `OPTIONAL` probes and `EXISTS` probes, up to 4,096 per call ([`_run_probes`](../src/vortex_rdflib/pushdown.py#L2132)). Point probes and too few scans stay plain calls ([`_batched_probes`](../src/vortex_rdflib/pushdown.py#L2117)). |
| `TermDict.filter_codes(kind, arg)` | The native FILTER predicates ([`native_verdicts`](../src/vortex_rdflib/filters.py#L922)), memoized per store; their undecided codes are all the Python routes still evaluate. |
| `keep=` on `match_codes` / `count_quads` | FILTER conjuncts inside the match ([`native_restriction`](../src/vortex_rdflib/filters.py#L968)), tuple conjuncts specialized mid-join, the default graph cut from `GRAPH ?g` ([`_exclude_default_graph`](../src/vortex_rdflib/pushdown.py#L2623)); exact `ASK` and `COUNT` answers. |
| `match_codes(limit=)` / `count_quads(limit=)` | `LIMIT` over a single pattern ([`_limit_reaches_match`](../src/vortex_rdflib/pushdown.py#L2216)), `ASK`, `EXISTS` probes. `offset` is unused: a slice's offset still applies to the code rows. |
| Tolerant `TermDict.encode` / `encode_many` | `VALUES` tables, in one call and without the `count_quads` guard ([`_constant_codes`](../src/vortex_rdflib/pushdown.py#L1333)). |
| `TermDict.prefix_range` / `lower_bound` | The kind bounds ([`kind_bounds`](../src/vortex_rdflib/terms.py#L146)) and the kind tests as code ranges. |
| `U32Column.distinct` / `value_counts` | `DISTINCT` over one variable ([`_distinct_codes`](../src/vortex_rdflib/pushdown.py#L2695)), `GROUP BY` counts ([`_columnar_groups`](../src/vortex_rdflib/pushdown.py#L1117)). |
| `U32Column.join_indices` / `take` | The columnar equi-join ([`_join_columns`](../src/vortex_rdflib/pushdown.py#L2829)), a group's rows for `COUNT(DISTINCT)`, intersecting code-set keeps ([`intersect_keeps`](../src/vortex_rdflib/filters.py#L1003)). |
| `term_dict()` over a file-backed dictionary | The code path, and with it the pushdown, past the residency budget: a dictionary over it is read from the file on demand, and the budget trades memory for decode speed instead of switching the pushdown off. |

The benchmark's query set before and after the rework, in milliseconds
(*0.11* conditions: 250,000 quads in 8 graphs, best of at least 5 prepared
runs over interleaved rounds; "secondary indexes" is the file-backed store
built with `secondary-by-copy`). On this 4-vCPU machine, differences under
about 10% on the sub-millisecond queries are within the run-to-run noise
(repeated runs flip their sign). Instruction counts (callgrind) are
steadier: on those queries this version executes 1–4% more instructions per
run — in memory mostly inside vortex-rdf 0.11's scans, which cost 1.4–2.3%
more than 0.10's for the same calls; on the indexed store, whose joins take
≈0.15 ms, in the batching and native-filter checks those plans cannot
profit from.

| query | rows | in memory | file-backed | file-backed, secondary indexes |
| --- | ---: | ---: | ---: | ---: |
| `ask-spo` | 1 | 0.030 → 0.031 (0.96x) | 0.469 → 0.457 (1.02x) | 0.481 → 0.410 (1.17x) |
| `po-lookup` | 1 | 0.393 → 0.301 (1.31x) | 0.580 → 0.649 (0.89x) | 0.076 → 0.071 (1.07x) |
| `o-scan` | 2 | 0.226 → 0.210 (1.08x) | 0.798 → 0.732 (1.09x) | 0.079 → 0.061 (1.30x) |
| `p-scan` | 7,576 | 31.8 → 18.3 (1.74x) | 32.8 → 19.4 (1.69x) | 28.5 → 19.9 (1.43x) |
| `ask-var` | 1 | 0.171 → 0.166 (1.03x) | 0.257 → 0.163 (1.58x) | 0.029 → 0.028 (1.02x) |
| `limit-scan` | 10 | 0.855 → 0.084 (10.19x) | 1.30 → 0.104 (12.47x) | 1.42 → 1.39 (1.02x) |
| `star-2` | 1 | 0.983 → 0.814 (1.21x) | 1.36 → 1.19 (1.14x) | 0.139 → 0.139 (1.00x) |
| `star-3` | 1 | 1.21 → 0.872 (1.38x) | 1.61 → 1.32 (1.22x) | 0.153 → 0.187 (0.82x) |
| `chain-2` | 908 | 7.60 → 4.25 (1.79x) | 7.81 → 6.26 (1.25x) | 8.85 → 7.87 (1.12x) |
| `optional` | 1 | 1.29 → 0.983 (1.31x) | 1.80 → 1.59 (1.13x) | 0.178 → 0.227 (0.79x) |
| `filter-probe` | 1 | 1.12 → 0.877 (1.27x) | 1.95 → 1.77 (1.10x) | 0.212 → 0.211 (1.01x) |
| `filter-join-limit` | 10 | 3.50 → 1.50 (2.33x) | 5.34 → 3.09 (1.73x) | 5.85 → 4.32 (1.36x) |
| `filter-band-probe` | 3,770 | 72.3 → 18.3 (3.95x) | 77.3 → 24.2 (3.20x) | 68.7 → 21.5 (3.20x) |
| `values-64` | 64 | 1.03 → 0.994 (1.04x) | 1.62 → 1.54 (1.05x) | 1.09 → 1.05 (1.04x) |
| `optional-wide` | 7,576 | 42.7 → 36.2 (1.18x) | 45.9 → 38.4 (1.20x) | 46.5 → 41.2 (1.13x) |
| `not-exists` | 3,788 | 19.1 → 16.1 (1.19x) | 21.5 → 18.0 (1.20x) | 24.1 → 18.2 (1.33x) |
| `minus` | 47 | 26.7 → 3.26 (8.19x) | 23.1 → 6.52 (3.55x) | 25.6 → 7.69 (3.33x) |
| `filter-range` | 189 | 17.5 → 1.18 (14.84x) | 15.7 → 2.76 (5.70x) | 20.0 → 3.44 (5.81x) |
| `filter-arith` | 377 | 32.8 → 2.13 (15.41x) | 33.2 → 3.80 (8.74x) | 34.3 → 4.31 (7.95x) |
| `filter-class` | 4,544 | 21.3 → 11.9 (1.79x) | 22.9 → 15.6 (1.47x) | 23.1 → 13.0 (1.78x) |
| `distinct` | 7,576 | 25.5 → 18.1 (1.41x) | 27.3 → 23.5 (1.16x) | 28.0 → 18.6 (1.51x) |
| `order-limit` | 10 | 23.3 → 16.0 (1.46x) | 30.1 → 15.1 (2.00x) | 25.3 → 15.2 (1.67x) |
| `order-var` | 7,576 | 63.6 → 58.0 (1.10x) | 75.6 → 72.3 (1.05x) | 65.9 → 60.4 (1.09x) |
| `agg-count` | 33 | 10.1 → 1.92 (5.27x) | 13.0 → 3.17 (4.09x) | 11.3 → 2.51 (4.52x) |
| `distinct-p` | 33 | 5.66 → 0.885 (6.40x) | 7.32 → 1.79 (4.08x) | 7.99 → 1.45 (5.49x) |
| `count-all` | 1 | 0.079 → 0.067 (1.19x) | 0.076 → 0.084 (0.90x) | 0.082 → 0.072 (1.14x) |
| `count-distinct` | 33 | 276 → 38.1 (7.25x) | 306 → 45.3 (6.75x) | 304 → 47.5 (6.40x) |
| `graph-scan` | 947 | 5.01 → 3.14 (1.60x) | 5.21 → 3.53 (1.47x) | 6.07 → 4.89 (1.24x) |
| `graph-star` | 474 | 5.32 → 3.58 (1.49x) | 7.13 → 4.73 (1.51x) | 9.14 → 7.68 (1.19x) |
| `graph-chain` | 114 | 4.90 → 3.76 (1.30x) | 6.15 → 5.00 (1.23x) | 7.23 → 6.46 (1.12x) |
| `graph-var` | 6,629 | 29.8 → 19.8 (1.51x) | 31.5 → 20.3 (1.55x) | 31.4 → 20.9 (1.50x) |
| `graph-names` | 7 | 129 → 15.1 (8.57x) | 132 → 2.22 (59.78x) | 154 → 2.53 (60.86x) |
| `graph-count` | 7 | 160 → 15.5 (10.34x) | 195 → 2.69 (72.38x) | 159 → 2.87 (55.65x) |
| **geometric mean** | | **2.34x** | **2.29x** | **2.03x** |

## Further improvements

What the measurements above leave on the table, by where the change
belongs.

### In vortex-rdflib

- **The literal alias pass, natively.** The alias pass still looks every
  distinct literal code of a query up in Python — `count-distinct` meets
  ≈50,000 of them — though for most stores none aliases. Knowing that once
  per store (no typed literal of the dictionary spelled other than rdflib
  would spell its value) would skip the pass altogether; value keys from
  vortex-rdf (below) would make it a column operation.
- **Rows through rdflib.** Row-heavy queries are now mostly rdflib's own
  per-row objects: `p-scan` spends ≈2.4 µs a row, more than half of it in
  rdflib's `ResultRow` construction around our `FrozenBindings`, and
  `optional-wide`, `distinct`, `order-var` and `graph-var` likewise. A
  store-level API returning decoded tuples (or the code columns) for a query
  would bypass it for callers that do not need rdflib's result objects.
- **Literal ranking for `ORDER BY`.** Ranking still decodes every distinct
  literal and sorts with rdflib's comparator (`order-var`: 58 ms for 7,576
  literals). Numeric datatypes could be ranked by a value key computed from
  the parsed spelling, keeping rdflib's comparator for the mixed and
  ill-typed rest.
- **More FILTER shapes as keeps.** `?v IN (...)`, `sameTerm(?v, <iri>)` and
  `?v = <iri>` are code sets one `encode_many` gives exactly (with numeric
  `=` left to `num_eq`); a `regex(str(?v), "^abc")` whose pattern is a
  plain anchored prefix is `strstarts`; and `strstarts(str(?v), ...)` over
  IRIs is a `prefix_range`, no dictionary scan.
- **`LIMIT` and `ASK` through joins.** The limit reaches only
  single-pattern matches; a probe join could stop probing once it has
  produced enough rows, which `filter-join-limit` and an `ASK` over a join
  would feel.
- **The probe threshold.** [`_PROBE_FANOUT`](../src/vortex_rdflib/pushdown.py#L1804)
  (100) was calibrated for one native call per probe. Batched scanning
  probes are cheaper per probe, so the probe/match crossover deserves a
  fresh sweep, per store kind.
- **Decode batches over a file-backed dictionary.** Each `decode_many`
  against a dictionary left in the file costs ≈1 ms whatever its size, and
  the yield decodes in chunks growing from 64 rows; starting at a larger
  chunk when the dictionary is file-backed would pay that cost fewer times.

### In vortex-rdf

- **The `list_contains` keep path.** A file scan evaluates a keep of 33 to
  4,096 codes as one `list_contains` expression, slower than the scan it
  narrows (2,000 codes: ≈25x an unnarrowed match); a hash-set or bitmap
  test, as the larger sets get in memory, would let
  [`_split_keeps`](../src/vortex_rdflib/pushdown.py#L2060) go.
- **In-memory keeps and counts, vectorized.** An in-memory match tests its
  keep row by row (≈60 ns a row): `graph-names` takes 15.1 ms in memory and
  2.2 ms file-backed. A columnar pass over the resident arrays would close
  that gap and make narrowed counts cheap enough for planning.
- **Small batches.** A batch call costs 60–150 µs before its first probe
  runs — `count_quads_many([p])` takes 60–150 µs more than `count_quads(*p)` —
  which is why two in-memory scans are not batched. Running a small batch on
  the calling thread, or keeping the runtime's workers warm for a moment,
  would make every batch worth issuing.
- **Code-valued patterns.** A probe binds codes the relation already holds,
  yet goes to the native layer as N-Triples spellings, decoded for the
  purpose and parsed and encoded back natively. Accepting codes in place of
  spellings in `match_codes`, `count_quads` and the batch calls would remove
  the round trip.
- **`xsd:long` and `xsd:unsignedLong` bounds.** `filter_codes` rejects
  their values beyond 64 bits, as XSD does, where rdflib bounds neither;
  vortex-rdflib re-checks them with two extra `filter_codes` calls and a
  join per numeric predicate
  ([`wide_integer_codes`](../src/vortex_rdflib/filters.py#L945)). Reporting
  them as undecided would make that unnecessary.
- **Keeps beyond one range or set.** Several ranges per position (a union)
  and complements would turn `isIRI(?v) || isBlank(?v)`, `!isLiteral(?v)`,
  `?v != 5` and `lang(?v) != "en"` into keeps too.
- **Column projection.** `match_codes` returns all four columns; a pattern
  rarely needs more than its variables'. Asking for those alone would save
  materializing the rest.
- **Multi-column kernels.** `distinct` and `value_counts` over several
  columns at once, and a semi-join (`isin`) that answers membership without
  producing index pairs, for `DISTINCT` over several variables, `GROUP BY`
  several keys, `EXISTS` and `MINUS`.
- **Value keys for typed literals.** A canonical value key per literal code
  (the number, the instant) would rank `ORDER BY` natively and find
  `"042"^^xsd:integer` and `"42"^^xsd:integer` equal without decoding
  either, the alias pass included.
- **The in-memory scan itself.** For the same calls, 0.11's in-memory
  `count_quads` and `match_codes` execute 1.4–2.3% more instructions than
  0.10's (`count_quads` of a predicate–object pattern over 250,000 quads:
  3.77 M → 3.86 M), while a bound-subject lookup got 3% cheaper.
