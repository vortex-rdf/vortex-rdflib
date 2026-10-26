#!/usr/bin/env bash
# Paired performance gate on this machine: BASE against HEAD.
#
#   scripts/perf-gate.sh BASE [HEAD]     # HEAD: a revision; omitted, the working tree
#
# Each side is a snapshot of its src/vortex_rdflib on PYTHONPATH, over this
# checkout's environment and bench/, so the two sides differ in vortex-rdflib
# alone (an editable install would load differently from a snapshot). Every
# measurement runs as a quartet, BASE, HEAD, HEAD, BASE, back to back: its two
# rounds pair runs seconds to minutes apart, and steady drift over the quartet
# lands on both sides.
#
# 1. The 250k synthetic set on the flagship configuration, a store with the
#    secondary-by-copy index, file-backed and in memory, one at a time, with
#    BENCH_HEAVY_ITERS=15 and BENCH_HEAVY_BUDGET_S=5, compared by
#    `python -m bench.compare`.
# 2. The cells it flags, measured again: CONFIRM_ROUNDS rounds of the flagged
#    queries alone (BENCH_QUERIES), in fresh processes and in quartets of the
#    other order, HEAD, BASE, BASE, HEAD, so an effect of a run's position in
#    its quartet favours the other side. A flag stands when the confirmation
#    repeats it.
# 3. When $BSBM_DIR holds prepared BSBM data, the BSBM cell
#    (`python -m bench.bsbm.cell`): the official Explore streams on a store
#    built from it once with the secondary-by-copy index, file-backed and in
#    memory, in two quartets of opposite order; a cell is flagged only when
#    both quartets flag it.
#
# Each run first waits until the 1-minute load average drops under
# GATE_MAX_LOAD, however long that takes. Exits 1 on a standing regression or
# page-fault shift, an answer that differs, or a failure. Results and both
# reports land in $GATE_DIR.
#
# Env: GATE_DIR (bench/gate/<base>-<head>), GATE_ADAPTERS (the synthetic set's
# configurations, vortex_dict_file_copy,vortex_dict_mem_copy; any run_bench
# slugs), CONFIRM_ROUNDS (2; even, 0 skips the confirmation), GATE_MAX_LOAD
# (1.5), BENCH_HEAVY_ITERS (15), BENCH_HEAVY_BUDGET_S (5), BSBM_DIR
# (bench/bsbm-data/p10000, which `scripts/refresh.sh --bsbm` prepares),
# BASE_PATH / HEAD_PATH (PYTHONPATH entries after a side's snapshot,
# for a side on another vortex-rdf: a directory from
# `uv pip install --target DIR --no-deps vortex-rdf==VERSION`).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

usage() { awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; }
case "${1:-}" in -h | --help) usage; exit 0 ;; esac
if [ $# -lt 1 ] || [ $# -gt 2 ]; then usage >&2; exit 2; fi

BASE=$1
HEAD=${2:-}
base_label=$(git rev-parse --short "$BASE")
if [ -n "$HEAD" ]; then head_label=$(git rev-parse --short "$HEAD"); else head_label=worktree; fi
GATE_DIR=${GATE_DIR:-bench/gate/$base_label-$head_label}
CONFIRM_ROUNDS=${CONFIRM_ROUNDS:-2}
BSBM_DIR=${BSBM_DIR:-bench/bsbm-data/p10000}
GATE_MAX_LOAD=${GATE_MAX_LOAD:-1.5}
export BENCH_HEAVY_ITERS=${BENCH_HEAVY_ITERS:-15}
export BENCH_HEAVY_BUDGET_S=${BENCH_HEAVY_BUDGET_S:-5}
if [ $((CONFIRM_ROUNDS % 2)) -ne 0 ]; then echo "CONFIRM_ROUNDS must be even" >&2; exit 2; fi
PY=(uv run --no-sync python)
ADAPTERS=${GATE_ADAPTERS:-vortex_dict_file_copy,vortex_dict_mem_copy}

mkdir -p "$GATE_DIR/tmp" "$GATE_DIR/runs" "$GATE_DIR/bsbm"
GATE_DIR=$(cd "$GATE_DIR" && pwd)
RUNS="$GATE_DIR/runs"
export TMPDIR="$GATE_DIR/tmp"  # run_bench's work dirs, removed after each run
load_avg() { cut -d' ' -f1 /proc/loadavg 2>/dev/null || true; }
log() { echo "$(date +%H:%M:%S) $* (load $(load_avg))"; }
quiet() {  # wait until the 1-minute load average drops under GATE_MAX_LOAD
  local waited=0 load
  while load=$(load_avg) && [ -n "$load" ] &&
    awk -v l="$load" -v m="$GATE_MAX_LOAD" 'BEGIN { exit !(l >= m) }'; do
    if [ $((waited % 300)) -eq 0 ]; then
      log "waiting for the load average, $load, to drop under $GATE_MAX_LOAD"
    fi
    sleep 15
    waited=$((waited + 15))
  done
}
measure() { "${PY[@]}" "$@" < /dev/null; }

snapshot() {  # snapshot REV|"" DIR: the side's src/vortex_rdflib in DIR/src
  rm -rf "$2"
  mkdir -p "$2/src"
  if [ -z "$1" ]; then
    cp -R src/vortex_rdflib "$2/src/"
  else
    git archive "$1" src/vortex_rdflib | tar -x -C "$2"
  fi
}
snapshot "$BASE" "$GATE_DIR/base"
snapshot "$HEAD" "$GATE_DIR/head"

side_path() {  # side_path base|head: the side's PYTHONPATH
  local extra
  if [ "$1" = base ]; then extra=${BASE_PATH:-}; else extra=${HEAD_PATH:-}; fi
  echo "$GATE_DIR/$1/src${extra:+:$extra}"
}
for side in base head; do
  found=$(PYTHONPATH=$(side_path "$side") "${PY[@]}" -c \
    "import vortex_rdf, vortex_rdflib; print(vortex_rdflib.__file__, vortex_rdf.__version__)")
  case "$found" in
    "$GATE_DIR/$side/src/"*) log "$side: $found" ;;
    *) echo "the $side side imports another vortex_rdflib: $found" >&2; exit 2 ;;
  esac
done

quartets() {  # quartets ROUNDS OUTER INNER [FIRST]: "side round" lines, OUTER INNER INNER OUTER for every two rounds, numbered from FIRST
  local p r
  for p in $(seq 1 $(($1 / 2))); do
    r=$((${4:-1} + 2 * (p - 1)))
    printf '%s\n' "$2 $r" "$3 $r" "$3 $((r + 1))" "$2 $((r + 1))"
  done
}
synthetic() {  # synthetic PREFIX ROUNDS ADAPTER OUTER INNER: the adapter's quartets of run_bench
  local side r
  while read -r side r; do
    quiet
    log "$1 $3: $side, round $r"
    PYTHONPATH=$(side_path "$side") measure -m bench.run_bench --adapters "$3" \
      --out "$RUNS/$1-$side-r$r-$3.json" > "$RUNS/$1-$side-r$r-$3.log" 2>&1
    rm -rf "$TMPDIR"/vortex-rdflib-bench-*
  done < <(quartets "$2" "$4" "$5")
}
rounds() {  # rounds PREFIX SIDE ROUNDS ADAPTERS: a side's rounds, comma-separated, each its stores' files joined with +
  local out="" r a round
  for r in $(seq 1 "$3"); do
    round=""
    for a in ${4//,/ }; do round+="${round:++}$RUNS/$1-$2-r$r-$a.json"; done
    out+="${out:+,}$round"
  done
  echo "$out"
}

log "gate $base_label (base) against $head_label (head) in $GATE_DIR"
for adapter in ${ADAPTERS//,/ }; do synthetic syn 2 "$adapter" base head; done
main=("$(rounds syn base 2 "$ADAPTERS")" "$(rounds syn head 2 "$ADAPTERS")")

confirm=()
"${PY[@]}" -m bench.compare "${main[@]}" --flagged-out "$GATE_DIR/flagged.json" > /dev/null
flagged() { "${PY[@]}" -c 'import json, sys; print(",".join(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$GATE_DIR/flagged.json" "$1"; }
flagged_adapters=$(flagged adapters)
if [ -n "$flagged_adapters" ] && [ "$CONFIRM_ROUNDS" -gt 0 ]; then
  flagged_queries=$(flagged queries)
  log "confirming $flagged_queries on $flagged_adapters"
  export BENCH_QUERIES=$flagged_queries
  for adapter in ${flagged_adapters//,/ }; do
    synthetic confirm "$CONFIRM_ROUNDS" "$adapter" head base
  done
  unset BENCH_QUERIES
  confirm=(--confirm "$(rounds confirm base "$CONFIRM_ROUNDS" "$flagged_adapters")"
    "$(rounds confirm head "$CONFIRM_ROUNDS" "$flagged_adapters")")
fi

bsbm=0
if [ -f "$BSBM_DIR/meta.json" ]; then
  bsbm=1
  store="$GATE_DIR/bsbm/store.vortex"
  if [ ! -f "$store" ]; then
    log "BSBM cell: building $store from $BSBM_DIR"
    PYTHONPATH=$(side_path head) "${PY[@]}" -m bench.bsbm.cell prepare "$BSBM_DIR" "$store"
  fi
  for where in file in-memory; do
    while read -r side r; do
      quiet
      log "BSBM cell, $where: $side, round $r"
      PYTHONPATH=$(side_path "$side") measure -m bench.bsbm.run_stream "$store" "--$where" \
        "$BSBM_DIR/warmup.json" "$BSBM_DIR/measured.json" "$RUNS/bsbm-$side-r$r-$where.json" \
        --query-timeout 30 2> "$RUNS/bsbm-$side-r$r-$where.log"
    done < <(quartets 2 base head 1; quartets 2 head base 3)
  done
else
  log "no $BSBM_DIR/meta.json: the BSBM cell is skipped"
fi

status=0
echo
"${PY[@]}" -m bench.compare "${main[@]}" ${confirm[@]+"${confirm[@]}"} --check \
  --json "$GATE_DIR/synthetic.json" | tee "$GATE_DIR/synthetic.txt" || status=1
if [ "$bsbm" = 1 ]; then
  echo
  streams() {  # streams SIDE: the side's BSBM runs, file-backed then in memory, rounds 1 to 4
    local out="" where r
    for where in file in-memory; do
      for r in 1 2 3 4; do out+="${out:+,}$RUNS/bsbm-$1-r$r-$where.json"; done
    done
    echo "$out"
  }
  "${PY[@]}" -m bench.bsbm.cell compare "$(streams base)" "$(streams head)" --check \
    --json "$GATE_DIR/bsbm.json" | tee "$GATE_DIR/bsbm.txt" || status=1
fi
echo
if [ "$status" = 0 ]; then
  log "gate: nothing flagged (reports in $GATE_DIR)"
else
  log "gate: FLAGGED, see $GATE_DIR/synthetic.txt and $GATE_DIR/bsbm.txt"
fi
exit "$status"
