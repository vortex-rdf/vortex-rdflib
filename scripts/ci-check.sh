#!/usr/bin/env bash
# Mirrors the jobs in .github/workflows/ci.yml so failures are caught before
# they reach GitHub. Run directly (`./scripts/ci-check.sh`) or let the
# pre-push hook (scripts/hooks/pre-push) invoke it automatically.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$1"; }

# `--locked` fails if uv.lock is out of date, exactly like CI. The bench group
# comes along so `ty` can resolve bench/'s optional imports (and so a push does
# not silently uninstall the benchmark contenders).
info "uv sync --locked --group bench"
uv sync --locked --group bench

# --- lint job ---
info "uv run ruff format --check"
uv run ruff format --check

info "uv run ruff check"
uv run ruff check

info "uv run ty check"
uv run ty check

# Needs only bash and python3, so it does not depend on the sync above. The
# line anchors the docs use (`file.py#L42`) go stale whenever the source
# moves, which nothing else here would catch.
info "scripts/check-doc-anchors.sh"
if ! scripts/check-doc-anchors.sh; then
  warn "Broken doc links above; re-anchor them against the current sources."
  exit 1
fi

# --- test job ---
# CI's version matrix (3.11–3.14, plus macOS/Windows) is not mirrored here —
# one local interpreter catches real breakage; GitHub runs the rest.
info "uv run pytest"
uv run pytest

# --- package job ---
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
info "uv build"
uv build --out-dir "$out"

info "twine check"
uvx twine check "$out"/*

info "All CI checks passed."
