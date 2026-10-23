// OpenCode V2 status and Stop parks (DESIGN.md §49.12, #513).
// Only an agent-token header authenticates a request. Never print a caught error or response body:
// either can contain credentials. No subprocess, file write, permission reply or permission rule is used.
import { realpath } from 'node:fs/promises';
const ROUND_S = 900;
const TEXT_MAX = 65536;
const ID = /^[A-Za-z0-9_-]{1,128}$/;
const OWN_TOOLS = new Set(['join', 'leave', 'who', 'say', 'read', 'wait', 'pass', 'away', 'status',
  'review', 'claim'].map(name => `switchboard_${name}`));
const IDLE = new Set(['session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted']);
const EVENTS = new Set([...IDLE, 'session.execution.started', 'session.deleted', 'permission.asked']);

function sleep(ms, signal) {
  return new Promise(resolve => {
    if (signal.aborted) return resolve();
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, {once: true});
  });
}

async function readWake(response) {
  if (!response.body) return '';
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > TEXT_MAX) { await reader.cancel(); return ''; }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder().decode(bytes);
}

export async function listen({url, token, sessionID, ctx, signal, canWake,
  request = globalThis.fetch, sleep: pause = sleep, warn = console.warn}) {
  let again = '0', backoff = 1000;
  while (!signal.aborted) {
    let response;
    try {
      response = await request(`${url}/mcp/listen`, {method: 'POST', redirect: 'error',
        signal: AbortSignal.any([signal, AbortSignal.timeout((ROUND_S + 30) * 1000)]),
        headers: {Authorization: `Bearer ${token}`, 'X-Switchboard-Session': sessionID,
          'X-Switchboard-Harness': 'opencode', 'X-Switchboard-Wait': String(ROUND_S),
          'X-Switchboard-Again': again}});
    } catch {
      if (signal.aborted) return;
    }
    if (signal.aborted) return;
    again = '1';
    if (response?.status === 200) {
      let text;
      try {
        text = await readWake(response);
      } catch {
        if (signal.aborted) return;
        await pause(backoff, signal);
        backoff = Math.min(backoff * 2, 60000);
        continue; // headers arrived, but no wake body: a proxy cut the round, so wait again
      }
      try {
        if (signal.aborted || !text || text.includes(token) || !await canWake() || signal.aborted) return;
        // The broker already wrote and bounded the envelope, with peer text as read() stubs.
        // Queue was measured starting an idle 2.1.4 session; it also preserves any concurrent user input.
        await ctx.session.prompt({sessionID, text, delivery: 'queue', metadata: {source: 'switchboard'}});
      } catch { if (!signal.aborted) warn('[switchboard] wake could not be queued'); }
      return; // never retry a prompt: admission may have succeeded even if its answer was lost
    }
    if (response?.status === 408) {
      await response.body?.cancel();
      backoff = 1000;
      await pause(1000, signal);
    } else if (!response || [429, 500, 502, 503, 504].includes(response.status)) {
      await response?.body?.cancel();
      await pause(backoff, signal);
      backoff = Math.min(backoff * 2, 60000);
    } else {
      await response.body?.cancel();
      if (response.status !== 409) warn(`[switchboard] listener stopped (HTTP ${response.status})`);
      return; // 401, 409, 410 or any unrecognized response: nothing to wake for
    }
  }
}

function serverURL(raw) {
  try {
    const url = new URL(raw);
    const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.hostname.endsWith('.localhost');
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) return;
    if (url.username || url.password || url.search || url.hash) return;
    return url.href.replace(/\/$/, '');
  } catch { /* no configured server: inert */ }
}

export async function setup(ctx, {env = process.env, request = globalThis.fetch,
  sleep: pause = sleep, warn = console.warn} = {}) {
  const token = env.SWITCHBOARD_AGENT_TOKEN; // the only source; never stored outside this process
  const url = serverURL(env.SWITCHBOARD_URL);
  if (!url || !token || /[\r\n]/.test(token)) return () => {};
  const sessions = new Map();
  const events = new AbortController();
  const registrations = [];
  let closed = false;

  function record(sessionID) {
    if (!ID.test(sessionID ?? '')) return;
    if (!sessions.has(sessionID)) sessions.set(sessionID, {sessionID, revision: 0, state: 'busy',
      pending: false, permissionsKnown: false, owned: false, chain: Promise.resolve(), listener: null});
    return sessions.get(sessionID);
  }
  function stop(s) { s.listener?.abort(); s.listener = null; }
  async function own(s) {
    try {
      const info = await ctx.session.get({sessionID: s.sessionID});
      const directory = info.location.directory;
      s.owned = directory === ctx.location.directory;
      if (!s.owned) {
        const [sessionDir, pluginDir] = await Promise.all([realpath(directory), realpath(ctx.location.directory)]);
        s.owned = sessionDir === pluginDir;
      }
      if (!s.owned) stop(s);
      return s.owned;
    } catch { stop(s); return false; }
  }
  async function report(s, event, fields = {}) {
    try {
      const r = await request(`${url}/mcp/status`, {method: 'POST', redirect: 'error',
        signal: AbortSignal.timeout(2000), headers: {Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'}, body: JSON.stringify({event, session_id: s.sessionID, ...fields})});
      await r.body?.cancel();
      if (r.status !== 200) warn(`[switchboard] status report refused (HTTP ${r.status})`);
      return r.status === 200;
    } catch { warn('[switchboard] status server unreachable'); return false; }
  }
  async function permissions(s) {
    // Fail closed on an unavailable API, including a restart with no permission.asked in memory.
    const revision = s.revision;
    const pending = await ctx.permission.list({sessionID: s.sessionID});
    if (revision !== s.revision || closed) return false;
    if (!Array.isArray(pending) || pending.length) {
      s.pending = true;
      stop(s);
      await report(s, 'permission.asked');
      return false;
    }
    if (s.pending || !s.permissionsKnown) {
      s.pending = false;
      if (!await report(s, 'permission.cleared')) return false;
      s.permissionsKnown = true;
    }
    return true;
  }
  function enqueue(s, action) {
    s.chain = s.chain.then(action).catch(() => {
      stop(s);
      warn('[switchboard] session state unavailable');
    });
    return s.chain;
  }
  async function handle(s, event, fields, revision) {
    if (event === 'session.deleted') {
      // 2.1.4 emits only the ID, after get() is gone. A tombstone may end a previously
      // verified local session, never create ownership or authorize a wake. A known move clears owned.
      const wasOwned = s.owned;
      const stillOwned = await own(s);
      if (!closed && revision === s.revision && (stillOwned || (wasOwned && s.owned))) {
        await report(s, event);
      }
      sessions.delete(s.sessionID);
      return;
    }
    if (!await own(s) || closed || revision !== s.revision) return;
    if (event === 'permission.asked') { await report(s, event); return; }
    if (!await permissions(s) || closed || revision !== s.revision) return;
    const ok = await report(s, event, fields);
    if (s.state !== 'idle' || !ok || closed || revision !== s.revision) return;
    stop(s);
    const controller = new AbortController();
    s.listener = controller;
    void listen({url, token, sessionID: s.sessionID, ctx, signal: controller.signal, request,
      sleep: pause, warn, canWake: async () => {
        if (closed || s.state !== 'idle' || s.pending || s.revision !== revision || !await own(s)) return false;
        return await permissions(s) && !closed && s.state === 'idle' && !s.pending && s.revision === revision;
      }}).catch(() => { if (!controller.signal.aborted) warn('[switchboard] listener ended'); });
  }
  function event(type, sessionID) {
    const s = record(sessionID);
    if (!s) return;
    s.revision++;
    stop(s); // before any await: a permission event must win a race with an arriving wake
    if (type === 'permission.asked') s.pending = true;
    else s.state = IDLE.has(type) ? 'idle' : type === 'session.deleted' ? 'offline' : 'busy';
    const revision = s.revision;
    return enqueue(s, () => handle(s, type, {}, revision));
  }
  async function cleanup() {
    if (closed) return;
    closed = true;
    events.abort();
    for (const s of sessions.values()) { s.revision++; stop(s); }
    await Promise.all([...sessions.values()].map(s => enqueue(s, async () => {
      if (await own(s)) await report(s, 'location.shutdown');
    })));
    for (const r of registrations) await r.dispose();
    sessions.clear();
  }
  for (const hook of ['execute.before', 'execute.after']) {
    registrations.push(await ctx.tool.hook(hook, async e => {
      const s = record(e.sessionID);
      if (!s || closed) return;
      s.revision++;
      s.state = 'busy';
      stop(s);
      const fields = {};
      if (ID.test(e.tool ?? '')) fields.tool_name = e.tool;
      if (ID.test(e.id ?? '')) fields.tool_use_id = e.id;
      if (hook === 'execute.after' && e.status === 'completed' && OWN_TOOLS.has(e.tool)) {
        // Code Mode emits this same inner hook. Never scan execute's aggregate or another tool's result.
        const texts = (e.result?.content ?? []).filter(c => c.type === 'text' && typeof c.text === 'string')
          .map(c => c.text.slice(0, 256 * 1024)).join(' ').slice(0, 256 * 1024);
        fields.tool_response = [...new Set(texts.match(/yk:b\d{1,12}\.[0-9a-f]{8}/g) ?? [])].slice(0, 20).join(' ');
      }
      const type = hook === 'execute.before' ? 'tool.execute.before'
        : e.status === 'completed' ? 'tool.execute.after' : 'tool.execute.failed';
      const revision = s.revision;
      await enqueue(s, () => handle(s, type, fields, revision));
    }));
  }
  void (async () => {
    try {
      for await (const e of ctx.event.subscribe({signal: events.signal})) {
        if (closed) break;
        if (e.type === 'location.shutdown' && e.data.directory === ctx.location.directory) {
          await cleanup(); break;
        }
        if (EVENTS.has(e.type)) void event(e.type, e.data.sessionID);
      }
    } catch { if (!closed) { warn('[switchboard] event stream ended'); await cleanup(); } }
  })();
  return cleanup;
}
