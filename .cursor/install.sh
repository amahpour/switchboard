#!/usr/bin/env bash
# Prepares a Cursor Cloud Agent's machine for switchboard's checks (CONTRIBUTING.md, "Cloud agents"):
# uv and CPython 3.13 as CI pins them (.github/actions/setup-uv-python), Node 22 for the UI's
# JavaScript tests and ESLint (.github/workflows/test.yml), and the project's .venv.
#
# Cursor runs it from the repository root each time it prepares a Build (.cursor/environment.json),
# on a fresh disk or one an earlier Build prepared, so every step checks before it acts.
# Every download is a pinned release archive, refused unless its SHA-256 is the one below; nothing
# downloaded is run before that check. tests/unit/test_cursor_environment.py keeps the pins equal
# to CI's.
set -euo pipefail

UV_VERSION=0.7.13
PYTHON_VERSION=3.13
NODE_VERSION=22.23.5
# From uv's release .sha256 files, and Node's SHASUMS256.txt (signature checked with nodejs/release-keys).
UV_SHA256_X86_64=909278eb197c5ed0e9b5f16317d1255270d1f9ea4196e7179ce934d48c4c2545
UV_SHA256_AARCH64=0b2ad9fe4295881615295add8cc5daa02549d29cc9a61f0578e397efcf12f08f
NODE_SHA256_X64=cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8
NODE_SHA256_ARM64=2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09

case "$(uname -m)" in
  x86_64)
    uv_target=x86_64-unknown-linux-gnu uv_sha256=$UV_SHA256_X86_64
    node_arch=x64 node_sha256=$NODE_SHA256_X64
    ;;
  aarch64 | arm64)
    uv_target=aarch64-unknown-linux-gnu uv_sha256=$UV_SHA256_AARCH64
    node_arch=arm64 node_sha256=$NODE_SHA256_ARM64
    ;;
  *)
    echo "install.sh: no uv or Node build pinned for $(uname -m)" >&2
    exit 1
    ;;
esac

BIN="$HOME/.local/bin"
NODE_HOME="$HOME/.local/node"
mkdir -p "$BIN"
# CI's setting: only a uv-managed CPython, never a system one.
export UV_PYTHON_PREFERENCE=only-managed

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fetch NAME URL SHA256: download URL to $tmp/NAME, and stop unless its SHA-256 is the pinned one.
fetch() {
  curl -fsSL "$2" -o "$tmp/$1"
  echo "$3  $tmp/$1" | sha256sum -c --quiet -
}

# Later shells (the agent's) find these tools first.
for rc in "$HOME/.profile" "$HOME/.bashrc"; do
  # shellcheck disable=SC2016 # written as is, for the shell that reads the file to expand
  grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' "$rc" 2>/dev/null ||
    echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$rc"
done

# uv, at CI's version.
if [ "$("$BIN/uv" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  fetch uv.tar.gz "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_target.tar.gz" "$uv_sha256"
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  install -m 0755 "$tmp/uv-$uv_target/uv" "$tmp/uv-$uv_target/uvx" "$BIN/"
fi
"$BIN/uv" --version

# Node with its npm, at the pinned release. Whatever Node the machine already had is not used.
if [ "$("$NODE_HOME/bin/node" --version 2>/dev/null)" != "v$NODE_VERSION" ] || [ ! -x "$NODE_HOME/bin/npm" ]; then
  fetch node.tar.xz "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node_arch.tar.xz" "$node_sha256"
  rm -rf "$NODE_HOME"
  mkdir -p "$NODE_HOME"
  tar -xJf "$tmp/node.tar.xz" -C "$NODE_HOME" --strip-components=1
fi
for tool in node npm npx; do
  ln -sf "$NODE_HOME/bin/$tool" "$BIN/$tool"
done
echo "node $("$BIN/node" --version), npm $("$BIN/npm" --version)"

# CPython, managed by uv, and the project's .venv on it. A .venv an earlier Build made from another
# interpreter (a system Python) is rebuilt, since uv would otherwise keep it.
"$BIN/uv" python install "$PYTHON_VERSION"
python="$("$BIN/uv" python find "$PYTHON_VERSION")"
base() { "$1" -c 'import sys; print(sys.base_prefix)'; }
if [ -x .venv/bin/python ] && [ "$(base .venv/bin/python)" != "$(base "$python")" ]; then
  rm -rf .venv
fi
"$BIN/uv" sync --frozen --python "$python"
