// The opaque board document (DESIGN §56.8). No cookie, socket, API or untrusted HTML.
import './asset-path.js';
import { attachArrows, centerLabels } from './geometry.js';
import rules from '../../src/switchboard/web/static/board-rules.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw, MainMenu, restoreElements, exportToBlob } from '@excalidraw/excalidraw';
import '@excalidraw/excalidraw/index.css';
import './frame.css';
let playing = null, fullScene = [];
// What this frame has sent and not yet seen come back, by element id: the version it sent. A copy of the board that
// was already on its way when the frame sent more holds an older version of that element, and would put back the
// text a person has typed past (#937). It is dropped when a copy holds the element at that version or newer, and
// all of it when the broker answers with the whole board, which is what a refused write is answered with (#893).
const sent = new Map();
let api = null, session = null, room = null, known = new Map(), applying = false, timer = null, pointer = false, dirty = false;
const sig = e => `${e.version}:${e.versionNonce}`;
const post = (action, extra = {}) => parent.postMessage({ t: 'sb-board', action, session, room, ...extra }, '*');
function flush() {
  clearTimeout(timer);
  if (!api || applying || pointer || !session || !dirty || playing) return;
  const cleaned = rules.batch(api.getSceneElementsIncludingDeleted(), true);
  if (!cleaned) { post('error', { text: 'The drawing exceeds the board limits.' }); return; }
  const changed = cleaned.filter(e => known.get(e.id) !== sig(e));
  dirty = false;
  if (!changed.length) return;
  changed.forEach(e => { known.set(e.id, sig(e)); sent.set(e.id, e.version); });
  fullScene = rules.merge(fullScene, changed);
  post('change', { elements: changed });
}
function apply(elements, held = new Set()) {
  applying = true;
  const before = known;
  known = new Map(elements.map(e => [e.id, sig(e)]));
  // Restore the full stored scene, not a skeleton conversion of new shapes in isolation.
  // This preserves both arrow bindings and the existing targets' exact dimensions.
  // Only what changed since it was last sent or received is merged in: a shape that was sent, was not kept
  // and has not changed since is gone with the broker's board, while what is still being drawn stays (#893).
  // `held` is what was sent and is not in this copy at that version: the frame's own copy of it stays (#937).
  const live = dirty || held.size ? rules.batch(api.getSceneElementsIncludingDeleted(), true) || [] : [];
  const local = live.filter(e => held.has(e.id) || (dirty && before.get(e.id) !== sig(e)));
  live.forEach(e => { if (held.has(e.id) && before.get(e.id) === sig(e)) known.set(e.id, sig(e)); });
  const scene = local.length ? rules.merge(elements, local) : elements;
  fullScene = scene;
  const shown = playing ? scene.filter(e => e.id === playing || e.frameId === playing) : scene;
  api.updateScene({ elements: attachArrows(centerLabels(restoreElements(shown, null, { repairBindings: true, refreshDimensions: true }))), captureUpdate: 'NEVER' });
  applying = false;
  const count = scene.filter(e => !e.isDeleted).length;
  document.getElementById('board-count').textContent = `${count} ${count === 1 ? 'shape' : 'shapes'}`;
  post('rendered');
}
window.addEventListener('message', async ev => {
  const m = ev.data;
  if (ev.source !== parent || !m || m.t !== 'sb-board' || typeof m.session !== 'string' || typeof m.room !== 'string' || !api) return;
  if (m.action === 'init') {
    const elements = rules.batch(m.elements, true);
    if (!elements) return;
    sent.clear();
    session = m.session; room = m.room;
    api.updateScene({ appState: { theme: m.dark === true ? 'dark' : 'light' } });
    apply(elements);
    if (elements.some(e => !e.isDeleted)) api.scrollToContent(undefined, { fitToContent: true });
  } else if (m.session === session && m.room === room && m.action === 'elements') {
    const elements = rules.batch(m.elements, true);
    if (!elements || typeof m.full !== 'boolean') return;
    // A whole board from the broker answers every write sent before it (a refused one included): nothing is held.
    // Any other copy holds back an element only where it is older than what this frame sent, or has not got it yet.
    const held = new Set();
    if (m.full) sent.clear();
    else {
      const seen = new Map(elements.map(e => [e.id, e.version]));
      for (const [id, version] of sent) {
        if (seen.has(id) && seen.get(id) >= version) sent.delete(id); else held.add(id);
      }
    }
    apply(elements, held);
  } else if (m.session === session && m.action === 'theme') {
    api.updateScene({ appState: { theme: m.dark === true ? 'dark' : 'light' } });
  } else if (m.session === session && m.room === room && m.action === 'present' && (m.id === null || (typeof m.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(m.id)))) {
    if (!playing) { flush(); fullScene = rules.batch(api.getSceneElementsIncludingDeleted(), true) || fullScene; }
    dirty = false; playing = m.id;
    api.updateScene({ appState: { viewModeEnabled: !!playing, selectedElementIds: {} } });
    apply(fullScene);
    api.scrollToContent(undefined, { fitToViewport: true, viewportZoomFactor: 0.85, maxZoom: 8 });
  } else if (m.session === session && m.room === room && m.action === 'convert' && typeof m.request === 'string' && /^[0-9a-f]{32}$/.test(m.request)) {
    try {
      const { convert } = await import('./mermaid.js');
      const elements = await convert(m.source);
      post('converted', { request: m.request, elements });
    } catch { post('converted', { request: m.request, error: 'cannot_convert' }); }
  } else if (m.session === session && m.action === 'export' && typeof m.request === 'string') {
    // The picture is of the committed scene the parent sends, never the live drawing, which may hold text the broker has not read (#893).
    const blob = await exportToBlob({ elements: attachArrows(centerLabels(restoreElements(rules.batch(m.elements, true) || [], null, { repairBindings: true, refreshDimensions: true }))), appState: { ...api.getAppState(), exportBackground: true }, files: {}, maxWidthOrHeight: 1200 });
    if (blob.size <= 600000) post('image', { request: m.request, bytes: await blob.arrayBuffer() });
  }
});
document.addEventListener('pointerdown', () => { pointer = true; dirty = true; });
// Capture, so a key is counted before Excalidraw acts on it: Escape commits an open text inside Excalidraw's own
// handler, and that change was dropped when the board was not yet dirty (a flush had just gone out, #933).
document.addEventListener('keydown', () => { dirty = true; }, true);
document.addEventListener('input', () => { dirty = true; clearTimeout(timer); timer = setTimeout(flush, 250); });
document.addEventListener('pointerup', () => { pointer = false; timer = setTimeout(flush, 0); });
// Font loading is asynchronous in a cold browser. Remeasure after its metrics are available.
document.fonts.addEventListener('loadingdone', () => { if (api && session) apply(fullScene); });
createRoot(document.getElementById('board-root')).render(
  <Excalidraw langCode="en" aiEnabled={false} excalidrawAPI={value => { api = value; window.sbBoard = { api, flush }; post('ready'); }}
    initialData={{ appState: { currentItemFontFamily: 1 }, elements: [] }}
    onChange={() => { if (!applying && dirty) { clearTimeout(timer); timer = setTimeout(flush, 250); } }}
    onLinkOpen={(_e, ev) => ev.preventDefault()}
    onPaste={() => false}
    UIOptions={{ tools: { image: false }, canvasActions: { loadScene: false, saveToActiveFile: false, export: false, saveAsImage: false, changeViewBackgroundColor: false, toggleTheme: false, clearCanvas: false } }}>
    <MainMenu />
  </Excalidraw>
);

document.addEventListener('keydown', ev => { if (playing && ['ArrowRight','ArrowLeft','Escape'].includes(ev.key)) { ev.preventDefault(); post('navigate', { key: ev.key }); } });
