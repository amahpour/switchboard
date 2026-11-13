// One fixed sandboxed board frame. The app alone owns authentication and the room socket (§56.8).
'use strict';
(function () {
  const rules = window.SBBoardRules;
  let frame = null, current = null, options = null, serial = 0, session = '', ready = false;
  const cache = new Map();
  const pendingImages = new Set();
  let playing = false, selected = null;
  const el = id => document.getElementById('whiteboard-' + id);
  // Match the summary's round-to-even row grouping, including negative coordinates and half rows.
  const row = y => { const n = Math.floor(y / 100), f = y / 100 - n; return f === 0.5 ? n + Math.abs(n % 2) : Math.round(y / 100); };
  const frames = () => (cache.get(current) || []).filter(e => e.type === 'frame' && !e.isDeleted).sort((a,b) => row(a.y) - row(b.y) || a.x - b.x);
  function showFrame(index) {
    const all = frames();
    if (!all.length) { stopPlay(); return; }
    const i = Math.max(0, Math.min(all.length-1, index)), f = all[i];
    selected = f.id;
    el('caption').textContent = (i+1) + ' / ' + all.length + ' · ' + (f.name || 'Untitled frame');
    el('prev').disabled = i === 0; el('next').disabled = i === all.length-1;
    post('present', { id: f.id });
  }
  // A step is fitted to the view when it is shown, so a view that changes size afterwards (a phone turned
  // sideways, a window dragged, full screen settling after Play) asks for the same step again, which fits it
  // again. It waits a moment for the frame's own size to settle, and sends nothing when nothing is playing.
  let refitTimer = 0;
  if (window.ResizeObserver && el('host')) {
    new ResizeObserver(() => {
      clearTimeout(refitTimer);
      refitTimer = setTimeout(() => { if (playing && selected) post('present', { id: selected }); }, 120);
    }).observe(el('host'));
  }
  function stopPlay() {
    playing = false; selected = null;
    if (document.fullscreenElement === el('panel')) document.exitFullscreen().catch(() => {});
    el('panel').classList.remove('whiteboard-playing');
    for (const key of ['prev','next','exit','caption']) el(key).classList.add('hidden');
    el('play').classList.remove('hidden');
    post('present', { id: null });
  }
  function navigate(key) {
    if (!playing) return;
    if (key === 'Escape') { stopPlay(); el('play').focus(); return; }
    showFrame(frames().findIndex(f => f.id === selected) + (key === 'ArrowRight' ? 1 : -1));
  }
  el('play').addEventListener('click', () => {
    if (!frames().length) return;
    playing = true; el('panel').classList.add('whiteboard-playing');
    for (const key of ['prev','next','exit','caption']) el(key).classList.remove('hidden');
    el('play').classList.add('hidden'); showFrame(0); el('exit').focus();
    if (el('panel').requestFullscreen) el('panel').requestFullscreen().catch(() => {});
  });
  document.addEventListener('fullscreenchange', () => { if (playing && !document.fullscreenElement) { stopPlay(); el('play').focus(); } });
  el('prev').addEventListener('click', () => navigate('ArrowLeft'));
  el('next').addEventListener('click', () => navigate('ArrowRight'));
  el('exit').addEventListener('click', () => navigate('Escape'));
  document.addEventListener('keydown', ev => { if (playing && ['ArrowRight','ArrowLeft','Escape'].includes(ev.key)) { ev.preventDefault(); navigate(ev.key); } });
  function post(action, extra) {
    if (frame && ready && current) frame.contentWindow.postMessage({ t: 'sb-board', action, room: current, session, ...extra }, '*');
  }
  function dark() { return document.documentElement.dataset.theme === 'dark' || (document.documentElement.dataset.theme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches); }
  function init() { post('init', { elements: cache.get(current) || [], dark: dark() }); }
  // The board mounts in Excalidraw's light theme and learns the app's theme from the `init` above, so for a
  // moment a dark app showed a white frame (#684). The frame stays hidden, over the host's own background
  // (the app's), until it reports its first render, which is after the theme is set; two animation frames
  // later it has painted that. A frame that never renders is shown after a while, not kept hidden for good.
  let revealTimer = 0;
  function reveal(now) {
    const shown = frame;
    const show = () => { if (frame && frame === shown) options.host.classList.remove('whiteboard-loading'); };
    if (now) show(); else requestAnimationFrame(() => requestAnimationFrame(show));
  }
  function close() {
    stopPlay();
    clearTimeout(revealTimer);
    if (frame) frame.remove();
    frame = null; current = null; ready = false; pendingImages.clear();
    if (options) options.host.classList.remove('whiteboard-loading');
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
    if (m.action === 'navigate' && Object.keys(m).length === 5 && ['ArrowRight','ArrowLeft','Escape'].includes(m.key)) { navigate(m.key);
    } else if (m.action === 'change' && Object.keys(m).every(k => ['t', 'action', 'session', 'room', 'elements'].includes(k))) {
      const elements = rules.batch(m.elements);
      if (!elements || !elements.length) return;
      const merged = rules.batch(rules.merge(cache.get(current) || [], elements));
      if (!merged) return;
      if (options.send(current, elements)) cache.set(current, merged);
      else options.status.textContent = 'Disconnected. Reconnect before drawing.';
    } else if (m.action === 'rendered' && Object.keys(m).length === 4) {
      reveal(false);
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
      close(); current = room; el('play').disabled = !(cache.get(room) || []).some(e => e.type === 'frame' && !e.isDeleted); session = 'board-' + (++serial);
      frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('credentialless', '');
      frame.setAttribute('title', 'Whiteboard for ' + room);
      frame.src = '/board';
      options.host.classList.add('whiteboard-loading');
      revealTimer = setTimeout(() => reveal(true), 8000);
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
      if (current === room) {
        post('elements', { elements: merged });
        el('play').disabled = !frames().length;
        if (playing) showFrame(Math.max(0, frames().findIndex(f => f.id === selected)));
      }
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
