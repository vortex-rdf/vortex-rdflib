# vortex-rdflib

[![CI](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml/badge.svg)](https://github.com/vortex-rdf/vortex-rdflib/actions/workflows/ci.yml)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/vortex-rdf/vortex-rdflib?utm_source=badge)
[![PyPI](https://img.shields.io/pypi/v/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![Python versions](https://img.shields.io/pypi/pyversions/vortex-rdflib.svg)](https://pypi.org/project/vortex-rdflib/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An [rdflib](https://rdflib.readthedocs.io/) `Store` implementation for
[Vortex-RDF](https://github.com/vortex-rdf/vortex-rdf), a columnar
zero-copy RDF serialization format — so `.vortex` files can be queried with
SPARQL.

The native layer is the [vortex-rdf](https://pypi.org/project/vortex-rdf/)
package (PyO3 bindings over the `vortex-rdf-core` Rust crate), pulled in as
a dependency: stores are **opened lazily from `.vortex` files** by default,
and queried in place without loading the dataset into memory. However, a `.vortex`
file can also be fully loaded in memory, with exactly the same data
structure and queried.

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
the Vortex file. The store is read-only so far; mutation support is on the roadmap.

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
the view over the **whole file**, every graph included. It is a multiset
view — a triple in two graphs is yielded twice: the store streams the quads
it holds rather than building the RDF merge.

To produce a `.vortex` file from an RDF file, use the binding layer directly
(or the [vortex-rdf CLI](https://github.com/vortex-rdf/vortex-rdf)):

```python
from vortex_rdf import serialize_rdf

serialize_rdf("data.nq", "data.vortex", format="nquads", layout="dictionary")
```

`layout` accepts `"default"`, `"typed-object"` and `"dictionary"`
([described here](https://github.com/vortex-rdf/vortex-rdf/blob/main/docs/file-format.md#4-the-quad-table));
opening
auto-detects the layout. The `"dictionary"` layout is the fastest to query
from Python — it enables the SPARQL pushdowns described below.

## How it works

**Dictionary-encoded terms.** For vortex-rdf Dictionary-layout stores, matched rows cross the native boundary as zero-copy `u32` term-code columns
(`vortex_rdf.VortexRdfStore.match_codes`), and each distinct code is decoded
to an rdflib term once — in one GIL-released `TermDict.decode_many` call per
batch — and cached for the store's lifetime. Other layouts fall back to
N-Triples string columns, parsing each distinct term once.

**SPARQL pushdown.** Constructing a `VortexRdflibStore` registers an rdflib
`CUSTOM_EVALS` hook that answers the algebra operators it understands over
vortex term codes instead of leaving them to rdflib's per-row evaluation: basic
graph patterns, `FILTER`, `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS`, nested groups and `VALUES`, projection, `DISTINCT`, `ORDER BY`, `LIMIT`/`OFFSET`,
`ASK` and `COUNT` aggregates above them. Anything else is evaluated by
rdflib. Much of that work runs inside vortex-rdf itself: batched counts and
probes, FILTER predicates decided over the term dictionary and applied
inside the scans, `LIMIT` and `ASK` stopping the scan, native joins,
distinct and group counts over the code columns. Each pushdown is described,
with an example and numbers, in [docs/pushdown.md](docs/pushdown.md); the
switches to disable or narrow it are in the table below.

**File-backed vs in-memory.** The default open is lazy and file-backed.
`VortexRdflibStore(path, in_memory=True)` (or env `VORTEX_RDF_IN_MEMORY=1`) loads
the store into memory once, so queries skip the per-call file-read pipeline.
That helps mainly point lookups and joins; the scan-dominated queries are
bound by rdflib's own result handling either way.

**Secondary indexes.** `serialize_rdf(..., indexes=["secondary-by-copy"])`
(or `"secondary-by-reference"`) writes index components into the `.vortex`
file, for a modest increase in build time and file size. They pay off on
file-backed stores answering single-pattern lookups, where they largely erase
the file-backed penalty for object and predicate-object lookups. On an
in-memory store they change nothing measurable, since the rows are resident
already, and on multi-pattern joins the run-to-run spread is wider than any
effect they have. Enable them for lookup-heavy file-backed workloads.

For Dictionary-layout files, the term dictionary is loaded into memory at open when its compressed size fits a given residency budget (1 GiB by default). Otherwise it stays in the file and is read on demand. Either way the store answers in `u32` codes and the pushdown stays on: a dictionary left in the file only makes decoding (and looking constants up) slower, since every call reads from the file, so the budget trades memory for speed. Pass `max_resident_bytes=...` (the dictionary's compressed size in bytes) to set it yourself, or set
`VORTEX_RDF_DICT_MAX_RESIDENT_BYTES` to give the native layer a process-wide
budget instead. In-memory stores keep the dictionary resident regardless.

## Environment variables

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_IN_MEMORY=1` | Load stores into memory instead of file-backed lazy open |
| `VORTEX_RDF_DISABLE_CODE_PATH=1` | Force the N-Triples string path instead of `u32` codes |
| `VORTEX_RDF_DICT_MAX_RESIDENT_BYTES=<bytes>` | Process-wide term-dictionary residency budget, read by the native layer; file-backed stores then stop defaulting to 1 GiB |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Keep rdflib's default evaluator for every operator (see [docs/pushdown.md](docs/pushdown.md)) |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only push down the listed algebra nodes (`bgp` = basic graph patterns only) |
| `VORTEX_RDF_FILTER_FAST=0` | Evaluate every FILTER value through rdflib's expression evaluator (still once per distinct value) |
| `VORTEX_RDF_NATIVE_FILTERS=0` | Decide no FILTER conjunct inside vortex-rdf (no `filter_codes` verdicts or scan constraints): every value goes through the Python routes |
| `VORTEX_RDF_TRACE_TRIPLES=1` | Print every `triples()` pattern (debugging) |
| `VORTEX_RDF_TRACE_QUERY=1` | Print the pushdown's query plan as JSON lines on stderr — counts, matches, probes, restrictions, joins, mid-join FILTER prunes and every native call, with row counts and timings (debugging; `VORTEX_RDF_TRACE_QUERY_ID` labels the lines) |

## Benchmarks

A comparative benchmark — `VortexRdflibStore` against rdflib's in-memory `Memory`
store, [oxrdflib](https://github.com/oxigraph/oxrdflib) (Oxigraph),
[pycottas](https://github.com/cottas-rdf/pycottas) (COTTAS) and
[rdflib-hdt](https://pypi.org/project/rdflib-hdt/) (HDT), with
[pyoxigraph](https://pypi.org/project/pyoxigraph/) as a reference point — runs
on every push to `main` and publishes the current numbers to GitHub Pages:
**<https://vortex-rdf.github.io/vortex-rdflib/>**.

It executes a synthetic representative SPARQL set with each store's full lifecycle running in its own process. SPARQL evaluation is rdflib's engine for every store but `pyoxigraph`.

Results are cross-checked across stores after every run: same data, same
query, so a store that returns a different number is reported as a failure on
the dashboard.

Run it locally with `scripts/refresh.sh` — it syncs the contenders, ensures
the HDT builder, measures, and re-renders the dashboard:

```bash
scripts/refresh.sh                      # every stage, at the 250k CI scale
BENCH_TRIPLES=20000 scripts/refresh.sh  # scale down
scripts/refresh.sh --only render        # template-only edits: no re-measurement
scripts/refresh.sh --history            # plot the working tree on the history chart
```

Below the overview, the dashboard has a history chart: for each commit on
`main`, how many times faster each vortex-rdflib configuration answers the
query set than rdflib's in-memory store, as a geometric mean (exec only, or
full). CI records one point per benchmark run on the `bench-history` branch.
To plot a change on your branch against that line before merging it, run
`scripts/refresh.sh --history` (about 6 minutes at the default scale): it
measures the working tree and renders `public/index.html` with your point after
`main`'s.

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


## License

MIT — see [LICENSE](LICENSE).
