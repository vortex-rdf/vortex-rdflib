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
distinct and group counts over the code columns. How each pushdown works, and
where in the code it runs, is described in [docs/pushdown.md](docs/pushdown.md);
the switches to disable or narrow it are in the table below.

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

Every section of the dashboard has a tab per dataset:

- **Synthetic**: a generated dataset of 250,000 quads in 8 graphs and a
  representative SPARQL set (lookups and scans, star and chain joins, FILTER /
  DISTINCT / ORDER BY / GROUP BY, named graphs).
- **BSBM**: the [Berlin SPARQL Benchmark](https://github.com/Tpt/bsbm-tools)
  Explore use case at 10,000 products (3.5M triples). The data comes from the
  official generator. The queries come from the unmodified official test driver
  (seed 808080, 5 warm-up and 20 measured mixes), so every query instance
  carries its own parameters. A query running past 5 s is aborted and counted as
  a timeout. Its time up to the abort still counts in its mix's time, and so in
  QMpH, as the official driver counts it. A store still running after 300 s of
  measured mixes finishes its mix, stops, and is marked *partial*.

Each store's whole lifecycle runs in its own process. SPARQL evaluation is
rdflib's engine for every store but `pyoxigraph`. Results are cross-checked
across stores after every run:
- on the synthetic set, every store must return the same number of rows;
- on BSBM, the rdflib-engine stores must return the same answer to every
  instance (compared by digest), and pyoxigraph the same number of rows.

A store that does not is reported as a failure on the dashboard. The memory
panel shows each store's peak RSS and its peak anonymous memory (RssAnon). A
memory-mapped store's RSS includes file pages the kernel may drop again; its
RssAnon does not.

Run it locally with `scripts/refresh.sh` — it syncs the contenders, ensures
the HDT builder, measures, and re-renders the dashboard:

```bash
scripts/refresh.sh                             # every default stage, at the 250k CI scale
BENCH_TRIPLES=20000 scripts/refresh.sh         # scale down
scripts/refresh.sh --bsbm                      # the BSBM tab at CI's scale (needs Java)
BSBM_PRODUCTS=1000 scripts/refresh.sh --bsbm   # a smaller BSBM run
scripts/refresh.sh --only render               # template-only edits: no re-measurement
scripts/refresh.sh --history                   # plot the working tree on the synthetic history chart
```

`--bsbm` runs the BSBM and render stages alone, so install the contenders
first: `uv sync --group bench`, or one plain `scripts/refresh.sh`, which also
installs the HDT builder. It needs Java and bash, on Linux or macOS.

`BENCH_FRESH_CONSTANTS=1` makes every sample of a synthetic query with a FILTER
constant (`filter-range`, `filter-arith`, `filter-band-probe`, `minus`) use a
new constant. Work a store memoizes per constant is then paid in every sample,
as in a BSBM run. CodSpeed tracks the same for `filter-range` and
`filter-arith` on every commit (`test_query_fresh_constant`).

Below the overview, each dataset has a history chart. For each commit on `main`,
it shows how many times faster each vortex-rdflib configuration answers than
rdflib's in-memory store. The figure is a geometric mean over the queries
(synthetic) or the Explore templates (BSBM), exec only or full. CI records one
point per dataset and run on the `bench-history` branch (`records/`,
`records-bsbm/`). To plot a change on your branch against the synthetic line
before merging it, run `scripts/refresh.sh --history` (about 6 minutes at the
default scale). It measures the working tree on the synthetic set and renders
`public/index.html` with your point after `main`'s. The BSBM chart takes no
local points.

### BSBM harness

`bench/bsbm` prepares the BSBM tab's data and streams. It also runs paired
comparisons of two vortex-rdflib or vortex-rdf builds at any scale. `prepare`
downloads the official tools ([Tpt/bsbm-tools](https://github.com/Tpt/bsbm-tools),
pinned and checksum-verified, cached in `$BENCH_CACHE` or
`~/.cache/vortex-rdflib/bsbm`). It needs Java and bash, on Linux or macOS. The
examples keep everything under `bench/bsbm-data/`, which git ignores:

```bash
# the dashboard's data and streams: 10,000 products, 5 warm-up and 20 measured mixes
python -m bench.bsbm.prepare --products 10000 --warmup-mixes 5 --mixes 20 --out bench/bsbm-data/s10k
python -m bench.run_bench --dataset bsbm --bsbm-dir bench/bsbm-data/s10k   # every store, as on the dashboard
# the official Q6 (regex) alone, over the same dataset
python -m bench.bsbm.prepare --from bench/bsbm-data/s10k --only-query 6 --warmup-mixes 2 --mixes 50 --out bench/bsbm-data/q6
# a paired comparison: one store file, then the stream once per build
python -c 'import vortex_rdf; vortex_rdf.serialize_rdf("bench/bsbm-data/s10k/dataset.nt", "bench/bsbm-data/store.vortex", format="ntriples", layout="dictionary")'
PYTHONPATH=../baseline/src python -m bench.bsbm.run_stream bench/bsbm-data/store.vortex --file bench/bsbm-data/s10k/warmup.json bench/bsbm-data/s10k/measured.json bench/bsbm-data/baseline.json
python -m bench.bsbm.run_stream bench/bsbm-data/store.vortex --file bench/bsbm-data/s10k/warmup.json bench/bsbm-data/s10k/measured.json bench/bsbm-data/run.json
python -m bench.bsbm.compare bench/bsbm-data/baseline.json bench/bsbm-data/run.json
```

- `prepare`: re-running it with the same parameters is a cache hit. Other
  mixes, or another seed, in the same directory reuse its dataset and capture
  the streams again.
  - `--from` reuses another prepared directory's dataset, without regenerating
    or copying it.
  - `--only-query 6` captures the official Q6 (regex), which the official mix
    leaves out.
- `run_stream` measures the vortex-rdflib and vortex-rdf that Python imports,
  so a baseline is the same stream run once per build, each chosen by
  `PYTHONPATH`: above, `../baseline` is a worktree of the baseline commit, and
  its `src/` goes first. A vortex-rdf build is a directory it was installed into
  (`uv pip install --target DIR --no-deps vortex-rdf==VERSION`).
- `run_stream` applies a limit only when given one: `--query-timeout`,
  `--store-budget`, or both. The dashboard's limits come from
  `BSBM_QUERY_TIMEOUT_S` (5) and `BSBM_STORE_BUDGET_S` (300). The two limits
  are independent, and 0 turns either off. A timed-out query counts its time up
  to the abort.
- `bench.bsbm.record_native` and `bench.bsbm.replay_native` record a stream's
  native calls and replay them against another vortex-rdf build.

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
