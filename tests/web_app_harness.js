// Runs the web UI's md.js and app.js in node against a tiny fake DOM, a fake fetch and a
// fake WebSocket, and prints what one scenario left on screen and on the socket as JSON
// (tests/unit/test_web_app_behavior.py). No browser, no network, no timers waited on:
// settle() only lets queued promise callbacks and setImmediate turns run.
//
// The fake DOM is deliberately small: app.js promises to use only what it offers on the
// boot, frame, send and command paths (see app.js's header), so a call outside that set
// fails here first.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.join(__dirname, '..', 'src', 'switchboard', 'web', 'static');
const MD = path.join(STATIC, 'md.js');
const APP = path.join(STATIC, 'app.js');

class ClassList {
  constructor() { this.s = new Set(); }
  add(...c) { for (const x of c) this.s.add(x); }
  remove(...c) { for (const x of c) this.s.delete(x); }
  contains(c) { return this.s.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.s.has(c) : !!force;
    if (on) this.s.add(c); else this.s.delete(c);
    return on;
  }
}

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.classList = new ClassList();
    this.dataset = {};
    this.attrs = {};
    this.listeners = {};
    this.own = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.value = '';
    this.disabled = false;
    this.nodeType = tag === '#text' ? 3 : (tag === '#fragment' ? 11 : 1);
  }
  get childNodes() { return this.children; }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  get lastChild() { return this.children.length ? this.children[this.children.length - 1] : null; }
  set className(v) {
    this.classList = new ClassList();
    for (const c of String(v).split(/\s+/)) if (c) this.classList.add(c);
  }
  get className() { return Array.from(this.classList.s).join(' '); }
  set textContent(v) { this.children = []; this.own = String(v); }
  get textContent() {
    return this.own + this.children.map(function (c) { return typeof c === 'string' ? c : c.textContent; }).join('');
  }
  append(...xs) {
    for (const x of xs) {
      if (x instanceof El && x.tag === '#fragment') this.children.push(...x.children);
      else this.children.push(x);
    }
  }
  replaceChildren(...xs) { this.children = []; this.own = ''; this.append(...xs); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatch(t, ev) { for (const f of this.listeners[t] || []) f(ev || { preventDefault() {} }); }
  focus() {}
  showModal() { this.open = true; }
  close(value) { this.returnValue = value || ''; this.open = false; if (this.onclose) this.onclose(); }
  requestSubmit() { this.dispatch('submit', { preventDefault() {} }); }
}

// ------------------------------------------------------------------ fakes
function makeWorld(rooms) {
  const byId = new Map();
  const document = {
    title: '',
    documentElement: new El('html'),
    activeElement: null,
    listeners: {},
    getElementById(id) {
      if (!byId.has(id)) {
        const e = new El(id === 'app-dialog' ? 'dialog' : 'div');
        e.id = id;
        e.ownerDocument = document;
        // as index.html starts them
        if (['empty', 'closed-panel', 'remotes-panel', 'closed-rooms', 'room-empty', 'palette', 'mentions', 'scrim',
             'remotes-section', 'catchup-menu', 'insp-queue', 'machines-panel', 'passkeys-panel',
             'add-machine'].includes(id)) e.classList.add('hidden');
        byId.set(id, e);
      }
      return byId.get(id);
    },
    createElement(tag) { const e = new El(tag); e.ownerDocument = document; return e; },
    createDocumentFragment() { return new El('#fragment'); },
    createTextNode(t) { const n = new El('#text'); n.own = String(t); return n; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  };

  const server = {
    rooms: rooms,   // [{id, name, created_at}]
    closed: 0,
    fail: new Set(),  // 'GET /api/rooms': answer 500 once
    holdNext: new Set(),  // hold the next request to this key until release(key)
    held: [],
    onCommand: null,  // (slug, text) -> body; may change state and deliver frames first
    members: {},      // slug -> [member_dict], for GET /api/rooms/{slug}/members
    details: {},      // 'slug/name' -> the member-detail body; missing -> 404
    commands: [],     // every command text the page sent, in order
    said: [],         // every /say text the page sent, in order
  };

  function roomDict(r) {
    return { id: r.id, name: r.name, slug: r.name.slice(1), created_at: r.created_at, members: 0, last_id: 0,
             settings: {} };
  }

  function route(method, url, body) {
    if (method === 'GET' && url === '/api/me') return function () { return [200, { human: 'alice', test_mode: false }]; };
    if (method === 'GET' && url === '/api/rooms') {
      return function () { return [200, { rooms: server.rooms.map(roomDict), closed: server.closed }]; };
    }
    if (method === 'POST' && url === '/api/rooms') {
      if (server.onCreate) server.onCreate(body.name);
      server.rooms = server.rooms.concat([{ id: 9, name: body.name, created_at: 900 }]);
      return function () { return [200, { room: roomDict(server.rooms[server.rooms.length - 1]) }]; };
    }
    if (method === 'GET' && url === '/api/remotes') return function () { return [200, { remotes: [] }]; };
    if (method === 'GET' && url === '/api/closed-rooms') return function () { return [200, { rooms: [] }]; };
    const m = /^\/api\/rooms\/([^/]+)\/command$/.exec(url);
    if (method === 'POST' && m) {
      server.commands.push(body.text);
      const got = server.onCommand ? server.onCommand(decodeURIComponent(m[1]), body.text)  // side effects now
        : { ok: true, text: 'ok' };
      return function () { return [200, got]; };
    }
    const ms = /^\/api\/rooms\/([^/]+)\/say$/.exec(url);
    if (method === 'POST' && ms) {
      server.said.push(body.text);
      return function () { return [200, { ok: true }]; };
    }
    const ml = /^\/api\/rooms\/([^/]+)\/members$/.exec(url);
    if (method === 'GET' && ml) {
      return function () { return [200, { members: server.members[decodeURIComponent(ml[1])] || [] }]; };
    }
    const md = /^\/api\/rooms\/([^/]+)\/members\/([^/]+)$/.exec(url);
    if (method === 'GET' && md) {
      const key = decodeURIComponent(md[1]) + '/' + decodeURIComponent(md[2]);
      return function () {
        const d = server.details[key];
        return d ? [200, d] : [404, { error: 'not_found', message: key + ' is not in the room' }];
      };
    }
    return function () { return [404, { message: 'no route ' + method + ' ' + url }]; };
  }

  function respond(key, answer, resolve) {
    let status;
    let data;
    if (server.fail.has(key)) {
      server.fail.delete(key);
      status = 500;
      data = { message: 'broker hiccup' };
    } else {
      [status, data] = answer();
    }
    resolve({ status: status, ok: status < 400, statusText: String(status), json: async function () { return data; } });
  }

  function fetch(url, opts) {
    const method = opts.method || 'GET';
    const key = method + ' ' + url;
    const answer = route(method, url, opts.body ? JSON.parse(opts.body) : null);
    return new Promise(function (resolve) {
      if (server.holdNext.has(key)) {
        server.holdNext.delete(key);
        server.held.push({ key: key, go: function () { respond(key, answer, resolve); } });
      } else {
        setImmediate(function () { respond(key, answer, resolve); });
      }
    });
  }

  server.release = function (key) {
    const i = server.held.findIndex(function (h) { return h.key === key; });
    if (i < 0) throw new Error('nothing held for ' + key);
    server.held.splice(i, 1)[0].go();
  };

  const sockets = [];
  class WebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.listeners = {};
      sockets.push(this);
    }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    send(data) { this.sent.push(JSON.parse(data)); }
    fire(t, ev) { for (const f of this.listeners[t] || []) f(ev); }
    open() { this.readyState = 1; this.fire('open', {}); }
    deliver(frame) { this.fire('message', { data: JSON.stringify(frame) }); }
  }
  WebSocket.OPEN = 1;

  const location = {
    protocol: 'http:', host: '127.0.0.1:7419', hostname: '127.0.0.1', pathname: '/', hash: '',
    replace(u) { this.replaced = u; },
  };
  const history = {
    replaceState(_s, _t, u) { location.hash = String(u).startsWith('#') ? String(u) : ''; },
  };

  const ctx = {
    document: document, location: location, history: history, fetch: fetch, WebSocket: WebSocket,
    console: console, Date: Date, JSON: JSON, Map: Map, Set: Set, Promise: Promise, Error: Error,
    setTimeout: setTimeout, clearTimeout: clearTimeout,
    setInterval: function () { return 0; }, clearInterval: function () {},  // the ping and refresh timers
    encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent, URL: URL,
  };
  ctx.window = ctx;
  ctx.window.alert = function (m) { throw new Error('alert: ' + m); };
  ctx.window.prompt = function () { return null; };
  ctx.window.confirm = function () { throw new Error('native confirm called'); };
  vm.createContext(ctx);
  // as index.html loads them: md.js (window.SBMarkdown) first, then app.js
  if (fs.existsSync(MD)) vm.runInContext(fs.readFileSync(MD, 'utf8'), ctx, { filename: 'md.js' });
  vm.runInContext(fs.readFileSync(APP, 'utf8'), ctx, { filename: 'app.js' });

  return { document: document, server: server, sockets: sockets, location: location, window: ctx,
           $: document.getElementById.bind(document) };
}

async function settle() {
  for (let i = 0; i < 50; i++) await new Promise(function (r) { setImmediate(r); });
}

async function boot(w) {
  for (const f of w.document.listeners.DOMContentLoaded) f();
  await settle();
  const ws = w.sockets[w.sockets.length - 1];
  ws.open();
  await settle();
  return ws;
}

function lines(w) {
  return w.$('log').children.map(function (c) { return c.textContent; });
}

function hellos(ws) {
  return ws.sent.filter(function (f) { return f.t === 'hello'; });
}

function report(w, ws, extra) {
  const tabs = w.$('tabs').children.map(function (c) { return c.dataset.room; });
  return Object.assign({
    log: lines(w),
    logHidden: w.$('log').classList.contains('hidden'),
    tabs: tabs,
    title: w.document.title,
    hellos: hellos(ws),
  }, extra || {});
}

async function send(w, text) {
  w.$('input').value = text;
  w.$('composer').dispatch('submit', { preventDefault() {} });
}

// ------------------------------------------------------------ tree helpers
function walk(node, fn) {
  if (!(node instanceof El)) return;
  fn(node);
  for (const c of node.children) walk(c, fn);
}

function findAll(root, pred) {
  const out = [];
  walk(root, function (n) { if (pred(n)) out.push(n); });
  return out;
}

function key(el, k, extra) {
  el.dispatch('keydown', Object.assign({ key: k, preventDefault() {}, stopPropagation() {} }, extra || {}));
}

function type(w, text) {
  const input = w.$('input');
  input.value = text;
  input.dispatch('input', {});
}

function click(el) { el.dispatch('click', { target: el, preventDefault() {}, stopPropagation() {} }); }

function memberButton(w, name) {
  const b = findAll(w.$('buddy-list'), function (n) { return n.tag === 'button' && n.dataset.name === name; })[0];
  if (!b) throw new Error('no member row for ' + name);
  return b;
}

const TS = 1790000000;

// a room with the four agents of the approved mockups (Chosen.dc): one approvals-off, one
// parked, one on a remote machine
const MEMBERS = [
  { name: 'claude-1', harness: 'claude', status: 'idle', tier: 'claude:inbox', tier_note: '', away: null,
    approval_mode: 'prompting', env_leak: false, held: false, queued: 0, inflight: 0, parked: false, parked_reason: null, host: '' },
  { name: 'codex-1', harness: 'codex', status: 'busy', tier: 'codex:daemon', tier_note: '', away: null,
    approval_mode: 'bypass', env_leak: false, held: false, queued: 2, inflight: 1, parked: false, parked_reason: null, host: '' },
  { name: 'devin-1', harness: 'devin', status: 'idle', tier: 'devin:wait-loop', tier_note: '', away: null,
    approval_mode: 'prompting', env_leak: false, held: false, queued: 4, inflight: 0, parked: true,
    parked_reason: 'its turn ended without wait()', host: '' },
  { name: 'bench', harness: 'claude', status: 'idle', tier: 'claude:inbox', tier_note: '', away: null,
    approval_mode: 'prompting', env_leak: false, held: false, queued: 0, inflight: 0, parked: false, parked_reason: null,
    host: 'fpga-pi' },
];

function chat(id, from, text, extra) {
  return Object.assign({ id: id, ts: TS + id, kind: 'chat', from: from, harness: 'claude', sender_kind: 'agent',
                         via: 'mcp', text: text, reply_to: null, mentions: [], host: null }, extra || {});
}

async function buildRoom(members) {
  const w = makeWorld([{ id: 1, name: '#build', created_at: 100 }]);
  w.location.hash = '#build';
  const ws = await boot(w);
  if (members) {
    ws.deliver({ t: 'members', room: '#build', members: members });
    await settle();
  }
  return { w: w, ws: ws };
}

function rowInfo(w) {
  return w.$('log').children.map(function (c) {
    return { cls: c.className, role: c.attrs.role || null, text: c.textContent, from: c.dataset.from || null,
             id: c.dataset.id || null };
  });
}

const CLOSE_REPLY = 'closed #build: 2 agent(s) removed (1 on fpga-pi); history kept.' +
  ' The name is free again; reopen this room from Closed rooms in the web UI';

// A room tab's badge (red count, addressed to you) and quiet dot (other activity), by name.
function tabInfo(w, name) {
  const b = findAll(w.$('tabs'), function (n) { return n.tag === 'button' && n.dataset.room === name; })[0];
  if (!b) return null;
  const badge = findAll(b, function (n) { return n.classList.contains('badge'); })[0];
  const dot = findAll(b, function (n) { return n.classList.contains('dot'); })[0];
  return {
    badge: badge ? { text: badge.textContent, label: badge.attrs['aria-label'] } : null,
    quietDot: dot ? { label: dot.attrs['aria-label'] } : null,
  };
}

function roomsBadge(w) {
  return { text: w.$('rooms-badge').textContent, hidden: w.$('rooms-badge').classList.contains('hidden') };
}

// ---------------------------------------------------------------- scenarios
const SCENARIOS = {
  // A rooms frame whose listing looks unchanged (the room was closed, which dropped this page's
  // subscription, then reopened under the same id before the listing was served).
  async rooms_frame_hellos_again() {
    const w = makeWorld([{ id: 1, name: '#build', created_at: 100 }]);
    w.location.hash = '#build';
    const ws = await boot(w);
    ws.sent.length = 0;
    ws.deliver({ t: 'rooms', rooms: [] });  // the close's frame; the listing already has #build again
    await settle();
    return report(w, ws);
  },

  // The resync after a (re)connect fails: the socket must still follow the tabs it has.
  async resync_fails_on_open() {
    const w = makeWorld([{ id: 1, name: '#build', created_at: 100 }]);
    for (const f of w.document.listeners.DOMContentLoaded) f();
    await settle();
    w.server.fail.add('GET /api/rooms');
    const ws = w.sockets[0];
    ws.open();
    await settle();
    return report(w, ws);
  },

  // /close from the composer: the reply must still be on screen after the tab is pruned.
  async close_reply_frame_late() { return closeScenario(true, true); },
  async close_reply_frame_first() { return closeScenario(true, false); },
  async close_reply_last_room() { return closeScenario(false, true); },

  // The active room was closed and a new room took its name (another id).
  async replaced_active_room() { return replacedScenario(2, 200); },
  // ... or it was deleted and re-created, which reused its id (only created_at differs).
  async reused_id_room() { return replacedScenario(1, 200); },

  // Chat bodies are Markdown (md.js): markup renders, raw HTML stays text, a non-http(s)
  // link is never a link.
  async markdown_in_log() {
    const { w, ws } = await buildRoom(MEMBERS);
    const bad = 'java' + 'script:alert(1)';
    ws.deliver({ t: 'msg', room: '#build', msg: chat(10, 'claude-1', '**hi** <b>x</b> [a](' + bad + ')') });
    await settle();
    const log = w.$('log');
    return report(w, ws, {
      rows: rowInfo(w),
      strong: findAll(log, function (n) { return n.tag === 'strong'; }).map(function (n) { return n.textContent; }),
      hrefs: findAll(log, function (n) { return n.href !== undefined || n.attrs.href !== undefined; }).length,
      anchors: findAll(log, function (n) { return n.tag === 'a'; }).length,
      blocked: findAll(log, function (n) { return n.classList.contains('md-blocked'); }).length,
      bodyText: findAll(log, function (n) { return n.classList.contains('msg-text'); }).map(function (n) { return n.textContent; }),
    });
  },

  // A second message from the same sender within 5 minutes is a continuation row; another
  // sender, or a reply, starts a new group.
  async grouped_continuation() {
    const { w, ws } = await buildRoom(MEMBERS);
    ws.deliver({ t: 'msg', room: '#build', msg: chat(10, 'claude-1', 'first') });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(11, 'claude-1', 'second') });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(12, 'codex-1', 'other', { harness: 'codex' }) });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(13, 'codex-1', 'a reply', { harness: 'codex', reply_to: 10 }) });
    await settle();
    return report(w, ws, { rows: rowInfo(w) });
  },

  // Focus mode (#99): which messages collapse (an agent message not addressed to alice - the
  // same addressedToMe check as the room badges, #109) and which don't; a click expands just
  // the row clicked, and Focus off rebuilds every row back to normal. Issue #138: an agent's
  // own @humans (id 15) is addressed to alice too, so it never collapses either.
  async focus_mode_collapsing() {
    const { w, ws } = await buildRoom(MEMBERS);
    ws.deliver({ t: 'msg', room: '#build', msg: { id: 9, ts: TS + 9, kind: 'chat', from: 'alice', sender_kind: 'human',
                                                    via: 'web', text: 'any update on the sweep?', reply_to: null,
                                                    mentions: [], host: null } });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(10, 'claude-1', 'on it, checking now', { reply_to: 9 }) });
    ws.deliver({ t: 'msg', room: '#build',
      msg: chat(11, 'codex-1', '@alice take a look', { harness: 'codex', mentions: ['alice'] }) });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(12, 'devin-1', 'nothing unusual here', { harness: 'devin' }) });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(13, 'codex-1', 'thanks', { harness: 'codex', reply_to: 10 }) });
    ws.deliver({ t: 'msg', room: '#build', msg: { id: 14, ts: TS + 14, kind: 'chat', from: 'alice', sender_kind: 'human',
                                                    via: 'web', text: 'thanks all', reply_to: null, mentions: [], host: null } });
    ws.deliver({ t: 'msg', room: '#build',
      msg: chat(15, 'claude-1', '@humans need a decision: ship tonight or wait?', { mentions: ['humans'] }) });
    await settle();
    const before = rowInfo(w);  // Focus off: nobody is marked collapsible yet

    click(w.$('focus-toggle'));
    await settle();
    const on = { pressed: w.$('focus-toggle').attrs['aria-pressed'], rows: rowInfo(w) };

    // expand only devin-1's collapsed message (id 12) by clicking its summary
    const devinRow = findAll(w.$('log'), function (n) { return n.dataset && n.dataset.id === '12'; })[0];
    const summary = findAll(devinRow, function (n) { return n.classList.contains('collapsed-line'); })[0];
    click(summary);
    await settle();
    const expanded = { rows: rowInfo(w), summaryText: summary.textContent };

    click(w.$('focus-toggle'));
    await settle();
    const off = { pressed: w.$('focus-toggle').attrs['aria-pressed'], rows: rowInfo(w) };

    return { before: before, on: on, expanded: expanded, off: off };
  },

  // A warn notice is a red row, announced (role=alert) only when it arrives live.
  async warn_notice() {
    const { w, ws } = await buildRoom(MEMBERS);
    ws.deliver({ t: 'msg', room: '#build', msg: { id: 10, ts: TS, kind: 'notice', from: 'switchboard', sender_kind: 'system',
                                                    text: 'loop guard: 30 agent messages in a row; #build paused', level: 'warn',
                                                    mentions: [] } });
    ws.deliver({ t: 'msg', room: '#build', msg: { id: 11, ts: TS + 1, kind: 'notice', from: 'switchboard', sender_kind: 'system',
                                                    text: 'devin-1 is parked', mentions: [] } });
    await settle();
    return report(w, ws, { rows: rowInfo(w) });
  },

  // The Members list keeps every flag of the old buddy list.
  async member_flags() {
    const { w, ws } = await buildRoom(MEMBERS);
    return report(w, ws, {
      buddies: w.$('buddy-list').textContent,
      agentsTitle: w.$('agents-title').textContent,
      approvals: w.$('st-approvals').textContent,
      approvalsHidden: w.$('st-approvals').classList.contains('hidden'),
      alert: w.$('buddy-alert').textContent,
      roomSub: w.$('room-sub').textContent,
      roomEmptyHidden: w.$('room-empty').classList.contains('hidden'),
    });
  },

  async approvals_chip_cases() {
    const one = await buildRoom(MEMBERS);
    const oneText = one.w.$('st-approvals').textContent;
    const manyMembers = MEMBERS.map(function (m) { return m.name === 'bench' ? m : { ...m, approval_mode: 'bypass' }; });
    const many = await buildRoom(manyMembers);
    const mixedMembers = MEMBERS.map(function (m) { return Object.assign({}, m, {
      approval_mode: m.name === 'codex-1' || m.name === 'claude-1' ? 'bypass' : 'unknown',
    }); });
    const mixed = await buildRoom(mixedMembers);
    return {
      one: oneText,
      many: many.w.$('st-approvals').textContent,
      mixed: mixed.w.$('st-approvals').textContent,
      mixedTip: mixed.w.$('approvals-tip').textContent,
    };
  },

  async member_flag_tooltips() {
    const members = MEMBERS.map(function (m) { return m.name === 'bench' ? Object.assign({}, m, { approval_mode: 'unknown' }) : m; });
    const { w, ws } = await buildRoom(members);
    const list = w.$('buddy-list');
    const flags = findAll(list, function (n) { return n.classList && n.classList.contains('approval-flag'); });
    return report(w, ws, {
      warnings: findAll(list, function (n) { return n.classList && n.classList.contains('m-warn'); }).length,
      flags: flags.map(function (f) { return { role: f.attrs.role, tabindex: f.attrs.tabindex, label: f.attrs['aria-label'] }; }),
    });
  },

  // An unknown approval mode may also let the agent act without asking.
  async unknown_approval_mode() {
    const members = MEMBERS.map(function (m) { return m.name === 'codex-1' ? { ...m, approval_mode: 'unknown' } : m; });
    const { w, ws } = await buildRoom(members);
    const row = memberButton(w, 'codex-1');
    const flag = findAll(w.$('buddy-list'), function (n) { return n.classList.contains('flag-unknown'); })[0];
    click(row);
    return report(w, ws, {
      row: row.textContent,
      flagLabel: flag.attrs['aria-label'],
      chipTip: w.$('approvals-tip').textContent,
      inspector: w.$('insp-body').textContent,
    });
  },

  // Header chips: Paused, and the loop guard off.
  async status_chips() {
    const { w, ws } = await buildRoom(MEMBERS);
    ws.deliver({ t: 'room', room: '#build', settings: { paused: true, paused_reason: 'loop guard', hop_limit: 0, hop_count: 7,
                                                         budget_remaining: 0, budget_per_hour: 60 } });
    await settle();
    const on = { t: 'room', room: '#build', settings: { paused: false, hop_limit: 30, hop_count: 3, budget_remaining: 47,
                                                         budget_per_hour: 60 } };
    const first = {
      state: w.$('st-state').textContent, stateBad: w.$('st-state').classList.contains('bad'),
      hops: w.$('st-hops').textContent, hopsBad: w.$('st-hops').classList.contains('bad'),
      budget: w.$('st-budget').textContent, budgetBad: w.$('st-budget').classList.contains('bad'),
      paused: w.$('banner-paused').textContent, pausedHidden: w.$('banner-paused').classList.contains('hidden'),
      pauseLabel: w.$('pause-toggle').attrs['aria-label'],
    };
    ws.deliver(on);
    await settle();
    const fill = findAll(w.$('st-budget'), function (n) { return n.classList.contains('meter-fill'); })[0];
    return report(w, ws, {
      first: first,
      second: {
        state: w.$('st-state').textContent, hops: w.$('st-hops').textContent, hopsBad: w.$('st-hops').classList.contains('bad'),
        budget: w.$('st-budget').textContent, fill: fill ? fill.className : null,
        pausedHidden: w.$('banner-paused').classList.contains('hidden'), pauseLabel: w.$('pause-toggle').attrs['aria-label'],
      },
    });
  },

  // An empty room shows the join hint; the first member hides it.
  async room_empty_hint() {
    const { w, ws } = await buildRoom(null);
    const before = { hidden: w.$('room-empty').classList.contains('hidden'), line: w.$('join-line').textContent };
    ws.deliver({ t: 'members', room: '#build', members: MEMBERS.slice(0, 1) });
    await settle();
    return report(w, ws, { before: before, after: w.$('room-empty').classList.contains('hidden') });
  },

  // The Inspector: opens from a member row, renders from member_dict at once, then from the
  // member-detail GET; only the newest answer counts; a member that leaves closes it.
  async inspector() {
    const { w, ws } = await buildRoom(MEMBERS);
    ws.deliver({ t: 'msg', room: '#build', msg: chat(212, 'claude-1', 'please review parse_port') });
    ws.deliver({ t: 'msg', room: '#build', msg: chat(213, 'codex-1', 'Two issues', { harness: 'codex' }) });
    await settle();
    w.server.details['build/codex-1'] = {
      room: '#build',
      member: Object.assign({}, MEMBERS[1], { joined_at: TS - 600, held_at: null, status_at: TS - 60, status_src: 'hook',
                                              last_seen: TS + 213, last_seen_what: 'said' }),
      session: { id: '3f2a0000000000000000000000c91e', why: '', where: 'this machine' },
      queued: [{ id: 212, prio: 'mention' }, { id: 999, prio: 'chatter' }],
      counts: { pending: 2, offered: 0, handled: 4 },
      timeline: [{ ts: TS + 100, kind: 'offer', path: 'inbox', n: 2, from: ['claude-1', 'bench@fpga-pi'], prio: 'mention' },
                 { ts: TS + 213, kind: 'said', id: 213 }, { ts: TS + 214, kind: 'pass' }],
    };
    w.server.details['build/devin-1'] = {
      room: '#build', member: Object.assign({}, MEMBERS[2], { joined_at: TS - 600, last_seen: null, last_seen_what: null }),
      session: { id: null, why: 'a test session', where: 'this machine' }, queued: [], counts: {},
      timeline: [{ ts: TS + 50, kind: 'parked', reason: 'its turn ended without wait()' }],
    };
    // codex-1's answer is held; devin-1 is opened next and answers first; codex-1's late answer must not win
    w.server.holdNext.add('GET /api/rooms/build/members/codex-1');
    click(memberButton(w, 'codex-1'));
    const early = { body: w.$('insp-body').textContent, inspecting: w.$('pane').classList.contains('inspecting') };
    const sel = rowInfo(w).filter(function (r) { return r.cls.split(' ').includes('sel'); }).map(function (r) { return r.from; });
    click(memberButton(w, 'devin-1'));
    await settle();
    w.server.release('GET /api/rooms/build/members/codex-1');
    await settle();
    const devin = w.$('insp-body').textContent;
    // back to codex-1, answered this time
    click(memberButton(w, 'codex-1'));
    await settle();
    const codex = { body: w.$('insp-body').textContent, pos: w.$('insp-pos').textContent };
    // codex-1 leaves
    ws.deliver({ t: 'members', room: '#build', members: MEMBERS.filter(function (m) { return m.name !== 'codex-1'; }) });
    await settle();
    return report(w, ws, {
      early: early, sel: sel, devin: devin, codex: codex,
      after: { inspecting: w.$('pane').classList.contains('inspecting'), membersOff: w.$('members-view').classList.contains('offscreen') },
    });
  },

  // The Inspector's Hold and Kick run the same command path as the composer.
  async inspector_actions() {
    const { w, ws } = await buildRoom(MEMBERS);
    w.server.details['build/claude-1'] = { room: '#build', member: MEMBERS[0], session: { id: null, why: 'x' }, queued: [], timeline: [] };
    click(memberButton(w, 'claude-1'));
    await settle();
    const btns = findAll(w.$('insp-body'), function (n) { return n.tag === 'button'; });
    const byId = function (id) { return btns.find(function (b) { return b.id === id; }); };
    click(byId('insp-hold'));
    await settle();
    click(byId('insp-kick'));
    await settle();
    const confirm = w.$('app-dialog');
    const confirmShown = confirm.open && confirm.textContent.includes('Kick claude-1 from #build?');
    const kick = findAll(confirm, function (n) { return n.id === 'app-dialog-action'; })[0];
    click(kick);
    await settle();
    return report(w, ws, { commands: w.server.commands, confirmShown: confirmShown,
                           inspecting: w.$('pane').classList.contains('inspecting') });
  },

  // A catch-up entry fills the composer but keeps the message being written: the draft comes
  // back once the command has gone out (review finding: it used to be overwritten for good).
  async catchup_keeps_the_draft() {
    const { w, ws } = await buildRoom(MEMBERS);
    w.server.details['build/claude-1'] = { room: '#build', member: MEMBERS[0], session: { id: null, why: 'x' }, queued: [], timeline: [] };
    const input = w.$('input');
    type(w, 'half-written note to codex-1');
    click(memberButton(w, 'claude-1'));
    await settle();
    const item = function (label) {
      return findAll(w.$('insp-body'), function (n) { return n.classList.contains('menu-item') && n.textContent.indexOf(label) === 0; })[0];
    };
    click(item('The whole room'));
    const filled = input.value;
    // a second pick while the command is in the composer must not stash the command as the draft
    click(item('A topic…'));
    const refilled = input.value;
    w.$('composer').dispatch('submit', { preventDefault() {} });
    await settle();
    const afterSend = input.value;
    // a plain message sent next does not bring anything back
    await send(w, 'hello');
    await settle();
    return report(w, ws, { filled: filled, refilled: refilled, afterSend: afterSend, afterPlain: input.value,
                           commands: w.server.commands, said: w.server.said });
  },

  // /kick typed in the composer asks first; declining gives the text back.
  async kick_typed_declined() {
    const { w, ws } = await buildRoom(MEMBERS);
    await send(w, '/kick codex-1');
    await settle();
    const dialog = w.$('app-dialog');
    const title = findAll(dialog, function (n) { return n.id === 'app-dialog-title'; })[0].textContent;
    const action = findAll(dialog, function (n) { return n.id === 'app-dialog-action'; })[0].textContent;
    click(findAll(dialog, function (n) { return n.id === 'app-dialog-cancel'; })[0]);
    await settle();
    return report(w, ws, { commands: w.server.commands, title: title, action: action, input: w.$('input').value });
  },

  // The slash palette and @mention autocomplete complete without sending anything.
  async palette_and_mentions() {
    const { w, ws } = await buildRoom(MEMBERS);
    const input = w.$('input');
    type(w, '/');
    const all = { hidden: w.$('palette').classList.contains('hidden'),
                  items: findAll(w.$('palette'), function (n) { return n.classList.contains('pal-cmd'); }).map(function (n) { return n.textContent; }) };
    type(w, '/ho');
    const ho = findAll(w.$('palette'), function (n) { return n.classList.contains('pal-cmd'); }).map(function (n) { return n.textContent; });
    key(input, 'ArrowDown');
    const active = input.attrs['aria-activedescendant'];
    key(input, 'Tab');
    const completed = { value: input.value, hidden: w.$('palette').classList.contains('hidden') };
    type(w, '//pause is text');
    const slashText = w.$('palette').classList.contains('hidden');
    type(w, 'hi @co');
    const men = findAll(w.$('mentions'), function (n) { return n.classList.contains('m-name'); }).map(function (n) { return n.textContent; });
    key(input, 'Enter');
    const mention = { value: input.value, hidden: w.$('mentions').classList.contains('hidden') };
    type(w, '/wh');
    key(input, 'Escape');
    const esc = { value: input.value, hidden: w.$('palette').classList.contains('hidden') };
    await settle();
    return report(w, ws, { all: all, ho: ho, active: active, completed: completed, slashText: slashText, men: men,
                           mention: mention, esc: esc, commands: w.server.commands, said: w.server.said });
  },

  // Issue #112: the @ popover matches any part of a name, ranked exact, whole-name prefix,
  // word prefix (split on "-"/"_"), then substring; case-insensitively; a prefix match ranks
  // above a mid-name one even when the mid-name match is online and the prefix match isn't;
  // and the matched slice is its own <mark> node, never baked into the name's textContent.
  async mention_match_anywhere() {
    const base = MEMBERS[0];
    const agent = function (name, status) { return Object.assign({}, base, { name: name, status: status || 'idle' }); };
    const members = [
      agent('darius-skills-agent'), agent('mr-owner-2'), agent('eval-reviewer-resumed'),
      agent('rev-helper', 'offline'),  // a whole-name prefix match, but offline
      agent('xa-call-beta', 'offline'), agent('xb-call-alpha'),  // a tied rank: only the online one should lead
    ];
    const { w, ws } = await buildRoom(members);
    const options = function () {
      return findAll(w.$('mentions'), function (n) { return n.attrs.role === 'option'; }).map(function (n) { return n.id; });
    };
    const hitsOf = function (name) {
      const row = findAll(w.$('mentions'), function (n) { return n.id === 'men-' + name; })[0];
      return findAll(row, function (n) { return n.classList.contains('m-name-hit'); }).map(function (n) { return n.textContent; });
    };

    type(w, '@skill');  // a word prefix: "skill" is a prefix of the "skills" word
    const skill = { options: options(), hits: hitsOf('darius-skills-agent') };
    type(w, '@Skill');  // the same query, capitalized: case-insensitive matching
    const skillCased = options();
    type(w, '@owner');  // another word prefix
    const owner = options();
    type(w, '@esumed');  // a substring that isn't at any word boundary ("resumed")
    const substring = options();
    type(w, '@rev');  // ranking: rev-helper's whole-name prefix beats the word-prefix match,
    const rev = options();  // even though rev-helper is offline and the other agent is online
    type(w, '@call');  // a ranking tie: the online agent leads the offline one
    const call = options();
    await settle();
    return report(w, ws, { skill: skill, skillCased: skillCased, owner: owner, substring: substring, rev: rev, call: call });
  },

  // Issue #110: the composer's mirror layer (#input-mirror) highlights an @mention only when
  // it matches an active member or a person (the human, "alice" here) -- the same set
  // parse_mentions on the broker would deliver to. An unknown @name is left as plain text, with
  // no warning style of its own. Picking a known name from the @ popover (choosePop) sets
  // #input's value by script, not by typing, and must land in the mirror just the same.
  async mention_highlight_in_composer() {
    const { w, ws } = await buildRoom(MEMBERS);
    const mirror = function () { return w.$('input-mirror'); };
    const hl = function () {
      return findAll(mirror(), function (n) { return n.classList.contains('mention-hl'); })
        .map(function (n) { return n.textContent; });
    };
    type(w, 'ask @claude-1 and @nope-one, cc @alice');
    const typed = { text: mirror().textContent, hl: hl() };
    w.$('input').value = '';
    type(w, 'hi @cod');
    key(w.$('input'), 'Enter');  // completes to "hi @codex-1 " through the mentions popover
    const picked = { value: w.$('input').value, hl: hl() };
    w.$('input').value = '';
    // Issue #111: @here/@everyone highlight as you type too, same as a real member's name.
    // Issue #138: @humans does too, unconditionally for the same reason -- the composer is
    // always a person's, and both a person's and an agent's @humans are real.
    type(w, '@humans needs eyes, @everyone look, then @here too, but not @all');
    const broadcast = { text: mirror().textContent, hl: hl() };
    await settle();
    return report(w, ws, { typed: typed, picked: picked, broadcast: broadcast });
  },

  // Issue #111: @here and @everyone show first in the @ popover, each with a one-line
  // description, above the member list; an empty query (bare "@") lists both; narrowing to a
  // query only one of them matches (here's own prefix "ev" has no member whose name contains
  // it either) leaves just that one; the header's count is the matched members only, so the
  // two broadcasts never inflate "Agents in #build <n>"; picking one completes it like a
  // member would. Issue #138: @humans leads both of them, with a description of its own, and
  // "hu" (its own prefix, shared with no member and not with "here"/"everyone") matches only it.
  async broadcast_popover() {
    const { w, ws } = await buildRoom(MEMBERS);
    const optionIds = function () {
      return findAll(w.$('mentions'), function (n) { return n.attrs.role === 'option'; }).map(function (n) { return n.id; });
    };
    const descOf = function (name) {
      const row = findAll(w.$('mentions'), function (n) { return n.id === 'men-' + name; })[0];
      return findAll(row, function (n) { return n.classList.contains('m-status'); })[0].textContent;
    };
    const count = function () {
      return findAll(w.$('mentions'), function (n) { return n.classList.contains('muted'); })[0].textContent;
    };

    type(w, 'hi @');
    const all = {
      ids: optionIds(),
      count: count(),
      humansDesc: descOf('humans'),
      hereDesc: descOf('here'),
      everyoneDesc: descOf('everyone'),
    };

    type(w, 'hi @hu');
    const humansFiltered = optionIds();

    type(w, 'hi @ever');
    const filtered = optionIds();

    type(w, 'hi @ev');  // "everyone" (a prefix) ranks before "devin-1" (a substring match)
    key(w.$('input'), 'Enter');
    const picked = { value: w.$('input').value };
    await settle();
    return report(w, ws, { all: all, humansFiltered: humansFiltered, filtered: filtered, picked: picked });
  },

  // An argument-less palette command runs at once (Enter), through the normal send path.
  async palette_runs() {
    const { w, ws } = await buildRoom(MEMBERS);
    w.server.onCommand = function () { return { ok: true, text: 'help text' }; };
    type(w, '/he');
    key(w.$('input'), 'Enter');
    await settle();
    return report(w, ws, { commands: w.server.commands });
  },

  // Issue #109: the red badge, the rooms toggle badge and the title count only go up for a
  // message addressed to you (an @mention or a reply to a message you sent, alice here, per
  // the fake /api/me); other agent chatter in a room you're not viewing gets the quiet dot
  // instead, with no number; opening the room clears both. Issue #138: an agent's own @humans
  // counts too, the same as an @mention.
  async room_badges_addressed_vs_quiet() {
    const w = makeWorld([{ id: 1, name: '#build', created_at: 100 }, { id: 2, name: '#ops', created_at: 101 }]);
    w.location.hash = '#build';
    const ws = await boot(w);

    // #ops isn't active: two agents just talking to each other is quiet, not a red count.
    ws.deliver({ t: 'msg', room: '#ops', msg: chat(10, 'claude-1', 'just telling codex-1 the build is green') });
    await settle();
    const quiet = { tab: tabInfo(w, '#ops'), title: w.document.title, roomsBadge: roomsBadge(w) };

    // An @mention of alice does count, alongside the earlier quiet activity.
    ws.deliver({ t: 'msg', room: '#ops', msg: chat(11, 'claude-1', '@alice please look', { mentions: ['alice'] }) });
    await settle();
    const mentioned = { tab: tabInfo(w, '#ops'), title: w.document.title, roomsBadge: roomsBadge(w) };

    // A reply to alice's own message counts too, even without an @mention in its text.
    ws.deliver({ t: 'msg', room: '#ops', msg: { id: 12, ts: TS + 12, kind: 'chat', from: 'alice', sender_kind: 'human',
                                                  via: 'web', text: 'can you check this build', reply_to: null,
                                                  mentions: [], host: null } });
    ws.deliver({ t: 'msg', room: '#ops', msg: chat(13, 'codex-1', 'on it', { harness: 'codex', reply_to: 12 }) });
    await settle();
    const replied = { tab: tabInfo(w, '#ops'), title: w.document.title, roomsBadge: roomsBadge(w) };

    // An agent's own @humans counts too (issue #138), the same as an @mention.
    ws.deliver({ t: 'msg', room: '#ops',
      msg: chat(15, 'codex-1', '@humans need a decision', { harness: 'codex', mentions: ['humans'] }) });
    await settle();
    const humans = { tab: tabInfo(w, '#ops'), title: w.document.title, roomsBadge: roomsBadge(w) };

    // A reply to someone else's message (not alice's) does not count on its own.
    ws.deliver({ t: 'msg', room: '#ops', msg: { id: 14, ts: TS + 14, kind: 'chat', from: 'claude-1', harness: 'claude',
                                                  sender_kind: 'agent', via: 'mcp', text: 'noted', reply_to: 10,
                                                  mentions: [], host: null } });
    await settle();
    const repliedOther = { tab: tabInfo(w, '#ops'), roomsBadge: roomsBadge(w) };

    // Opening the room clears both the red count and the quiet dot.
    click(findAll(w.$('tabs'), function (n) { return n.tag === 'button' && n.dataset.room === '#ops'; })[0]);
    await settle();
    const opened = { tab: tabInfo(w, '#ops'), title: w.document.title, roomsBadge: roomsBadge(w) };

    return { quiet: quiet, mentioned: mentioned, replied: replied, humans: humans, repliedOther: repliedOther, opened: opened };
  },

  // First run: the Welcome form creates the room named in its field.
  async welcome_create() {
    const w = makeWorld([]);
    const ws = await boot(w);
    const shown = !w.$('empty').classList.contains('hidden');
    w.$('new-room-name').value = 'ops';
    w.$('new-room-name').dispatch('input', {});
    const label = w.$('create-build').textContent;
    const preview = w.$('join-preview').textContent;
    let posted = null;
    const orig = w.server.rooms;
    w.server.onCreate = function (name) { posted = name; };
    w.$('create-form').dispatch('submit', { preventDefault() {} });
    await settle();
    return report(w, ws, { shown: shown, label: label, preview: preview, posted: posted, placeholder: w.$('input').placeholder,
                           rooms: orig.length });
  },
};

async function closeScenario(twoRooms, frameLate) {
  const rooms = [{ id: 1, name: '#build', created_at: 100 }];
  if (twoRooms) rooms.push({ id: 2, name: '#ops', created_at: 101 });
  const w = makeWorld(rooms);
  w.location.hash = '#build';
  const ws = await boot(w);
  let ws2 = ws;
  if (!frameLate) w.server.holdNext.add('POST /api/rooms/build/command');
  w.server.onCommand = function (slug, text) {
    if (slug !== 'build' || text !== '/close') throw new Error('unexpected command ' + slug + ' ' + text);
    w.server.rooms = w.server.rooms.filter(function (r) { return r.name !== '#build'; });
    w.server.closed = 1;
    // the broker sends the rooms frame before it answers the command
    if (frameLate) w.server.holdNext.add('GET /api/rooms');
    ws2.deliver({ t: 'rooms', rooms: w.server.rooms.map(function (r) { return r.name; }) });
    return { ok: true, text: CLOSE_REPLY };
  };
  await send(w, '/close');
  await settle();
  click(findAll(w.$('app-dialog'), function (n) { return n.id === 'app-dialog-action'; })[0]);
  await settle();
  if (frameLate) w.server.release('GET /api/rooms');  // the frame's listing lands last
  else w.server.release('POST /api/rooms/build/command');  // the command's answer lands last
  await settle();
  return report(w, ws, { hash: w.location.hash });
}

async function replacedScenario(newId, newCreated) {
  const w = makeWorld([{ id: 1, name: '#build', created_at: 100 }]);
  w.location.hash = '#build';
  const ws = await boot(w);
  ws.deliver({ t: 'msg', room: '#build', msg: { id: 10, ts: 1790000000, kind: 'chat', from: 'alice',
                                                  sender_kind: 'human', text: 'old history line' } });
  await settle();
  ws.sent.length = 0;
  w.server.rooms = [{ id: newId, name: '#build', created_at: newCreated }];
  ws.deliver({ t: 'rooms', rooms: ['#build'] });
  await settle();
  return report(w, ws);
}

async function main() {
  const name = process.argv[2];
  if (!SCENARIOS[name]) throw new Error('no scenario ' + name);
  const out = await SCENARIOS[name]();
  process.stdout.write(JSON.stringify(out) + '\n');
}

main().catch(function (e) {
  process.stderr.write(String(e && e.stack || e) + '\n');
  process.exit(1);
});
