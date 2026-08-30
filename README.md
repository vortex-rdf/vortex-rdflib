# vortex-rdflib

[![CI](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml/badge.svg)](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/vortex-rdf/vortex-rdflib?utm_source=badge)
[![PyPI](https://img.shields.io/pypi/v/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![Python versions](https://img.shields.io/pypi/pyversions/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [rdflib](https://rdflib.readthedocs.io/) `Store` implementation for
[Vortex-RDF](https://github.com/vortex-rdf/vortex-rdf), a columnar zero-copy RDF serialization format — so `.vortex` files can be queried with SPARQL.

The native layer is the [vortex-rdf](https://pypi.org/project/vortex-rdf/) package (PyO3 bindings over the `vortex-rdf-core` Rust crate), pulled in as a dependency: stores are **opened lazily from `.vortex` files** by default, and queried in place without loading the dataset into memory. A `.vortex` file can also be fully loaded in memory if desired, with exactly the same data structure.

## Install

```bash
pip install vortex-rdflib
```

Python 3.11+. The `vortex-rdf` dependency ships prebuilt wheels for Linux
(x86_64, aarch64), macOS (x86_64, arm64) and Windows (x64); on other
platforms it builds from source.

## Usage

```python
from rdflib import Graph
from vortex_rdflib import VortexRdflibStore

graph = Graph(store=VortexRdflibStore("data.vortex"))
for row in graph.query("""
    SELECT ?s ?o WHERE { 
        ?s <http://xmlns.com/foaf/0.1/name> ?o 
    } 
    LIMIT 10
"""):
    print(row.s, row.o)
```

SPARQL evaluation is rdflib's engine; the store serves quad patterns from
the Vortex file. The store is read-only so far. Support for mutations is in the roadmap.

### Named graphs

A `.vortex` file holds quads, so the store is context-aware. A `Dataset` gives
the named graphs, and `GRAPH` works in SPARQL:

```python
from rdflib import Dataset
from vortex_rdflib import VortexRdflibStore

# default_union=True makes the SPARQL default graph the union of every graph
dataset = Dataset(store=VortexRdflibStore("data.vortex"), default_union=True)

for graph in dataset.graphs():
    print(graph.identifier, len(graph))

for row in dataset.query("""
    SELECT ?g (COUNT(*) AS ?n) WHERE {
        GRAPH ?g { ?s ?p ?o }
    }
    GROUP BY ?g
"""):
    print(row.g, row.n)
```

A plain `Graph(store=VortexRdflibStore(path))` — as in the example above — is
the view over the **whole file**, every graph included: rdflib names a graph
constructed without an identifier with a blank node, which names nothing the
file holds. It is a multiset view, so a triple in two graphs is yielded twice;
the store streams the quads it holds rather than building the RDF merge, which
would cost a set the size of the result. Give the graph an identifier
(`Graph(store=store, identifier=URIRef(...))`) to see one named graph, and note
the corollary: a *blank-node* graph name is reachable only through a `Dataset`.

To produce a `.vortex` file from an RDF file, use the binding layer directly
(or the [vortex-rdf CLI](https://github.com/vortex-rdf/vortex-rdf)):

```python
from vortex_rdf import serialize_rdf

serialize_rdf("data.nq", "data.vortex", format="nquads", layout="dictionary")
```

`layout` accepts `"default"`, `"typed-object"` and `"dictionary"` (see a [description here](https://github.com/vortex-rdf/vortex-rdf/blob/main/docs/file-format.md#4-the-quad-table)); opening
auto-detects the layout. The `"dictionary"` layout is the fastest to query
from Python — it enables the SPARQL pushdowns described below.

## How it works

**Term codes instead of strings.** For Dictionary-layout stores, matched rows
cross the native boundary as zero-copy `u32` term-code columns
(`vortex_rdf.VortexRdfStore.match_codes`), and each distinct code is decoded
to an rdflib term once — in one GIL-released `TermDict.decode_many` call per
batch — and cached for the store's lifetime. Other layouts fall back to
N-Triples string columns, parsing each distinct term once.

**SPARQL pushdown.** Constructing a `VortexRdflibStore` registers an rdflib
`CUSTOM_EVALS` hook that answers the algebra operators it understands over
vortex term codes instead of leaving them to rdflib's per-row evaluation: basic
graph patterns, `FILTER`, `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS`, nested groups
and `VALUES`, projection, `DISTINCT`, `ORDER BY`, `LIMIT`/`OFFSET`,
`ASK` and `COUNT` aggregates above them. Anything else is evaluated by rdflib. A more detailed description is available in [docs/pushdown.md](docs/pushdown.md); the switches to disable or narrow it are in the table below.

**File-backed vs in-memory.** The default open is lazy and file-backed.
`VortexRdflibStore(path, in_memory=True)` (or env `VORTEX_RDF_IN_MEMORY=1`) loads
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
fits the residency budget; pass `VortexRdflibStore(path, max_resident_bytes=...)`
(the dictionary's compressed size in bytes) to raise the budget
(recommended for large stores).

## Environment variables

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_IN_MEMORY=1` | Load stores into memory instead of file-backed lazy open |
| `VORTEX_RDF_DISABLE_CODE_PATH=1` | Force the N-Triples string path instead of `u32` codes |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Keep rdflib's default evaluator for every operator (see [docs/pushdown.md](docs/pushdown.md)) |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only push down the listed algebra nodes (`bgp` = basic graph patterns only) |
| `VORTEX_RDF_FILTER_FAST=0` | Evaluate every FILTER value through rdflib's expression evaluator (still once per distinct value) |
| `VORTEX_RDF_TRACE_TRIPLES=1` | Print every `triples()` pattern (debugging) |

## Benchmarks

A comparative benchmark — `VortexRdflibStore` against rdflib's in-memory `Memory`
store, [oxrdflib](https://github.com/oxigraph/oxrdflib) (Oxigraph),
[pycottas](https://github.com/cottas-rdf/pycottas) (COTTAS) and
[rdflib-hdt](https://pypi.org/project/rdflib-hdt/) (HDT) — runs on every
push to `main` and publishes the current numbers to GitHub Pages:
**<https://vortex-rdf.github.io/vortex-rdflib/>**. That dashboard is the
reference for how these variants actually compare; timings vary with machine
and dataset, so this README deliberately quotes none.

It executes a synthetic representative SPARQL set (lookups/scans, star and
chain joins, FILTER/DISTINCT/ORDER BY/GROUP BY, and the shapes that name a
graph) and records per-store peak RSS; each store's full lifecycle runs in its
own process. SPARQL evaluation is rdflib's engine for every store, so the
store serving quad patterns is the only variable — a store's own SPARQL engine
is out of scope, since it skips rdflib's parse and algebra and is not
measuring the same work.

The dataset is quads: every statement about a subject goes into one graph, so
the union of the graphs is exactly the triple set, and each store loads it as
an rdflib `Dataset` whose default graph is that union. HDT and COTTAS cannot
serve named graphs through rdflib — HDT's format has none, and pycottas'
`COTTASStore` does not expose the ones COTTAS files can hold — so those two
rows load the flattened N-Triples, the same statements, and are not asked the
`graphs` group, whose cells stay empty for them.

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
primary configuration (Dictionary layout, in-memory, pushdown on) and
each other axis is isolated on the queries where it can move the number —
the pushdown A/B on the join queries, file-backed opens and secondary
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
