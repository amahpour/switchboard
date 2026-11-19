#!/usr/bin/env bash
# Prepares a Cursor Cloud Agent's machine for switchboard's checks (CONTRIBUTING.md, "Cloud agents"):
# uv and CPython 3.13 as CI pins them (.github/actions/setup-uv-python), Node 22 for the UI's
# JavaScript tests and ESLint (.github/workflows/test.yml), and the project's .venv.
#
# Cursor runs it from the repository root each time it prepares a Build (.cursor/environment.json),
# on a fresh disk or one an earlier Build prepared, so every step checks before it acts.
# tests/unit/test_cursor_environment.py keeps the pins equal to CI's.
set -euo pipefail

UV_VERSION=0.7.13
PYTHON_VERSION=3.13
NODE_MAJOR=22

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"

# uv, at CI's version. The installer also puts ~/.local/bin on the PATH of later shells.
if [ "$(uv --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_INSTALL_DIR="$BIN" sh
fi
uv --version

# CPython, managed by uv as in CI.
uv python install "$PYTHON_VERSION"

# Node, the latest release of CI's major, checked against nodejs.org's SHA-256 list.
if [ "$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')" != "$NODE_MAJOR" ]; then
  case "$(uname -m)" in
    x86_64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
      echo "install.sh: no Node build for $(uname -m)" >&2
      exit 1
      ;;
  esac
  base="https://nodejs.org/dist/latest-v$NODE_MAJOR.x"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  file="$(awk -v want="linux-$arch.tar.xz" '$2 ~ ("-" want "$") { print $2 }' "$tmp/SHASUMS256.txt")"
  [ -n "$file" ] || {
    echo "install.sh: no linux-$arch.tar.xz in $base" >&2
    exit 1
  }
  curl -fsSL "$base/$file" -o "$tmp/$file"
  (cd "$tmp" && grep "  $file\$" SHASUMS256.txt | sha256sum -c -)
  rm -rf "$HOME/.local/node"
  mkdir -p "$HOME/.local/node"
  tar -xJf "$tmp/$file" -C "$HOME/.local/node" --strip-components=1
  for tool in node npm npx; do
    ln -sf "$HOME/.local/node/bin/$tool" "$BIN/$tool"
  done
fi
node --version

# The project's .venv, from the lockfile.
uv sync --frozen
