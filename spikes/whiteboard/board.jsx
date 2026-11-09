// The board, running inside a sandboxed iframe (no allow-same-origin): an opaque origin, so it has
// no cookie and no way to call the broker. It talks to its host page with postMessage only; the
// host relays to the WebSocket. Throwaway prototype for issue 622.
import "./asset-path.js"; // first: Excalidraw reads the asset path when its module is evaluated
import React from "react";
import { createRoot } from "react-dom/client";
import "@excalidraw/excalidraw/index.css";
import {
  Excalidraw, convertToExcalidrawElements, reconcileElements, exportToBlob,
} from "@excalidraw/excalidraw";
import { parseMermaidToExcalidraw } from "@excalidraw/mermaid-to-excalidraw";

const known = new Map(); // id -> "version:nonce" last seen (sent or received), so we never echo
let api = null;
const post = (m) => parent.postMessage(m, "*");
const sig = (e) => e.version + ":" + e.versionNonce;

// Nothing on the board is ever a link, whoever sent it (the broker strips it too).
const scrub = (els) => els.map((e) => (e.link ? { ...e, link: null } : e));

function remember(els) { for (const e of els) known.set(e.id, sig(e)); }

function applyRemote(incoming) {
  if (!api) return;
  const local = api.getSceneElementsIncludingDeleted();
  const merged = reconcileElements(local, scrub(incoming).map((e) => ({ ...e })), api.getAppState());
  remember(incoming);
  api.updateScene({ elements: merged, captureUpdate: "NEVER" });
}

function withFrame(sk, frameId) { return frameId ? { ...sk, frameId } : sk; }

async function convert(skeleton) {
  // convertToExcalidrawElements binds arrows only to shapes in the same list, so a shape already on the
  // board that an arrow names goes in too, and comes out again.
  const mine = new Set(skeleton.map((s) => s.id).filter(Boolean));
  const refs = new Set();
  for (const s of skeleton) for (const k of ["start", "end"]) if (s[k] && s[k].id && !mine.has(s[k].id)) refs.add(s[k].id);
  const extra = api.getSceneElementsIncludingDeleted().filter((e) => refs.has(e.id));
  if (refs.size && extra.length !== refs.size) throw new Error("no such shape to connect");
  const all = convertToExcalidrawElements([...extra, ...skeleton], { regenerateIds: false });
  const els = scrub(all.filter((e) => !refs.has(e.id)));
  for (const sk of skeleton) {
    if (sk.frameId) for (const e of els) if (e.id === sk.id) e.frameId = sk.frameId;
  }
  return els;
}

async function onHost(ev) {
  const m = ev.data || {};
  if (m.t === "elements") { applyRemote(m.els); return; }
  if (m.t === "init") { applyRemote(m.els); return; }
  if (m.t === "skeleton") {
    try {
      const els = await convert(m.skeleton);
      const local = api.getSceneElementsIncludingDeleted();
      // Arrows bind to shapes already on the board: mark those shapes as bound back.
      const byId = new Map(local.map((e) => [e.id, e]));
      for (const e of els) {
        if (e.type === "arrow") for (const b of [e.startBinding, e.endBinding]) {
          const t = b && byId.get(b.elementId);
          if (t) { t.boundElements = [...(t.boundElements || []), { id: e.id, type: "arrow" }]; t.version++; t.versionNonce = (Math.random() * 2 ** 31) | 0; }
        }
      }
      api.updateScene({ elements: [...local, ...els], captureUpdate: "NEVER" });
      remember(els); remember([...byId.values()].filter((x) => x.boundElements));
      post({ t: "result", req: m.req, els: [...els, ...local.filter((x) => (x.boundElements || []).some((b) => els.some((n) => n.id === b.id)))] });
    } catch (e) { post({ t: "result", req: m.req, error: String(e) }); }
    return;
  }
  if (m.t === "mermaid") {
    try {
      const { elements } = await parseMermaidToExcalidraw(m.source, { themeVariables: { fontSize: "16px" } });
      const local = api.getSceneElementsIncludingDeleted();
      const live = local.filter((e) => !e.isDeleted);
      const els = scrub(convertToExcalidrawElements(elements, { regenerateIds: true }));
      if (live.length && els.length) { // Mermaid lays out from the origin: put the drawing in free space to the right
        const right = Math.max(...live.map((e) => e.x + (e.width || 0)));
        const top = Math.min(...live.map((e) => e.y));
        const dx = right + 80 - Math.min(...els.map((e) => e.x)), dy = top - Math.min(...els.map((e) => e.y));
        for (const e of els) { e.x += dx; e.y += dy; }
      }
      api.updateScene({ elements: [...local, ...els], captureUpdate: "NEVER" });
      remember(els);
      post({ t: "result", req: m.req, els });
    } catch (e) { post({ t: "result", req: m.req, error: "mermaid: " + String(e).slice(0, 300) }); }
    return;
  }
  if (m.t === "export") {
    try {
      const blob = await exportToBlob({ elements: api.getSceneElements(), appState: { exportBackground: true, viewBackgroundColor: "#fff" }, files: null, mimeType: "image/png", maxWidthOrHeight: 1200 });
      const buf = new Uint8Array(await blob.arrayBuffer());
      let s = ""; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
      post({ t: "result", req: m.req, png: btoa(s) });
    } catch (e) { post({ t: "result", req: m.req, error: String(e) }); }
    return;
  }
  if (m.t === "patch") { // test driver only: a person edits one shape (same effect as dragging it)
    const els = api.getSceneElementsIncludingDeleted().map((e) => (e.id === m.id
      ? { ...e, ...m.props, version: e.version + 1, versionNonce: (Math.random() * 2 ** 31) | 0 } : e));
    api.updateScene({ elements: els });
    return;
  }
  if (m.t === "eval") { // test driver only: draw as a person would, with the API
    try {
      const els = await convert(m.skeleton);
      const local = api.getSceneElementsIncludingDeleted();
      api.updateScene({ elements: [...local, ...els] });
      post({ t: "result", req: m.req, ok: true });
    } catch (e) { post({ t: "result", req: m.req, error: String(e) }); }
  }
}

let timer = null;
function onChange(elements) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    const changed = elements.filter((e) => known.get(e.id) !== sig(e));
    if (!changed.length) return;
    remember(changed);
    post({ t: "elements", els: changed });
  }, 120);
}

addEventListener("message", (ev) => { if (ev.source === parent) onHost(ev); });
createRoot(document.getElementById("root")).render(
  <Excalidraw
    excalidrawAPI={(a) => { api = a; post({ t: "ready" }); }}
    onChange={onChange}
    UIOptions={{ canvasActions: { loadScene: false, saveToActiveFile: false, export: false, toggleTheme: true } }}
    libraryReturnUrl=""
  />,
);
