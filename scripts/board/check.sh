#!/usr/bin/env bash
# Rebuild the whiteboard bundle from its source and compare it, byte for byte, with the committed one
# (DESIGN §56.8, #958). Needs Node 22 and the network once, for the locked dependencies.
#   scripts/board/check.sh          fails (and shows the difference) when they differ
#   scripts/board/check.sh --write  replaces the committed bundle with the rebuild
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
committed="$here/../../src/switchboard/web/static/vendor/board"
out="$(mktemp -d)/board"
trap 'rm -rf "$(dirname "$out")"' EXIT
(cd "$here" && npm ci --ignore-scripts --no-audit --no-fund >/dev/null && node build.mjs "$out")
if [ "${1:-}" = "--write" ]; then
  rm -rf "$committed" && cp -r "$out" "$committed"
  echo "wrote $committed"
elif diff -r "$committed" "$out"; then
  echo "the committed whiteboard bundle is what scripts/board builds"
else
  echo "the committed whiteboard bundle is not what scripts/board builds: run scripts/board/check.sh --write," >&2
  echo "commit every file it changes, and update the hashes in tests/unit/test_web_static_lint.py" >&2
  exit 1
fi
