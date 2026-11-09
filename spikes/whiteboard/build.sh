#!/bin/sh
# Builds the board's bundle into $BOARD_BUILD (default ./build). Needs: node_modules from `npm ci`
# in this folder (or $NODE_DIR), and esbuild on PATH or in $ESBUILD. Nothing built is committed.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
nm=${NODE_DIR:-$here}/node_modules
out=${BOARD_BUILD:-$here/build}
esb=${ESBUILD:-esbuild}
rm -rf "$out"; mkdir -p "$out"
NODE_PATH="$nm" "$esb" "$here/board.jsx" --bundle --minify --format=esm --splitting --outdir="$out" \
  --entry-names=board --chunk-names=chunks/[name]-[hash] --loader:.woff2=file --loader:.woff=file \
  --conditions=production --define:process.env.NODE_ENV='"production"' --define:import.meta.env='{"DEV":false}' --log-level=warning
mkdir -p "$out/fonts"
for f in Excalifont Nunito Virgil Assistant Cascadia Liberation Lilita ComicShanns; do
  cp -r "$nm/@excalidraw/excalidraw/dist/prod/fonts/$f" "$out/fonts/"
done

# Excalidraw 0.18.1 adds a hard-coded fallback font host (esm.sh) to every font's source list, which a
# strict CSP blocks with a console error per font face. Point it at this broker instead (the broker
# serves the same fonts there): the one patch to the package, made on the built files.
grep -rl 'https://esm.sh/' "$out" | while read -r f; do
  sed -i 's#`https://esm.sh/\${#`${new URL("esm/", document.baseURI).href}${#g' "$f"
done
