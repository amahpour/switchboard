// The host page: the only code with the cookie/ticket. It owns the WebSocket and relays between it and
// the sandboxed frame by postMessage, checking where each message came from. (Prototype of the §33 shape.)
'use strict';
(function () {
  const q = new URLSearchParams(location.search);
  const who = q.get('who') || 'alice';
  const ticket = q.get('ticket') || '';
  const frame = document.getElementById('board');
  const status = document.getElementById('status');
  document.getElementById('who').textContent = who;
  let ready = false; const backlog = [];
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?who=' + encodeURIComponent(who) + '&ticket=' + encodeURIComponent(ticket));
  const toFrame = (m) => frame.contentWindow.postMessage(m, '*');
  ws.onopen = () => { status.textContent = 'connected'; };
  ws.onclose = (e) => { status.textContent = 'closed ' + e.code; };
  window.__rx = []; window.__tx = [];   // test hooks: what crossed the socket, and how big
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    window.__rx.push({ t: m.t, bytes: ev.data.length, at: performance.now(), n: (m.els || []).length });
    if (!ready) backlog.push(m); else toFrame(m);
  };
  window.addEventListener('message', (ev) => {
    if (ev.source !== frame.contentWindow) return;     // only our own frame
    const m = ev.data || {};
    if (m.t === 'ready') { ready = true; backlog.splice(0).forEach(toFrame); return; }
    if (m.t === 'elements' || m.t === 'result') {
      const raw = JSON.stringify(m); window.__tx.push({ t: m.t, bytes: raw.length, at: performance.now(), n: (m.els || []).length }); ws.send(raw);
    }
    if (m.t === 'result' && m.ok) window.__lastEval = m;
  });
  window.__draw = (skeleton) => toFrame({ t: 'eval', req: 'eval', skeleton }); // test driver
  window.__send = (m) => toFrame(m);
})();
