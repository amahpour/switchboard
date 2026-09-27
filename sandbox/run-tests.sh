#!/bin/bash
# Entry point of the switchboard-test image (sandbox/Dockerfile.test).
#
# Runs `uv run pytest -q "$@"` against the copy of the repo baked into the image.
# If the repo is also bind-mounted read-only at /src/switchboard, its source is copied
# over the baked one first (never written to), so you can re-test local edits
# without rebuilding. From the repo root:
#   docker compose -f sandbox/compose.yaml run --rm -v "$PWD:/src/switchboard:ro" test
# Only the paths the root .dockerignore lets into the image are copied, minus
# local junk. A changed uv.lock needs a rebuild (runs have no network).
set -euo pipefail
cd /home/dev/switchboard
if [ -f /src/switchboard/pyproject.toml ]; then
  if ! cmp -s /src/switchboard/uv.lock uv.lock; then
    echo "uv.lock changed since the image was built (runs have no network): rebuild with" >&2
    echo "  docker compose -f sandbox/compose.yaml build test" >&2
    exit 2
  fi
  find . -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
  srcs=()
  for p in src tests pyproject.toml uv.lock .python-version README.md LICENSE; do
    if [ -e "/src/switchboard/$p" ]; then srcs+=("$p"); fi
  done
  tar -C /src/switchboard \
      --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude=.ruff_cache \
      --exclude=node_modules --exclude=logs --exclude='*.log' --exclude='*.db' --exclude='*.db-*' \
      --exclude='*.sqlite' --exclude='*.sqlite-*' --exclude='.switchboard*' --exclude='.env*' \
      --exclude=.DS_Store --exclude=tests/live/_runs -cf - "${srcs[@]}" | tar -xf -
  uv sync --frozen --offline --quiet   # no network at run time; the image's uv cache has everything
fi
# --no-sync: the venv is already synced (in the image, or just above)
echo "switchboard tests on $(uname -srm), $(uv run --frozen --no-sync python -VV | head -1), uid $(id -u)" >&2
exec uv run --frozen --no-sync pytest -q -p no:cacheprovider "$@"
