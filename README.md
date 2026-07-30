# vortex-rdflib

[![CI](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml/badge.svg)](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml)
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
from Python — it enables the code path and BGP pushdown described below.

## How it stays fast

**Term codes instead of strings.** For Dictionary-layout stores, matched rows
cross the native boundary as zero-copy `u32` term-code columns
(`vortex_rdf.VortexRdfStore.match_codes`); `VortexStore.triples()` decodes
each distinct code to an rdflib term once and caches it for the store's
lifetime. Other layouts fall back to a de-duplicated N-Triples term table
(`match_compact`). Set `VORTEX_RDF_DISABLE_CODE_PATH=1` to force the string
path.

**SPARQL BGP pushdown.** Constructing a `VortexStore` registers an rdflib
`CUSTOM_EVALS` hook that evaluates whole basic graph patterns in one pass:
each triple pattern is matched natively once and the join runs as hash joins
over `u32` codes, decoding terms only for the final solutions. This replaces
rdflib's default nested-loop evaluation (one `triples()` call per candidate
binding) and is what makes joins fast — measured ~6x on an in-memory store
and ~50x on a file-backed one, 3x faster than rdflib's own in-memory store.
The hook only fires for VortexStore graphs with the code path available and
falls back to the default evaluator otherwise (other stores, RDF-star
patterns, non-BGP algebra). Set `VORTEX_RDF_DISABLE_PUSHDOWN=1` to keep the
default evaluator.

**File-backed vs in-memory.** The default open is lazy and file-backed.
`VortexStore(path, in_memory=True)` (or env `VORTEX_RDF_IN_MEMORY=1`) loads
the store into memory once: each `triples()` call then skips the per-call
file-scan pipeline (~1 ms → ~0.15 ms per call), which is decisive for SPARQL
joins evaluated by per-binding probing.

For Dictionary-layout files, the term dictionary is held in memory when it
fits the residency budget; pass `VortexStore(path, max_resident_terms=...)`
to raise the budget (recommended for large stores).

## Environment variables

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_IN_MEMORY=1` | Load stores into memory instead of file-backed lazy open |
| `VORTEX_RDF_DISABLE_CODE_PATH=1` | Force the N-Triples string path instead of `u32` codes |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Keep rdflib's default BGP evaluator |
| `VORTEX_RDF_TRACE_TRIPLES=1` | Print every `triples()` pattern (debugging) |

## Benchmarks

A comparative benchmark — `VortexStore` vs rdflib's in-memory `Memory` store
vs [oxrdflib](https://github.com/oxigraph/oxrdflib) (Oxigraph) — runs on every
push to `main` and publishes a dashboard to GitHub Pages:
<https://vortex-rdf.github.io/vortex-rdflib/>.

It executes a synthetic representative SPARQL set (lookups/scans, star and
chain joins, FILTER/DISTINCT/ORDER BY/GROUP BY) and records per-store peak
RSS; each store's full lifecycle runs in its own process. SPARQL evaluation
is rdflib's engine for every store, with two labeled reference rows: Vortex
with the BGP pushdown, and Oxigraph's native SPARQL engine.

Run it locally (defaults to the full 250k-triple CI scale; scale down with
`BENCH_TRIPLES`):

```bash
uv sync --group bench
BENCH_TRIPLES=20000 uv run python -m bench.run_bench --out bench/results.json
uv run python scripts/render_bench_dashboard.py bench/results.json public/index.html
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
3. Commit, then push a matching `v<version>` tag.

The full CI matrix runs on the tagged commit and must pass before anything is
built, and the workflow refuses to publish if the tag and `pyproject.toml`
disagree.

## License

MIT — see [LICENSE](LICENSE).
