// Runs diagram.js (window.SBDiagram) in node with a bare window, for tests/unit/test_web_diagram.py.
// Reads a JSON list of diagram sources on stdin; prints withoutHandlers of each (issue #917), or
// with --count that and how much indexOf read.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const window = { addEventListener() {}, matchMedia() { return { matches: false, addEventListener() {} }; } };
const ctx = vm.createContext({ window, document: { documentElement: { dataset: {} } } });
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'src/switchboard/web/static/diagram.js'), 'utf8'), ctx);
const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
if (process.argv[2] === '--count') {
  // How many characters String.prototype.indexOf reads inside diagram.js, for a test that the
  // clean-up is one pass over a line, not one per attribute (#918's review): a count, not a clock.
  const proto = vm.runInContext('String.prototype', ctx);
  const indexOf = proto.indexOf;
  let read = 0;
  proto.indexOf = function (needle, from) {
    const start = Math.min(Math.max(Math.trunc(from) || 0, 0), this.length);
    const at = indexOf.call(this, needle, start);
    read += (at < 0 ? this.length : at + 1) - start;
    return at;
  };
  const out = sources.map((s) => window.SBDiagram.withoutHandlers(s));
  process.stdout.write(JSON.stringify({ out, read }));
} else {
  process.stdout.write(JSON.stringify(sources.map((s) => window.SBDiagram.withoutHandlers(s))));
}
