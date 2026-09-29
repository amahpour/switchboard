// Runs the web UI's app.js in node against a tiny fake DOM, a fake fetch and a fake
// WebSocket, and prints what one scenario left on screen and on the socket as JSON
// (tests/unit/test_web_app_behavior.py). No browser, no network, no timers waited on:
// settle() only lets queued promise callbacks and setImmediate turns run.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'src', 'switchboard', 'web', 'static', 'app.js');

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
  }
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
  requestSubmit() { this.dispatch('submit', { preventDefault() {} }); }
}

// ------------------------------------------------------------------ fakes
function makeWorld(rooms) {
  const byId = new Map();
  const document = {
    title: '',
    activeElement: null,
    listeners: {},
    getElementById(id) {
      if (!byId.has(id)) {
        const e = new El('div');
        e.id = id;
        // as index.html starts them
        if (['empty', 'closed-panel', 'remotes-panel', 'closed-rooms'].includes(id)) e.classList.add('hidden');
        byId.set(id, e);
      }
      return byId.get(id);
    },
    createElement(tag) { return new El(tag); },
    createDocumentFragment() { return new El('#fragment'); },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  };

  const server = {
    rooms: rooms,   // [{id, name, created_at}]
    closed: 0,
    fail: new Set(),  // 'GET /api/rooms': answer 500 once
    holdNext: new Set(),  // hold the next request to this key until release(key)
    held: [],
    onCommand: null,  // (slug, text) -> body; may change state and deliver frames first
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
    if (method === 'GET' && url === '/api/remotes') return function () { return [200, { remotes: [] }]; };
    if (method === 'GET' && url === '/api/closed-rooms') return function () { return [200, { rooms: [] }]; };
    const m = /^\/api\/rooms\/([^/]+)\/command$/.exec(url);
    if (method === 'POST' && m) {
      const got = server.onCommand(decodeURIComponent(m[1]), body.text);  // side effects now
      return function () { return [200, got]; };
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
    protocol: 'http:', host: '127.0.0.1:7419', pathname: '/', hash: '',
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
    encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
  };
  ctx.window = ctx;
  ctx.window.confirm = function () { return true; };
  ctx.window.alert = function (m) { throw new Error('alert: ' + m); };
  ctx.window.prompt = function () { return null; };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(APP, 'utf8'), ctx, { filename: 'app.js' });

  return { document: document, server: server, sockets: sockets, location: location, $: document.getElementById.bind(document) };
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
  const tabs = w.$('tabs').children.map(function (c) { return c.own; });
  return Object.assign({
    log: lines(w),
    logHidden: w.$('log').classList.contains('hidden'),
    tabs: tabs,
    title: w.$('title').textContent,
    hellos: hellos(ws),
  }, extra || {});
}

async function send(w, text) {
  w.$('input').value = text;
  w.$('composer').dispatch('submit', { preventDefault() {} });
}

const CLOSE_REPLY = 'closed #build: 2 agent(s) removed (1 on fpga-pi); history kept.' +
  ' The name is free again; reopen this room from Closed rooms in the web UI';

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
