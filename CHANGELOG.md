# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- hand-written — curated notes for the first standalone release. Cut it with
     `scripts/update-changelog.sh v0.1.0`; afterwards [Unreleased] is
     regenerated from Conventional Commits (scripts/update-changelog.sh). -->

First standalone release. The rdflib integration previously lived in the
[vortex-rdf](https://github.com/vortex-rdf/vortex-rdf) repository under
`python/`; it is now a pure-Python package built on the published
[vortex-rdf](https://pypi.org/project/vortex-rdf/) binding layer (0.5.x).

### Added

- `VortexStore`: a read-only rdflib `Store` over `.vortex` files, file-backed
  and lazily opened by default, or loaded into memory with `in_memory=True`.
- Dictionary-layout code path: matched rows arrive as zero-copy `u32`
  term-code columns and each distinct code is decoded to an rdflib term once,
  with a string-table fallback for other layouts.
- SPARQL pushdown: whole basic graph patterns are evaluated in one pass as
  hash joins over `u32` term codes; a `FILTER` over a pattern is evaluated
  once per distinct value — a whitelist of expression shapes as predicates
  over the stored spellings, everything else through rdflib's own evaluator —
  with single-variable conditions applied to the pattern scans before the
  join; `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS` and nested groups run as
  hash left joins, anti-joins, semi-joins and joins over code tuples; the
  projection, `DISTINCT`, `ORDER BY` on variables,
  `LIMIT`/`OFFSET`, `ASK` and `COUNT` aggregates (with or without
  `GROUP BY`) above a block are answered on the same code-space result, and
  solutions are decoded lazily in chunks, only for the rows and variables
  actually consumed. Registered
  automatically; disable with `VORTEX_RDF_DISABLE_PUSHDOWN=1`, narrow with
  `VORTEX_RDF_PUSHDOWN_OPS`, force the generic FILTER route with
  `VORTEX_RDF_FILTER_FAST=0`.
- Benchmark: a "pushdown off" row and nine operator-shaped queries
  (`ask-var`, `limit-scan`, `filter-class`, `distinct-p`, `count-all`,
  `count-distinct`, `optional-wide`, `not-exists`, `minus`, `order-var`)
  on the dashboard.
- `py.typed` marker.
