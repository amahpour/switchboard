// switchboard web UI ("A · Native", issue #19). Vanilla JS, no build step, one IIFE.
//
// Security model (DESIGN.md §5.5, §12.1, §29):
// - Every node is built with createElement / textContent. No HTML strings are ever parsed,
//   so text from agents (or remote machines) can never become markup or script.
// - Chat bodies go through md.js (window.SBMarkdown.render), which builds a DOM fragment
//   from a small Markdown subset. md.js owns the only link path in the static JS (http(s)
//   only, never this switchboard page). This file never sets a link target itself.
// - Icons are inline SVG built with createElementNS (SVG namespace only) and a fixed path
//   table below; no icon data ever comes from the network.
// - The page never holds a token: the session is an HttpOnly cookie, and every write
//   carries the X-Switchboard header (the broker's CSRF check).
// - Nothing is kept in browser storage; all state lives in memory for this page view.
//
// Test harness contract (tests/web_app_harness.js runs this file in node against a tiny
// fake DOM): the boot, msg, members, room, rooms, notice, send and command paths use only
// getElementById, createElement, createDocumentFragment, className/classList, dataset,
// textContent, append, replaceChildren, setAttribute, addEventListener, focus,
// requestSubmit and plain property sets. Anything else (createElementNS, navigator,
// matchMedia, querySelector, closest, scrollIntoView, ...) is feature-checked or used
// only from click handlers.
'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const MAX_LINES = 2000;
  const CONT_WINDOW_S = 300;       // a same-sender message within 5 minutes is a continuation row
  const REFETCH_MS = 800;          // Inspector refetch debounce
  const COPIED_MS = 1600;          // how long "Copied" / "Session id copied" shows

  // harness key -> [avatar monogram, display name]
  const HARNESS = {
    claude: ['CC', 'Claude Code'], codex: ['CX', 'Codex'], devin: ['DV', 'Devin'],
    cursor: ['CU', 'Cursor'], test: ['TS', 'Test'], unknown: ['??', 'Unknown'],
  };
  const STATUS_WORD = {
    starting: 'Starting', idle: 'Idle', busy: 'Busy', 'waiting-approval': 'Waiting for approval', offline: 'Offline',
  };

  // blocked(reason) on a remote row: a few words; the panel has the whole story (DESIGN.md §27.11)
  const BLOCK_SHORT = {
    host_key: 'host key changed', auth: 'key refused', files: 'key files', proto: 'version mismatch',
    name: 'wrong name', shell_noise: 'shell prints text', replaced: 'link taken over', local_broker: 'broker there',
    test_mode: 'test mode', ssh_bin: 'no /usr/bin/ssh', negotiate: 'no common algorithm',
    command: 'forced command failed', satellite: 'satellite refused', exposed: 'stdio exposed',
  };

  const state = {
    me: null,          // { human, test_mode, version, port, hosted, passkeys, fresh }
    rooms: new Map(),  // name -> { name, slug, id, createdAt, lastId, msgs: [], members: [], settings: {}, unread: 0 }
    closed: 0,         // how many closed rooms there are (GET /api/rooms), for the Closed row
    closedRooms: [],   // GET /api/closed-rooms, as the Closed sheet shows it
    active: null,
    ws: null,
    wsOpen: false,
    backoff: 500,
    pingTimer: null,
    remotes: [],       // GET /api/remotes and the `remotes` event: every remote link's state
    remotesAt: 0,      // when that snapshot arrived (ms), for the retry countdowns
    remotesError: null,
    enabling: new Set(),  // remotes whose Enable is in flight (a long poll)
    chipsKey: null,    // remotesKey() of the sidebar rows and the sheet as rendered
    panelKey: null,
    sendQueue: null,   // sends (typed or from the Inspector) go out one at a time, in order
    // Inspector: which agent, the latest member-detail GET, and its UI toggles
    inspect: null,     // { room, name } or null (the pane shows Members)
    detail: null,      // { room, name, data } from GET /api/rooms/{slug}/members/{name}
    inspSeq: 0,        // only the newest detail response is applied
    inspTimer: null,
    inspUi: null,      // { menu, confirm, queueOpen, copied, pokeCopied }
    insp: {},          // references to Inspector nodes built here (ids are not looked up)
    rowRefs: new Map(),  // member name -> its row button, for focus on "back"
    pop: null,         // composer popover: { kind: 'palette'|'mentions', items, sel, start }
    sheetOpener: null, // the control that opened the Closed, Remotes or Passkeys sheet (focus returns to it)
    passkeyBusy: false, // an Add a passkey ceremony is under way
    // machines that dial in (a hosted broker, §31.8): GET /api/machines and the `machines` event
    machinesHosted: false,
    machines: [],
    machineCodes: [],  // the live codes nobody used yet: [{ name, expires_in_s }]
    machinesAt: 0,     // when that list arrived (ms): what's left of those codes
    pairing: null,     // the code this page made: { name, code, install, join, expiresAt (ms) }
    codeBusy: false,   // making a code (a passkey check may come first)
    machineBusy: new Map(),  // machine name -> 'approve' | 'remove', while that request runs
    machineDraft: '',  // the name typed in Add a machine, kept across renders
    copiedCmd: null,   // which pairing command was just copied ('install' | 'join')
    focusAfter: null,  // a data-focus key to move to at the next render (the new approval card)
    machinesKey: null, // machinesKey() of the sheet as rendered
    machineRowsKey: null,
    sheetOpenerKey: null, // its data-focus key, to find it again if renderChips rebuilt it
    draft: null,       // composer text a catch-up entry replaced; it comes back after the command (fillComposer)
  };

  // ------------------------------------------------------------ helpers
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function btn(cls, text) {
    const b = el('button', cls, text);
    b.type = 'button';
    return b;
  }

  // removeAttribute where the DOM has it (the harness's fake DOM does not)
  function unsetAttr(e, k) {
    if (typeof e.removeAttribute === 'function') e.removeAttribute(k);
    else e.setAttribute(k, '');
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // `bench@fpga-pi` for a member or sender on a remote machine, the plain name on this one
  function label(name, host) { return host ? name + '@' + host : name; }

  function hhmm(ts) {
    const d = new Date(ts * 1000);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function dayKey(ts) {
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
  }

  function dayLabel(ts) {
    const now = Date.now() / 1000;
    if (dayKey(ts) === dayKey(now)) return 'Today';
    if (dayKey(ts) === dayKey(now - 86400)) return 'Yesterday';
    return new Date(ts * 1000).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }

  // "Today, 14:02" / "Yesterday, 09:10" / "Mon, Sep 28, 14:02"
  function dayTime(ts) { return dayLabel(ts) + ', ' + hhmm(ts); }

  function timeEl(ts, cls) {
    const t = el('time', cls, hhmm(ts));
    const d = new Date(ts * 1000);
    t.dateTime = d.toISOString();
    t.title = d.toLocaleString();
    return t;
  }

  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : null; }

  function has(v) { return v !== null && v !== undefined; }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  // A fixed-width local stamp, "2026-09-28 13:40" (Composer.dc's Closed rooms card).
  function stamp(ts) {
    const d = new Date(ts * 1000);
    const p2 = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
  }

  function harnessOf(h) { return HARNESS[h] || HARNESS.unknown; }

  function mq(q) {
    try { return typeof window.matchMedia === 'function' && window.matchMedia(q).matches; } catch (e) { return false; }
  }
  function narrow() { return mq('(max-width: 1100px)'); }  // the pane is an overlay (drawer or sheet)
  function phone() { return mq('(max-width: 760px)'); }    // rooms drawer, bottom sheet

  function clipboardWrite(text) {
    if (typeof navigator === 'undefined' || !navigator.clipboard || !navigator.clipboard.writeText) {
      return Promise.reject(new Error('no clipboard'));
    }
    return navigator.clipboard.writeText(text);
  }

  // The first line of a message, markup stripped (md.js), for reply lines, the queue and the timeline.
  function firstLine(text, max) {
    const md = window.SBMarkdown;
    if (md && typeof md.firstLine === 'function') {
      try { return md.firstLine(String(text || ''), max); } catch (e) { /* fall through */ }
    }
    const line = String(text || '').split('\n').find(function (l) { return l.trim(); }) || '';
    return line.length > max ? line.slice(0, max - 1) + '…' : line;
  }

  // ---------------------------------------------------------------- icons
  // A fixed table of 16x16 stroke icons (paths from the approved mockups). Stroke and fill
  // come from CSS (`.ic`). Built with the SVG namespace only; without createElementNS (the
  // node harness) an icon is an empty span.ic.ic-<name>. An icon never has text content.
  function rr(x, y, w, h, r) {  // a rounded rectangle as path data
    const iw = w - 2 * r;
    const ih = h - 2 * r;
    const a = 'a' + r + ' ' + r + ' 0 0 1 ';
    return 'M' + (x + r) + ' ' + y + 'h' + iw + a + r + ' ' + r + 'v' + ih + a + (-r) + ' ' + r +
      'h' + (-iw) + a + (-r) + ' ' + (-r) + 'v' + (-ih) + a + r + ' ' + (-r) + 'z';
  }
  function circ(cx, cy, r) {
    return 'M' + (cx - r) + ' ' + cy + 'a' + r + ' ' + r + ' 0 1 0 ' + 2 * r + ' 0a' + r + ' ' + r + ' 0 1 0 ' + (-2 * r) + ' 0';
  }
  const ICONS = {
    hash: 'M6 2.5 4.8 13.5M11.2 2.5 10 13.5M2.8 6h11M2.2 10h11',
    plus: 'M8 3v10M3 8h10',
    archive: 'M2 3.5h12v3H2zM3 6.5v6h10v-6M6.5 9h3',
    server: rr(2.5, 3, 11, 4.5, 1.2) + rr(2.5, 8.5, 11, 4.5, 1.2) + 'M5 5.25h.01M5 10.75h.01',
    laptop: rr(3, 3.5, 10, 7, 1.2) + 'M1.5 12.5h13',
    'chev-left': 'M10 3 5 8l5 5',
    'chev-right': 'm6 3.5 4.5 4.5L6 12.5',
    'chev-down': 'm4 6 4 4 4-4',
    pause: 'M5.5 3.5v9M10.5 3.5v9',
    play: 'M5 3.5v9l7-4.5-7-4.5Z',
    sidebar: rr(2, 3, 12, 10, 2) + 'M10 3v10',
    people: circ(6, 5, 2.5) + 'M1.5 13.5c.4-2.4 2.3-3.8 4.5-3.8s4.1 1.4 4.5 3.8M10.5 2.7a2.5 2.5 0 0 1 0 4.6M12 9.9c1.4.5 2.3 1.7 2.5 3.6',
    at: circ(8, 8, 2.5) + 'M10.5 8v1a2 2 0 0 0 4 0V8a6.5 6.5 0 1 0-2.6 5.2',
    slash: rr(2, 2, 12, 12, 3) + 'M9.8 5 6.2 11',
    'arrow-up': 'M8 13V3M3.5 7.5 8 3l4.5 4.5',
    logout: 'M6 3H3.5v10H6M9.5 5 12.5 8l-3 3M12.5 8H6',
    copy: rr(5.5, 5.5, 8, 8, 1.5) + 'M10.5 3.5V3a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h.5',
    check: 'm3.5 8.5 3 3 6-7',
    reply: 'M13.5 12.5V8.5a3 3 0 0 0-3-3H5.5M8 3 5.5 5.5 8 8',
    door: 'M3.5 14V2.5h8V14M1.5 14h13M9 8.5h.01',
    info: circ(8, 8, 6) + 'M8 7.3V11M8 5h.01',
    warn: 'M8 2.3 14.6 13.6H1.4L8 2.3ZM8 6.6v3.2M8 11.8h.01',
    hourglass: 'M4.5 2.5h7M4.5 13.5h7M5.5 2.5c0 3 5 3 5 5.5s-5 2.5-5 5.5M10.5 2.5c0 3-5 3-5 5.5s5 2.5 5 5.5',
    history: 'M2.5 8a5.5 5.5 0 1 0 1.7-4M2.5 2.5V5.5h3M8 5.5V8l2 1.5',
    kick: circ(6.5, 5, 2.5) + 'M1.5 13.5c.4-2.4 2.3-3.8 5-3.8 1.2 0 2.2.3 3 .8M11 10l3.5 3.5M14.5 10 11 13.5',
    close: 'M4 4l8 8M12 4l-8 8',
    terminal: 'M3 4.5 6.5 8 3 11.5M8.5 11.5H13',
    browser: rr(2, 3, 12, 10, 1.5) + 'M2 6.2h12',   // "web only": needs this signed-in browser
    key: circ(5.5, 8, 3) + 'M8.5 8h5.5M12 8v2.5M14 8v1.5',
  };

  function icon(name) {
    if (typeof document.createElementNS !== 'function') return el('span', 'ic ic-' + name);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('class', 'ic ic-' + name);
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', ICONS[name] || '');
    svg.append(p);
    return svg;
  }

  // ------------------------------------------------------------------ api
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
    if (!r.ok) {
      const err = new Error((data && data.message) || r.statusText || ('HTTP ' + r.status));
      err.status = r.status;  // the Inspector tells a departed member (404) from a hiccup
      err.code = data && data.error;  // 'reauth': a passkey check first (withFreshCheck)
      throw err;
    }
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

  function activeRoom() { return state.active ? state.rooms.get(state.active) || null : null; }

  function memberOf(r, name, host) {
    if (!r) return null;
    return r.members.find(function (m) { return m.name === name && (m.host || '') === (host || ''); }) || null;
  }

  function nearBottom(log) {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }

  // The log shrinks when a band appears above it (approvals off, paused, test mode) or the
  // composer grows; the browser keeps scrollTop, so the newest rows would slide out of view.
  // Remember whether the reader was at the end (updated on every scroll) and, when the log's
  // box changes size, pin it back to the end. Feature-checked: the node harness has no
  // ResizeObserver, and the page works without it (it just no longer re-pins).
  function keepLogPinned() {
    const log = $('log');
    if (typeof ResizeObserver !== 'function' || !log.addEventListener) return;
    let pinned = true;
    log.addEventListener('scroll', function () { pinned = nearBottom(log); }, { passive: true });
    new ResizeObserver(function () {
      if (pinned) log.scrollTop = log.scrollHeight;
    }).observe(log);
  }

  // ---------------------------------------------------- shared small parts
  function avatar(harness, kind, name, extra) {
    let a;
    if (kind === 'human') a = el('span', 'avatar human', (name || '?').charAt(0).toUpperCase());
    else if (kind === 'system') a = el('span', 'avatar sys', '⚙');
    else a = el('span', 'avatar h-' + (HARNESS[harness] ? harness : 'unknown'), harnessOf(harness)[0]);
    if (extra) for (const c of extra.split(' ')) if (c) a.classList.add(c);
    a.setAttribute('aria-hidden', 'true');
    return a;
  }

  function statusKey(m) { return m.parked ? 'parked' : m.status; }

  function statusWord(m) { return m.parked ? 'Parked' : (STATUS_WORD[m.status] || m.status || ''); }

  function withDot(av, m) {
    const d = el('span', 'dot s-' + statusKey(m));
    d.title = statusWord(m);
    av.append(d);
    return av;
  }

  function approvalsFlag(m) {
    if (m.approval_mode === 'bypass') {
      const f = el('span', 'flag-approvals');
      f.setAttribute('role', 'img');
      f.setAttribute('aria-label', 'approvals off');
      f.title = 'approvals are off in this session: room messages can make it act without asking';
      f.append(icon('warn'));
      return f;
    }
    if (m.approval_mode === 'unknown') {
      const f = el('span', 'flag-unknown', '?');
      f.title = 'approval mode unknown: treat like approvals off';
      return f;
    }
    return null;
  }

  // a Codex thread proof still running reads "verifying...", as in /who (models.tier_label)
  function tierChip(m) {
    const t = m.tier_note === 'verifying...' ? m.tier_note
      : (m.tier || 'no tier yet') + (m.tier_note ? ' (' + m.tier_note + ')' : '');
    const c = el('code', 'tier', t);
    c.title = 'delivery tier: how switchboard wakes this agent';
    return c;
  }

  function remoteByName(name) { return state.remotes.find(function (x) { return x.name === name; }) || null; }

  // the host chip: "@fpga-pi" in lists, "fpga-pi · up 2 ms" in the Inspector, "This machine" locally
  function hostChip(m, long) {
    if (!m.host) {
      const c = el('span', 'host-chip local');
      c.append(icon('laptop'), 'This machine');
      c.title = 'runs on this machine';
      return c;
    }
    const rem = remoteByName(m.host);
    const c = el('span', 'host-chip remote');
    c.append(icon('server'), long ? m.host + (rem ? ' · ' + chipText(rem) : '') : '@' + m.host);
    c.title = 'runs on ' + m.host + ', a remote machine: its text may quote what that machine saw';
    if (rem && rem.state !== 'up') c.classList.add('bad');
    return c;
  }

  // ------------------------------------------------------------- log rows
  function mentionsMe(m) {
    if (!state.me || m.sender_kind === 'human') return false;
    const me = state.me.human.toLowerCase();
    return (m.mentions || []).indexOf(me) >= 0;
  }

  // Markdown for every sender (human messages too), via md.js; plain text if md.js is missing.
  function mdBody(text, mentions) {
    const box = el('div', 'msg-text md');
    const md = window.SBMarkdown;
    if (md && typeof md.render === 'function') {
      try {
        const lower = (mentions || []).map(function (x) { return String(x).toLowerCase(); });
        box.append(md.render(String(text || ''), { mentions: lower, localHost: location.hostname || '' }));
        return box;
      } catch (e) { box.replaceChildren(); }
    }
    box.textContent = String(text || '');
    box.classList.add('md-plain');  // white-space: pre-wrap
    return box;
  }

  // Is m a continuation of prev: same sender, host and kind, within 5 minutes, same day, no reply.
  function isCont(prev, m) {
    return !!(prev && prev.kind === 'chat' && m.kind === 'chat' && !m.reply_to &&
      prev.from === m.from && (prev.host || '') === (m.host || '') && prev.sender_kind === m.sender_kind &&
      m.ts - prev.ts >= 0 && m.ts - prev.ts < CONT_WINDOW_S && dayKey(prev.ts) === dayKey(m.ts));
  }

  function inspectedLabel() {
    const ins = state.inspect;
    if (!ins || ins.room !== state.active) return null;
    const m = memberOf(activeRoom(), ins.name, ins.host);
    return m ? label(m.name, m.host) : null;
  }

  function replyLine(r, m) {
    const parent = r.msgs.find(function (x) { return x.id === m.reply_to; });
    if (!parent) return null;  // not loaded: the reply line is omitted
    const b = btn('reply-to');
    b.title = 'Jump to the message it replies to';
    const g = el('span', 'reply-gutter');
    g.append(icon('reply'));
    b.append(g, avatar(parent.harness, parent.sender_kind, parent.from, 'mini'),
      el('span', 'reply-nick', label(parent.from, parent.host)),
      el('span', 'reply-snippet', firstLine(parent.text, 80)));
    b.addEventListener('click', function () { jumpTo(parent.id); });
    return b;
  }

  // click-driven only: scroll to a loaded row and flash it
  function jumpTo(id) {
    const log = $('log');
    if (typeof log.querySelector !== 'function') return;
    const row = log.querySelector('[data-id="' + String(Number(id)) + '"]');
    if (!row) return;
    if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' });
    row.classList.add('flash');
    setTimeout(function () { row.classList.remove('flash'); }, 1200);
  }

  function chatRow(r, m, prev) {
    const who = label(m.from, m.host);
    const line = el('article', 'line k-chat');
    line.dataset.id = String(m.id);
    line.dataset.from = who;
    if (mentionsMe(m)) line.classList.add('mention');
    if (m.sender_kind === 'agent' && inspectedLabel() === who) line.classList.add('sel');
    if (isCont(prev, m)) {
      line.classList.add('cont');
      line.append(timeEl(m.ts, 'ts gutter'), mdBody(m.text, m.mentions));
      return line;
    }
    const reply = m.reply_to ? replyLine(r, m) : null;
    if (reply) {
      line.classList.add('has-reply');
      line.append(reply);
    }
    line.append(avatar(m.harness, m.sender_kind, m.from));
    const body = el('div', 'msg-body');
    const head = el('div', 'msg-head');
    let nick;
    if (m.sender_kind === 'agent') {
      nick = btn('nick nick-agent', m.from);
      nick.title = 'Open ' + who + ' in the inspector';
      nick.addEventListener('click', function () { openInspector(m.from, m.host || ''); });
    } else {
      nick = el('span', 'nick nick-human' + (m.sender_kind === 'system' ? ' nick-system' : ''), m.from);
    }
    if (m.host) nick.append(el('span', 'host-tag', '@' + m.host));
    head.append(nick);
    const cur = m.sender_kind === 'agent' ? memberOf(r, m.from, m.host) : null;
    const flag = cur ? approvalsFlag(cur) : null;
    if (flag) head.append(flag);
    if (m.sender_kind === 'agent') head.append(el('span', 'harness-name', harnessOf(m.harness)[1]));
    if (m.via === 'cli') head.append(el('span', 'via', 'via cli'));
    head.append(timeEl(m.ts, 'ts'));
    body.append(head, mdBody(m.text, m.mentions));
    line.append(body);
    return line;
  }

  // One log row for a message. `live` is true when appendMsg adds it as it arrives:
  // only then is a warning announced (role=alert); history renders as role=note.
  function renderMsg(r, m, prev, live) {
    if (m.kind === 'chat') return chatRow(r, m, prev);
    const line = el('div', 'line k-' + m.kind);
    line.dataset.id = String(m.id);
    line.setAttribute('role', 'note');
    const text = el('span', 'text');
    if (m.kind === 'join' || m.kind === 'leave') {
      text.append(el('strong', null, label(m.from, m.host)), ' ' + (m.text || (m.kind === 'join' ? 'joined' : 'left')));
      line.append(icon('door'), text);
    } else {
      // A warn notice (loop guard, budget, watchdog) arrives once, as this room line.
      if (m.level === 'warn') line.classList.add('warn');
      const warn = line.classList.contains('warn');
      // the row's own warning icon replaces a leading "⚠" in the broker's text (no doubled glyph)
      text.textContent = warn ? String(m.text || '').replace(/^\u26a0\ufe0f?\s*/, '') : m.text;
      if (warn && live) line.setAttribute('role', 'alert');
      line.append(icon(warn ? 'warn' : (/parked/i.test(m.text || '') ? 'hourglass' : 'info')), text);
    }
    return line;
  }

  function dayRow(ts) {
    const d = el('div', 'line day', dayLabel(ts));
    d.setAttribute('role', 'separator');
    return d;
  }

  // A client-only line: a command's reply (in pre.cmd-out), an error, a notice for this page.
  // Its textContent is exactly text + body (the time is in its title, not on screen).
  function renderLocal(text, isError, body) {
    const line = el('div', 'line local' + (isError ? ' error' : ''));
    line.title = new Date().toLocaleTimeString();
    line.append(icon(isError ? 'warn' : 'terminal'), el('span', 'text', text));
    if (body) line.append(el('pre', 'cmd-out', body));
    const log = $('log');
    log.classList.remove('hidden');  // with no room left, the log still shows this line
    const stick = nearBottom(log);
    log.append(line);
    if (stick) log.scrollTop = log.scrollHeight;
  }

  // #room-empty: the active room has nobody in it and no chat yet
  function updateRoomEmpty() {
    const r = activeRoom();
    const show = !!r && r.members.length === 0 && !r.msgs.some(function (m) { return m.kind === 'chat'; });
    $('room-empty').classList.toggle('hidden', !show);
    if (r) $('join-line').textContent = 'join switchboard room ' + r.name;
  }

  function renderLog() {
    const log = $('log');
    log.replaceChildren();
    const r = activeRoom();
    const noRooms = state.rooms.size === 0;
    $('empty').classList.toggle('hidden', !noRooms);
    // first run (Welcome.dc): no members pane and no composer, only the Welcome steps
    $('app').classList.toggle('no-rooms', noRooms);
    log.classList.toggle('hidden', noRooms);
    const input = $('input');
    input.disabled = noRooms;
    $('send').disabled = noRooms;
    $('cmd-btn').disabled = noRooms;
    $('mention-btn').disabled = noRooms;
    input.placeholder = noRooms ? 'Create a room first.'
      : 'Message ' + (state.active || '') + ' — @ to mention, / for commands';
    updateRoomEmpty();
    if (!r) return;
    let lastDay = null;
    let prev = null;
    const frag = document.createDocumentFragment();
    for (const m of r.msgs) {
      const k = dayKey(m.ts);
      if (k !== lastDay) {
        frag.append(dayRow(m.ts));
        lastDay = k;
        prev = null;
      }
      frag.append(renderMsg(r, m, prev, false));
      prev = m;
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
    const newDay = !prev || dayKey(prev.ts) !== dayKey(m.ts);
    if (newDay) log.append(dayRow(m.ts));
    // group only under the row just above (a local line in between starts a new group)
    const kids = log.children;
    const last = kids.length ? kids[kids.length - 1] : null;
    const above = !newDay && prev && last && last.dataset && last.dataset.id === String(prev.id) ? prev : null;
    log.append(renderMsg(r, m, above, true));
    if (m.kind === 'chat') updateRoomEmpty();
    if (stick) log.scrollTop = log.scrollHeight;
  }

  // mark the inspected agent's chat rows (.sel) without redrawing the log
  function markSel() {
    const who = inspectedLabel();
    const r = activeRoom();
    for (const row of $('log').children) {
      if (!row.classList || !row.classList.contains('k-chat')) continue;
      let on = false;
      if (who && row.dataset.from === who) {
        const id = Number(row.dataset.id);
        const m = r ? r.msgs.find(function (x) { return x.id === id; }) : null;
        on = !!m && m.sender_kind === 'agent';  // a human named like an agent is never marked
      }
      row.classList.toggle('sel', on);
    }
  }

  // ------------------------------------------------------- sidebar, title
  function renderTabs() {
    const tabs = $('tabs');
    tabs.replaceChildren();
    const names = Array.from(state.rooms.keys()).sort();
    let total = 0;
    for (const name of names) {
      const r = state.rooms.get(name);
      const b = btn('room');
      b.dataset.room = name;
      b.title = name;
      if (name === state.active) b.setAttribute('aria-current', 'page');
      b.append(icon('hash'), el('span', 'room-name', name.replace(/^#/, '')));
      if (r.unread > 0 && name !== state.active) {
        total += r.unread;
        const badge = el('span', 'badge', r.unread > 99 ? '99+' : r.unread);
        badge.setAttribute('aria-label', r.unread + ' unread');
        b.append(badge);
      }
      b.addEventListener('click', function () {
        setNav(false);
        selectRoom(name);
      });
      tabs.append(b);
    }
    const rb = $('rooms-badge');
    rb.textContent = total > 99 ? '99+' : String(total);
    rb.classList.toggle('hidden', total === 0);
    updateTitle();
  }

  function updateTitle() {
    let unread = 0;
    state.rooms.forEach(function (r) { if (r.name !== state.active) unread += r.unread; });
    const base = state.active ? 'switchboard — ' + state.active : 'switchboard';
    document.title = (unread ? '(' + unread + ') ' : '') + base;
    const r = activeRoom();
    $('room-title').textContent = r ? r.name.replace(/^#/, '') : (state.rooms.size ? '' : 'Get started');
    $('room-sub').textContent = r && state.me ? state.me.human + ' and ' + plural(r.members.length, 'agent', 'agents') : '';
  }

  // ------------------------------------------------------------- members
  function memberRow(m) {
    const li = el('li');
    const b = btn('member');
    b.dataset.name = m.name;
    b.dataset.focus = 'member:' + m.name;
    b.title = 'Open ' + label(m.name, m.host) + ' in the inspector';
    const ins = state.inspect;
    if (ins && ins.room === state.active && ins.name === m.name) b.setAttribute('aria-current', 'true');
    const main = el('span', 'm-main');
    const nameLine = el('span', 'm-name-line');
    nameLine.append(el('span', 'm-name', m.name));
    if (m.host) nameLine.append(hostChip(m, false));
    const flag = approvalsFlag(m);
    if (flag) nameLine.append(flag);
    let st = statusWord(m) + ' · ' + harnessOf(m.harness)[1];
    if (m.queued) st += ' · ' + m.queued + ' queued';
    if (m.inflight) st += ' · ' + m.inflight + ' in flight';
    if (m.held) st += ' · held';
    if (m.env_leak) st += ' · env shared';
    const chips = el('span', 'm-chips');
    chips.append(tierChip(m));
    main.append(nameLine, el('span', 'm-status', st), chips);
    if (m.approval_mode === 'bypass') main.append(el('span', 'm-warn', 'Approvals off: what it reads can steer it'));
    else if (m.approval_mode === 'unknown') main.append(el('span', 'm-warn', 'Approval mode unknown: treat like approvals off'));
    if (m.away) main.append(el('span', 'm-away', 'Away: ' + m.away));
    if (m.parked) {
      const p = el('span', 'm-parked');
      p.append(icon('hourglass'), el('strong', null, 'Parked — needs a poke.'), m.parked_reason ? ' ' + m.parked_reason : '');
      main.append(p);
    }
    const chev = icon('chev-right');
    chev.classList.add('m-chev');
    b.append(withDot(avatar(m.harness, 'agent', m.name), m), main, chev);
    b.addEventListener('click', function () { openInspector(m.name, m.host || ''); });
    state.rowRefs.set(m.name, b);
    li.append(b);
    return li;
  }

  function renderBuddies() {
    const me = $('buddy-me');
    me.replaceChildren();
    if (state.me) {
      const li = el('li');
      const row = el('div', 'member me');
      const av = avatar(null, 'human', state.me.human);
      av.append(el('span', 'dot s-human'));
      const main = el('span', 'm-main');
      const nl = el('span', 'm-name-line');
      nl.append(el('span', 'm-name', state.me.human));
      main.append(nl, el('span', 'm-status', 'Human · online'));
      row.append(av, main);
      li.append(row);
      me.append(li);
      $('me-name').textContent = state.me.human;
      $('me-avatar').textContent = state.me.human.charAt(0).toUpperCase();
    }
    const list = $('buddy-list');
    const focused = focusKey(list);
    list.replaceChildren();
    state.rowRefs = new Map();
    const r = activeRoom();
    const members = r ? r.members : [];
    $('agents-title').textContent = 'Agents (' + members.length + ')';
    $('members-count').textContent = String(members.length + (state.me ? 1 : 0));
    if (!members.length) {
      list.append(el('li', 'member-empty', 'Nobody yet. Ask an agent to join ' + (state.active || 'a room') + '.'));
    }
    for (const m of members) list.append(memberRow(m));
    refocus(list, focused);
    const bypass = members.some(function (m) { return m.approval_mode === 'bypass'; });
    const prompting = members.some(function (m) { return m.approval_mode === 'prompting'; });
    $('banner-bridge').classList.toggle('hidden', !(bypass && prompting));
    renderApprovalsChip(members);
    renderBuddyPill(members);
    updateTitle();
  }

  // narrow: the Members pill (people icon, count, and a red badge for agents needing attention)
  function renderBuddyPill(members) {
    const total = members.length + (state.me ? 1 : 0);
    $('buddy-count').textContent = String(total);
    const need = [];
    for (const m of members) {
      if (m.approval_mode === 'bypass') need.push(m.name + ' (approvals off)');
      else if (m.approval_mode === 'unknown') need.push(m.name + ' (approval mode unknown)');
      if (m.parked) need.push(m.name + ' (parked)');
    }
    const alert = $('buddy-alert');
    alert.textContent = String(need.length);
    alert.classList.toggle('hidden', need.length === 0);
    const says = need.length ? 'Needs attention: ' + need.join(', ') : '';
    alert.setAttribute('aria-label', says);
    $('buddy-toggle').setAttribute('aria-label', 'Members (' + total + ')' + (says ? '. ' + says : ''));
  }

  // --------------------------------------------------------- header chips
  function meter(n, of) {
    const w = of > 0 ? Math.max(0, Math.min(10, Math.round(10 * n / of))) : 0;
    const m = el('span', 'meter');
    m.setAttribute('aria-hidden', 'true');
    m.append(el('span', 'meter-fill w' + w));
    return m;
  }

  function renderApprovalsChip(members) {
    const chip = $('st-approvals');
    const bypass = members.filter(function (m) { return m.approval_mode === 'bypass'; }).map(function (m) { return label(m.name, m.host); });
    const unknown = members.filter(function (m) { return m.approval_mode === 'unknown'; }).map(function (m) { return label(m.name, m.host); });
    chip.replaceChildren();
    chip.classList.toggle('hidden', !bypass.length && !unknown.length);
    if (bypass.length) {
      const t = el('span');
      t.append(el('span', 'wide-only', 'Approvals off: '), bypass.join(', '));
      chip.append(icon('warn'), t);
      chip.title = 'approvals are off in this session: room messages can make it act without asking';
    } else if (unknown.length) {
      chip.append(icon('warn'), el('span', null, 'Approval mode unknown: ' + unknown.join(', ')));
      chip.title = 'approval mode unknown: treat like approvals off';
    }
  }

  function renderStatus() {
    const r = activeRoom();
    const s = (r && r.settings) || {};
    const conn = $('st-conn');
    conn.textContent = state.wsOpen ? 'Connected' : 'Reconnecting…';
    conn.classList.toggle('bad', !state.wsOpen);
    $('status-chips').classList.toggle('hidden', !r);
    $('pause-toggle').classList.toggle('hidden', !r);

    const st = $('st-state');
    const paused = $('banner-paused');
    const pt = $('pause-toggle');
    st.replaceChildren(el('span', 'dot'));
    if (r && s.paused) {
      st.append('Paused');
      st.classList.add('bad');
      st.title = s.paused_reason || 'paused';
      paused.replaceChildren(icon('pause'),
        el('span', null, r.name + ' is paused (' + (s.paused_reason || 'paused') + '). No agent wakes until /resume.'));
      paused.classList.remove('hidden');
      pt.replaceChildren(icon('play'));
      pt.setAttribute('aria-label', 'Resume room');
      pt.title = 'Resume: wake agents again (web only)';
    } else {
      st.append('Running');
      st.classList.remove('bad');
      st.title = 'Not paused: agents are woken as messages arrive';
      paused.classList.add('hidden');
      pt.replaceChildren(icon('pause'));
      pt.setAttribute('aria-label', 'Pause room');
      pt.title = 'Pause: stop every agent wake until you resume';
    }

    const budget = $('st-budget');
    budget.replaceChildren();
    const hasBudget = !!r && s.budget_per_hour !== undefined;
    budget.classList.toggle('hidden', !hasBudget);
    if (hasBudget) {
      const left = Number(s.budget_remaining) || 0;
      const per = Number(s.budget_per_hour) || 0;
      budget.append(el('span', 'chip-label', 'Budget'), meter(left, per), el('span', 'chip-num', left + '/' + per));
      budget.classList.toggle('bad', left <= 0);                       // empty: danger fill
      budget.classList.toggle('low', left > 0 && per > 0 && left / per < 0.2);  // under 20%: amber fill
      budget.title = 'Wake budget: wakes left this hour';
    }

    const hops = $('st-hops');
    hops.replaceChildren();
    const guardOff = !!(r && s.hop_limit === 0);
    hops.classList.toggle('hidden', !r || s.hop_limit === undefined);
    if (!r || s.hop_limit === undefined) {
      hops.title = '';
    } else if (guardOff) {
      hops.textContent = 'loop guard off ⚠';
      hops.title = 'hop limit 0: agents may message each other without limit (' + s.hop_count +
        ' in a row now). /hops <n> turns the loop guard back on.';
    } else {
      hops.append(el('span', 'chip-label', 'Hops'), meter(s.hop_count, s.hop_limit),
        el('span', 'chip-num', s.hop_count + '/' + s.hop_limit));
      hops.title = 'Loop guard: agent messages in a row with none from you / the limit. /hops n changes it.';
    }
    hops.classList.toggle('bad', guardOff);
    $('banner-test').classList.toggle('hidden', !(state.me && state.me.test_mode));
  }

  function selectRoom(name) {
    if (!state.rooms.has(name)) return;
    if (state.inspect && state.inspect.room !== name) closeInspector(false);
    closePop();
    state.active = name;
    state.rooms.get(name).unread = 0;
    if (location.hash !== '#' + state.rooms.get(name).slug) {
      history.replaceState(null, '', '#' + state.rooms.get(name).slug);
    }
    $('palette').setAttribute('aria-label', 'Commands for ' + name);
    $('mentions').setAttribute('aria-label', 'Agents in ' + name);
    renderTabs();
    renderLog();
    renderBuddies();
    renderStatus();
    $('input').focus();
  }

  // ------------------------------------------------ pane, drawers, sheets
  // Desktop (> 1100 px): the pane is a column; #app.pane-closed hides it.
  // 761–1100 px: the pane is an overlay drawer; <= 760 px: a bottom sheet. Both open with
  // #app.sheet-open. <= 760 px the sidebar is a drawer too: #app.nav-open. #scrim shows
  // under any open overlay.
  function setOverlay() {
    const app = $('app');
    const isNarrow = narrow();
    if (!isNarrow) app.classList.remove('sheet-open');
    if (!phone()) app.classList.remove('nav-open');
    const sheet = app.classList.contains('sheet-open');
    const nav = app.classList.contains('nav-open');
    $('scrim').classList.toggle('hidden', !(sheet || nav));
    const shown = isNarrow ? sheet : !app.classList.contains('pane-closed');
    const pt = $('pane-toggle');
    pt.setAttribute('aria-pressed', String(shown));
    pt.setAttribute('aria-label', shown ? 'Hide members' : 'Show members');
    pt.title = shown ? 'Hide members' : 'Show members';
    $('buddy-toggle').setAttribute('aria-expanded', String(sheet));
    $('rooms-toggle').setAttribute('aria-expanded', String(nav));
    const pane = $('pane');
    const modal = sheet && phone();
    if (modal) {
      pane.setAttribute('role', 'dialog');
      pane.setAttribute('aria-modal', 'true');
    } else {
      unsetAttr(pane, 'role');
      unsetAttr(pane, 'aria-modal');
    }
    // aria-modal only tells assistive tech; inert makes it true for Tab and clicks as well: while
    // the phone sheet is open, the page behind the scrim takes no focus (Esc and the scrim close it)
    $('main').inert = modal;
    $('sidebar').inert = modal;
  }

  // Is keyboard focus somewhere inside `box`? (activeElement is null in the node harness.)
  function hasFocusIn(box) {
    const f = document.activeElement;
    return !!f && f !== box && typeof box.contains === 'function' && box.contains(f);
  }

  // Focus n and report whether it took: a node that is hidden (visibility or display), inert or
  // no longer in the page refuses focus without an error, and the caller then picks another.
  function tryFocus(n) {
    if (!n || typeof n.focus !== 'function') return false;
    n.focus();
    return document.activeElement === n;
  }

  // The first thing to focus in the pane's visible view: the Inspector's heading, else the first
  // member row, else the Members heading (tabindex=-1 in index.html).
  function focusPaneStart() {
    if (state.inspect && tryFocus(state.insp.name)) return;
    const first = state.rowRefs && state.rowRefs.size ? state.rowRefs.values().next().value : null;
    if (tryFocus(first)) return;
    tryFocus($('members-title'));
  }

  // After an agent's row went away (a kick, a leave) with focus in the pane: focus the row that
  // took its place in the list (the next one, or the previous one when it was last), else the
  // Members heading, else the composer, so focus never drops to <body>. `idx` is the gone row's
  // index in the list it was in; `gone` its name, skipped if that row is still on screen.
  function focusNear(idx, gone) {
    const r = activeRoom();
    const rest = (r ? r.members : []).filter(function (x) { return x.name !== gone; });
    if (rest.length) {
      const next = rest[Math.max(0, Math.min(idx, rest.length - 1))];
      if (tryFocus(state.rowRefs.get(next.name))) return;
    }
    if (tryFocus($('members-title'))) return;
    $('input').focus();
  }

  function showPane() {
    if (narrow()) $('app').classList.add('sheet-open');
    else $('app').classList.remove('pane-closed');
    setOverlay();
  }

  function setSheet(open) {
    $('app').classList.toggle('sheet-open', !!open);
    if (open) $('app').classList.remove('nav-open');
    setOverlay();
  }

  function setNav(open) {
    $('app').classList.toggle('nav-open', !!open);
    if (open) $('app').classList.remove('sheet-open');
    setOverlay();
  }

  function togglePane() {
    const app = $('app');
    if (narrow()) setSheet(!app.classList.contains('sheet-open'));
    else {
      app.classList.toggle('pane-closed');
      setOverlay();
    }
  }

  // ------------------------------------------------------------ Inspector
  function inspectedMember() {
    const ins = state.inspect;
    if (!ins) return null;
    const r = state.rooms.get(ins.room);
    return r ? r.members.find(function (m) { return m.name === ins.name; }) || null : null;
  }

  function setPaneView(inspecting) {
    $('pane').classList.toggle('inspecting', inspecting);
    $('members-view').classList.toggle('offscreen', inspecting);
    $('inspector').classList.toggle('offscreen', !inspecting);
  }

  function openInspector(name, host) {
    const r = activeRoom();
    if (!r) return;
    const m = memberOf(r, name, host) || r.members.find(function (x) { return x.name === name; });
    if (!m) {
      renderLocal(label(name, host) + ' is not in ' + r.name, true);
      return;
    }
    const same = state.inspect && state.inspect.room === r.name && state.inspect.name === m.name;
    state.inspect = { room: r.name, name: m.name, host: m.host || '' };
    if (!same) {
      state.detail = null;
      state.inspUi = { menu: false, confirm: false, queueOpen: false, copied: false, pokeCopied: false };
    }
    setPaneView(true);
    showPane();
    markSel();
    renderBuddies();
    renderInspector();
    if (state.insp.name) state.insp.name.focus();
    fetchDetail();
  }

  // Back to Members. Focus goes to the agent's row when asked (the back button, Esc), and also
  // whenever focus was inside the pane: the Inspector view is about to be hidden, and focus
  // left on a node in it would drop to <body>. If the agent's row is gone (it left or was
  // kicked), its neighbour gets focus instead (focusNear).
  function closeInspector(focus) {
    const was = state.inspect;
    const inPane = hasFocusIn($('pane'));
    state.inspect = null;
    state.detail = null;
    state.inspSeq += 1;  // a response still in flight is dropped
    clearTimeout(state.inspTimer);
    state.inspTimer = null;
    setPaneView(false);
    markSel();
    renderBuddies();
    if (focus || inPane) {
      const row = was ? state.rowRefs.get(was.name) : null;
      if (!tryFocus(row)) focusNear(was && was.idx >= 0 ? was.idx : 0, was ? was.name : '');
    }
  }

  function fetchDetail() {
    const ins = state.inspect;
    if (!ins) return;
    const r = state.rooms.get(ins.room);
    if (!r) return;
    const seq = ++state.inspSeq;
    api('GET', '/api/rooms/' + encodeURIComponent(r.slug) + '/members/' + encodeURIComponent(ins.name)).then(function (d) {
      if (seq !== state.inspSeq || !state.inspect || state.inspect.name !== ins.name || state.inspect.room !== ins.room) return;
      state.detail = { room: ins.room, name: ins.name, data: d || {} };
      renderInspector();
    }, function (e) {
      if (seq !== state.inspSeq) return;
      if (e && e.status === 404) memberLeft(ins);
    });
  }

  function scheduleRefetch() {
    clearTimeout(state.inspTimer);
    state.inspTimer = setTimeout(function () {
      state.inspTimer = null;
      fetchDetail();
    }, REFETCH_MS);
  }

  function memberLeft(ins) {
    if (!state.inspect || state.inspect.name !== ins.name || state.inspect.room !== ins.room) return;
    closeInspector(false);
    if (ins.room === state.active) renderLocal(label(ins.name, ins.host) + ' left ' + ins.room);
  }

  function fact(dl, k, v) {
    const dd = el('dd');
    if (typeof v === 'string') dd.textContent = v;
    else dd.append(v);
    dl.append(el('dt', null, k), dd);
    return dd;
  }

  function note(kind, ic, title, rest) {
    const n = el('div', 'note note-' + kind);
    const body = el('div');
    body.append(el('strong', null, title));
    for (const x of rest) body.append(x);
    n.append(icon(ic), body);
    return n;
  }

  // delivery timeline entry -> [label, detail, dot] (the whitelist is the broker's, §4.2)
  function timelineRow(e, r) {
    const from = (e.from || []).join(', ');
    const fromText = e.n ? e.n + ' from ' + from : (from ? 'from ' + from : '');
    if (e.kind === 'offer') {
      const p = e.path;
      if (p === 'inbox' || p === 'turn_start' || p === 'queue') {
        return ['Turn start', p + ': ' + (e.n || 0) + ' from ' + from, 'tl-turn'];
      }
      if (p === 'steer') return ['Steer', fromText, 'tl-steer'];
      if (p === 'hook_ctx' || p === 'hook_ups') return ['Mid-task', fromText, 'tl-steer'];
      if (p === 'wait') return ['wait() answered', fromText, 'tl-turn'];
      if (p === 'read' || p === 'say') return ['Pulled', fromText, 'tl-other'];
      if (p === 'stop_followup' || p === 'stop_block') return ['Re-armed', 'Stop asked it to continue', 'tl-other'];
      return ['Offered', fromText, 'tl-other'];
    }
    if (e.kind === 'expire') return ['Expired', e.reason || '', 'tl-other'];
    if (e.kind === 'cancel') return ['Cancelled', '', 'tl-other'];
    if (e.kind === 'parked') return ['Parked', e.reason || '', 'tl-parked'];
    if (e.kind === 'unparked') return ['Unparked', has(e.seconds) ? 'after ' + Math.round(e.seconds) + 's' : '', 'tl-other'];
    if (e.kind === 'rearm') return ['Re-armed', '', 'tl-other'];
    if (e.kind === 'requeue') return ['Offered again', has(e.n) ? String(e.n) : '', 'tl-other'];
    if (e.kind === 'watchdog_remind') return ['Watchdog', 'reminded', 'tl-other'];
    if (e.kind === 'watchdog_escalate') return ['Watchdog', 'told you', 'tl-other'];
    if (e.kind === 'pass') return ['Passed', 'called pass()', 'tl-other'];
    if (e.kind === 'said') {
      const msg = r ? r.msgs.find(function (x) { return x.id === e.id; }) : null;
      return ['Said', msg ? firstLine(msg.text, 60) : '', 'tl-said'];
    }
    return [String(e.kind || ''), '', 'tl-other'];
  }

  function renderInspector() {
    const ins = state.inspect;
    const body = $('insp-body');
    if (!ins) return;
    const r = state.rooms.get(ins.room);
    const m = inspectedMember();
    if (!r || !m) {
      memberLeft(ins);
      return;
    }
    const ui = state.inspUi;
    const d = state.detail && state.detail.room === ins.room && state.detail.name === ins.name ? state.detail.data : null;
    const dm = (d && d.member) || {};
    const who = label(m.name, m.host);
    const idx = r.members.indexOf(m);
    ins.idx = idx;  // where its row is, for focusNear() once the row is gone
    $('insp-pos').textContent = 'Agent ' + (idx + 1) + ' of ' + r.members.length;
    const focused = focusKey(body);
    const scroll = body.scrollTop;
    const refs = {};

    // 1. identity
    const id = el('div', 'insp-id');
    const nameH = el('h2', null, who);
    nameH.id = 'insp-name';
    nameH.tabIndex = -1;
    // every focusable node rebuilt here carries a data-focus key, so a re-render (the detail
    // fetch, a members frame, a refetch) puts focus back on the same control (refocus below)
    nameH.dataset.focus = 'name';
    const flag = approvalsFlag(m);
    if (flag) nameH.append(flag);
    refs.name = nameH;
    const since = has(dm.status_at) && !m.parked ? ' since ' + hhmm(dm.status_at) : '';
    const chips = el('div', 'insp-chips');
    chips.append(tierChip(m), hostChip(m, true));
    if (m.held) {
      const h = el('span', 'chip-held');
      h.append(icon('pause'), 'Held');
      h.title = 'Delivery is held: messages wait until you release it';
      chips.append(h);
    }
    const idText = el('div');
    idText.append(nameH, el('p', 'insp-status', statusWord(m) + since + ' · ' + harnessOf(m.harness)[1]));
    // the chips get the pane's full width (InspectorStates.dc), so tier + host + Held fit on one row
    id.append(withDot(avatar(m.harness, 'agent', m.name, 'lg'), m), idText, chips);

    // 2. needs attention
    const attn = el('section', 'insp-attn');
    attn.setAttribute('aria-label', 'Needs attention');
    const notes = [];
    if (m.approval_mode === 'bypass') {
      notes.push(note('danger', 'warn', 'Approvals off', [' What it reads (tool output, web pages) can steer it. Room messages can make it act without asking.']));
    } else if (m.approval_mode === 'unknown') {
      notes.push(note('danger', 'warn', 'Approval mode unknown', [' Treat it like approvals off.']));
    }
    if (m.parked) {
      const pokeCmd = 'read ' + r.name + ' and go back to wait()';
      const poke = el('div', 'poke');
      const cmdBox = el('div', 'poke-cmd');
      const copy = btn('copy-btn');
      copy.dataset.focus = 'copy-poke';
      const lbl = ui.pokeCopied ? 'Command copied' : 'Copy the command';
      copy.setAttribute('aria-label', lbl);
      copy.title = lbl;
      copy.append(icon(ui.pokeCopied ? 'check' : 'copy'));
      copy.addEventListener('click', function () {
        clipboardWrite(pokeCmd).then(function () {
          ui.pokeCopied = true;
          renderInspector();
          setTimeout(function () { ui.pokeCopied = false; renderInspector(); }, COPIED_MS);
        }, function () {});
      });
      const cmdCode = el('code', null, pokeCmd);
      cmdCode.title = pokeCmd;  // the box ellipsizes a long room name; the title and the copy keep it whole
      cmdBox.append(cmdCode, copy);
      poke.append(el('strong', null, 'Poke it:'), ' type this in its own terminal', cmdBox);
      const rest = [' ' + (m.parked_reason ? m.parked_reason + '.' : ''), poke];
      if (m.queued) rest.push(el('p', null, plural(m.queued, 'message is', 'messages are') + ' waiting.'));
      notes.push(note('amber', 'hourglass', 'Parked — needs a poke', rest));
    }
    if (m.status === 'waiting-approval') {
      notes.push(note('amber', 'hourglass', 'Waiting for approval', [' It is asking in its own terminal.']));
    }
    if (m.env_leak) {
      notes.push(note('muted', 'info', 'Environment shared', [' It runs with the Codex daemon’s environment.']));
    }
    if (notes.length) attn.append(el('div', 'group-label', 'Needs attention'), ...notes);

    // 3. details
    const details = el('section', 'insp-details');
    details.setAttribute('aria-label', 'Details');
    const dl = el('dl', 'insp-facts');
    if (!d) {
      fact(dl, 'Session', el('span', 'muted', 'loading…'));
    } else if (d.session && d.session.id) {
      const sid = String(d.session.id);
      const box = el('span');
      const code = el('code', 'sess', sid.length > 12 ? sid.slice(0, 4) + '…' + sid.slice(-4) : sid);
      code.title = sid;
      const copy = btn('copy-btn');
      copy.dataset.focus = 'copy-session';
      const lbl = ui.copied ? 'Session id copied' : 'Copy session id';
      copy.setAttribute('aria-label', lbl);
      copy.title = lbl;
      copy.append(icon(ui.copied ? 'check' : 'copy'));
      copy.addEventListener('click', function () {
        clipboardWrite(sid).then(function () {
          ui.copied = true;
          renderInspector();
          setTimeout(function () { ui.copied = false; renderInspector(); }, COPIED_MS);
        }, function () {});
      });
      box.append(code, copy);
      fact(dl, 'Session', box);
    } else {
      fact(dl, 'Session', el('span', 'muted', (d.session && d.session.why) || 'none'));
    }
    fact(dl, 'Joined', has(dm.joined_at) ? dayTime(dm.joined_at) : '—');
    const what = dm.last_seen_what === 'seen' ? 'status' : dm.last_seen_what;
    fact(dl, 'Last seen', has(dm.last_seen) ? hhmm(dm.last_seen) + (what ? ' · ' + what : '') : '—');
    const queued = m.queued || 0;
    const queueList = el('ol', 'insp-queue' + (ui.queueOpen && queued ? '' : ' hidden'));
    queueList.id = 'insp-queue';
    if (!queued) {
      fact(dl, 'Queued', 'None');
    } else {
      const qb = btn('queue-toggle' + (ui.queueOpen ? ' open' : ''));
      qb.dataset.focus = 'queue';
      qb.setAttribute('aria-expanded', String(!!ui.queueOpen));
      qb.setAttribute('aria-controls', 'insp-queue');
      qb.append(plural(queued, 'message', 'messages'), icon('chev-down'));
      qb.addEventListener('click', function () { ui.queueOpen = !ui.queueOpen; renderInspector(); });
      fact(dl, 'Queued', qb);
      for (const q of (d && d.queued) || []) {
        const msg = r.msgs.find(function (x) { return x.id === q.id; });
        const li = el('li');
        if (msg) {
          li.append(timeEl(msg.ts), el('span', 'q-from', label(msg.from, msg.host)), el('span', 'q-text', firstLine(msg.text, 80)));
        } else {
          li.append(el('span'), el('span', 'q-text', 'message #' + q.id + ' (not loaded)'));
        }
        queueList.append(li);
      }
      if (!d) queueList.append(el('li', 'muted', 'loading…'));
    }
    if (m.inflight) fact(dl, 'In flight', plural(m.inflight, 'message', 'messages'));
    if (m.away) fact(dl, 'Away', m.away);
    const qNote = el('p', 'fine' + (ui.queueOpen && queued ? '' : ' hidden'),
      'Peer messages wait while it is busy and go out as one batch when it is idle.');
    details.append(el('div', 'group-label', 'Details'), dl, queueList, qNote);

    // 4. delivery timeline
    const tl = el('section', 'insp-timeline');
    tl.setAttribute('aria-label', 'Delivery history');
    const events = (d && d.timeline) || [];
    const tlLabel = el('div', 'group-label', 'Delivery');
    if (events.length) tlLabel.append(el('span', 'group-count', ' · last ' + events.length));
    tl.append(tlLabel);
    if (!d) tl.append(el('p', 'muted', 'loading…'));
    else if (!events.length) tl.append(el('p', 'muted', 'No deliveries yet.'));
    else {
      const ol = el('ol', 'timeline');
      for (const e of events) {
        const row = timelineRow(e, r);
        const li = el('li', 'tl ' + row[2]);
        const txt = el('span', 'tl-text');
        txt.append(el('strong', null, row[0]));
        if (row[1]) txt.append(' ', el('span', null, row[1]));
        li.append(has(e.ts) ? timeEl(e.ts) : el('span'), el('span', 'tl-dot'), txt);
        ol.append(li);
      }
      tl.append(ol);
    }

    // 5. actions: the same command path as the composer, so the reply shows in the log
    const actions = el('div', 'insp-actions');
    const row = el('div', 'insp-row');
    const hold = btn('btn');
    hold.id = 'insp-hold';
    hold.dataset.focus = 'hold';
    hold.setAttribute('aria-pressed', String(!!m.held));
    if (m.held) {
      hold.append(icon('play'), 'Release');
      hold.title = 'Release: resume delivery to ' + m.name + ' (web only)';
    } else {
      hold.append(icon('pause'), 'Hold');
      hold.title = 'Hold: stop delivery to ' + m.name + '; messages wait until you release it';
    }
    hold.addEventListener('click', function () { submitText((m.held ? '/release ' : '/hold ') + m.name); });
    const cu = btn('btn');
    cu.id = 'insp-catchup';
    cu.dataset.focus = 'catchup';
    cu.setAttribute('aria-haspopup', 'menu');
    cu.setAttribute('aria-expanded', String(!!ui.menu));
    cu.setAttribute('aria-controls', 'catchup-menu');
    cu.title = '/catchup ' + m.name + ' on a member, a topic or the room';
    cu.append(icon('history'), 'Catch up on…', icon('chev-down'));
    cu.addEventListener('click', function () { setMenu(!ui.menu); });
    row.append(hold, cu);
    refs.hold = hold;
    refs.catchup = cu;

    const menu = el('div', 'menu' + (ui.menu ? '' : ' hidden'));
    menu.id = 'catchup-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', 'Catch ' + m.name + ' up on');
    menu.append(el('div', 'group-label', 'Catch ' + m.name + ' up on'));
    const items = [];
    // shown: the command as the row shows it; fill: what goes in the composer; caretBack: see fillComposer
    function item(text, shown, fill, caretBack) {
      const b = btn('menu-item');
      b.setAttribute('role', 'menuitem');
      b.dataset.focus = 'menu:' + fill;  // the command: stable when the member list re-renders
      b.append(el('span', null, text), el('code', null, shown));
      b.addEventListener('click', function () {
        setMenu(false);
        fillComposer(fill, caretBack);
      });
      items.push(b);
      menu.append(b);
    }
    // commands take bare screen names; the labels keep bench@fpga-pi
    for (const o of r.members) {
      if (o.name === m.name) continue;
      const c = '/catchup ' + m.name + ' on ' + o.name;
      item(label(o.name, o.host) + '’s work', c, c, 0);
    }
    item('A topic…', '/catchup ' + m.name + ' on "…"', '/catchup ' + m.name + ' on ""', 1);
    item('The whole room', '/catchup ' + m.name, '/catchup ' + m.name, 0);
    if (m.approval_mode === 'bypass') {
      menu.append(el('p', 'note note-danger', m.name + ' has approvals off: session text it reads can steer it. Prefer an agent that prompts.'));
    }
    menu.addEventListener('keydown', function (ev) {  // arrow keys move between items
      const i = items.indexOf(document.activeElement);
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        const n = items.length;
        items[((i < 0 ? -1 : i) + (ev.key === 'ArrowDown' ? 1 : n - 1) + n) % n].focus();
      }
    });
    refs.menu = menu;
    refs.menuItems = items;

    const kick = btn('btn-danger-ghost');
    kick.id = 'insp-kick';
    kick.dataset.focus = 'kick';
    kick.title = '/kick ' + m.name + ': remove it and revoke its membership';
    kick.append(icon('kick'), 'Kick ' + m.name);
    kick.addEventListener('click', function () { setConfirm(true); });
    refs.kick = kick;
    const confirm = el('div', 'confirm' + (ui.confirm ? '' : ' hidden'));
    confirm.id = 'kick-confirm';
    confirm.setAttribute('role', 'group');
    confirm.setAttribute('aria-label', 'Confirm kick');
    const cancel = btn('btn', 'Cancel');
    cancel.dataset.focus = 'kick-cancel';
    cancel.addEventListener('click', function () { setConfirm(false); });
    const doKick = btn('btn-danger', 'Kick');
    doKick.dataset.focus = 'kick-do';
    doKick.addEventListener('click', function () {
      const name = m.name;
      const at = r.members.indexOf(m);
      closeInspector(false);
      submitText('/kick ' + name, { confirmed: true });
      // the kicked agent's row goes away with the next members frame, so focus the row that
      // takes its place now; renderBuddies keeps it focused across that re-render
      focusNear(at, name);
    });
    const btns = el('div', 'dialog-buttons');
    btns.append(cancel, doKick);
    confirm.append(el('span', null, 'Kick ' + m.name + ' from ' + r.name + '? It is removed and its membership revoked.'), btns);
    refs.confirm = confirm;
    refs.cancel = cancel;
    const fine = el('p', 'fine');
    fine.append('Same as ', el('code', null, '/hold'), ', ', el('code', null, '/catchup'), ' and ', el('code', null, '/kick'), ' in the composer.');
    actions.append(menu, row, kick, confirm, fine);

    body.replaceChildren(id);
    if (notes.length) body.append(attn);
    body.append(details, tl, actions);
    body.scrollTop = scroll;
    state.insp = refs;
    refocus(body, focused);
  }

  function setMenu(open) {
    if (!state.inspUi) return;
    state.inspUi.menu = !!open;
    if (open) state.inspUi.confirm = false;
    renderInspector();
    if (open && state.insp.menuItems && state.insp.menuItems.length) state.insp.menuItems[0].focus();
    else if (!open && state.insp.catchup) state.insp.catchup.focus();
  }

  function setConfirm(open) {
    if (!state.inspUi) return;
    state.inspUi.confirm = !!open;
    if (open) state.inspUi.menu = false;
    renderInspector();
    if (open && state.insp.cancel) state.insp.cancel.focus();
    else if (!open && state.insp.kick) state.insp.kick.focus();
  }

  // put a command in the composer for the human to finish and send (nothing is sent here);
  // caretBack > 0 leaves the caret that many characters before the end (inside the quotes).
  // A message the human was writing is not thrown away: it is kept in state.draft and comes
  // back into the composer once the command has gone out, or on Esc (restoreDraft). Setting
  // value from script also clears the textarea's own undo, so Ctrl+Z could not bring it back.
  function fillComposer(text, caretBack) {
    const input = $('input');
    const had = String(input.value || '');
    if (had.trim() && had.trim()[0] !== '/' && !state.draft) state.draft = had;
    if (narrow()) setSheet(false);  // first: the phone sheet makes the composer inert while open
    input.value = text;
    autoGrow();
    input.focus();
    if (typeof input.setSelectionRange === 'function') {
      const at = text.length - (caretBack || 0);
      input.setSelectionRange(at, at);
    }
    if (state.draft) renderLocal('Your draft is kept: it comes back after this command is sent (or press Esc).');
  }

  // put a kept draft back in the composer (see fillComposer); true if there was one
  function restoreDraft() {
    const d = state.draft;
    if (!d) return false;
    state.draft = null;
    const input = $('input');
    input.value = d;
    autoGrow();
    input.focus();
    if (typeof input.setSelectionRange === 'function') input.setSelectionRange(d.length, d.length);
    return true;
  }

  // ----------------------------------------------------- composer popovers
  // The slash palette. Commands take bare screen names (the broker's _screen_name): the UI
  // never writes bench@fpga-pi into a command. The /help reply stays the authority.
  const COMMANDS = [
    { group: 'Room', cmd: 'pause', args: '', desc: 'Freeze every agent wake in {room}', pill: '', usage: '/pause',
      detail: 'Open wait() calls return “paused”; read() still works.' },
    { group: 'Room', cmd: 'resume', args: '', desc: 'Unfreeze wakes; also resets the loop guard', pill: 'web only', usage: '/resume',
      detail: 'Raises agent activity, so it needs this signed-in browser.' },
    { group: 'Room', cmd: 'budget', args: '[n]', desc: 'Show or set wakes left this hour', pill: 'raise: web only',
      usage: '/budget   /budget <n>', detail: 'Lowering works from anywhere; raising needs this browser.', live: 'budget' },
    { group: 'Room', cmd: 'hops', args: '[n]', desc: 'Show or set the loop-guard limit', pill: 'raise: web only',
      usage: '/hops   /hops <n>   (0–1000, 0 turns it off)', detail: 'A new limit never lifts a loop-guard pause: /resume does.', live: 'hops' },
    { group: 'Room', cmd: 'close', args: '', desc: 'Close the room; the history is kept', pill: '', usage: '/close',
      detail: 'Asks first. Reopen it from Closed rooms.' },
    { group: 'Agents', cmd: 'hold', args: '<name>', desc: 'Stop delivery to one agent', pill: '', usage: '/hold <name>',
      detail: 'Nothing is dropped: held messages go out on /release.' },
    { group: 'Agents', cmd: 'release', args: '<name>', desc: 'Resume delivery to a held agent', pill: 'web only', usage: '/release <name>',
      detail: 'Raises agent activity, so it needs this signed-in browser.' },
    { group: 'Agents', cmd: 'kick', args: '<name>', desc: 'Remove an agent and revoke its membership', pill: '', usage: '/kick <name>',
      detail: 'Asks first. Removes it from {room} and revokes its membership.' },
    { group: 'Agents', cmd: 'catchup', args: '<agent> [on …]', desc: 'Get one agent up to speed from session history', pill: '',
      usage: '/catchup <agent> [on <member> | on "<topic>"] [note]',
      detail: 'e.g. /catchup bench on claude-1 — the member being read is not woken.' },
    { group: 'Info', cmd: 'who', args: '', desc: 'Members, their sessions and hosts', pill: '', usage: '/who',
      detail: 'Shows session: <id> @ <host> for each member that has one.' },
    { group: 'Info', cmd: 'status', args: '', desc: 'Room status: paused, budget, hops', pill: '', usage: '/status',
      detail: 'Paused or running, the wake budget and the loop guard, as a reply.' },
    { group: 'Info', cmd: 'help', args: '', desc: 'List every command', pill: '', usage: '/help',
      detail: 'Lists every command as a reply in the room.' },
  ];

  function cmdDesc(c) {
    const r = activeRoom();
    const s = (r && r.settings) || {};
    let d = c.desc.replace('{room}', r ? r.name : 'the room');
    if (c.live === 'budget' && s.budget_per_hour !== undefined) d += ' · ' + s.budget_remaining + '/' + s.budget_per_hour;
    if (c.live === 'hops' && s.hop_limit !== undefined) d += ' · ' + s.hop_count + '/' + s.hop_limit;
    return d;
  }

  function autoGrow() {
    const input = $('input');
    const lines = String(input.value).split('\n').length;
    input.rows = Math.max(1, Math.min(8, lines));
  }

  // What should be open for the text before the caret: the palette, the mention list, or nothing.
  function updatePopover() {
    const input = $('input');
    const v = String(input.value);
    const r = activeRoom();
    if (!r) return closePop();
    if (/^\/[a-z]*$/.test(v) && !v.startsWith('//')) {
      const pre = v.slice(1);
      const items = COMMANDS.filter(function (c) { return c.cmd.startsWith(pre); });
      if (!items.length) return closePop();
      openPop({ kind: 'palette', items: items, sel: keepSel('palette', items), prefix: pre });
      return;
    }
    const caret = typeof input.selectionStart === 'number' ? input.selectionStart : v.length;
    const mm = /(^|[\s(])@([a-z0-9_-]{0,23})$/.exec(v.slice(0, caret));
    if (mm) {
      const pre = mm[2];
      const items = r.members.filter(function (m) { return m.name.startsWith(pre); });
      if (!items.length) return closePop();
      openPop({ kind: 'mentions', items: items, sel: keepSel('mentions', items), start: caret - pre.length - 1, end: caret });
      return;
    }
    closePop();
  }

  // keep the highlighted row across keystrokes when it is still listed
  function keepSel(kind, items) {
    const p = state.pop;
    if (!p || p.kind !== kind) return 0;
    const cur = p.items[p.sel];
    const i = items.indexOf(cur);
    return i < 0 ? 0 : i;
  }

  function optionId(p, i) { return (p.kind === 'palette' ? 'pal-' + p.items[i].cmd : 'men-' + p.items[i].name); }

  function openPop(p) {
    state.pop = p;
    const other = p.kind === 'palette' ? 'mentions' : 'palette';
    $(other).classList.add('hidden');
    $(other).replaceChildren();
    renderPop();
  }

  function closePop() {
    if (!state.pop) return;
    state.pop = null;
    for (const id of ['palette', 'mentions']) {
      $(id).classList.add('hidden');
      $(id).replaceChildren();
    }
    const input = $('input');
    unsetAttr(input, 'aria-activedescendant');
    input.setAttribute('aria-expanded', 'false');
    popHint(null);
  }

  // While a popover is open, the composer says what Enter does (Composer.dc): the palette runs the
  // highlighted command ("Run"), the mention list picks the highlighted agent.
  function popHint(text, run) {
    $('composer-hint').classList.toggle('hidden', !!text);
    const h = $('pop-hint');
    h.textContent = text || '';
    h.classList.toggle('hidden', !text);
    $('send-label').textContent = run ? 'Run' : 'Send';
  }

  // the palette head's legend for the "web only" pills, with the same browser glyph (Composer.dc)
  function webOnlyKey() {
    const k = el('span', 'muted pal-key');
    k.append(icon('browser'), 'web only = needs this signed-in browser');
    return k;
  }

  function renderPop() {
    const p = state.pop;
    if (!p) return;
    const box = $(p.kind);
    const r = activeRoom();
    box.replaceChildren();
    box.classList.remove('hidden');
    let selected = null;
    if (p.kind === 'palette') {
      const head = el('div', 'pal-head');
      head.append(el('strong', null, 'Commands'), el('span', 'muted', p.items.length + ' match “/' + p.prefix + '”'),
        webOnlyKey());
      box.append(head);
      let group = null;
      let g = null;
      p.items.forEach(function (c, i) {
        if (c.group !== group) {
          group = c.group;
          g = el('div', 'pal-group');
          g.setAttribute('role', 'group');
          g.setAttribute('aria-label', group);
          g.append(el('div', 'group-label', group));
          box.append(g);
        }
        const o = el('div', 'pal-item');
        o.id = optionId(p, i);
        o.setAttribute('role', 'option');
        o.setAttribute('aria-selected', String(i === p.sel));
        o.append(el('span', 'pal-cmd', '/' + c.cmd), el('span', 'pal-args', c.args), el('span', 'pal-desc', cmdDesc(c)));
        if (c.pill) {
          const pill = el('span', 'pill');
          pill.append(icon('browser'), c.pill);
          o.append(pill);
        }
        if (i === p.sel) {
          // the usage line only when it says more than the command itself (/pause has no arguments)
          if (c.usage && c.usage !== '/' + c.cmd) o.append(el('code', 'pal-usage', c.usage));
          o.append(el('span', 'pal-detail', c.detail.replace('{room}', r ? r.name : 'the room')));
          selected = o;
        }
        bindOption(o, i);
        g.append(o);
      });
      const foot = el('div', 'pal-foot');
      foot.append(el('kbd', 'kbd', '↑'), el('kbd', 'kbd', '↓'), ' move ', el('kbd', 'kbd', 'Tab'), ' complete ',
        el('kbd', 'kbd', 'Esc'), ' close');
      const tip = el('span', 'pal-tip');
      tip.append(el('code', null, '//text'), ' posts text that begins with /');
      foot.append(tip);
      box.append(foot);
    } else {
      const head = el('div', 'pal-head');
      head.append(el('strong', null, 'Agents in ' + (r ? r.name : '')), el('span', 'muted', String(p.items.length)));
      box.append(head);
      p.items.forEach(function (m, i) {
        const o = el('div', 'pal-item mention-opt');
        o.id = optionId(p, i);
        o.setAttribute('role', 'option');
        o.setAttribute('aria-selected', String(i === p.sel));
        const main = el('span', 'm-main');
        const nl = el('span', 'm-name-line');
        nl.append(el('span', 'm-name', m.name));
        if (m.host) nl.append(hostChip(m, false));
        const flag = approvalsFlag(m);
        if (flag) nl.append(flag);
        nl.append(el('span', 'm-status', statusWord(m) + ' · ' + harnessOf(m.harness)[1]));
        main.append(nl);
        if (m.approval_mode === 'bypass') main.append(el('span', 'm-warn', 'Approvals off: what it reads can steer it'));
        if (m.parked) main.append(el('span', 'm-warn', 'Parked — needs a poke'));
        o.append(withDot(avatar(m.harness, 'agent', m.name), m), main, tierChip(m));
        if (i === p.sel) selected = o;
        bindOption(o, i);
        box.append(o);
      });
      box.append(el('div', 'pal-foot', 'An @mention wakes that agent at once.'));
    }
    const input = $('input');
    input.setAttribute('aria-controls', p.kind);
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-activedescendant', optionId(p, p.sel));
    const cur = p.items[p.sel];
    if (p.kind === 'palette') popHint('Enter runs the highlighted command', true);
    else popHint(cur ? 'Tab or Enter picks ' + cur.name : 'No agent matches', false);
    if (selected && typeof selected.scrollIntoView === 'function') selected.scrollIntoView({ block: 'nearest' });
  }

  function bindOption(o, i) {
    // mousedown keeps the focus in the textarea; the click picks the row
    o.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
    o.addEventListener('click', function () {
      if (!state.pop) return;
      state.pop.sel = i;
      choosePop();
    });
  }

  function movePop(delta) {
    const p = state.pop;
    const n = p.items.length;
    p.sel = (p.sel + delta + n) % n;
    renderPop();
  }

  // Tab / Enter / click on a row: complete it (or run an argument-less command)
  function choosePop() {
    const p = state.pop;
    if (!p) return;
    const input = $('input');
    if (p.kind === 'palette') {
      const c = p.items[p.sel];
      closePop();
      if (c.args) {
        input.value = '/' + c.cmd + ' ';
        input.focus();
      } else {
        input.value = '/' + c.cmd;
        $('composer').requestSubmit();
      }
      return;
    }
    const m = p.items[p.sel];
    const v = String(input.value);
    const ins = '@' + m.name + ' ';
    input.value = v.slice(0, p.start) + ins + v.slice(p.end);
    closePop();
    input.focus();
    if (typeof input.setSelectionRange === 'function') input.setSelectionRange(p.start + ins.length, p.start + ins.length);
  }

  // ---------------------------------------------------------- websocket
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const ws = new WebSocket(proto + location.host + '/ws');
    state.ws = ws;
    ws.addEventListener('open', function () {
      state.wsOpen = true;
      state.backoff = 500;
      // a close, reopen, create or delete missed while the socket was down: resync, then hello all.
      // The new socket follows nothing until a hello: if the resync fails, hello the tabs we have.
      loadRooms(true).catch(function () { hello(Array.from(state.rooms.keys())); });
      if (!$('closed-panel').classList.contains('hidden')) loadClosed().catch(function () {});
      loadRemotes().catch(function () {});  // the events missed while the socket was down
      if (state.machinesHosted) loadMachines().catch(function () {});
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

  // Frames for a room that is not open here (closed, deleted, or not listed yet) are dropped:
  // only loadRooms() adds a room row, so a late frame never brings back a ghost room.
  function onFrame(f) {
    const r = f.room ? state.rooms.get(f.room) : null;
    const ins = state.inspect;
    if (f.t === 'msg' && f.msg) {
      if (!r) return;
      appendMsg(r, f.msg);
      // the inspected agent spoke: its Last seen and timeline changed
      if (ins && ins.room === f.room && f.msg.from === ins.name && (f.msg.host || '') === (ins.host || '')) scheduleRefetch();
    } else if (f.t === 'members') {
      if (!r) return;
      r.members = f.members || [];
      if (f.room === state.active) {
        renderBuddies();
        updateRoomEmpty();
      }
      if (ins && ins.room === f.room) {
        if (!inspectedMember()) memberLeft(ins);
        else {
          renderInspector();  // at once from member_dict; the detail follows
          scheduleRefetch();
        }
      }
    } else if (f.t === 'room') {
      if (!r) return;
      r.settings = f.settings || {};
      if (f.room === state.active) renderStatus();
    } else if (f.t === 'notice') {
      if (!f.room || f.room === state.active) renderLocal(f.text, f.level === 'warn');
    } else if (f.t === 'rooms') {
      // hello every room, not only new ones: a close drops this page's subscription, and a
      // reopen (same id, same name) may land before this listing, so nothing looks changed
      loadRooms(true).catch(function () { hello(Array.from(state.rooms.keys())); });
      if (!$('closed-panel').classList.contains('hidden')) loadClosed().catch(function () {});
    } else if (f.t === 'remotes') {
      setRemotes(f.remotes || [], f.config_error || null);
    } else if (f.t === 'machines') {
      setMachines(f.machines || [], f.codes || []);
    }
  }

  // ------------------------------------------------------------ remotes
  // blocked reasons where Enable trusts something new (a host key, a satellite): it asks first
  const ASK_BEFORE_ENABLE = new Set(['host_key', 'replaced', 'exposed']);

  function retryLeft(r) {
    if (r.retry_in_s === null || r.retry_in_s === undefined) return null;
    return Math.max(0, Math.round(r.retry_in_s - (Date.now() - state.remotesAt) / 1000));
  }

  function fmtMs(ms) { return ms < 10 ? ms.toFixed(1) : String(Math.round(ms)); }

  function needsEnable(r) {
    return r.state === 'blocked' || (r.state === 'disabled' && r.reason !== 'removed');
  }

  function chipText(r) {
    if (r.state === 'up') return 'up' + (has(r.rtt_ms) ? ' · ' + fmtMs(r.rtt_ms) + ' ms' : '');
    if (r.state === 'connecting') return 'connecting…';
    if (r.state === 'down') {
      const left = retryLeft(r);
      return 'down: ' + (r.reason || '?') + (left !== null ? ' (retry in ' + left + ' s)' : '');
    }
    if (r.state === 'blocked') return 'blocked: ' + (BLOCK_SHORT[r.reason] || r.reason || '?');
    if (r.reason === 'config_changed') return 'needs enable (config changed)';
    if (r.reason === 'not_enabled') return 'needs enable';
    return r.reason || r.state;
  }

  // Everything a render shows except the numbers that tick (the RTT, the retry countdown):
  // when only those changed, the rows and the sheet update them in place, so the focus
  // and a selection in the sheet survive the 20 s refresh.
  function remotesKey() {
    return JSON.stringify([state.remotesError, Array.from(state.enabling).sort(), state.remotes.map(function (r) {
      const c = Object.assign({}, r);
      c.rtt_ms = has(r.rtt_ms);
      c.retry_in_s = has(r.retry_in_s);
      delete c.text;
      return c;
    })]);
  }

  function focusKey(box) {
    const f = document.activeElement;
    return f && box.contains(f) && f.dataset ? f.dataset.focus || null : null;
  }

  // focus the node in box whose data-focus is key; true if it took focus
  function refocus(box, key) {
    if (!key) return false;
    for (const n of box.querySelectorAll('[data-focus]')) {
      if (n.dataset.focus === key) return tryFocus(n);
    }
    return false;
  }

  function byRemote(box, cls) {
    const out = new Map();
    for (const n of box.getElementsByClassName(cls)) out.set(n.dataset.remote, n);
    return out;
  }

  // a row's tooltip: its state in full (a long one ends in an ellipsis in the row) and what a click does
  function chipTitle(r) {
    return 'Remote machine ' + r.name + ' (' + chipText(r) + '): open the remotes panel';
  }

  // the sidebar's Remote machines rows (button.remote.st-<state>); the dot is CSS (::before)
  function renderChips() {
    const bar = $('remotes');
    const key = remotesKey();
    if (key === state.chipsKey) {
      if (!state.remotes.length) return;
      const texts = byRemote(bar, 'remote-state');
      for (const r of state.remotes) {
        const t = texts.get(r.name);
        if (t) {
          t.textContent = chipText(r);
          t.parentNode.title = chipTitle(r);
        }
      }
      return;
    }
    state.chipsKey = key;
    const focused = focusKey(bar);
    bar.replaceChildren();
    renderRemotesSection();
    for (const r of state.remotes) {
      const b = btn('remote st-' + r.state);
      b.dataset.focus = 'chip:' + r.name;
      b.title = chipTitle(r);
      const t = el('span', 'remote-state', chipText(r));
      t.dataset.remote = r.name;
      b.append(icon('server'), el('span', 'remote-name', r.name), t);
      b.addEventListener('click', openRemotes);
      bar.append(b);
    }
    if (state.remotesError) {
      const b = btn('remote st-error');
      b.dataset.focus = 'chip:config';
      b.title = state.remotesError;
      b.append(icon('server'), el('span', 'remote-name', 'remotes.toml'), el('span', 'remote-state', 'not read'));
      b.addEventListener('click', openRemotes);
      bar.append(b);
    }
    refocus(bar, focused);
  }

  function kv(dl, k, v, cls) {
    if (v === null || v === undefined || v === '') return null;
    const dd = el('dd', cls || null, v);
    dl.append(el('dt', null, k), dd);
    return dd;
  }

  function remoteCard(r) {
    const card = el('div', 'remote-card');
    const head = el('div', 'remote-head');
    const st = el('span', 'remote-state', chipText(r));
    st.dataset.remote = r.name;
    head.append(icon('server'), el('span', 'remote-name', r.name), st);
    head.classList.add('st-' + r.state);
    card.append(head);
    const dl = el('dl', 'remote-facts');
    kv(dl, 'State', r.state + (r.reason ? ' (' + r.reason + ')' : '') + (r.since ? ' since ' + when(r.since) : ''));
    kv(dl, 'What to do', r.hint);
    if (r.state === 'up' && has(r.rtt_ms)) kv(dl, 'RTT', fmtMs(r.rtt_ms) + ' ms', 'remote-rtt').dataset.remote = r.name;
    // what an Enable consents to: where the link dials, the host key it trusts, the config's hash
    kv(dl, 'Destination', r.dest);
    kv(dl, 'Host key', (r.host_keys || []).join('\n'), 'remote-keys');
    if (r.config_hash) {
      kv(dl, 'Config', r.config_hash.slice(0, 12) + (r.reason === 'config_changed' ? ' (changed since you enabled it)' : ''));
    }
    if (r.version) kv(dl, 'Versions', 'satellite ' + r.version + ' (link protocol ' + r.proto + '), this broker ' + (state.me ? state.me.version : '?'));
    kv(dl, 'Remote hooks', has(r.hooks) ? r.hooks + ' (as the remote reports)' : null);
    if (has(r.skew_s)) kv(dl, 'Clock', (r.skew_s >= 0 ? '+' : '') + r.skew_s.toFixed(2) + ' s');
    kv(dl, 'Hardened', r.harden);
    kv(dl, 'Transport', r.transport + (r.test_mode ? ' (test mode)' : ''));
    kv(dl, 'Rooms', (r.rooms || []).join(', '));
    kv(dl, 'Harnesses', (r.harnesses || []).join(', '));
    kv(dl, 'Members', (r.members || []).length ? r.members.join(', ') + ' (max ' + r.max_members + ')' : 'none (max ' + r.max_members + ')');
    kv(dl, 'Enabled', r.enabled ? 'via ' + (r.enabled_via || '?') + ' on ' + when(r.enabled_at) : 'no');
    kv(dl, 'Last up', when(r.last_up_at));
    kv(dl, 'Dials', String(r.attempts));
    card.append(dl);
    if (r.detail) {
      // text the remote machine may have printed (ssh's last line): data, shown to you only
      card.append(el('div', 'fine', 'Last line from the link (the remote may have written it):'),
        el('pre', 'cmd-out remote-detail', r.detail));
    }
    const btns = el('div', 'dialog-buttons');
    if (needsEnable(r) || state.enabling.has(r.name)) {
      const b = btn('btn btn-primary', state.enabling.has(r.name) ? 'Dialing…' : (r.state === 'blocked' ? 'Enable / reconnect' : 'Enable'));
      b.dataset.focus = 'enable:' + r.name;
      b.disabled = state.enabling.has(r.name);
      b.title = 'consent to this remote\'s current config (the destination and host key above) and dial it now (the same as `switchboard remote enable ' + r.name + '`)';
      b.addEventListener('click', function () { enableRemote(r.name); });
      btns.append(b);
    }
    if (r.enabled && (r.state === 'up' || r.state === 'connecting' || r.state === 'down')) {
      const d = btn('btn', 'Disable');
      d.dataset.focus = 'disable:' + r.name;
      d.title = 'stop dialing ' + r.name + ' (its members go offline)';
      d.addEventListener('click', function () { disableRemote(r.name); });
      btns.append(d);
    }
    if (btns.childNodes.length) card.append(btns);
    const out = el('div', 'fine remote-result');
    out.id = 'remote-result-' + r.name;
    card.append(out);
    return card;
  }

  function renderRemotesPanel() {
    const body = $('remotes-body');
    if ($('remotes-panel').classList.contains('hidden')) return;
    const key = remotesKey();
    if (key === state.panelKey) {
      const states = byRemote(body, 'remote-state');
      const rtts = byRemote(body, 'remote-rtt');
      for (const r of state.remotes) {
        if (states.has(r.name)) states.get(r.name).textContent = chipText(r);
        if (rtts.has(r.name) && has(r.rtt_ms)) rtts.get(r.name).textContent = fmtMs(r.rtt_ms) + ' ms';
      }
      return;
    }
    state.panelKey = key;
    const focused = focusKey(body);
    const results = {};  // an Enable's answer survives a re-render
    for (const n of body.querySelectorAll('.remote-result')) results[n.id] = [n.textContent, n.classList.contains('bad')];
    body.replaceChildren();
    if (state.remotesError) body.append(el('p', 'remote-error', 'remotes.toml was not read: ' + state.remotesError));
    if (!state.remotes.length) body.append(el('p', null, 'No remotes. Add one with `switchboard remote add` in your terminal.'));
    for (const r of state.remotes) body.append(remoteCard(r));
    for (const id of Object.keys(results)) {
      const n = document.getElementById(id);
      if (n) {
        n.textContent = results[id][0];
        n.classList.toggle('bad', results[id][1]);
      }
    }
    refocus(body, focused);
  }

  function setRemotes(list, err) {
    state.remotes = list;
    state.remotesAt = Date.now();
    state.remotesError = err;
    renderChips();
    renderRemotesPanel();
  }

  async function loadRemotes() {
    const data = await api('GET', '/api/remotes');
    setRemotes(data.remotes || [], data.config_error || null);
  }

  const SHEETS = ['remotes-panel', 'machines-panel', 'closed-panel', 'passkeys-panel'];

  function openSheet(id) {
    state.sheetOpener = document.activeElement || null;
    state.sheetOpenerKey = state.sheetOpener && state.sheetOpener.dataset ? state.sheetOpener.dataset.focus || null : null;
    for (const other of SHEETS) if (other !== id) $(other).classList.add('hidden');
    $(id).classList.remove('hidden');
    setNav(false);
  }

  // Focus goes back to the opener if it can still take it. It can't when the phone's rooms
  // drawer closed as the sheet opened (the opener sits in the now hidden drawer), when
  // renderChips rebuilt the remote rows meanwhile, or when the Closed button went away (the last
  // closed room was reopened elsewhere). Then: the Rooms toggle on a phone, a rebuilt remote row
  // with the same key, else the composer.
  function closeSheet(id) {
    $(id).classList.add('hidden');
    const back = state.sheetOpener;
    const key = state.sheetOpenerKey;
    state.sheetOpener = null;
    state.sheetOpenerKey = null;
    if (tryFocus(back)) return;
    if (phone() && tryFocus($('rooms-toggle'))) return;
    if (key && refocus($('remotes'), key)) return;
    if (key && refocus($('machines'), key)) return;
    $('input').focus();
  }

  function openRemotes() {
    openSheet('remotes-panel');
    state.panelKey = null;  // it was not kept current while closed
    renderRemotesPanel();
    loadRemotes().catch(function () {});
    $('remotes-close').focus();
  }

  function remoteResult(name, text, bad) {
    const n = document.getElementById('remote-result-' + name);
    if (n) {
      n.textContent = text;
      n.classList.toggle('bad', !!bad);
    }
  }

  // an Enable's or a Disable's outcome, from the broker's facts (the hooks row stays in the
  // panel, labelled: that text is the remote's)
  function resultText(res) {
    if (res.state === 'up') {
      return 'link ok: satellite ' + (res.version || '?') + ' (link protocol ' + res.proto + ')' +
        (has(res.rtt_ms) ? ', rtt ' + fmtMs(res.rtt_ms) + ' ms' : '') +
        (has(res.skew_s) ? ', clock ' + (res.skew_s >= 0 ? '+' : '') + res.skew_s.toFixed(2) + ' s' : '');
    }
    return res.state + (res.reason ? ' (' + res.reason + ')' : '') + (res.hint ? ': ' + res.hint : '');
  }

  async function enableRemote(name) {
    const r = state.remotes.find(function (x) { return x.name === name; });
    if (!r) return;
    if (r.state === 'blocked' && ASK_BEFORE_ENABLE.has(r.reason) &&
        !window.confirm(name + ' is blocked (' + r.reason + '): ' + (r.hint || '') + '\n\nEnable it and dial ' +
                        (r.dest || name) + ' again?')) return;
    state.enabling.add(name);
    renderRemotesPanel();
    remoteResult(name, 'dialing ' + name + '…');
    try {
      // the consent is for the config this page shows: the broker refuses it (409) if it changed since
      const res = await api('POST', '/api/remotes/' + encodeURIComponent(name) + '/enable', { config_hash: r.config_hash });
      state.enabling.delete(name);
      await loadRemotes().catch(function () {});
      remoteResult(name, resultText(res), res.state !== 'up');
    } catch (e) {
      state.enabling.delete(name);
      await loadRemotes().catch(function () {});
      renderRemotesPanel();
      remoteResult(name, String(e.message || e), true);
    }
  }

  async function disableRemote(name) {
    if (!window.confirm('Disable ' + name + '? Its members go offline until you enable it again.')) return;
    try {
      const res = await api('POST', '/api/remotes/' + encodeURIComponent(name) + '/disable', {});
      await loadRemotes().catch(function () {});
      remoteResult(name, resultText(res), false);
    } catch (e) {
      remoteResult(name, String(e.message || e), true);
    }
  }

  // ------------------------------------------------------------ machines
  // Machines that dial in (DESIGN.md §31.8), on a hosted broker only: their rows under Remote
  // machines and Add a machine in the sidebar, and the Machines sheet. In the sheet, top down: a
  // pending machine's approval card (what it says about itself, shown as its own claims, and its
  // key's fingerprint to compare with what `remote join` printed there); Add a machine (a name,
  // then the two commands with Copy buttons, a countdown and "waiting for … to dial in"); and each
  // approved machine's card (state, last seen, Remove). Making a code and approving need a passkey
  // check in the last five minutes: withFreshCheck asks for a passkey once, then tries again.
  const MACHINE_NAME = /^[a-z][a-z0-9-]{0,23}$/;
  // why a machine's link was refused, each time it dials, until that's fixed on the machine
  const MACHINE_REFUSED = new Set(['proto', 'name', 'test_mode']);

  function machineState(m) {
    if (m.state === 'pending') return 'pending';
    if (m.state === 'blocked' || (m.state === 'down' && MACHINE_REFUSED.has(m.reason))) return 'blocked';
    if (m.state === 'up' || m.state === 'connecting') return m.state;
    return 'offline';  // its dialer isn't connected: the machine is off, asleep or out of reach
  }

  function machineChip(m) {
    const s = machineState(m);
    if (s === 'pending') return 'needs approval';
    if (s === 'up') return 'up' + (has(m.rtt_ms) ? ' · ' + fmtMs(m.rtt_ms) + ' ms' : '');
    if (s === 'connecting') return 'connecting…';
    if (s === 'blocked') return 'refused: ' + (BLOCK_SHORT[m.reason] || m.reason || '?');
    return 'offline';
  }

  // when a machine was last heard from: "just now", "4 min ago", "3 h ago", then a date
  function ago(ts) {
    if (!ts) return null;
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 45) return 'just now';
    if (s < 5400) return Math.max(1, Math.round(s / 60)) + ' min ago';
    if (s < 129600) return Math.round(s / 3600) + ' h ago';
    return stamp(ts);
  }

  function mmss(s) { return Math.floor(s / 60) + ':' + pad2(s % 60); }

  function pairingLeft() {
    const p = state.pairing;
    return p ? Math.max(0, Math.ceil((p.expiresAt - Date.now()) / 1000)) : 0;
  }

  // text whose `quoted` parts are code, into e (a new element with withCode)
  function fillCode(e, text) {
    String(text).split('`').forEach(function (part, i) {
      if (part) e.append(i % 2 ? el('code', null, part) : part);
    });
    return e;
  }

  function withCode(tag, cls, text) { return fillCode(el(tag, cls), text); }

  // the Remote machines section: ssh remotes, machines that dial in, or Add a machine
  function renderRemotesSection() {
    $('remotes-section').classList.toggle('hidden',
      state.remotes.length === 0 && !state.remotesError && !state.machinesHosted);
    $('add-machine').classList.toggle('hidden', !state.machinesHosted);
  }

  // the sidebar's rows, as the ssh remotes' (button.remote.st-<state>, the dot from CSS)
  function renderMachineRows() {
    renderRemotesSection();
    const box = $('machines');
    const key = JSON.stringify(state.machines.map(function (m) { return [m.name, machineState(m), machineChip(m)]; }));
    if (key === state.machineRowsKey) return;
    state.machineRowsKey = key;
    const focused = focusKey(box);
    box.replaceChildren();
    for (const m of state.machines) {
      const b = btn('remote st-' + machineState(m));
      b.dataset.focus = 'machine:' + m.name;
      b.title = 'Machine ' + m.name + ' (' + machineChip(m) + '): open Machines';
      b.append(icon('laptop'), el('span', 'remote-name', m.name), el('span', 'remote-state', machineChip(m)));
      b.addEventListener('click', function () { openMachines(m.name); });
      box.append(b);
    }
    refocus(box, focused);
  }

  // Everything the sheet shows except what ticks (the countdown, last seen, the RTT): when only
  // those changed, tickMachines updates them in place and the focus stays put.
  function machinesKey() {
    const p = state.pairing;
    return JSON.stringify([p && [p.name, p.code, pairingLeft() === 0], state.codeBusy, state.copiedCmd,
      Array.from(state.machineBusy.entries()).sort(), state.machineCodes.map(function (c) { return c.name; }),
      state.machines.map(function (m) {
        const c = Object.assign({}, m);
        c.rtt_ms = has(m.rtt_ms);
        delete c.last_seen;
        return c;
      })]);
  }

  function machineHead(m, iconName, title, chip) {
    const head = el('div', 'remote-head');
    head.append(icon(iconName), el('span', 'remote-name', title));
    if (chip) head.append(el('span', 'remote-state', chip));
    return head;
  }

  function machineResultLine(id) {
    const out = el('div', 'fine machine-result');
    out.id = 'machine-result-' + id;
    return out;
  }

  // what a machine said about itself when it paired: its own claims, as text
  function claimFacts(f) {
    const dl = el('dl', 'remote-facts');
    f = f || {};
    kv(dl, 'Host name', f.hostname);
    kv(dl, 'System', [f.os, f.arch].filter(Boolean).join(' · ') || null);
    kv(dl, 'switchboard', f.version);
    kv(dl, 'Harnesses', (f.harnesses || []).join(', ') || null);
    return dl;
  }

  // a machine's card: focusable (not in the tab order), so opening the sheet at it can move the
  // focus there, and a re-render finds it again by its key
  function cardFor(m, cls) {
    const card = el('div', 'machine-card ' + cls);
    card.dataset.machine = m.name;
    card.dataset.focus = 'card:' + m.name;
    card.tabIndex = -1;
    return card;
  }

  function approvalCard(m) {
    const card = cardFor(m, 'st-pending');
    card.append(machineHead(m, 'laptop', m.name, machineChip(m)));
    card.append(withCode('p', 'fine', m.dialed_in
      ? 'It dialed in and is waiting for you. Until you approve it, its agents reach no room.'
      : 'It paired, and its dialer isn\'t connected: `switchboard start` there starts it. You can approve it first.'));
    const facts = claimFacts(m.facts);
    if (facts.childNodes.length) card.append(el('p', 'machine-label', 'What it says about itself'), facts);
    card.append(el('p', 'machine-label', 'Its key'), el('code', 'machine-fp', m.key_fp));
    const check = el('p', 'machine-check');
    check.append(icon('warn'), withCode('span', null,
      'Check this matches what `remote join` printed on your machine. If it doesn\'t, reject it: someone else used the code.'));
    card.append(check);
    const busy = state.machineBusy.get(m.name);
    const btns = el('div', 'dialog-buttons');
    const ok = btn('btn-primary', busy === 'approve' ? 'Approving…' : 'Approve');
    ok.dataset.focus = 'approve:' + m.name;
    ok.disabled = !!busy;
    ok.title = (state.me && state.me.fresh ? '' : 'asks for one of your passkeys first, then ') +
      'lets ' + m.name + ' in: its agents can join rooms';
    ok.addEventListener('click', function () { approveMachine(m.name); });
    const no = btn('btn', busy === 'remove' ? 'Rejecting…' : 'Reject');
    no.dataset.focus = 'reject:' + m.name;
    no.disabled = !!busy;
    no.title = 'forget ' + m.name + ' and its key: its dialer stops for good';
    no.addEventListener('click', function () { removeMachine(m.name, true); });
    btns.append(ok, no);
    card.append(btns, machineResultLine(m.name));
    return card;
  }

  function stepLine(n, text) {
    const p = el('p', 'machine-step');
    p.append(el('b', null, n + '.'), ' ' + text);
    return p;
  }

  // a command and its Copy button. The command wraps where it must; with keepLast, its last
  // word (the pairing code, typed by hand as often as it's pasted) never breaks
  function cmdBox(text, key, keepLast) {
    const box = el('div', 'machine-cmd');
    const code = el('code');
    const cut = keepLast ? text.lastIndexOf(' ') + 1 : text.length;
    code.append(text.slice(0, cut));
    if (cut < text.length) code.append(el('span', 'nowrap', text.slice(cut)));
    code.id = 'pair-' + key;
    const copy = btn('copy-btn');
    copy.dataset.focus = 'copy:' + key;
    const copied = state.copiedCmd === key;
    const lbl = copied ? 'Copied' : 'Copy the command';
    copy.setAttribute('aria-label', lbl);
    copy.title = lbl;
    copy.append(icon(copied ? 'check' : 'copy'));
    copy.addEventListener('click', function () {
      clipboardWrite(text).then(function () {
        state.copiedCmd = key;
        renderMachinesPanel();
        setTimeout(function () {
          if (state.copiedCmd !== key) return;
          state.copiedCmd = null;
          renderMachinesPanel();
        }, COPIED_MS);
      }, function () {});
    });
    box.append(code, copy);
    return box;
  }

  // the code this page made: the two commands, the countdown, "waiting for … to dial in"
  function pairingCard(p) {
    const left = pairingLeft();
    const card = el('div', 'machine-card');
    card.append(machineHead(null, 'plus', 'Add ' + p.name, null));
    card.append(stepLine('1', 'Install switchboard on ' + p.name + ', if it isn\'t there yet:'), cmdBox(p.install, 'install'),
      stepLine('2', 'Pair it with this switchboard. The code works once:'), cmdBox(p.join, 'join', true));
    const wait = el('div', 'machine-wait' + (left ? '' : ' expired'));
    const t = el('span', 'machine-left', left ? mmss(left) + ' left' : '');
    t.id = 'pair-left';
    wait.append(el('span', 'pulse'), el('span', null, left ? 'Waiting for ' + p.name + ' to dial in…'
      : 'The code expired before a machine used it.'), t);
    card.append(wait);
    const btns = el('div', 'dialog-buttons');
    if (!left) {
      const again = btn('btn-primary', state.codeBusy ? 'Waiting…' : 'Make a new code');
      again.dataset.focus = 'pair-again';
      again.disabled = state.codeBusy;
      again.addEventListener('click', function () { makeCode(p.name); });
      btns.append(again);
    }
    const cancel = btn('btn', 'Cancel');
    cancel.dataset.focus = 'pair-cancel';
    cancel.title = left ? 'the code stops working now' : 'back to Add a machine';
    cancel.addEventListener('click', function () { cancelCode(p.name); });
    btns.append(cancel);
    card.append(btns, machineResultLine('pair'));
    return card;
  }

  function agentsAs(typed) {
    const n = String(typed || '').trim().toLowerCase();
    return 'Lowercase letters, digits and dashes. Its agents show up as bench@' + (MACHINE_NAME.test(n) ? n : 'work-laptop') + '.';
  }

  function addCard() {
    const card = el('div', 'machine-card');
    card.append(machineHead(null, 'plus', 'Add a machine', null));
    card.append(el('p', 'fine', 'Name it, and run the two commands you get on it. Once it dials in, you approve it here.'));
    const form = el('form', 'machine-add');
    form.id = 'machine-add';
    form.setAttribute('autocomplete', 'off');
    const label = el('label', null, 'Its name');
    label.htmlFor = 'machine-name';
    const row = el('div', 'machine-row');
    const input = el('input');
    input.type = 'text';
    input.id = 'machine-name';
    input.name = 'name';
    input.maxLength = 24;
    input.spellcheck = false;
    input.setAttribute('autocapitalize', 'none');
    input.placeholder = 'work-laptop';
    input.value = state.machineDraft;
    input.dataset.focus = 'machine-name';
    const hint = el('p', 'fine', agentsAs(state.machineDraft));
    input.addEventListener('input', function () {
      state.machineDraft = input.value;
      hint.textContent = agentsAs(input.value);
    });
    const go = btn('btn-primary', state.codeBusy ? 'Waiting…' : 'Make a pairing code');
    go.type = 'submit';
    go.id = 'machine-pair-btn';
    go.disabled = state.codeBusy;
    go.title = (state.me && state.me.fresh ? '' : 'asks for one of your passkeys first, then ') +
      'makes a code that works once, for 10 minutes';
    row.append(input, go);
    form.append(label, row, hint);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      makeCode(input.value);
    });
    card.append(form);
    // codes made elsewhere (another tab, before a reload) that no machine used yet
    for (const c of state.machineCodes) {
      const left = c.expires_in_s - (Date.now() - state.machinesAt) / 1000;
      if (left <= 0) continue;
      const line = el('div', 'machine-wait');
      const cancel = btn('btn-danger-ghost', 'Cancel it');
      cancel.dataset.focus = 'cancel:' + c.name;
      cancel.title = 'the code for ' + c.name + ' stops working now';
      cancel.addEventListener('click', function () { cancelCode(c.name); });
      line.append(el('span', 'pulse'), el('span', null, 'A code for ' + c.name + ' works for ' +
        Math.max(1, Math.ceil(left / 60)) + ' more min.'), cancel);
      card.append(line);
    }
    card.append(machineResultLine('add'));
    return card;
  }

  function machineCard(m) {
    const s = machineState(m);
    const card = cardFor(m, 'st-' + s);
    card.append(machineHead(m, 'laptop', m.name, machineChip(m)));
    const dl = el('dl', 'remote-facts');
    const what = { up: 'up', connecting: 'connecting', blocked: 'refused each time it dials (' + (m.reason || '?') + ')',
                   offline: 'offline: its dialer isn\'t connected' }[s];
    kv(dl, 'State', what + (m.since && s !== 'offline' ? ' since ' + stamp(m.since) : ''));
    if (m.hint) {
      const todo = kv(dl, 'What to do', ' ');
      todo.textContent = '';
      fillCode(todo, m.hint);
    }
    if (s !== 'up') kv(dl, 'Last seen', ago(m.last_seen) || 'never', 'machine-seen').dataset.machine = m.name;
    if (s === 'up' && has(m.rtt_ms)) kv(dl, 'RTT', fmtMs(m.rtt_ms) + ' ms');
    kv(dl, 'Transport', 'wss: it dials in' + (m.test_mode ? ' (test mode)' : ''));
    kv(dl, 'Key', m.key_fp, 'remote-keys');
    const f = m.facts || {};
    kv(dl, 'Says it is', [f.hostname, f.os, f.arch].filter(Boolean).join(' · ') || null);
    if (m.version) kv(dl, 'Versions', 'switchboard ' + m.version + ' there, ' + (state.me ? state.me.version : '?') + ' here');
    const members = m.members || [];
    kv(dl, 'Members', (members.length ? members.join(', ') : 'none') + ' (max ' + (m.max_members || 8) + ')');
    kv(dl, 'Approved', m.approved_at ? 'via ' + (m.approved_via || '?') + ' on ' + stamp(m.approved_at) : null);
    card.append(dl);
    if (m.detail) {
      card.append(el('div', 'fine', 'Last line from the link (the machine may have written it):'),
        el('pre', 'cmd-out remote-detail', m.detail));
    }
    const busy = state.machineBusy.get(m.name);
    const btns = el('div', 'dialog-buttons');
    const rm = btn('btn', busy === 'remove' ? 'Removing…' : 'Remove');
    rm.dataset.focus = 'remove:' + m.name;
    rm.disabled = !!busy;
    rm.title = 'forget ' + m.name + ': its agents leave every room at once and its dialer stops for good';
    rm.addEventListener('click', function () { removeMachine(m.name, false); });
    btns.append(rm);
    card.append(btns, machineResultLine(m.name));
    return card;
  }

  function renderMachinesPanel(force) {
    const body = $('machines-body');
    if ($('machines-panel').classList.contains('hidden')) return;
    const key = machinesKey();
    if (!force && key === state.machinesKey) {
      tickMachines();
      return;
    }
    state.machinesKey = key;
    const focused = focusKey(body);
    const f = document.activeElement;
    const inSheet = !f || f === document.body || $('machines-panel').contains(f);
    const results = {};  // a result line survives a re-render
    for (const n of body.querySelectorAll('.machine-result')) results[n.id] = [n.textContent, n.classList.contains('bad')];
    body.replaceChildren();
    const note = machineResultLine('note');
    body.append(note);
    for (const m of state.machines) if (m.state === 'pending') body.append(approvalCard(m));
    body.append(state.pairing ? pairingCard(state.pairing) : addCard());
    for (const m of state.machines) if (m.state !== 'pending') body.append(machineCard(m));
    for (const id of Object.keys(results)) {
      const n = document.getElementById(id);
      if (n) {
        n.textContent = results[id][0];
        n.classList.toggle('bad', results[id][1]);
      }
    }
    const after = state.focusAfter;
    state.focusAfter = null;
    if (after && inSheet && refocus(body, after)) return;
    if (focused && !refocus(body, focused)) tryFocus($('machines-close'));
  }

  function tickMachines() {
    const t = document.getElementById('pair-left');
    const left = pairingLeft();
    if (t && left) t.textContent = mmss(left) + ' left';
    for (const n of $('machines-body').getElementsByClassName('machine-seen')) {
      const m = state.machines.find(function (x) { return x.name === n.dataset.machine; });
      if (m && machineState(m) !== 'up') n.textContent = ago(m.last_seen) || 'never';
    }
  }

  function setMachines(list, codes) {
    const p = state.pairing;
    state.machines = list;
    state.machinesAt = Date.now();
    state.machineCodes = codes.filter(function (c) { return !p || c.name !== p.name; });
    // the machine this page made a code for has paired: its approval card takes the pairing's place
    if (p && list.some(function (m) { return m.name === p.name; })) {
      state.pairing = null;
      state.copiedCmd = null;
      state.focusAfter = 'approve:' + p.name;
    }
    renderMachineRows();
    renderMachinesPanel();
  }

  async function loadMachines() {
    const data = await api('GET', '/api/machines');
    state.machinesHosted = !!data.hosted;
    setMachines(data.machines || [], data.codes || []);
  }

  function machineResult(id, text, bad) {
    const n = document.getElementById('machine-result-' + id);
    if (n) {
      n.textContent = text;
      n.classList.toggle('bad', !!bad);
    }
  }

  // A request that needs a passkey check in the last five minutes. It asks the broker first
  // (GET /api/me's fresh), so the usual case sends no request that is refused (a browser logs
  // those as errors), and asks for one of the owner's passkeys (window.SBWebAuthn) if needed.
  // Refused anyway ('reauth': the five minutes ran out in between), it checks once more.
  async function freshCheck() {
    const w = window.SBWebAuthn;
    if (!w) throw new Error('this page can\'t ask for a passkey');
    if (!w.supported()) throw new Error('this browser can\'t use passkeys');
    await w.signIn();
    if (state.me) state.me.fresh = true;
  }

  async function withFreshCheck(call) {
    let me = null;
    try { me = await api('GET', '/api/me'); } catch (e) { me = null; }
    if (me) state.me = me;
    if (me && !me.fresh) await freshCheck();
    try {
      return await call();
    } catch (e) {
      if (e.code !== 'reauth') throw e;
      await freshCheck();
      return call();
    }
  }

  async function makeCode(raw) {
    const name = String(raw || '').trim().toLowerCase();
    const where = state.pairing ? 'pair' : 'add';
    if (!MACHINE_NAME.test(name)) {
      machineResult(where, 'A name looks like work-laptop: a letter first, then letters, digits or dashes, at most 24.', true);
      return;
    }
    if (state.codeBusy) return;
    state.codeBusy = true;
    renderMachinesPanel();
    machineResult(where, '');
    machineResult('note', '');
    try {
      const res = await withFreshCheck(function () { return api('POST', '/api/machines/pair', { name: name }); });
      state.codeBusy = false;
      state.machineDraft = '';
      state.pairing = { name: res.name, code: res.code, install: res.install, join: res.join,
                        expiresAt: Date.now() + res.expires_in_s * 1000 };
      state.machineCodes = state.machineCodes.filter(function (c) { return c.name !== res.name; });
      state.focusAfter = 'copy:install';
      renderMachinesPanel(true);
    } catch (e) {
      state.codeBusy = false;
      renderMachinesPanel(true);
      machineResult(where, String(e.message || e), true);
    }
  }

  async function cancelCode(name) {
    if (state.pairing && state.pairing.name === name) {
      state.pairing = null;
      state.copiedCmd = null;
      state.machineDraft = name;
    }
    state.machineCodes = state.machineCodes.filter(function (c) { return c.name !== name; });
    state.focusAfter = 'machine-name';
    renderMachinesPanel(true);
    try { await api('POST', '/api/machines/' + encodeURIComponent(name) + '/cancel', {}); } catch (e) { /* it expires anyway */ }
  }

  async function approveMachine(name) {
    if (state.machineBusy.has(name)) return;
    machineResult('note', '');
    state.machineBusy.set(name, 'approve');
    renderMachinesPanel();
    try {
      await withFreshCheck(function () { return api('POST', '/api/machines/' + encodeURIComponent(name) + '/approve', {}); });
      state.machineBusy.delete(name);
      await loadMachines().catch(function () {});
      renderMachinesPanel(true);
    } catch (e) {
      state.machineBusy.delete(name);
      renderMachinesPanel(true);
      machineResult(name, String(e.message || e), true);
    }
  }

  async function removeMachine(name, pending) {
    const q = pending
      ? 'Reject ' + name + '? Its key is forgotten and its dialer stops for good. To pair it again, make a new code.'
      : 'Remove ' + name + '? Its agents leave every room at once, and its dialer stops for good. To bring it back, pair it again with a new code.';
    if (!window.confirm(q)) return;
    machineResult('note', '');
    state.machineBusy.set(name, 'remove');
    renderMachinesPanel();
    try {
      const res = await api('POST', '/api/machines/' + encodeURIComponent(name) + '/remove', {});
      state.machineBusy.delete(name);
      await loadMachines().catch(function () {});
      renderMachinesPanel(true);
      machineResult('note', (pending ? 'Rejected ' : 'Removed ') + name +
        (res && res.ended ? ': ' + plural(res.ended, 'member', 'members') + ' left the rooms.' : '.'), false);
      tryFocus($('machines-close'));
    } catch (e) {
      state.machineBusy.delete(name);
      renderMachinesPanel(true);
      machineResult(name, String(e.message || e), true);
    }
  }

  // name: the machine whose card to show (a sidebar row), or null for Add a machine
  function openMachines(name) {
    openSheet('machines-panel');
    renderMachinesPanel(true);
    loadMachines().catch(function () {});
    const body = $('machines-body');
    if (name) {
      for (const c of body.getElementsByClassName('machine-card')) {
        if (c.dataset.machine !== name) continue;
        if (typeof c.scrollIntoView === 'function') c.scrollIntoView({ block: 'nearest' });
        if (tryFocus(c)) return;
      }
    } else {
      if (tryFocus(document.getElementById('machine-name'))) return;
      if (refocus(body, 'copy:install')) return;
    }
    $('machines-close').focus();
  }

  // ------------------------------------------------------------ passkeys
  // The passkeys sheet (DESIGN.md §31.4), on a hosted broker where passkeys work: how many
  // there are, Add a passkey (window.SBWebAuthn: a fresh passkey check first unless this
  // session had one in the last five minutes), and Sign out everywhere. The full list, with
  // removing one, comes later.
  function renderPasskeysPanel() {
    const body = $('passkeys-body');
    if ($('passkeys-panel').classList.contains('hidden')) return;
    const me = state.me || {};
    const n = me.passkeys || 0;
    body.replaceChildren();

    // your passkeys, and adding one: a name and the button on one row
    const card = el('div', 'passkey-card');
    const head = el('div', 'passkey-head');
    head.append(icon('key'), el('span', 'passkey-title', 'Your passkeys'),
      el('span', 'passkey-count', n ? plural(n, 'passkey', 'passkeys') : 'none yet'));
    card.append(head);
    card.append(el('p', 'fine', n ? 'Any of them signs this switchboard in.'
      : 'None yet: this switchboard is signed in to with `switchboard login`.'));
    const form = el('form', 'passkey-add');
    form.id = 'passkey-add';
    form.setAttribute('autocomplete', 'off');
    const label = el('label', null, 'Name the new passkey');
    label.htmlFor = 'passkey-name';
    const row = el('div', 'passkey-row');
    const input = el('input');
    input.type = 'text';
    input.id = 'passkey-name';
    input.name = 'name';
    input.maxLength = 40;
    input.spellcheck = false;
    input.placeholder = 'phone, security key, this Mac';
    const add = btn('btn btn-primary', state.passkeyBusy ? 'Waiting…' : 'Add a passkey');
    add.type = 'submit';
    add.id = 'passkey-add-btn';
    add.disabled = state.passkeyBusy || !me.hosted;
    add.title = me.fresh ? 'register a new passkey for this switchboard'
      : 'asks for one of your passkeys first, then registers the new one';
    row.append(input, add);
    form.append(label, row);
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      addPasskey((input.value || '').trim() || 'passkey');
    });
    card.append(form);
    const out = el('div', 'fine passkey-result');
    out.id = 'passkey-result';
    card.append(out);
    body.append(card);

    // every browser signed out; the passkeys stay
    const all = el('div', 'passkey-card');
    const head2 = el('div', 'passkey-head');
    head2.append(icon('logout'), el('span', 'passkey-title', 'Sign out everywhere'));
    all.append(head2);
    all.append(el('p', 'fine', 'Signed in somewhere you no longer trust? Every browser is signed out, this one included; your passkeys stay.'));
    const btns = el('div', 'dialog-buttons');
    const so = btn('btn', 'Sign out everywhere');
    so.id = 'logout-all';
    so.addEventListener('click', logoutEverywhere);
    btns.append(so);
    all.append(btns);
    body.append(all);
  }

  function passkeyResult(text, bad) {
    const n = document.getElementById('passkey-result');
    if (n) {
      n.textContent = text;
      n.classList.toggle('bad', !!bad);
    }
  }

  async function addPasskey(name) {
    if (state.passkeyBusy || !window.SBWebAuthn) return;
    if (!window.SBWebAuthn.supported()) {
      passkeyResult('this browser can\'t create passkeys', true);
      return;
    }
    state.passkeyBusy = true;
    renderPasskeysPanel();
    try {
      const res = await window.SBWebAuthn.addPasskey(name);
      state.passkeyBusy = false;
      if (state.me) {
        state.me.passkeys = res.passkeys;
        state.me.fresh = true;
      }
      renderPasskeysPanel();
      passkeyResult('added "' + res.name + '"', false);
    } catch (e) {
      state.passkeyBusy = false;
      renderPasskeysPanel();
      passkeyResult(String(e.message || e), true);
    }
  }

  async function logoutEverywhere() {
    if (!window.confirm('Sign out every browser, this one included? Your passkeys stay; `switchboard login` and your passkeys sign in again.')) return;
    try { await api('POST', '/logout', { all: true }); } catch (e) { /* already signed out */ }
    location.replace('/');
  }

  function openPasskeys() {
    openSheet('passkeys-panel');
    renderPasskeysPanel();
    api('GET', '/api/me').then(function (me) {
      state.me = me;
      renderPasskeysPanel();
    }, function () {});
    $('passkeys-close').focus();
  }

  // ------------------------------------------------------- closed rooms
  function renderClosedButton() {
    $('closed-label').textContent = 'Closed (' + state.closed + ')';
    $('closed-rooms').classList.toggle('hidden', state.closed === 0);
    $('empty-title').textContent = state.closed > 0 ? 'No open rooms.' : 'No rooms yet.';
  }

  function closedCard(c) {
    const card = el('div', 'closed-card');
    const head = el('div', 'closed-head');
    head.append(icon('hash'), el('span', 'closed-name', String(c.display).replace(/^#/, '')));
    card.append(head);
    const facts = el('div', 'fine');
    facts.append('closed' + (c.closed_at ? ' ' + stamp(c.closed_at) : '') + (c.closed_by ? ' by ' + c.closed_by : '') +
      ' · ' + plural(c.messages, 'message', 'messages') + ' · ', el('code', null, c.name));
    card.append(facts);
    const out = el('div', 'fine closed-result');
    const btns = el('div', 'dialog-buttons');
    const b = btn('btn btn-primary', 'Reopen');
    b.disabled = !c.reopenable;
    b.title = c.reopenable
      ? 'bring ' + c.display + ' back under its name; agents join() it again'
      : c.display + ' is taken by an open room: close or delete that one first';
    b.addEventListener('click', function () { reopenRoom(c, b, out); });
    btns.append(b, el('span', 'fine', c.reopenable ? 'Agents join() it again.' : 'The name is taken by an open room.'));
    card.append(btns);
    const del = el('div', 'fine');
    del.className = 'fine closed-delete';
    del.append('Delete for good, from your own terminal:', el('code', 'cmd-block', "switchboard rooms delete '" + c.name + "'"));
    card.append(del, out);
    return card;
  }

  function renderClosedPanel() {
    const body = $('closed-body');
    body.replaceChildren();
    if (!state.closedRooms.length) body.append(el('p', null, 'No closed rooms.'));
    for (const c of state.closedRooms) body.append(closedCard(c));
  }

  async function loadClosed() {
    const data = await api('GET', '/api/closed-rooms');
    state.closedRooms = data.rooms || [];
    renderClosedPanel();
  }

  function openClosed() {
    openSheet('closed-panel');
    renderClosedPanel();
    loadClosed().catch(function (e) {
      $('closed-body').replaceChildren(el('p', 'remote-error', String(e.message || e)));
    });
    $('closed-close').focus();
  }

  async function reopenRoom(c, b, out) {
    b.disabled = true;
    out.classList.remove('bad');
    out.textContent = 'reopening ' + c.display + '…';
    try {
      const res = await api('POST', '/api/closed-rooms/' + encodeURIComponent(String(c.id)) + '/reopen', {});
      await loadRooms();
      selectRoom(res.room.name);
      $('closed-panel').classList.add('hidden');
    } catch (e) {
      b.disabled = !c.reopenable;
      out.textContent = String(e.message || e);
      out.classList.add('bad');
    }
  }

  // -------------------------------------------------------------- input
  const CATCHUP_HINT = 'Commands run only at the start of a message, and agents can’t run them. ' +
    'To catch an agent up, send: /catchup <agent> on <member> (or on "<topic>"; /help lists every form).';

  // opts.confirmed: the Inspector already asked about a /kick
  async function send(text, opts) {
    const r = state.active ? state.rooms.get(state.active) : null;
    if (!r) {
      $('create-build').focus();
      return false;
    }
    // /close removes every agent and the room row: ask first (false gives the text back)
    if (text.split(/\s+/)[0].toLowerCase() === '/close' &&
        !window.confirm('Close ' + r.name + '? ' + r.members.length + ' agent(s) leave it and its tab goes away; ' +
                        'the history is kept and you can reopen it from Closed rooms.')) return false;
    // /kick removes an agent and revokes its membership: ask first too
    const words = text.split(/\s+/);
    if (words[0].toLowerCase() === '/kick' && words[1] && !(opts && opts.confirmed) &&
        !window.confirm('Kick ' + words[1] + ' from ' + r.name + '? It is removed and its membership revoked.')) return false;
    const path = '/api/rooms/' + encodeURIComponent(r.slug);
    try {
      if (text.startsWith('//')) {
        await api('POST', path + '/say', { text: text.slice(1) });
      } else if (text.startsWith('/')) {
        const verb = text.split(/\s+/)[0];
        const res = await api('POST', path + '/command', { text: text });
        // a done /close prunes this room, and the new view would wipe the reply: prune first
        if (res.ok && verb.toLowerCase() === '/close') await loadRooms().catch(function () {});
        renderLocal(verb, !res.ok, res.text);
      } else {
        await api('POST', path + '/say', { text: text });
        // "/catchup" inside a sentence is only text to the agents: say how to run it
        if (/(^|\s)\/(catchup|review)\b/i.test(text)) renderLocal('/catchup', false, CATCHUP_HINT);
      }
      return true;
    } catch (e) {
      renderLocal(String(e.message || e), true);
      return false;
    }
  }

  // queue a send behind the ones in flight; resolves to whether it went out
  function submitText(text, opts) {
    const p = state.sendQueue.then(function () { return send(text, opts); });
    state.sendQueue = p.then(function () {}, function () {});
    return p;
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

  // the Welcome form's room name, without a leading '#'
  function welcomeName() {
    return String($('new-room-name').value || '').trim().replace(/^#+/, '');
  }

  function updateWelcome() {
    const name = welcomeName() || 'build';
    $('create-build').textContent = 'Create #' + name;
    $('join-preview').textContent = 'join switchboard room #' + name;
  }

  // Esc closes the topmost thing: catch-up menu, kick confirm, composer popover, a sheet,
  // the narrow drawer or sheet, then the Inspector. Focus goes back to what opened it.
  function onEscape() {
    const ui = state.inspUi;
    if (state.inspect && ui && ui.menu) return setMenu(false);
    if (state.inspect && ui && ui.confirm) return setConfirm(false);
    if (state.pop) {
      closePop();
      $('input').focus();
      return;
    }
    // in the composer, with a draft a catch-up entry replaced: Esc puts the draft back
    if (state.draft && document.activeElement === $('input') && restoreDraft()) return;
    for (const id of SHEETS) {
      if (!$(id).classList.contains('hidden')) return closeSheet(id);
    }
    const app = $('app');
    if (app.classList.contains('nav-open')) {
      setNav(false);
      $('rooms-toggle').focus();
      return;
    }
    if (app.classList.contains('sheet-open')) {
      setSheet(false);
      (phone() ? $('buddy-toggle') : $('pane-toggle')).focus();
      return;
    }
    if (state.inspect) closeInspector(true);
  }

  function bindInput() {
    const input = $('input');
    state.sendQueue = Promise.resolve();  // sends go out one at a time, in order
    $('composer').addEventListener('submit', function (ev) {
      ev.preventDefault();
      const text = input.value.replace(/\s+$/, '');
      if (!text.trim()) return;
      input.value = '';
      input.rows = 1;
      closePop();
      // a draft that a catch-up entry replaced comes back once this command has gone out
      const draft = text[0] === '/' ? state.draft : null;
      if (draft) state.draft = null;
      submitText(text).then(function (ok) {
        if (!ok && !input.value) {
          input.value = text;  // give a failed message back
          autoGrow();
          if (draft && !state.draft) state.draft = draft;  // and keep the draft for the retry
        } else if (draft && !state.draft) {
          state.draft = draft;
          // back in the composer, unless something new was typed meanwhile: then it waits for
          // Esc or the next command
          if (!input.value) restoreDraft();
        }
      });
      input.focus();
    });
    input.addEventListener('keydown', function (ev) {
      // the popover (palette or mentions) takes the keys first
      if (state.pop) {
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
          ev.preventDefault();
          movePop(ev.key === 'ArrowDown' ? 1 : -1);
          return;
        }
        if (ev.key === 'Tab' || (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing)) {
          ev.preventDefault();
          choosePop();
          return;
        }
        if (ev.key === 'Escape') {
          ev.preventDefault();
          if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
          closePop();
          return;
        }
      }
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        $('composer').requestSubmit();
      }
    });
    input.addEventListener('input', function () {
      autoGrow();
      updatePopover();
    });
    input.addEventListener('click', updatePopover);
    input.addEventListener('blur', closePop);
    $('cmd-btn').addEventListener('click', function () {
      if (!input.value) input.value = '/';
      input.focus();
      updatePopover();
    });
    $('mention-btn').addEventListener('click', function () {
      const v = String(input.value);
      const at = typeof input.selectionStart === 'number' ? input.selectionStart : v.length;
      const before = v.slice(0, at);
      const ins = (before && !/[\s(]$/.test(before) ? ' ' : '') + '@';
      input.value = before + ins + v.slice(at);
      input.focus();
      if (typeof input.setSelectionRange === 'function') input.setSelectionRange(at + ins.length, at + ins.length);
      updatePopover();
    });

    $('new-room').addEventListener('click', function () {
      const name = window.prompt('New room name (for example #build):', '#');
      if (!name || name === '#') return;
      createRoom(name);
    });
    // first run: the Welcome form creates the room named in its field ("Create #build" by default)
    $('create-form').addEventListener('submit', function (ev) {
      ev.preventDefault();
      const name = welcomeName();
      if (!name) {
        $('new-room-name').focus();
        return;
      }
      createRoom('#' + name);
    });
    $('new-room-name').addEventListener('input', updateWelcome);
    $('copy-join').addEventListener('click', function () {
      // only the label changes: textContent on the button itself would drop its copy icon
      const b = $('copy-join');
      const lbl = b.querySelector('span');
      clipboardWrite($('join-line').textContent).then(function () {
        lbl.textContent = 'Copied';
        setTimeout(function () { lbl.textContent = 'Copy'; }, COPIED_MS);
      }, function () {});
    });

    $('logout').addEventListener('click', async function () {
      try { await api('POST', '/logout', {}); } catch (e) { /* already signed out */ }
      location.replace('/');
    });
    $('remotes-close').addEventListener('click', function () { closeSheet('remotes-panel'); });
    $('add-machine').addEventListener('click', function () { openMachines(null); });
    $('machines-close').addEventListener('click', function () { closeSheet('machines-panel'); });
    $('passkeys').addEventListener('click', openPasskeys);
    $('passkeys-close').addEventListener('click', function () { closeSheet('passkeys-panel'); });
    $('closed-rooms').addEventListener('click', openClosed);
    $('closed-close').addEventListener('click', function () { closeSheet('closed-panel'); });

    $('pause-toggle').addEventListener('click', function () {
      const r = activeRoom();
      if (r) submitText(r.settings && r.settings.paused ? '/resume' : '/pause');
    });
    $('pane-toggle').addEventListener('click', togglePane);
    $('buddy-toggle').addEventListener('click', function () {
      const open = !$('app').classList.contains('sheet-open');
      setSheet(open);
      // at 760 px and below the sheet is a modal dialog: focus moves into it (setOverlay makes
      // the page behind inert, so focus left on the pill would have nowhere to go)
      if (open) focusPaneStart();
    });
    $('rooms-toggle').addEventListener('click', function () { setNav(!$('app').classList.contains('nav-open')); });
    // The scrim closes the drawer or sheet it covers for. Clicking it already moved focus to
    // <body>, so focus goes to the toggle that opens what was closed, as Esc does.
    $('scrim').addEventListener('click', function () {
      const app = $('app');
      const nav = app.classList.contains('nav-open');
      const sheet = app.classList.contains('sheet-open');
      setNav(false);
      setSheet(false);
      if (nav) $('rooms-toggle').focus();
      else if (sheet) (phone() ? $('buddy-toggle') : $('pane-toggle')).focus();
    });
    $('insp-back').addEventListener('click', function () { closeInspector(true); });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') onEscape();
    });
    // a click outside the catch-up menu closes it (click-driven: closest is fine here)
    document.addEventListener('click', function (ev) {
      const ui = state.inspUi;
      if (!state.inspect || !ui || !ui.menu) return;
      const t = ev.target;
      if (t && typeof t.closest === 'function' && t.closest('#catchup-menu, #insp-catchup')) return;
      ui.menu = false;
      renderInspector();
    });
    if (typeof window.addEventListener === 'function') window.addEventListener('resize', setOverlay);
    setPaneView(false);
    setOverlay();
    updateWelcome();
  }

  // --------------------------------------------------------------- boot
  // The open rooms, from the broker. A room whose room is gone, or was replaced under the same
  // name (another id, or the same id reused after a delete: another created_at), is dropped;
  // a replaced room starts over at lastId 0. helloAll (a reconnect, a rooms frame) subscribes
  // every room again from its lastId. The log is redrawn only when the active room changed,
  // so local lines (a command's reply) survive a listing that changed nothing on screen.
  async function loadRooms(helloAll) {
    const data = await api('GET', '/api/rooms');
    state.closed = data.closed || 0;
    const listed = new Map();
    for (const r of data.rooms) listed.set(r.name, r);
    const gone = new Set();
    for (const [name, r] of Array.from(state.rooms)) {
      const l = listed.get(name);
      if (!l || l.id !== r.id || l.created_at !== r.createdAt) {
        state.rooms.delete(name);
        gone.add(name);
      }
    }
    const fresh = [];
    for (const l of data.rooms) {
      if (!state.rooms.has(l.name)) fresh.push(l.name);
      const r = room(l.name);
      r.id = l.id;
      r.createdAt = l.created_at;
      r.settings = l.settings || {};
    }
    const was = state.active;
    const pruned = was !== null && gone.has(was);  // a replaced active room counts as pruned
    if (pruned) state.active = null;
    if (state.inspect && gone.has(state.inspect.room)) closeInspector(false);
    renderTabs();
    renderClosedButton();
    const names = helloAll ? Array.from(state.rooms.keys()) : fresh;
    if (names.length) hello(names);
    if (!state.active && state.rooms.size) {
      const want = '#' + decodeURIComponent(location.hash.replace(/^#/, ''));
      selectRoom(state.rooms.has(want) ? want : Array.from(state.rooms.keys()).sort()[0]);
    } else {
      if (pruned) history.replaceState(null, '', location.pathname);  // nothing left to point at
      // redraw only a view that is out of date: local lines (a command's reply) stay
      if (pruned || $('empty').classList.contains('hidden') === (state.rooms.size === 0)) renderLog();
      renderBuddies();
      renderStatus();
    }
    if (pruned) renderLocal(was + ' is no longer open (closed or deleted); Closed rooms can reopen a closed room');
    return fresh;
  }

  async function boot() {
    bindInput();
    keepLogPinned();
    try {
      state.me = await api('GET', '/api/me');
    } catch (e) {
      return;
    }
    // the brand's tooltip names the running version (§1.2); plain text, no markup
    if (state.me && state.me.version) $('brand-name').setAttribute('title', 'switchboard ' + state.me.version);
    // the passkeys sheet and machines that dial in: a hosted broker where passkeys work (§31.4, §31.8)
    $('passkeys').classList.toggle('hidden', !(state.me && state.me.hosted));
    state.machinesHosted = !!(state.me && state.me.hosted);
    renderRemotesSection();
    renderBuddies();
    await loadRooms();
    renderStatus();
    await loadRemotes().catch(function () {});
    if (state.machinesHosted) await loadMachines().catch(function () {});
    connect();
    // the rows' countdowns tick; RTTs refresh (a state change arrives at once, by the socket)
    setInterval(function () {
      if (state.remotes.some(function (r) { return r.state === 'down'; })) {
        renderChips();
        renderRemotesPanel();
      }
    }, 1000);
    setInterval(function () { if (state.remotes.length) loadRemotes().catch(function () {}); }, 20000);
    if (state.machinesHosted) {
      // the pairing's countdown and the last-seen times tick; the RTTs refresh with the list
      setInterval(function () { renderMachinesPanel(); }, 1000);
      setInterval(function () { if (state.machines.length) loadMachines().catch(function () {}); }, 20000);
    }
    // Keep the sliding session (and its cookie) fresh while the page is open.
    setInterval(function () { api('GET', '/api/me').catch(function () {}); }, 30 * 60 * 1000);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
