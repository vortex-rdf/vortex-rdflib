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
a dependency: stores are **memory-mapped from `.vortex` files** by default,
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
the Vortex file. The store is read-only; mutation support is on the roadmap.

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

## Cancelling a query

`cancel_scope` gives the queries started inside it a `vortex_rdf.CancelToken`.
Cancel the token, from any thread, and the query raises `vortex_rdf.Cancelled`
at the next point that checks it:

- A call into vortex-rdf that races the token: every batch and chunk call, and
  each one-shot call that can scan a file-backed store.
- Every other one-shot call, which checks the token before it starts and then
  runs to completion: an in-memory call, a point lookup, a call capped at one
  row, and the string fallback (a layout other than `"dictionary"`, or
  `VORTEX_RDF_DISABLE_CODE_PATH=1`). The probes a join sends per row are calls
  of one kind or the other, so each probe is a check point of its own.
- Between calls: the pieces of a streamed join, the rows rdflib finishes when a
  join over the pair budget is left to it, and the slices and decode chunks the
  rows of a result are read in.

A cancel takes effect at the next of these points, so rows that are already
fetched are delivered until then: a result whose rows are decoded as they are
read stops within one decode chunk, at most 4,096 rows.

```python
import threading

from vortex_rdf import Cancelled, CancelToken
from vortex_rdflib import cancel_scope

token = CancelToken()
deadline = threading.Timer(30.0, token.cancel)  # or cancel from any other thread
deadline.start()
try:
    with cancel_scope(token):
        result = graph.query("SELECT ?s ?o WHERE { ?s ?p ?o }")
    for row in result:
        print(row.s, row.o)
except Cancelled:
    print("cancelled")
finally:
    deadline.cancel()
```

A lazy result keeps the token of the query that made it, so the check points it
still reaches while its rows are produced run under it, and a query started
outside any block has no token. In the snippet, the one pattern is matched
inside `graph.query(...)`, where the token is checked or raced; the loop then
reads the match's rows as they are decoded, and a cancel that arrives during it
stops the loop at the next decode chunk.

## Streaming a large answer

rdflib's `Result` keeps every row it yields (iterating it appends each binding
to its `bindings` list, so that it can be iterated again). A consumer that
streams a large answer and must stay flat in memory reads the bindings the query
produces instead of a `Result`:

```python
from rdflib import Variable
from rdflib.plugins.sparql.processor import SPARQLProcessor

query = "SELECT ?s ?o WHERE { ?s ?p ?o }"
answer = SPARQLProcessor(graph).query(query, initNs=dict(graph.namespaces()))
for binding in answer["bindings"]:  # one row at a time; nothing keeps it
    print(binding[Variable("s")], binding[Variable("o")])
```

`initNs` is what `Graph.query` hands the processor: the graph's namespace
bindings (a graph over a `VortexRdflibStore` holds none, so a query reads the
same prefixes either way). `answer["vars_"]` lists the projected variables, in
order. `Graph.query` makes the same call and wraps the dictionary in a
`Result`.

## How it works

**Dictionary-encoded terms.** For vortex-rdf Dictionary-layout stores, matched rows cross the native boundary as zero-copy `u64` term-code columns
(`vortex_rdf.VortexRdfStore.match_codes`), and each distinct code a query
returns is decoded to an rdflib term once — in one GIL-released
`TermDict.decode_many` call per batch — and kept for that query only (see
[Caches and lifetimes](docs/pushdown.md#caches-and-lifetimes)). Other layouts
fall back to N-Triples string columns, parsing each distinct term once.
Language tags are stored lower-cased: `"x"@EN` reads back as `"x"@en` (rdflib's
in-memory store keeps the tag as written).

**SPARQL pushdown.** Constructing a `VortexRdflibStore` registers an rdflib
`CUSTOM_EVALS` hook that answers the algebra operators it understands over
vortex term codes instead of leaving them to rdflib's per-row evaluation: basic
graph patterns, `FILTER`, `OPTIONAL`, `MINUS`, `FILTER (NOT) EXISTS`, nested groups and `VALUES`, projection, `DISTINCT`, `ORDER BY`, `LIMIT`/`OFFSET`,
`ASK` and `COUNT` aggregates above them. Anything else is evaluated by
rdflib. Much of that work runs inside vortex-rdf itself: batched counts and
probes, FILTER predicates evaluated over the codes a query produces (`REGEX`,
`CONTAINS`, `STRSTARTS`, `STRENDS`, numeric comparisons, `lang`, `datatype`),
kind tests and IRI equality applied inside the scans, `LIMIT` and `ASK`
stopping the scan, native joins, distinct and group counts over the code
columns. How each pushdown works, and where in the code it runs, is described
in [docs/pushdown.md](docs/pushdown.md); the switches to disable or narrow it
are in the table below.

**File-backed vs in-memory.** The default open is file-backed: vortex-rdf
memory-maps the `.vortex` file, so a query reads only the parts of the file it
touches. What stays in RAM is the operating system's page cache plus a bounded
cache of the dictionary windows vortex-rdf has read.
`VortexRdflibStore(path, in_memory=True)` (or env `VORTEX_RDF_IN_MEMORY=1`)
loads the whole store into memory up front instead.

**Secondary indexes.** `serialize_rdf(..., indexes=["secondary-by-copy"])`
(or `"secondary-by-reference"`) writes index components into the `.vortex`
file, for a modest increase in build time and file size. They give a pattern
with a bound predicate or object a sorted access path, so on a file-backed
store a single-pattern lookup reads only the rows it selects instead of
scanning the file. An in-memory store has its rows in memory already. Enable
them for lookup-heavy file-backed workloads.

**Memory.** A file-backed store keeps no copy of its data except a bounded
cache of the dictionary windows vortex-rdf reads. The mapped pages queries read
count towards the process's RSS as file-backed memory (`RssFile` in
`/proc/self/status`), which the kernel reclaims whenever it needs the RAM. The
process's own memory is its anonymous RSS (`RssAnon`): what a query learns
about terms (decoded terms, the codes of its constants) is dropped with the
query, so it does not grow with the number of queries. Stores are read-only;
truncating or rewriting a store file in place while a store has it open is
unsupported, and so is opening a store from a network filesystem.

**Join memory.** A join can make far more rows than either of its sides holds.
The pair budget (`VORTEX_RDF_PAIR_BUDGET`, 16,777,216 row pairs by default)
bounds what one join step of a basic graph pattern builds at once. A step that
could pass it is counted natively before it is built, and one over the budget
is not built whole: the last step is joined a piece at a time while the rows
are read, so a consumer that reads its rows as they come holds one piece
(`ORDER BY`, group joins, `OPTIONAL`, `MINUS` and `EXISTS` read the pieces into
rows first); or another pattern that fits is joined first; or rdflib finishes
the remaining patterns row by row. On a 10.9M-row file-backed store, a WatDiv
query, T051 (`SELECT *` over five patterns whose last join makes 4.17 G pairs),
made 0.3.0 ask for a 33 GB index column before its first row; 0.4 yields its
first row in 0.2 s, with native memory at about 1 GiB at the default budget
(about 220 MiB at `VORTEX_RDF_PAIR_BUDGET=4194304`). `triples()`, which
rdflib's own evaluator calls, reads a pattern of up to 65,536 rows as one match
and a larger one in chunks of 65,536 rows, so a scan of a large store holds one
chunk of codes at a time; that size is
`vortex_rdflib.settings.SETTINGS.triples_chunk_rows`, not an environment variable. How the
budget works is described in [docs/pushdown.md](docs/pushdown.md#the-pair-budget).

## Environment variables

| Variable | Effect |
| --- | --- |
| `VORTEX_RDF_IN_MEMORY=1` | Load stores into memory instead of memory-mapping the file |
| `VORTEX_RDF_DISABLE_CODE_PATH=1` | Force the N-Triples string path instead of `u64` codes |
| `VORTEX_RDF_DISABLE_PUSHDOWN=1` | Keep rdflib's default evaluator for every operator (see [docs/pushdown.md](docs/pushdown.md)) |
| `VORTEX_RDF_PUSHDOWN_OPS=<list>` | Only push down the listed algebra nodes (`bgp` = basic graph patterns only) |
| `VORTEX_RDF_FILTER_FAST=0` | Evaluate every FILTER value through rdflib's expression evaluator (still once per distinct value) |
| `VORTEX_RDF_NATIVE_FILTERS=0` | Evaluate no FILTER conjunct inside vortex-rdf (no `filter_codes` calls, no kind-test or IRI-equality keeps on the scans): every value goes through the Python routes |
| `VORTEX_RDF_PAIR_BUDGET=<n>` | The most row pairs one join step of a basic graph pattern builds at once: an integer of at least 0, 16,777,216 by default. A step over it is streamed, reordered around or left to rdflib (see [docs/pushdown.md](docs/pushdown.md#the-pair-budget)) |
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

Each store answers its queries in a process of its own. A store built into a
file (vortex-rdflib, COTTAS, HDT) is built in one process, timed as its load,
and queried in a fresh one that opens the file, as an application opens a
built store; the others load in the process that queries them. SPARQL
evaluation is rdflib's engine for every store but `pyoxigraph`. Results are
cross-checked across stores after every run:
- on the synthetic set, every store must return the same number of rows;
- on BSBM, the rdflib-engine stores must return the same answer to every
  instance (compared by digest), and pyoxigraph the same number of rows.

A store that does not is reported as a failure on the dashboard. The memory
panel shows the peak RSS and the peak anonymous memory (RssAnon) of the process
that answers a store's queries. A memory-mapped store's RSS includes file pages
the kernel may drop again; its RssAnon does not. Every query row in the results
file also carries `minflt`, the median minor page faults per sample.

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
`records-bsbm/`). A point's configurations and rdflib run in one job, so each
ratio divides two timings from one machine, and the point names that machine's
CPU in its tooltip: CI's runners differ in CPU model, so a step between two
points on different CPUs is no evidence by itself. Every record also keeps
each query's median minor page faults. The Benchmark History Backfill workflow
re-measures any commits of `main` in one job, so the points it writes share one
CPU. A long job runs slower as it goes, so the backfill also measures the last
listed commit, the anchor, first, again after every `anchor-every` others (4),
and last. Every other point is taken back to the anchor's first measurement by
the drift the anchor's measurements around it show, and its tooltip names the
anchor and each correction. A commit's newest measurement is its record, so a
re-run dashboard job never lays an older point over a backfill. A commit listed
twice is measured twice, every run hands over its raw results files as the
`backfill-results` artifact, and with `push` off it writes no record: an
experiment (`anchor-every` 0 measures the list as given). To plot a change on
your branch against the synthetic line before merging it, run
`scripts/refresh.sh --history` (about 6 minutes at the default scale). It
measures the working tree on the synthetic set and renders `public/index.html`
with your point after `main`'s. The BSBM chart takes no local points.

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

### Performance gate

`scripts/perf-gate.sh BASE [HEAD]` compares two vortex-rdflib versions on one
machine: `BASE` against `HEAD`, or against the working tree when `HEAD` is
left out. Each side is a snapshot of its `src/vortex_rdflib` on `PYTHONPATH`,
over one environment and this checkout's `bench/`. Every measurement runs as a
quartet, `BASE`, `HEAD`, `HEAD`, `BASE`, back to back, so its two rounds pair
runs taken close together and steady drift lands on both sides. It runs:

- the synthetic set on the flagship configuration, a store with the
  `secondary-by-copy` index, file-backed and in memory (`GATE_ADAPTERS` names
  others), compared by `python -m bench.compare`. A heavy query takes 15
  samples, or stops after three once they pass 5 s (`BENCH_HEAVY_ITERS`,
  `BENCH_HEAVY_BUDGET_S`);
- the flagged cells again, in fresh processes and in quartets of the other
  order, `HEAD`, `BASE`, `BASE`, `HEAD`, measuring their queries alone
  (`BENCH_QUERIES`): a flag stands only when this confirmation repeats it;
- when `bench/bsbm-data/p10000` holds prepared BSBM data (`scripts/refresh.sh
  --bsbm` writes it), the BSBM cell (`python -m bench.bsbm.cell`): the official
  Explore streams, every template, on the flagship configuration, a store with
  the `secondary-by-copy` index built from that data, file-backed and in
  memory. Each residency runs two quartets of opposite order, and a template is
  flagged only when both flag it; every instance must answer the same rows in
  every run.

A cell regresses when `HEAD` is slower than `BASE` in every round, by more
than the larger of 5 % and `BASE`'s own spread between its rounds. Minor page
faults per query follow the same rule against 25 %, and only above 256 faults:
an allocator returning freed buffers to the kernel shows there and in no
timer. Over a hundred cells judged at once flag a few by chance now and then,
and a run's position in its quartet can favour one side; the confirmation, in
the other order, repeats neither. rdflib, when `GATE_ADAPTERS` names it, runs no
vortex-rdflib code, so its ratio is reported as the machine's drift and never
flagged. Each run first waits for
the 1-minute load average to drop under `GATE_MAX_LOAD` (1.5). The script exits
1 on a standing regression or fault shift, an answer that differs, or a
failure, and writes its reports to `bench/gate/<base>-<head>/`. A run takes
about 4 minutes; compare only runs from one machine.

```bash
scripts/perf-gate.sh main            # main against the working tree
scripts/perf-gate.sh v0.4.0 main     # two revisions
# the working tree on another vortex-rdf, installed with
# uv pip install --target ../vortex-rdf-next --no-deps vortex-rdf==VERSION
HEAD_PATH=../vortex-rdf-next scripts/perf-gate.sh main
```

The CodSpeed cells (`bench/test_codspeed.py`) count instructions on
32,768-row stores. The gate covers what they cannot: costs that grow with a
store's size, the probe count of a join, or the allocator's state.

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
