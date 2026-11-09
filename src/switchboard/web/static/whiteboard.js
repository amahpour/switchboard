// One fixed sandboxed board frame. The app alone owns authentication and the room socket (§56.8).
'use strict';
(function () {
  const rules = window.SBBoardRules;
  let frame = null, current = null, options = null, serial = 0, session = '', ready = false;
  const cache = new Map();
  const pendingImages = new Set();
  function post(action, extra) {
    if (frame && ready && current) frame.contentWindow.postMessage({ t: 'sb-board', action, room: current, session, ...extra }, '*');
  }
  function dark() { return document.documentElement.dataset.theme === 'dark' || (document.documentElement.dataset.theme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches); }
  function init() { post('init', { elements: cache.get(current) || [], dark: dark() }); }
  function close() {
    if (frame) frame.remove();
    frame = null; current = null; ready = false; pendingImages.clear();
    if (options) options.host.classList.add('hidden');
  }
  window.addEventListener('message', function (ev) {
    if (!frame || ev.source !== frame.contentWindow || ev.origin !== 'null') return;
    const m = ev.data;
    if (!m || typeof m !== 'object' || m.t !== 'sb-board' || typeof m.action !== 'string') return;
    if (m.action === 'ready' && Object.keys(m).every(k => ['t', 'action', 'session', 'room'].includes(k))) {
      ready = true; init(); return;
    }
    if (m.session !== session || m.room !== current) return;
    if (m.action === 'change' && Object.keys(m).every(k => ['t', 'action', 'session', 'room', 'elements'].includes(k))) {
      const elements = rules.batch(m.elements);
      if (!elements || !elements.length) return;
      const merged = rules.batch(rules.merge(cache.get(current) || [], elements));
      if (!merged) return;
      if (options.send(current, elements)) cache.set(current, merged);
      else options.status.textContent = 'Disconnected. Reconnect before drawing.';
    } else if (m.action === 'rendered' && Object.keys(m).length === 4) {
      const count = (cache.get(current) || []).filter(e => !e.isDeleted).length;
      options.status.textContent = 'Live · ' + count + (count === 1 ? ' shape' : ' shapes');
    } else if (m.action === 'image' && Object.keys(m).length === 6 && typeof m.request === 'string' && pendingImages.has(m.request) && /^[0-9a-f]{32}$/.test(m.request) && m.bytes instanceof ArrayBuffer && m.bytes.byteLength <= 600000) {
      const bytes = new Uint8Array(m.bytes);
      if (bytes.length < 24 || bytes[0] !== 137 || bytes[1] !== 80 || bytes[2] !== 78 || bytes[3] !== 71) return;
      const view = new DataView(m.bytes);
      if (view.getUint32(16) > 1200 || view.getUint32(20) > 1200) return;
      let binary = '';
      for (const byte of bytes) binary += String.fromCharCode(byte);
      pendingImages.delete(m.request);
      options.result(current, m.request, btoa(binary));
    } else if (m.action === 'error' && Object.keys(m).length === 5 && typeof m.text === 'string' && m.text.length <= 200) {
      options.status.textContent = 'The drawing exceeds the board limits.';
    }
  });
  window.SBWhiteboard = {
    configure(value) { options = value; },
    open(room) {
      if (!options || typeof room !== 'string') return;
      close(); current = room; session = 'board-' + (++serial);
      frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('credentialless', '');
      frame.setAttribute('title', 'Whiteboard for ' + room);
      frame.src = '/board';
      options.status.textContent = 'Opening whiteboard…';
      options.host.replaceChildren(frame);
      options.host.classList.remove('hidden');
    },
    close,
    receive(room, raw, full) {
      const elements = rules.batch(raw);
      if (!elements || typeof room !== 'string') return;
      const merged = rules.batch(full ? elements : rules.merge(cache.get(room) || [], elements));
      if (!merged) return;
      cache.set(room, merged);
      if (current === room) post('elements', { elements: merged });
    },
    task(room, task) {
      if (room === current && ready && task && task.kind === 'image' && typeof task.id === 'string' && /^[0-9a-f]{32}$/.test(task.id) && pendingImages.size < 16) {
        pendingImages.add(task.id);
        post('export', { request: task.id });
        setTimeout(() => pendingImages.delete(task.id), 4000);
      }
    },
    theme() { post('theme', { dark: dark() }); },
  };
})();
