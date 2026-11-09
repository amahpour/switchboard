// The opaque board document (DESIGN §56.8). No cookie, socket, API or untrusted HTML.
import './asset-path.js';
import { attachArrows } from './geometry.js';
import rules from '../../src/switchboard/web/static/board-rules.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw, MainMenu, restoreElements, exportToBlob } from '@excalidraw/excalidraw';
import '@excalidraw/excalidraw/index.css';
import './frame.css';
let api = null, session = null, room = null, known = new Map(), applying = false, timer = null, pointer = false, dirty = false;
const sig = e => `${e.version}:${e.versionNonce}`;
const post = (action, extra = {}) => parent.postMessage({ t: 'sb-board', action, session, room, ...extra }, '*');
function flush() {
  clearTimeout(timer);
  if (!api || applying || pointer || !session || !dirty) return;
  const cleaned = rules.batch(api.getSceneElementsIncludingDeleted());
  if (!cleaned) { post('error', { text: 'The drawing exceeds the board limits.' }); return; }
  const changed = cleaned.filter(e => known.get(e.id) !== sig(e));
  dirty = false;
  if (!changed.length) return;
  changed.forEach(e => known.set(e.id, sig(e)));
  post('change', { elements: changed });
}
function apply(elements) {
  applying = true;
  known = new Map(elements.map(e => [e.id, sig(e)]));
  // Restore the full stored scene, not a skeleton conversion of new shapes in isolation.
  // This preserves both arrow bindings and the existing targets' exact dimensions.
  const local = dirty ? rules.batch(api.getSceneElementsIncludingDeleted()) : null;
  const scene = local ? rules.merge(elements, local) : elements;
  api.updateScene({ elements: attachArrows(restoreElements(scene, null)), captureUpdate: 'NEVER' });
  applying = false;
  const count = scene.filter(e => !e.isDeleted).length;
  document.getElementById('board-count').textContent = `${count} ${count === 1 ? 'shape' : 'shapes'}`;
  post('rendered');
}
window.addEventListener('message', async ev => {
  const m = ev.data;
  if (ev.source !== parent || !m || m.t !== 'sb-board' || typeof m.session !== 'string' || typeof m.room !== 'string' || !api) return;
  if (m.action === 'init') {
    const elements = rules.batch(m.elements);
    if (!elements) return;
    session = m.session; room = m.room;
    api.updateScene({ appState: { theme: m.dark === true ? 'dark' : 'light' } });
    apply(elements);
    if (elements.some(e => !e.isDeleted)) api.scrollToContent(undefined, { fitToContent: true });
  } else if (m.session === session && m.room === room && m.action === 'elements') {
    const elements = rules.batch(m.elements);
    if (elements) apply(elements);
  } else if (m.session === session && m.action === 'theme') {
    api.updateScene({ appState: { theme: m.dark === true ? 'dark' : 'light' } });
  } else if (m.session === session && m.action === 'export' && typeof m.request === 'string') {
    const blob = await exportToBlob({ elements: api.getSceneElements(), appState: { ...api.getAppState(), exportBackground: true }, files: {}, maxWidthOrHeight: 1200 });
    if (blob.size <= 600000) post('image', { request: m.request, bytes: await blob.arrayBuffer() });
  }
});
document.addEventListener('pointerdown', () => { pointer = true; dirty = true; });
document.addEventListener('keydown', () => { dirty = true; });
document.addEventListener('input', () => { dirty = true; clearTimeout(timer); timer = setTimeout(flush, 250); });
document.addEventListener('pointerup', () => { pointer = false; timer = setTimeout(flush, 0); });
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
