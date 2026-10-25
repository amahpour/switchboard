// OpenCode V2 plugin contract (#513, DESIGN.md §49.12). All scheduling is event-driven.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, symlink, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { listedSessions, listen, setup } from '../plugins/opencode/listener.mjs';

const token = 'sbat_test-token-canary';
const sessionID = 'ses_00000000000000000000000001';
const wake = '[switchboard] from peer: call read("#build") [yk:b1.00000000]';
const ack = '0123456789abcdef0123456789abcdef';
const wakeResponse = text => new Response(text, {headers: {'X-Switchboard-Batch': '1', 'X-Switchboard-Ack': ack}});
const turn = () => new Promise(resolve => setImmediate(resolve));
async function until(check) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    if (check()) return;
    await turn();
  }
  assert.fail('expected event did not arrive');
}

// Each terminal response stops once, while timeouts and connection failures re-arm with bounded backoff.
test('listener rounds: 200, 408, 409, 401, unreachable; no credentials in output or argv', async () => {
  for (const terminal of [200, 409, 401]) {
    const requests = [], prompts = [], logs = [], delays = [];
    const responses = [new Error(token), new Error(token), new Response('', {status: 408}),
      terminal === 200 ? wakeResponse(wake) : new Response(token, {status: terminal})];
    await listen({url: 'https://example.com', token, sessionID,
      signal: new AbortController().signal, canWake: async () => true,
      ctx: {session: {prompt: async p => prompts.push(p)}},
      request: async (url, options) => {
        requests.push({url, options});
        const next = responses.shift();
        if (next instanceof Error) throw next;
        assert.ok(next, 'must stop after a terminal status');
        return next;
      }, sleep: async ms => delays.push(ms), warn: text => logs.push(text)});
    assert.equal(requests.length, 4);
    assert.deepEqual(delays, [1000, 2000, 1000]);
    assert.deepEqual(requests.map(r => r.options.headers['X-Switchboard-Again']), ['0','1','1','1']);
    for (const r of requests) {
      assert.equal(r.options.headers.Authorization, `Bearer ${token}`);
      assert.equal(r.options.headers['X-Switchboard-Harness'], 'opencode');
      assert.equal(r.options.redirect, 'error');
      assert.ok(!r.url.includes(token));
    }
    assert.deepEqual(prompts, terminal === 200 ? [{sessionID, text: wake, delivery: 'queue',
      metadata: {source: 'switchboard'}}] : []);
    assert.ok(!JSON.stringify(logs).includes(token));
    assert.ok(!process.argv.join(' ').includes(token));
  }
});

// Even a response already received cannot wake an approval-blocked, moved or deleted session.
test('listener rechecks eligibility after 200 and rejects oversized or credential-bearing text', async () => {
  for (const [text, allowed] of [[wake, false], [token, true], ['x'.repeat(65537), true], ['', true]]) {
    const prompts = [];
    await listen({url: 'https://example.com', token, sessionID, signal: new AbortController().signal,
      canWake: async () => allowed, ctx: {session: {prompt: async p => prompts.push(p)}},
      request: async () => wakeResponse(text), sleep: async () => {}, warn: () => {}});
    assert.deepEqual(prompts, []);
  }
});

// A gateway may send 200 headers and then drop the body; no turn was admitted, so this is still a retryable outage.
test('a broken 200 body retries without losing the listener or logging credentials', async () => {
  const prompts = [], logs = [], delays = [];
  let requests = 0;
  const broken = new ReadableStream({start(controller) {controller.error(new Error(token));}});
  await listen({url: 'https://example.com', token, sessionID, signal: new AbortController().signal,
    canWake: async () => true, ctx: {session: {prompt: async p => prompts.push(p)}},
    request: async () => ++requests === 1 ? wakeResponse(broken) : wakeResponse(wake),
    sleep: async ms => delays.push(ms), warn: text => logs.push(text)});
  assert.equal(requests, 2);
  assert.deepEqual(delays, [1000]);
  assert.equal(prompts.length, 1);
  assert.equal(prompts[0].text, wake);
  assert.ok(!JSON.stringify(logs).includes(token));
});

function world() {
  const queue = [], requests = [], prompts = [], hooks = new Map(), logs = [];
  let notify, closed = false;
  const state = {directory: '/project', pending: [], outcome: 'succeeded'};
  const ctx = {
    location: {directory: '/project'},
    session: {get: async () => ({location: {directory: state.directory}, outcome: state.outcome}),
      prompt: async p => prompts.push(p)},
    permission: {list: async () => state.pending},
    tool: {hook: async (name, fn) => {hooks.set(name, fn); return {dispose: async () => {}};}},
    event: {subscribe: async function* ({signal}) {
      signal.addEventListener('abort', () => {closed = true; notify?.();}, {once: true});
      while (!closed) {
        if (!queue.length) await new Promise(resolve => {notify = resolve;});
        if (queue.length) yield queue.shift();
      }
    }},
  };
  const request = async (url, options) => {
    const row = {url, options};
    requests.push(row);
    if (url.endsWith('/mcp/status')) return new Response('{}');
    return new Promise((resolve, reject) => {
      row.resolve = resolve;
      options.signal.addEventListener('abort', () => reject(new Error(token)), {once: true});
    });
  };
  return {ctx, state, requests, prompts, hooks, logs,
    emit: type => {queue.push(typeof type === 'string' ? {type, data: {sessionID}} : type); notify?.();},
    start: (extra = {}) => setup(ctx, {env: {SWITCHBOARD_URL: 'https://example.com', SWITCHBOARD_AGENT_TOKEN: token, ...extra},
      request, sleep: async () => {}, warn: text => logs.push(text)})};
}

// The documented desktop URL is a .localhost name; plain HTTP to a remote host remains refused.
test('desktop localhost URL connects, remote HTTP is inert', async () => {
  for (const url of ['http://switchboard.localhost:1234', 'http://example.com']) {
    const w = world();
    const cleanup = await w.start({SWITCHBOARD_URL: url});
    w.emit('session.execution.succeeded');
    if (url.includes('.localhost')) await until(() => w.requests.some(r => r.resolve));
    else { await turn(); assert.deepEqual(w.requests, []); }
    await cleanup();
  }
});

// macOS presents the same temp project via /var and /private/var; path spelling must not disable its listener.
test('location ownership compares canonical directories', async () => {
  const root = await mkdtemp(join(tmpdir(), 'switchboard-plugin-'));
  const alias = root + '-alias';
  await symlink(root, alias);
  const w = world();
  w.ctx.location.directory = alias;
  w.state.directory = root;
  const cleanup = await w.start();
  try {
    w.emit('session.execution.succeeded');
    await until(() => w.requests.some(r => r.resolve));
  } finally {
    await cleanup();
    await rm(alias);
    await rm(root, {recursive: true});
  }
});

// A location's plugin sees the service-wide stream, but must send nothing for a different location.
test('foreign sessions do not report status, park or prompt; moves are rechecked', async () => {
  const w = world();
  const cleanup = await w.start();
  w.state.directory = '/other';
  w.emit('session.execution.succeeded');
  for (let i = 0; i < 5; i++) await turn();
  assert.deepEqual(w.requests, []);
  w.state.directory = '/project';
  w.emit('session.execution.succeeded');
  await until(() => w.requests.some(r => r.url.endsWith('/mcp/listen')));
  w.state.directory = '/other';
  w.requests.find(r => r.resolve).resolve(wakeResponse(wake));
  for (let i = 0; i < 5; i++) await turn();
  assert.deepEqual(w.prompts, []);
  await cleanup();
});

// Approval events abort a park immediately; only a read-only empty permission.list clears the hold.
test('approval blocks idle status and a racing wake until permissions are empty', async () => {
  const w = world();
  const cleanup = await w.start();
  w.emit('session.execution.succeeded');
  await until(() => w.requests.some(r => r.resolve));
  const parked = w.requests.find(r => r.resolve);
  w.state.pending = [{id: 'permission-1'}];
  w.emit('permission.asked');
  await until(() => w.requests.some(r => r.options.body?.includes('permission.asked')));
  assert.ok(parked.options.signal.aborted);
  parked.resolve(wakeResponse(wake));
  w.emit('session.execution.succeeded');
  await turn();
  assert.deepEqual(w.prompts, []);
  assert.equal(w.requests.filter(r => r.resolve).length, 1);
  w.state.pending = [];
  w.emit('session.execution.succeeded');
  await until(() => w.requests.filter(r => r.resolve).length === 2);
  assert.ok(w.requests.some(r => r.options.body?.includes('permission.cleared')));
  await cleanup();
  assert.ok(!JSON.stringify(w.logs).includes(token));
});

// Only the inner MCP hook's batch tokens leave OpenCode, even when execute contains secrets and duplicate results.
test('tool reports send only identifiers and switchboard batch tokens, with no context injection', async () => {
  const w = world();
  const cleanup = await w.start();
  const input = {secret: token};
  for (const tool of ['execute', 'bash', 'switchboard_read']) {
    await w.hooks.get('execute.after')({sessionID, tool, id: 'call-1', input, status: 'completed',
      result: {content: [{type: 'text', text: `secret=${token} yk:b1.00000000`}]}});
  }
  await until(() => w.requests.filter(r => r.options.body?.includes('tool.execute.after')).length === 3);
  const bodies = w.requests.filter(r => r.options.body).map(r => JSON.parse(r.options.body));
  assert.ok(bodies.length >= 3);
  assert.ok(!JSON.stringify(bodies).includes(token));
  assert.equal(bodies.filter(b => b.tool_response).length, 1);
  assert.equal(bodies.find(b => b.tool_response).tool_response, 'yk:b1.00000000');
  assert.deepEqual(w.prompts, []);
  await cleanup();
});

// A silent broker must never hold up a tool; releasing its first report must flush both hooks in order.
test('tool hooks return within 100 ms while status reports wait in session order', async () => {
  const w = world(), reports = [];
  let release;
  const cleanup = await setup(w.ctx, {env: {SWITCHBOARD_URL: 'https://example.com',
    SWITCHBOARD_AGENT_TOKEN: token}, warn: text => w.logs.push(text),
    request: async (url, options) => {
      const body = JSON.parse(options.body);
      reports.push(body.event);
      if (reports.length === 1) await new Promise(resolve => {release = resolve;});
      return new Response('{}');
    }});
  try {
    for (const hook of ['execute.before', 'execute.after']) {
      const start = performance.now();
      let timer;
      try {
        await Promise.race([
          w.hooks.get(hook)({sessionID, tool: 'bash', id: 'call-1', status: 'completed', result: {content: []}}),
          new Promise((_, reject) => {timer = setTimeout(() => reject(new Error(`${hook} waited on broker`)), 100);}),
        ]);
      } finally {clearTimeout(timer);}
      assert.ok(performance.now() - start < 100, `${hook} blocked`);
    }
    await until(() => release);
    assert.deepEqual(reports, ['permission.cleared']);
    release();
    await until(() => reports.length === 3);
    assert.deepEqual(reports, ['permission.cleared', 'tool.execute.before', 'tool.execute.after']);
  } finally { release?.(); await cleanup(); }
});

// A user or tool becoming busy during a 200 body does not discard its human messages: queue is safe concurrently.
test('a session becoming busy mid-body still queues the exact wake and reports admission', async () => {
  const w = world();
  const cleanup = await w.start();
  try {
    w.emit('session.execution.succeeded');
    await until(() => w.requests.some(r => r.resolve));
    const parked = w.requests.find(r => r.resolve);
    let body;
    const stream = new ReadableStream({start(controller) {body = controller;}});
    parked.resolve(wakeResponse(stream));
    await until(() => stream.locked);
    w.emit('session.execution.started');
    await w.hooks.get('execute.before')({sessionID, tool: 'bash', id: 'call-1'});
    assert.equal(parked.options.signal.aborted, false);
    body.enqueue(new TextEncoder().encode(wake)); body.close();
    await until(() => w.prompts.length === 1);
    await until(() => w.requests.some(r => r.options.body?.includes('wake.queued')));
    assert.deepEqual(w.prompts, [{sessionID, text: wake, delivery: 'queue', metadata: {source: 'switchboard'}}]);
    const receipt = JSON.parse(w.requests.find(r => r.options.body?.includes('wake.queued')).options.body);
    assert.deepEqual(receipt, {event: 'wake.queued', session_id: sessionID, batch_id: 1, ack});
    assert.ok(!w.requests.some(r => r.options.body?.includes('wake.dropped')));
  } finally {await cleanup();}
});

// Only permission, lost ownership or shutdown may discard a received body, and each reports the exact offer dropped.
test('a permission, move or shutdown during a 200 body drops rather than acknowledges the wake', async () => {
  for (const cause of ['permission', 'move', 'shutdown']) {
    const w = world();
    const cleanup = await w.start();
    try {
      w.emit('session.execution.succeeded');
      await until(() => w.requests.some(r => r.resolve));
      const parked = w.requests.find(r => r.resolve);
      let body;
      const stream = new ReadableStream({start(controller) {body = controller;}});
      parked.resolve(wakeResponse(stream));
      await until(() => stream.locked);
      if (cause === 'permission') {
        w.state.pending = [{id: 'permission-1'}]; w.emit('permission.asked');
        await until(() => parked.options.signal.aborted);
      } else if (cause === 'move') {
        w.state.directory = '/other';
      } else {void cleanup(); await until(() => parked.options.signal.aborted);}
      if (!parked.options.signal.aborted) {body.enqueue(new TextEncoder().encode(wake)); body.close();}
      await until(() => w.requests.some(r => r.options.body?.includes('wake.dropped')));
      assert.deepEqual(w.prompts, []);
      assert.ok(!w.requests.some(r => r.options.body?.includes('wake.queued')));
      assert.ok(!JSON.stringify(w.logs).includes(token));
    } finally {await cleanup();}
  }
});

// Cleanup stops all in-flight work and reports own sessions offline without leaving a retry timer alive.
test('shutdown cancels listener and reports offline', async () => {
  const w = world();
  const cleanup = await w.start();
  w.emit('session.execution.succeeded');
  await until(() => w.requests.some(r => r.resolve));
  const parked = w.requests.find(r => r.resolve);
  await cleanup();
  assert.ok(parked.options.signal.aborted);
  assert.ok(w.requests.some(r => r.options.body?.includes('location.shutdown')));
  assert.deepEqual(w.prompts, []);
});

// The service sends location at the top level, with empty data. Another location must not stop this one;
// the canonical own event must cancel its park, report offline and dispose both hook registrations.
test('recorded location.shutdown travels through the event stream and matches canonical ownership', async () => {
  const fixture = JSON.parse(await readFile(new URL('./fixtures/mcp_http/opencode_plugin_events.json', import.meta.url)));
  const recorded = fixture.events.find(e => e.type === 'location.shutdown');
  assert.deepEqual(recorded.data, {});
  assert.equal(typeof recorded.location.directory, 'string');
  const root = await mkdtemp(join(tmpdir(), 'switchboard-shutdown-'));
  const alias = root + '-alias';
  await symlink(root, alias);
  const w = world(), disposed = [];
  w.ctx.location.directory = alias;
  w.state.directory = root;
  w.ctx.tool.hook = async (name, fn) => {
    w.hooks.set(name, fn); return {dispose: async () => {disposed.push(name);}};
  };
  const cleanup = await w.start();
  try {
    w.emit('session.execution.succeeded');
    await until(() => w.requests.some(r => r.resolve));
    w.emit({...recorded, location: {directory: root + '-foreign'}});
    w.emit('session.execution.succeeded'); // processed after the foreign event: this location is still active
    await until(() => w.requests.filter(r => r.resolve).length === 2);
    assert.ok(!w.requests.some(r => r.options.body?.includes('location.shutdown')));
    const parked = w.requests.filter(r => r.resolve)[1];
    w.emit({...recorded, location: {directory: root}});
    await until(() => disposed.length === 2);
    assert.ok(parked.options.signal.aborted);
    assert.equal(w.requests.filter(r => r.options.body?.includes('location.shutdown')).length, 1);
    assert.deepEqual(w.prompts, []);
    const before = w.requests.length;
    await w.hooks.get('execute.before')({sessionID, tool: 'bash', id: 'call-1'});
    assert.equal(w.requests.length, before);
  } finally {await cleanup(); await rm(alias); await rm(root, {recursive: true});}
});

// 2.1.4 deletes session.get data before emitting the id-only tombstone; only a previously verified session can go offline.
test('deleted session reports offline using its last verified ownership, never wakes', async () => {
  const w = world();
  const cleanup = await w.start();
  w.emit('session.execution.succeeded');
  await until(() => w.requests.some(r => r.resolve));
  const parked = w.requests.find(r => r.resolve);
  w.ctx.session.get = async () => { throw new Error('session removed'); };
  w.emit('session.deleted');
  await until(() => w.requests.some(r => r.options.body?.includes('session.deleted')));
  assert.ok(parked.options.signal.aborted);
  assert.deepEqual(w.prompts, []);
  await cleanup();
});

// ---- #520: a session that was idle before the plugin (re)loaded
// A world where several sessions exist when the plugin starts. ``dirs`` and ``permissions`` are per session;
// ``list`` is what ctx.session.list answers (a function, so a test can hold or change it).
function restarted(rows, {dirs = {}, permissions = {}} = {}) {
  const w = world(), gets = [];
  w.ctx.session.get = async ({sessionID: id}) => {
    gets.push(id);
    return {location: {directory: dirs[id] ?? '/project'}};
  };
  w.ctx.permission.list = async ({sessionID: id}) => permissions[id] ?? [];
  w.listCalls = [];
  w.ctx.session.list = async input => {
    w.listCalls.push(input);
    return typeof rows === 'function' ? rows() : rows;
  };
  w.gets = gets;
  w.sent = id => w.requests.filter(r => r.options.headers['X-Switchboard-Session'] === id
    || r.options.body?.includes(`"${id}"`));
  w.parked = id => w.requests.filter(r => r.url.endsWith('/mcp/listen')
    && r.options.headers['X-Switchboard-Session'] === id);
  w.events = id => w.requests.filter(r => r.options.body?.includes(`"${id}"`))
    .map(r => JSON.parse(r.options.body).event);
  return w;
}
const row = (id, extra = {}) => ({id, ...extra});
const settle = async () => { for (let i = 0; i < 20; i++) await turn(); };

// The point of #520: after a restart an idle session is reported idle and listened for without a new turn,
// and a mention then wakes it; a foreign location, a pending permission and a stale session are not.
test('startup lists this location\'s recent sessions and listens for the idle ones', async () => {
  const day = 24 * 3600 * 1000, now = Date.now();
  const w = restarted({items: [
    row('ses_idle', {time: {updated: now - 1000}}),
    row('ses_foreign'),
    row('ses_asking'),
    row('ses_stale', {time: {updated: now - 3 * day}}),
    row('ses_nested'),
  ]}, {dirs: {ses_foreign: '/other'}, permissions: {ses_asking: [{id: 'permission-1'}]}});
  const cleanup = await w.start();
  try {
    await until(() => w.parked('ses_idle').length === 1 && w.parked('ses_nested').length === 1);
    await settle();
    assert.deepEqual(w.listCalls, [{directory: '/project', limit: 20}]);
    assert.deepEqual(w.events('ses_idle'), ['permission.cleared', 'session.execution.succeeded']);
    // a foreign location sends nothing, a stale session isn't even asked about
    assert.deepEqual(w.sent('ses_foreign'), []);
    assert.deepEqual(w.sent('ses_stale'), []);
    assert.ok(!w.gets.includes('ses_stale'));
    // a pending permission is reported, and is never listened for
    assert.deepEqual(w.events('ses_asking'), ['permission.asked']);
    assert.deepEqual(w.parked('ses_asking'), []);
    // the broker's answer to the idle session's round wakes it
    w.parked('ses_idle')[0].resolve(wakeResponse(wake));
    await until(() => w.prompts.length === 1);
    assert.deepEqual(w.prompts, [{sessionID: 'ses_idle', text: wake, delivery: 'queue',
      metadata: {source: 'switchboard'}}]);
    assert.ok(!JSON.stringify(w.logs).includes(token));
  } finally {await cleanup();}
});

// A bare array is accepted too, and the cap holds however long the list is.
test('startup looks at no more than 20 sessions, in the order listed, and takes a bare array', async () => {
  const w = restarted(Array.from({length: 35}, (_, i) => row(`ses_${String(i).padStart(2, '0')}`)));
  const cleanup = await w.start();
  try {
    await until(() => w.requests.filter(r => r.url.endsWith('/mcp/listen')).length === 20);
    await settle();
    assert.equal(new Set(w.gets).size, 20);
    assert.ok(w.gets.includes('ses_19') && !w.gets.includes('ses_20'));
  } finally {await cleanup();}
});

// Whatever happened since the plugin started is newer than a list: an event or a tool call that named the session
// first decides its state, so a session that has just begun a turn isn't called idle.
test('a session an event already named is not overwritten by the startup list', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const w = restarted(async () => {await gate; return {items: [row('ses_busy'), row('ses_idle')]};});
  const cleanup = await w.start();
  try {
    w.emit({type: 'session.execution.started', data: {sessionID: 'ses_busy'}});
    await until(() => w.events('ses_busy').includes('session.execution.started'));
    release();
    await until(() => w.parked('ses_idle').length === 1);
    await settle();
    assert.ok(!w.events('ses_busy').includes('session.execution.succeeded'));
    assert.deepEqual(w.parked('ses_busy'), []);
  } finally {release(); await cleanup();}
});

// A tool must never wait on the startup list, and a plugin that is closed while it is slow does nothing after.
test('a slow or never-answered list holds up neither setup nor tool hooks, and cleanup ends it', async () => {
  let release;
  const w = restarted(() => new Promise(resolve => {release = () => resolve({items: [row('ses_idle')]});}));
  const started = performance.now();
  const cleanup = await w.start();
  assert.ok(performance.now() - started < 100, 'setup waited on the list');
  const t = performance.now();
  await w.hooks.get('execute.before')({sessionID, tool: 'bash', id: 'call-1'});
  assert.ok(performance.now() - t < 100, 'a tool hook waited on the list');
  await cleanup();
  const before = w.requests.length;
  release();
  await settle();
  assert.equal(w.requests.length, before, 'sent something after cleanup');
  assert.ok(!w.gets.includes('ses_idle'), 'looked at a listed session after cleanup');
});

// Fail closed: a list this plugin can't read, or that throws, or that isn't there, starts nothing, says so once, and
// leaves the event path as it was.
test('an unavailable or unrecognized list starts nothing and logs one line without credentials', async () => {
  for (const rows of [() => {throw new Error(token);}, {unexpected: [row('ses_idle')]}, 'ses_idle', null, undefined]) {
    const w = restarted(rows);
    const cleanup = await w.start();
    try {
      await settle();
      assert.deepEqual(w.requests, []);
      assert.equal(w.logs.filter(l => l.includes('could not list sessions')).length, 1);
      assert.ok(!JSON.stringify(w.logs).includes(token));
      w.emit('session.execution.succeeded');
      await until(() => w.requests.some(r => r.resolve));
    } finally {await cleanup();}
  }
  const w = world(); // OpenCode without ctx.session.list at all (the other tests' world has none)
  const cleanup = await w.start();
  await settle();
  assert.deepEqual(w.requests, []);
  assert.equal(w.logs.filter(l => l.includes('could not list sessions')).length, 1);
  await cleanup();
});

// The parsing on its own: ids are bounded and unique, odd rows are skipped, times may be seconds or milliseconds.
test('listedSessions reads ids and recency defensively', () => {
  const now = Date.UTC(2026, 9, 25);
  const day = 24 * 3600;
  const page = {data: [row('ses_a', {time: {updated: now / 1000 - 10}}), row('ses_a'), {sessionID: 'ses_b'},
    row('bad id'), row(7), null, 'x', row('ses_old', {time: {updated: now / 1000 - 2 * day}}),
    row('ses_ms', {time: {created: now - 5}})]};
  assert.deepEqual(listedSessions(page, now), ['ses_a', 'ses_b', 'ses_ms']);
  assert.equal(listedSessions({}, now), undefined);
  assert.equal(listedSessions({items: 'nope'}, now), undefined);
  assert.deepEqual(listedSessions([], now), []);
});
