# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.1...v0.1.2) - 2026-09-08

### Added

- Add opt-in SPARQL pushdown diagnostics ([`f961b87`](https://github.com/vortex-rdf/vortex-rdflib/commit/f961b8788557391c48eef4bbafe0d25ebd5a1dc2) by @fothot2)
- Add pattern-level BGP query diagnostics ([`6c36662`](https://github.com/vortex-rdf/vortex-rdflib/commit/6c36662496b716efb178265ec6c3f0ceda3bfaa5) by @fothot2)

### Changed

- Defer BGP filters to selective probe joins ([`5092f73`](https://github.com/vortex-rdf/vortex-rdflib/commit/5092f73fdddf002a7daa2160c1f3410929f36057) by @fothot2)
- Plan BGP joins through incremental native probes ([`14940cb`](https://github.com/vortex-rdf/vortex-rdflib/commit/14940cbcfa9813476037f6c3b35d8d1fb971e087) by @fothot2)
- Plan optional probes before matching right patterns ([`9721a50`](https://github.com/vortex-rdf/vortex-rdflib/commit/9721a509dedef55939e4d766153a7c48a966be29) by @fothot2)
- Plan BGP and OPTIONAL joins from native counts (pushdown) ([`6871a2f`](https://github.com/vortex-rdf/vortex-rdflib/commit/6871a2f8568c9e0a39a02abd6035e914f6348415) by @julianrojas87)

## [0.1.1](https://github.com/vortex-rdf/vortex-rdflib/compare/v0.1.0...v0.1.1) - 2026-09-03

### Changed

- Compile integer arithmetic in FILTER pushdown ([`4a427a6`](https://github.com/vortex-rdf/vortex-rdflib/commit/4a427a6f37350ed797dd2afadaa2a9b181505d6f) by @fothot2)

### Fixed

- Defer a comparison whose operand rdflib may raise on (filters) ([`7aca867`](https://github.com/vortex-rdf/vortex-rdflib/commit/7aca8679c211328a5ed9fc9bb90d742c61d71972) by @julianrojas87)

## [0.1.0] - 2026-09-01

First standalone release. The rdflib integration previously lived in the
[vortex-rdf](https://github.com/vortex-rdf/vortex-rdf) repository under
`python/`; it is now a pure-Python package built on the published
[vortex-rdf](https://pypi.org/project/vortex-rdf/) binding layer (0.10.x).

### Added

- `VortexRdflibStore`: a read-only rdflib `Store` over `.vortex` files,
  file-backed and lazily opened by default, or loaded into memory with
  `in_memory=True`.
- Named graphs: the store is context-aware, so a `.vortex` file's quads can be
  read through an rdflib `Dataset` — `contexts()`, per-graph `len()` and
  `triples()`, `quads()` carrying each row's graph — and `GRAPH` works in
  SPARQL. A `Graph` with no identifier of its own stays the view over the
  whole file, so the triple-oriented usage is unchanged.
- `GRAPH` pushdown: a named graph is the fourth position of every native match
  below it, and `GRAPH ?g` binds the graph from the match's fourth column, so
  the graph is an ordinary variable of the code-space relation — one match
  instead of rdflib's walk over the dataset's graphs. At 250k quads over 8
  graphs: `DISTINCT ?g` 2,678 ms -> 117 ms, `COUNT(*)` per graph
  1,593 ms -> 148 ms, a predicate scan under `GRAPH ?g` 87 ms -> 28 ms.
- Dictionary-layout code path: matched rows arrive as zero-copy `u32`
  term-code columns and each distinct code is decoded to an rdflib term once,
  with a string-table fallback for other layouts.
- SPARQL pushdown: whole basic graph patterns are evaluated in one pass as
  hash joins over `u32` term codes; a `FILTER` over a pattern is evaluated
  once per distinct value — a whitelist of expression shapes as predicates
  over the stored spellings, everything else through rdflib's own evaluator —
  with single-variable conditions applied to the pattern scans before the
  join; `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS`, nested groups and inline
  `VALUES` tables run as hash left joins, anti-joins, semi-joins and joins
  over code tuples; the
  projection, `DISTINCT`, `ORDER BY` on variables,
  `LIMIT`/`OFFSET`, `ASK` and `COUNT` aggregates (with or without
  `GROUP BY`) above a block are answered on the same code-space result, and
  solutions are decoded lazily in chunks, only for the rows and variables
  actually consumed. Registered
  automatically; disable with `VORTEX_RDF_DISABLE_PUSHDOWN=1`, narrow with
  `VORTEX_RDF_PUSHDOWN_OPS`, force the generic FILTER route with
  `VORTEX_RDF_FILTER_FAST=0`. Described in `docs/pushdown.md`.
- Benchmark: a "pushdown off" row and nine operator-shaped queries
  (`ask-var`, `limit-scan`, `filter-class`, `distinct-p`, `count-all`,
  `count-distinct`, `optional-wide`, `not-exists`, `minus`, `order-var`,
  `values-64`) on the dashboard.
- Benchmark: the dataset is quads, partitioned by subject over eight graphs
  (`BENCH_GRAPHS`), and the quad-capable stores are compared as rdflib
  `Dataset`s over their union — so every existing query keeps its row counts —
  with a `graphs` panel for the shapes that name one (`graph-scan`,
  `graph-star`, `graph-chain`, `graph-var`, `graph-names`, `graph-count`).
  HDT and COTTAS do not serve named graphs through rdflib, so they load the
  flattened N-Triples of the same statements and are not asked that group.
- `py.typed` marker.

### Fixed

- A `FILTER (NOT) EXISTS` in an `OPTIONAL`'s group was hoisted into the
  `LeftJoin` condition and then dropped, so the optional side could bind where
  it should have stayed unbound. That shape is now left to rdflib.
- Benchmark generator: the graph count is nudged coprime with the predicate
  count. A shared factor pinned a predicate's rows to one graph across a whole
  block, which left the graph-scoped chain query with no rows to derive its
  constants from at some dataset sizes — including the CodSpeed suite's
  default, whose query set is built at import.
