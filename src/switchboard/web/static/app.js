// switchboard web UI. Vanilla JS, no build step.
// Rendering uses textContent only; the page never holds a token.
'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const HARNESS_LETTER = { claude: 'C', codex: 'X', cursor: 'U', devin: 'D', test: 'T', unknown: '?' };
  const MAX_LINES = 2000;

  const state = {
    me: null,          // { human, test_mode, version, port }
    rooms: new Map(),  // name -> { name, slug, lastId, msgs: [], members: [], settings: {}, unread: 0 }
    active: null,
    ws: null,
    wsOpen: false,
    backoff: 500,
    pingTimer: null,
  };

  // ------------------------------------------------------------ helpers
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function hhmmss(ts) {
    const d = new Date(ts * 1000);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function dayKey(ts) {
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
  }

  function dayLabel(ts) {
    return new Date(ts * 1000).toDateString();
  }

  async function api(method, path, body) {
    const opts = { method: method, credentials: 'same-origin', cache: 'no-store', headers: {} };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-Switchboard'] = '1';
      opts.body = JSON.stringify(body || {});
    }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.status === 401) {
      location.replace('/');
      throw new Error('signed out');
    }
    if (!r.ok) throw new Error((data && data.message) || r.statusText || ('HTTP ' + r.status));
    return data;
  }

  function room(name) {
    let r = state.rooms.get(name);
    if (!r) {
      r = { name: name, slug: name.replace(/^#/, ''), lastId: 0, msgs: [], members: [], settings: {}, unread: 0 };
      state.rooms.set(name, r);
    }
    return r;
  }

  function nearBottom(log) {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }

  // ---------------------------------------------------------- rendering
  function mentionsMe(m) {
    if (!state.me || m.sender_kind === 'human') return false;
    const me = state.me.human.toLowerCase();
    return (m.mentions || []).indexOf(me) >= 0;
  }

  function renderMsg(m) {
    const line = el('div', 'line k-' + m.kind);
    line.dataset.id = String(m.id);
    line.append(el('span', 'ts', '[' + hhmmss(m.ts) + ']'), ' ');
    if (m.kind === 'chat') {
      const cls = m.sender_kind === 'human' ? 'nick-human' : (m.sender_kind === 'agent' ? 'nick-agent' : 'nick-system');
      if (m.sender_kind === 'agent' && m.harness) {
        const b = el('span', 'harness', HARNESS_LETTER[m.harness] || '?');
        b.title = m.harness;
        line.append(b);
      }
      line.append(el('span', 'nick ' + cls, '<' + m.from + '>'), ' ', el('span', 'text', m.text));
      if (m.via === 'cli') line.append(el('span', 'via', 'via cli'));
      if (mentionsMe(m)) line.classList.add('mention');
    } else if (m.kind === 'join' || m.kind === 'leave') {
      line.append(el('span', 'door', m.kind === 'join' ? '\u{1F6AA}→ ' : '←\u{1F6AA} '),
        el('span', 'text', m.from + ' ' + (m.text || (m.kind === 'join' ? 'joined' : 'left'))));
    } else {
      line.append(el('span', 'text', '*** ' + m.text));
      // A warn notice (loop guard, budget, watchdog) arrives once, as this room line.
      if (m.level === 'warn') line.classList.add('warn');
    }
    return line;
  }

  function renderLocal(text, isError, body) {
    const line = el('div', 'line local' + (isError ? ' error' : ''));
    line.append(el('span', 'ts', '[' + hhmmss(Date.now() / 1000) + ']'), ' ', el('span', 'text', text));
    if (body) line.append(el('pre', 'cmd-out', body));
    const log = $('log');
    const stick = nearBottom(log);
    log.append(line);
    if (stick) log.scrollTop = log.scrollHeight;
  }

  function renderLog() {
    const log = $('log');
    log.replaceChildren();
    const r = state.active ? state.rooms.get(state.active) : null;
    const noRooms = state.rooms.size === 0;
    $('empty').classList.toggle('hidden', !noRooms);
    log.classList.toggle('hidden', noRooms);
    const input = $('input');
    input.disabled = noRooms;
    $('send').disabled = noRooms;
    input.placeholder = noRooms
      ? 'Create a room first: click "Create #build" above.'
      : 'Type a message. Enter sends, Shift+Enter adds a line. /help lists commands.';
    if (!r) return;
    let lastDay = null;
    const frag = document.createDocumentFragment();
    for (const m of r.msgs) {
      const k = dayKey(m.ts);
      if (k !== lastDay) {
        frag.append(el('div', 'day', dayLabel(m.ts)));
        lastDay = k;
      }
      frag.append(renderMsg(m));
    }
    log.append(frag);
    log.scrollTop = log.scrollHeight;
  }

  function appendMsg(r, m) {
    if (m.id <= r.lastId) return;  // at-least-once: drop duplicates
    const prev = r.msgs.length ? r.msgs[r.msgs.length - 1] : null;
    r.msgs.push(m);
    r.lastId = m.id;
    if (r.msgs.length > MAX_LINES) r.msgs.splice(0, r.msgs.length - MAX_LINES);
    if (r.name !== state.active) {
      if (m.kind === 'chat' && m.sender_kind !== 'human') r.unread += 1;
      renderTabs();
      return;
    }
    const log = $('log');
    const stick = nearBottom(log);
    if (!prev || dayKey(prev.ts) !== dayKey(m.ts)) log.append(el('div', 'day', dayLabel(m.ts)));
    log.append(renderMsg(m));
    if (stick) log.scrollTop = log.scrollHeight;
  }

  function renderTabs() {
    const tabs = $('tabs');
    tabs.replaceChildren();
    const names = Array.from(state.rooms.keys()).sort();
    for (const name of names) {
      const r = state.rooms.get(name);
      const b = el('button', 'tab', name);
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(name === state.active));
      if (r.unread > 0 && name !== state.active) b.append(el('span', 'badge', r.unread > 99 ? '99+' : r.unread));
      b.addEventListener('click', function () { selectRoom(name); });
      tabs.append(b);
    }
    updateTitle();
  }

  function updateTitle() {
    let unread = 0;
    state.rooms.forEach(function (r) { if (r.name !== state.active) unread += r.unread; });
    const base = state.active ? 'switchboard — ' + state.active : 'switchboard';
    $('title').textContent = base;
    document.title = (unread ? '(' + unread + ') ' : '') + base;
  }

  function buddyRow(m) {
    const li = el('li', 'buddy');
    const row = el('div', 'row');
    const dot = el('span', 'dot s-' + m.status);
    dot.title = m.status;
    const hl = el('span', 'hl', HARNESS_LETTER[m.harness] || '?');
    hl.title = m.harness;
    row.append(dot, hl, el('span', 'name', m.name));
    if (m.approval_mode === 'bypass') {
      const f = el('span', 'flag warn', '⚠');
      f.title = 'approvals are off in this session: room messages can make it act without asking';
      row.append(f);
    } else if (m.approval_mode === 'unknown') {
      const f = el('span', 'flag warn', '?');
      f.title = 'approval mode unknown: treat like ⚠';
      row.append(f);
    }
    if (m.env_leak) row.append(el('span', 'flag', 'env shared'));
    if (m.held) row.append(el('span', 'flag', '⏸ held'));
    if (m.queued) row.append(el('span', 'flag', m.queued + ' queued'));
    if (m.inflight) row.append(el('span', 'flag', m.inflight + ' in flight'));
    li.append(row);
    const tier = (m.tier || 'no tier yet') + (m.tier_note ? ' (' + m.tier_note + ')' : '');
    li.append(el('div', 'sub', m.status + ' · ' + tier));
    if (m.away) li.append(el('div', 'away', 'away: ' + m.away));
    if (m.parked) li.append(el('div', 'parked', 'parked — needs a poke' + (m.parked_reason ? ' (' + m.parked_reason + ')' : '')));
    return li;
  }

  function renderBuddies() {
    const me = $('buddy-me');
    me.replaceChildren();
    if (state.me) {
      const li = el('li', 'buddy');
      const row = el('div', 'row');
      row.append(el('span', 'dot s-human'), el('span', 'name me', state.me.human), el('span', 'sub', '(you)'));
      li.append(row);
      me.append(li);
    }
    const list = $('buddy-list');
    list.replaceChildren();
    const r = state.active ? state.rooms.get(state.active) : null;
    const members = r ? r.members : [];
    $('agents-title').textContent = 'Agents (' + members.length + ')';
    if (!members.length) {
      list.append(el('li', 'buddy sub', 'nobody yet — ask an agent to join ' + (state.active || 'a room')));
    }
    for (const m of members) list.append(buddyRow(m));
    const bypass = members.some(function (m) { return m.approval_mode === 'bypass'; });
    const prompting = members.some(function (m) { return m.approval_mode === 'prompting'; });
    $('banner-bridge').classList.toggle('hidden', !(bypass && prompting));
  }

  function renderStatus() {
    const r = state.active ? state.rooms.get(state.active) : null;
    const s = (r && r.settings) || {};
    const conn = $('st-conn');
    conn.textContent = state.wsOpen ? 'online' : 'reconnecting…';
    conn.classList.toggle('bad', !state.wsOpen);
    const st = $('st-state');
    const paused = $('banner-paused');
    if (r && s.paused) {
      st.textContent = 'PAUSED';
      st.classList.add('bad');
      paused.textContent = '⏸ ' + r.name + ' is paused (' + (s.paused_reason || 'paused') + '). No agent wakes until /resume.';
      paused.classList.remove('hidden');
    } else {
      st.textContent = r ? 'active' : '';
      st.classList.remove('bad');
      paused.classList.add('hidden');
    }
    $('st-budget').textContent = r && s.budget_per_hour !== undefined ? 'budget ' + s.budget_remaining + '/' + s.budget_per_hour : '';
    const hops = $('st-hops');
    const guardOff = !!(r && s.hop_limit === 0);
    if (!r || s.hop_limit === undefined) {
      hops.textContent = '';
      hops.title = '';
    } else if (guardOff) {
      hops.textContent = 'loop guard off ⚠';
      hops.title = 'hop limit 0: agents may message each other without limit (' + s.hop_count +
        ' in a row now). /hops <n> turns the loop guard back on.';
    } else {
      hops.textContent = 'hops ' + s.hop_count + '/' + s.hop_limit;
      hops.title = 'agent messages in a row with none from you / the loop guard limit. /hops <n> changes it.';
    }
    hops.classList.toggle('bad', guardOff);
    $('banner-test').classList.toggle('hidden', !(state.me && state.me.test_mode));
  }

  function selectRoom(name) {
    if (!state.rooms.has(name)) return;
    state.active = name;
    state.rooms.get(name).unread = 0;
    if (location.hash !== '#' + state.rooms.get(name).slug) {
      history.replaceState(null, '', '#' + state.rooms.get(name).slug);
    }
    renderTabs();
    renderLog();
    renderBuddies();
    renderStatus();
    $('input').focus();
  }

  // ---------------------------------------------------------- websocket
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const ws = new WebSocket(proto + location.host + '/ws');
    state.ws = ws;
    ws.addEventListener('open', function () {
      state.wsOpen = true;
      state.backoff = 500;
      hello(Array.from(state.rooms.keys()));
      renderStatus();
      clearInterval(state.pingTimer);
      state.pingTimer = setInterval(function () {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'ping' }));
      }, 25000);
    });
    ws.addEventListener('message', function (ev) {
      let f;
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      onFrame(f);
    });
    ws.addEventListener('close', function () {
      state.wsOpen = false;
      clearInterval(state.pingTimer);
      renderStatus();
      const wait = state.backoff;
      state.backoff = Math.min(state.backoff * 2, 10000);
      setTimeout(function () {
        // A closed socket may mean a revoked session: /api/me sends us to sign-on if so.
        api('GET', '/api/me').then(function () { connect(); }, function () { setTimeout(connect, 2000); });
      }, wait);
    });
  }

  function hello(names) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !names.length) return;
    const after = {};
    for (const n of names) {
      const r = state.rooms.get(n);
      if (r && r.lastId > 0) after[n] = r.lastId;
    }
    state.ws.send(JSON.stringify({ t: 'hello', rooms: names, after: after }));
  }

  function onFrame(f) {
    if (f.t === 'msg' && f.room && f.msg) {
      appendMsg(room(f.room), f.msg);
    } else if (f.t === 'members' && f.room) {
      room(f.room).members = f.members || [];
      if (f.room === state.active) renderBuddies();
    } else if (f.t === 'room' && f.room) {
      room(f.room).settings = f.settings || {};
      if (f.room === state.active) renderStatus();
    } else if (f.t === 'notice') {
      if (!f.room || f.room === state.active) renderLocal('*** ' + f.text, f.level === 'warn');
    } else if (f.t === 'rooms') {
      loadRooms().catch(function () {});
    }
  }

  // -------------------------------------------------------------- input
  async function send(text) {
    const r = state.active ? state.rooms.get(state.active) : null;
    if (!r) {
      $('create-build').focus();
      return false;
    }
    const path = '/api/rooms/' + encodeURIComponent(r.slug);
    try {
      if (text.startsWith('//')) {
        await api('POST', path + '/say', { text: text.slice(1) });
      } else if (text.startsWith('/')) {
        const res = await api('POST', path + '/command', { text: text });
        renderLocal(text.split(/\s+/)[0], !res.ok, res.text);
      } else {
        await api('POST', path + '/say', { text: text });
      }
      return true;
    } catch (e) {
      renderLocal(String(e.message || e), true);
      return false;
    }
  }

  async function createRoom(name) {
    try {
      const res = await api('POST', '/api/rooms', { name: name });
      await loadRooms();
      selectRoom(res.room.name);
      $('input').focus();
    } catch (e) {
      if (state.rooms.size) renderLocal(String(e.message || e), true);
      else window.alert(String(e.message || e));
    }
  }

  function bindInput() {
    const input = $('input');
    let queue = Promise.resolve();  // sends go out one at a time, in order
    $('composer').addEventListener('submit', function (ev) {
      ev.preventDefault();
      const text = input.value.replace(/\s+$/, '');
      if (!text.trim()) return;
      input.value = '';
      queue = queue.then(function () { return send(text); }).then(function (ok) {
        if (!ok && !input.value) input.value = text;  // give a failed message back
      });
      input.focus();
    });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        $('composer').requestSubmit();
      }
    });
    $('new-room').addEventListener('click', function () {
      const name = window.prompt('New room name (for example #build):', '#');
      if (!name || name === '#') return;
      createRoom(name);
    });
    $('create-build').addEventListener('click', function () { createRoom('#build'); });
    $('logout').addEventListener('click', async function () {
      try { await api('POST', '/logout', {}); } catch (e) { /* already signed out */ }
      location.replace('/');
    });
    const toggle = $('buddy-toggle');
    toggle.addEventListener('click', function () {
      const open = $('buddies').classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
    });
  }

  // --------------------------------------------------------------- boot
  async function loadRooms() {
    const data = await api('GET', '/api/rooms');
    const fresh = [];
    for (const r of data.rooms) {
      if (!state.rooms.has(r.name)) fresh.push(r.name);
      room(r.name).settings = r.settings || {};
    }
    renderTabs();
    if (fresh.length) hello(fresh);
    if (!state.active && state.rooms.size) {
      const want = '#' + decodeURIComponent(location.hash.replace(/^#/, ''));
      selectRoom(state.rooms.has(want) ? want : Array.from(state.rooms.keys()).sort()[0]);
    } else {
      renderLog();
      renderStatus();
    }
    return fresh;
  }

  async function boot() {
    bindInput();
    try {
      state.me = await api('GET', '/api/me');
    } catch (e) {
      return;
    }
    renderBuddies();
    await loadRooms();
    renderStatus();
    connect();
    // Keep the sliding session (and its cookie) fresh while the page is open.
    setInterval(function () { api('GET', '/api/me').catch(function () {}); }, 30 * 60 * 1000);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
