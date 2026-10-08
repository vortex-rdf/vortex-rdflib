#!/usr/bin/env bash
# One-command dashboard refresh, whole or partial.
#
#   scripts/refresh.sh                       # every default stage
#   scripts/refresh.sh --only render         # template-only edits: no re-measurement
#   scripts/refresh.sh --only bench,render   # re-measure, skip the dependency sync
#   scripts/refresh.sh --bsbm                # the BSBM tab: prepare, measure, render (Java)
#   scripts/refresh.sh --history             # plot the working tree on the synthetic history chart
#   scripts/refresh.sh --adapters a,b        # measure a subset (see the warning below)
#   scripts/refresh.sh --force-build         # reinstall the HDT builder even if present
#   BENCH_TRIPLES=20000 scripts/refresh.sh   # scale down (default: the code's 250k)
#   BSBM_PRODUCTS=1000 scripts/refresh.sh --bsbm   # a smaller BSBM run (default: CI's 10K)
#
# Stages, in the order they run: deps, hdt, bench, bsbm, history, render. `bsbm`
# and `history` are not in the default set: --bsbm is --only bsbm,render and
# --history is --only history,render. BSBM knobs, CI's values by default:
# BSBM_PRODUCTS (10000), BSBM_WARMUP_MIXES (5), BSBM_MIXES (20), BSBM_SEED
# (808080), BSBM_QUERY_TIMEOUT_S (5), BSBM_STORE_BUDGET_S (300), BSBM_LOAD_ITERS
# (1). 0 turns either limit, the query timeout or the store budget, off.
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
    --bsbm) ONLY="bsbm,render"; shift ;;
    -h|--help) awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

for s in ${ONLY//,/ }; do
  case "$s" in deps|hdt|bench|bsbm|history|render) ;; *)
    echo "unknown stage: $s (stages: deps, hdt, bench, bsbm, history, render)" >&2; exit 2 ;;
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

if has bsbm; then
  BSBM_PRODUCTS="${BSBM_PRODUCTS:-10000}"
  BSBM_WARMUP_MIXES="${BSBM_WARMUP_MIXES:-5}"
  BSBM_MIXES="${BSBM_MIXES:-20}"
  BSBM_SEED="${BSBM_SEED:-808080}"
  stage "BSBM (${BSBM_PRODUCTS} products, ${BSBM_WARMUP_MIXES}+${BSBM_MIXES} mixes, seed ${BSBM_SEED})"
  # One directory per scale: the official tools write its data once, and capture
  # the streams again only when the mixes or the seed change (the same values
  # again are a cache hit). Then every store runs them in its own process.
  bsbm_dir="bench/bsbm-data/p${BSBM_PRODUCTS}"
  uv run python -m bench.bsbm.prepare --products "$BSBM_PRODUCTS" \
    --warmup-mixes "$BSBM_WARMUP_MIXES" --mixes "$BSBM_MIXES" --seed "$BSBM_SEED" --out "$bsbm_dir"
  bsbm_args=(--dataset bsbm --bsbm-dir "$bsbm_dir" --out bench/results-bsbm.json)
  if [ -n "$ADAPTERS" ]; then
    echo "note: --adapters replaces bench/results-bsbm.json with only these rows"
    bsbm_args+=(--adapters "$ADAPTERS")
  fi
  uv run python -m bench.run_bench "${bsbm_args[@]}"
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
  if [ ! -f bench/results.json ] && [ ! -f bench/results-bsbm.json ]; then
    echo "neither bench/results.json nor bench/results-bsbm.json exists — run bench or bsbm first" >&2
    exit 1
  fi
  # A missing results file disables its dataset's tabs, with a note on the page.
  render_args=(bench/results.json public/index.html --bsbm bench/results-bsbm.json)
  history_dir="$(mktemp -d)"
  if git fetch --quiet origin bench-history 2>/dev/null; then
    if git archive FETCH_HEAD records 2>/dev/null | tar -x -C "$history_dir" 2>/dev/null; then
      render_args+=(--history "$history_dir/records")
    fi
    if git archive FETCH_HEAD records-bsbm 2>/dev/null | tar -x -C "$history_dir" 2>/dev/null; then
      render_args+=(--bsbm-history "$history_dir/records-bsbm")
    fi
  else
    echo "note: origin has no bench-history branch yet; the history chart shows local points only"
  fi
  if [ -d bench/history-local ]; then render_args+=(--local bench/history-local); fi
  uv run python scripts/render_bench_dashboard.py "${render_args[@]}"
fi

echo
echo "done in $(( ($(date +%s) - t_start) / 60 )) min ($ONLY)"
