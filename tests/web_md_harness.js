// Runs the web UI's Markdown renderer (src/switchboard/web/static/md.js) in node against a tiny
// fake DOM and prints what it built, as JSON (tests/unit/test_web_markdown.py).
//
// stdin: a JSON list of cases, each one of
//   {text, mentions, localHost}          -> SBMarkdown.render(): the fragment as a tree, plus ms
//   ... plus diagrams: true              -> with a stand-in window.SBDiagram (diagram.js) present;
//       and click: true                  -> then click the first "Show diagram" button, and report
//                                           what reached SBDiagram.toggle (`toggled`)
//   {fn: 'firstLine', text, max}         -> SBMarkdown.firstLine()
//   {fn: 'safeUrl', raw, localHost}      -> SBMarkdown.safeUrl()
// stdout: a JSON list of results in the same order. Rendered nodes are
//   {tag, cls, text, href, rel, target, title, attrs, props, children}; text nodes are
//   {tag: '#text', text}. `href` is reported only when something set it, so the test can prove
//   that no element other than a.md-link ever gets one.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MD = path.join(__dirname, '..', 'src', 'switchboard', 'web', 'static', 'md.js');

// The fake El from web_app_harness.js, plus text nodes and property passthrough.
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

class Text {
  constructor(data) { this.tag = '#text'; this.data = String(data); }
  get textContent() { return this.data; }
}

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.classList = new ClassList();
    this.attrs = {};
    this.listeners = {};
    this.own = '';
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
      else this.children.push(typeof x === 'string' ? new Text(x) : x);
    }
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
}

const PROPS = ['start', 'tabIndex', 'type', 'referrerPolicy'];

function dump(node) {
  if (node instanceof Text) return { tag: '#text', text: node.data };
  const out = {
    tag: node.tag,
    cls: node.className,
    text: node.textContent,
    attrs: node.attrs,
    props: {},
    children: [],
  };
  const own = function (k) { return Object.prototype.hasOwnProperty.call(node, k); };
  for (const k of ['href', 'rel', 'target', 'title']) if (own(k)) out[k] = node[k];
  for (const k of PROPS) if (own(k)) out.props[k] = node[k];
  if (node.own) out.children.push({ tag: '#text', text: node.own });
  for (const c of node.children) out.children.push(dump(c));
  return out;
}

const document = {
  createElement(tag) { return new El(tag); },
  createDocumentFragment() { return new El('#fragment'); },
  createTextNode(t) { return new Text(t); },
};
const ctx = { document: document, URL: URL, console: console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(MD, 'utf8'), ctx, { filename: 'md.js' });
const MDAPI = ctx.window.SBMarkdown;

function find(node, cls) {
  if (node instanceof El && node.classList.contains(cls)) return node;
  for (const c of (node.children || [])) {
    const hit = c instanceof El ? find(c, cls) : null;
    if (hit) return hit;
  }
  return null;
}

function run(c) {
  if (c.fn === 'firstLine') return { value: MDAPI.firstLine(c.text, c.max) };
  if (c.fn === 'safeUrl') return JSON.parse(JSON.stringify(MDAPI.safeUrl(c.raw, c.localHost)));
  const toggled = [];
  ctx.SBDiagram = c.diagrams ? {
    toggle: function (box, source, button) { toggled.push({ box: box.className, source: source, button: button.textContent }); },
  } : undefined;
  const t0 = process.hrtime.bigint();
  const frag = MDAPI.render(c.text, { mentions: c.mentions || [], localHost: c.localHost || '' });
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  if (c.click) {
    const btn = find(frag, 'md-show-diagram');
    if (btn) for (const f of (btn.listeners.click || [])) f();
  }
  const tree = dump(frag);
  tree.ms = ms;
  if (c.click) tree.toggled = toggled;
  return tree;
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', function (d) { input += d; });
process.stdin.on('end', function () {
  try {
    const cases = JSON.parse(input);
    process.stdout.write(JSON.stringify(cases.map(run)) + '\n');
  } catch (e) {
    process.stderr.write(String(e && e.stack || e) + '\n');
    process.exit(1);
  }
});
