// Rebuild with the pinned lockfile and Node 22: npm ci --ignore-scripts, then node build.mjs <output>.
// The dependency folder defaults to this directory. No downloaded file is executed except the pinned esbuild binary.
import { createRequire } from 'node:module';
import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const deps = path.resolve(process.argv[3] || here);
const { build } = createRequire(path.join(deps, 'package.json'))('esbuild');
const notice = await readFile(path.join(here, 'NOTICE.template'), 'utf8');
const output = path.resolve(process.argv[2]);
if (output === path.parse(output).root || output === here || output === deps || here.startsWith(output + path.sep) || deps.startsWith(output + path.sep)) throw new Error('output must not contain the sources or dependencies');
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
const built = await build({ metafile: true, absWorkingDir: here, nodePaths: [path.join(deps, 'node_modules')], entryPoints: [path.join(here, 'frame.jsx')], outdir: output, entryNames: 'board', chunkNames: 'chunks/[name]-[hash]', bundle: true, minify: true, format: 'esm', splitting: true, conditions: ['production'], define: { 'process.env.NODE_ENV': '"production"', 'import.meta.env': '{"DEV":false}' }, loader: { '.woff2': 'file', '.woff': 'file' }, legalComments: 'linked' });
for (const name of ['Excalifont', 'Nunito', 'Virgil', 'Assistant', 'Cascadia', 'Liberation', 'Lilita', 'ComicShanns', 'Xiaolai']) {
  await cp(path.join(deps, 'node_modules/@excalidraw/excalidraw/dist/prod/fonts', name), path.join(output, 'fonts', name), { recursive: true });
}
async function patch(dir) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) await patch(p);
    else if (e.name.endsWith('.js')) {
      const s = await readFile(p, 'utf8');
      // The one upstream patch: replace the hard-coded CDN fallback with this document's local asset root.
      const local = s.replace(/`https:\/\/esm\.sh\/\$\{[\w$]+\.PKG_NAME\?`\$\{[\w$]+\.PKG_NAME\}@\$\{[\w$]+\.PKG_VERSION\}`:"@excalidraw\/excalidraw"\}\/dist\/prod\/`/g, 'new URL("/board-assets/",document.baseURI).href');
      // Browser KaTeX never runs a CLI. Remove its unused CLI-help metadata, not a runtime guard.
      await writeFile(p, local.replace(',cli:"-T, --trust"', ''));
    }
  }
}
await patch(output);

// Include the full licenses of the packages actually bundled, in stable package order.
const packages = new Map();
for (const input of Object.keys(built.metafile.inputs).sort()) {
  const absolute = path.resolve(here, input);
  if (!absolute.includes('/node_modules/')) continue;
  let dir = path.dirname(absolute);
  while (dir !== path.dirname(dir)) {
    try {
      const pkg = JSON.parse(await readFile(path.join(dir, 'package.json'), 'utf8'));
      if (pkg.name && pkg.version) packages.set(`${pkg.name}@${pkg.version}`, { dir, pkg });
      break;
    } catch { dir = path.dirname(dir); }
  }
}
let licenses = '';
for (const [label, { dir, pkg }] of [...packages].sort((a,b) => a[0].localeCompare(b[0], 'en'))) {
  licenses += `\n--- ${label} (${pkg.license || 'see bundled notice'}) ---\n`;
  for (const name of (await readdir(dir)).filter(n => /^(licen[cs]e|copying|notice)([.-]|$)/i.test(n)).sort()) {
    try { licenses += await readFile(path.join(dir, name), 'utf8'); } catch { /* directories are not notices */ }
    licenses += '\n';
  }
}
async function bytes(dir) {
  let n = 0;
  for (const e of await readdir(dir, { withFileTypes: true })) {
    n += e.isDirectory() ? await bytes(path.join(dir, e.name)) : (await readFile(path.join(dir, e.name))).length;
  }
  return n;
}
await writeFile(path.join(output, 'NOTICE'), notice + `\nBuilt assets excluding this NOTICE: ${await bytes(output)} bytes.\n` + licenses);
