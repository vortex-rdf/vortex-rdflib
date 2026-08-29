# vortex-rdflib

[![CI](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml/badge.svg)](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/vortex-rdf/vortex-rdflib?utm_source=badge)
[![PyPI](https://img.shields.io/pypi/v/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![Python versions](https://img.shields.io/pypi/pyversions/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [rdflib](https://rdflib.readthedocs.io/) `Store` implementation for
[Vortex-RDF](https://github.com/vortex-rdf/vortex-rdf), a modern,
high-performance columnar RDF serialization format — so `.vortex` files can
be queried with SPARQL.

This package is pure Python. The native layer is the
[vortex-rdf](https://pypi.org/project/vortex-rdf/) package (PyO3 bindings
over the `vortex-rdf-core` Rust crate), pulled in as a dependency: stores are
**opened lazily from `.vortex` files** and queried in place, without loading
the dataset into memory.

## Install

```bash
pip install vortex-rdflib
```

Python 3.11+. The `vortex-rdf` dependency ships prebuilt wheels for Linux
(x86_64, aarch64), macOS (x86_64, arm64) and Windows (x64); on other
platforms it builds from source, which needs a Rust toolchain.

## Usage

```python
from rdflib import Graph
from vortex_rdflib import VortexStore

graph = Graph(store=VortexStore("data.vortex"))
for row in graph.query("""
    SELECT ?s ?o WHERE { ?s <http://xmlns.com/foaf/0.1/name> ?o } LIMIT 10
"""):
    print(row.s, row.o)
```

SPARQL evaluation is rdflib's engine; the store serves triple patterns from
the Vortex file. The store is read-only.

To produce a `.vortex` file from an RDF file, use the binding layer directly
(or the [vortex-rdf CLI](https://github.com/vortex-rdf/vortex-rdf)):

```python
from vortex_rdf import serialize_rdf

serialize_rdf("data.nt", "data.vortex", layout="dictionary")
```

`layout` accepts `"default"`, `"typed-object"` and `"dictionary"`; opening
auto-detects the layout. The `"dictionary"` layout is the fastest to query
from Python — it enables the code path and SPARQL pushdown described below.

## How it stays fast

**Term codes instead of strings.** For Dictionary-layout stores, matched rows
cross the native boundary as zero-copy `u32` term-code columns
(`vortex_rdf.VortexRdfStore.match_codes`); `VortexStore.triples()` decodes
each distinct code to an rdflib term once — all of a match's new codes in a
single GIL-released `TermDict.decode_many` call — and caches it for the
store's lifetime. Fully-ground patterns (existence checks) are answered by
`count_quads` without materializing any term. Other layouts fall back to
N-Triples string columns (`match_columns`), parsing each distinct term once.
Set `VORTEX_RDF_DISABLE_CODE_PATH=1` to force the string path.

**SPARQL pushdown.** Constructing a `VortexStore` registers an rdflib
`CUSTOM_EVALS` hook that evaluates whole basic graph patterns in one pass:
each triple pattern is matched natively once and the join runs as hash joins
over `u32` codes, decoding terms only for the final solutions. This replaces
rdflib's default nested-loop evaluation (one `triples()` call per candidate
binding). A `FILTER` over a pattern is evaluated once per distinct value
instead of once per row — a whitelist of expression shapes (numeric and
string comparisons, `datatype`, `lang`, `langMatches`, `isIRI`/`isLiteral`/
`isBlank`, `regex`, `strstarts`, ...) runs as predicates over the stored term
spellings, and anything else, or any value outside that fast path's exact
domain, is answered by rdflib's own expression evaluator — with
single-variable conditions applied to the pattern scans before the join. The
projection, `LIMIT`/`OFFSET` and `ASK` above a pattern are answered on the
same code-space result, and solutions are decoded lazily in chunks, so a
`LIMIT 10` decodes a few dozen codes and an ASK none.

The gain is concentrated where that nested loop degenerates: unanchored joins
such as a two-hop chain (`?s ?pa ?m . ?m ?pb ?o`), where rdflib would
re-enter the store once per intermediate binding. The other query shapes land
close to the default evaluator — the single-pattern ones have no join to
improve, and a star anchored on a ground term gives rdflib's nested loop the
same one-row-then-probe access pattern the pushdown itself uses. Per-query
figures are on the [benchmark
dashboard](https://vortex-rdf.github.io/vortex-rdflib/).

The hook only fires for VortexStore graphs with the code path available and
falls back node by node otherwise: an unsupported construct (another store,
RDF-star patterns, algebra the hook does not handle) is evaluated by rdflib,
which re-enters the hook for the supported subtrees below it. Set
`VORTEX_RDF_DISABLE_PUSHDOWN=1` to keep the default evaluator for everything;
that switch is what the equivalence tests use as their oracle, and the
dashboard's "pushdown off" row runs with it so the pushdown's own
contribution is visible.

**File-backed vs in-memory.** The default open is lazy and file-backed.
`VortexStore(path, in_memory=True)` (or env `VORTEX_RDF_IN_MEMORY=1`) loads
the store into memory once, so queries skip the per-call file-read pipeline.
That helps point lookups and joins, and does nothing for the scan-dominated
queries, which are bound by rdflib's own result handling.

**Secondary indexes.** `serialize_rdf(..., indexes=["secondary-by-copy"])`
(or `"secondary-by-reference"`) writes index components into the `.vortex`
file, for a modest increase in build time and file size. They pay off on
file-backed stores answering single-pattern lookups, where they largely erase
the file-backed penalty for object and predicate-object lookups. On an
in-memory store they change nothing measurable, since the rows are resident
already, and on multi-pattern joins the run-to-run spread is wider than any
effect they have. Enable them for lookup-heavy file-backed workloads; measure
before assuming they help elsewhere.

For Dictionary-layout files, the term dictionary is held in memory when it
fits the residency budget; pass `VortexStore(path, max_resident_bytes=...)`
(the dictionary's compressed size in bytes) to raise the budget
(recommended for large stores).

## Environment variables

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_IN_MEMORY=1` | Load stores into memory instead of file-backed lazy open |
| `VORTEX_RDF_DISABLE_CODE_PATH=1` | Force the N-Triples string path instead of `u32` codes |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Keep rdflib's default evaluator for every operator |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only push down the listed algebra nodes (`bgp` = basic graph patterns only) |
| `VORTEX_RDF_FILTER_FAST=0` | Evaluate every FILTER value through rdflib's expression evaluator (still once per distinct value) |
| `VORTEX_RDF_TRACE_TRIPLES=1` | Print every `triples()` pattern (debugging) |

## Benchmarks

A comparative benchmark — `VortexStore` against rdflib's in-memory `Memory`
store, [oxrdflib](https://github.com/oxigraph/oxrdflib) (Oxigraph),
[pycottas](https://github.com/cottas-rdf/pycottas) (COTTAS) and
[rdflib-hdt](https://pypi.org/project/rdflib-hdt/) (HDT) — runs on every
push to `main` and publishes the current numbers to GitHub Pages:
**<https://vortex-rdf.github.io/vortex-rdflib/>**. That dashboard is the
reference for how these variants actually compare; timings vary with machine
and dataset, so this README deliberately quotes none.

It executes a synthetic representative SPARQL set (lookups/scans, star and
chain joins, FILTER/DISTINCT/ORDER BY/GROUP BY) and records per-store peak
RSS; each store's full lifecycle runs in its own process. SPARQL evaluation
is rdflib's engine for every store, so the store serving triple patterns is
the only variable — a store's own SPARQL engine is out of scope, since it
skips rdflib's parse and algebra and is not measuring the same work.

The Vortex rows are all Dictionary layout — the layout that enables the code
path — crossed over the two axes that change how a store answers: residency
(file-backed vs in-memory) and secondary index (none, by-copy, by-reference).

`pycottas` and `rdflib-hdt` pin dependencies that cannot share the project
environment — pycottas an exact `pyoxigraph`, rdflib-hdt an exact `rdflib` —
so `run_bench` builds each a throwaway virtualenv and runs that worker with
its interpreter. rdflib-hdt reads HDT but cannot write it, so the HDT file is
built by the Rust crate's CLI, which the refresh script installs on demand.

Run it locally with `scripts/refresh.sh` — it syncs the contenders, ensures
the HDT builder, measures, and re-renders the dashboard:

```bash
scripts/refresh.sh                      # every stage, at the 250k CI scale
BENCH_TRIPLES=20000 scripts/refresh.sh  # scale down
scripts/refresh.sh --only render        # template-only edits: no re-measurement
```

### Regression tracking (CodSpeed)

The dashboard answers "how does this compare?"; it cannot answer "did this
commit make things slower?", because wall-clock numbers from a shared CI
runner move on their own. `bench/test_codspeed.py` covers that: the **same**
dataset generator and the **same** query set, measured per commit under
CodSpeed's CPU simulation so every task gets a deterministic instruction
count. Every pull request gets a report at
<https://app.codspeed.io/vortex-rdf/vortex-rdflib>, so a change that costs
instructions is visible before it lands.

Only the vortex variants are measured: another library's instruction count
moves when *it* releases, which is not a signal this repo can act on. And
since instruction counts are deterministic, the suite does not run the full
configurations × queries cross product; the whole query set runs on the
primary configuration (Dictionary layout, in-memory, BGP pushdown on) and
each other axis is isolated on the queries where it can move the number —
the BGP-pushdown A/B on the join queries, file-backed opens and secondary
indexes on the lookups they target, the `Store.triples()` service per pattern
selectivity, the u32 code path against the N-Triples string fallback, and
each residency's open cost.

The suite is not part of `uv run pytest` (which runs `tests/` only); run it
explicitly. 32,768 triples by default — small enough for Valgrind, and the
size the vortex-rdf Rust and JS suites share, so a shared-core regression
lands in every tab at comparable magnitude — override with
`CODSPEED_BENCH_TRIPLES`:

```bash
uv run pytest bench/test_codspeed.py --codspeed   # wall-clock, no instrumentation
CODSPEED_BENCH_TRIPLES=5000 uv run pytest bench/test_codspeed.py --codspeed
```

## Development

The repo is managed with [uv](https://docs.astral.sh/uv/); `uv.lock` pins the
development environment.

```bash
uv sync              # create .venv and install deps + dev group
uv run pytest
uv run ruff format   # format (check mode in CI)
uv run ruff check    # lint
uv run ty check      # type check
uv build             # sdist + wheel into dist/
```

One-time setup after cloning — install the git hooks so `git push` runs the
same checks as CI first (and commit messages follow
[Conventional Commits](https://www.conventionalcommits.org)):

```bash
./scripts/install-git-hooks.sh
```

Run the checks manually with `./scripts/ci-check.sh`; skip a hook once with
`git commit --no-verify` / `git push --no-verify`.

## Releasing

`.github/workflows/release.yml` builds the sdist and wheel and uploads them
to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) —
no API token is stored in the repository. To cut a release:

1. Bump `version` in `pyproject.toml` (and run `uv lock` to sync the lockfile).
2. Update the changelog: `scripts/update-changelog.sh v<version>` stamps the
   `[Unreleased]` section (regenerated from Conventional Commits via
   [git-cliff](https://git-cliff.org/); refresh anytime with
   `scripts/update-changelog.sh`).
3. Commit, then push a matching `vX.Y.Z` tag.

The full CI matrix runs on the tagged commit and must pass before anything is
built; the workflow refuses to publish if the tag, `pyproject.toml` and
`uv.lock` disagree on the version; and the wheel is smoke-tested against the
test suite before upload. Running the workflow by hand (Actions → Release →
Run workflow) is a dry run — build, validate, smoke-test, publish nothing —
unless the "Publish to PyPI" toggle is on, which is how to retry a release
whose publish step failed.

## License

MIT — see [LICENSE](LICENSE).
