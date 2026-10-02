#!/usr/bin/env bash
# One-command dashboard refresh, whole or partial.
#
#   scripts/refresh.sh                       # every stage
#   scripts/refresh.sh --only render         # template-only edits: no re-measurement
#   scripts/refresh.sh --only bench,render   # re-measure, skip the dependency sync
#   scripts/refresh.sh --history             # plot the working tree on the history chart
#   scripts/refresh.sh --adapters a,b        # measure a subset (see the warning below)
#   scripts/refresh.sh --force-build         # reinstall the HDT builder even if present
#   BENCH_TRIPLES=20000 scripts/refresh.sh   # scale down (default: the code's 250k)
#
# Stages, in the order they run: deps, hdt, bench, history, render. `history`
# is not in the default set; --history is shorthand for --only history,render.
#
# The measurement runs one process per store, sequentially, so the timings do
# not contend with each other — the same reason bench/worker.py exists.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

ONLY="deps,hdt,bench,render"
FORCE_BUILD=0
ADAPTERS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    --only=*) ONLY="${1#*=}"; shift ;;
    --adapters) ADAPTERS="$2"; shift 2 ;;
    --adapters=*) ADAPTERS="${1#*=}"; shift ;;
    --force-build) FORCE_BUILD=1; shift ;;
    --history) ONLY="history,render"; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

for s in ${ONLY//,/ }; do
  case "$s" in deps|hdt|bench|history|render) ;; *)
    echo "unknown stage: $s (stages: deps, hdt, bench, history, render)" >&2; exit 2 ;;
  esac
done

has() { case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
t_start=$(date +%s)
stage() { echo; echo "══ $1 · $(date +%H:%M:%S) ══"; }

if has deps; then
  stage "Dependencies (project + bench contenders)"
  # oxrdflib and pyoxigraph live here; pycottas and rdflib-hdt pin versions that cannot share
  # this environment, so run_bench builds each of them a venv of its own.
  uv sync --locked --group bench
fi

if has hdt; then
  stage "HDT builder"
  # rdflib-hdt reads HDT but cannot write it, and hdt-cpp's rdf2hdt is not
  # packaged for Python, so the adapter shells out to the Rust crate's CLI.
  if [ "$FORCE_BUILD" = 0 ] && command -v hdt >/dev/null 2>&1; then
    echo "hdt is already on PATH — skipping (--force-build overrides)"
  elif command -v cargo >/dev/null 2>&1; then
    cargo install hdt --features cli --locked
  else
    echo "cargo not found: install Rust, or run with --only deps,bench,render" >&2
    echo "and --adapters without rdflib_hdt to skip the HDT row." >&2
    exit 1
  fi
fi

if has bench; then
  stage "Benchmark (BENCH_TRIPLES=${BENCH_TRIPLES:-250000})"
  args=(--out bench/results.json)
  if [ -n "$ADAPTERS" ]; then
    # run_bench writes only the adapters it ran, so a subset replaces the file
    # rather than merging into it: the dashboard will show those rows alone.
    echo "note: --adapters replaces bench/results.json with only these rows"
    args+=(--adapters "$ADAPTERS")
  fi
  uv run python -m bench.run_bench "${args[@]}"
fi

if has history; then
  stage "History point for the working tree (BENCH_TRIPLES=${BENCH_TRIPLES:-250000})"
  # The 8 vortex configurations plus rdflib, the reference the chart divides
  # by. Written apart from bench/results.json, which keeps the full run the
  # rest of the dashboard shows. Measure at the default scale: a point at
  # another one is skipped next to main's.
  history_run="$(mktemp -d)/results.json"
  uv run python -m bench.run_bench --adapters "$(uv run python -m bench.history adapters)" \
    --out "$history_run"
  uv run python -m bench.history record "$history_run" --source local --commit HEAD \
    --out bench/history-local
fi

if has render; then
  stage "Render (public/index.html)"
  if [ ! -f bench/results.json ]; then
    echo "bench/results.json is missing — run the bench stage at least once first" >&2
    exit 1
  fi
  render_args=(bench/results.json public/index.html)
  # The history chart: main's records from the bench-history branch, plus this
  # machine's local points. Either may be missing; the chart shows what there
  # is, or stays hidden.
  history_dir="$(mktemp -d)"
  if git fetch --quiet origin bench-history 2>/dev/null \
    && git archive FETCH_HEAD records | tar -x -C "$history_dir" 2>/dev/null; then
    render_args+=(--history "$history_dir/records")
  else
    echo "note: origin has no bench-history branch yet; the history chart shows local points only"
  fi
  if [ -d bench/history-local ]; then render_args+=(--local bench/history-local); fi
  uv run python scripts/render_bench_dashboard.py "${render_args[@]}"
fi

echo
echo "done in $(( ($(date +%s) - t_start) / 60 )) min ($ONLY)"
