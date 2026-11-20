// Runs diagram.js (window.SBDiagram) in node with a bare window, for tests/unit/test_web_diagram.py.
// Reads a JSON list of diagram sources on stdin; prints withoutHandlers of each (issue #917).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const window = { addEventListener() {}, matchMedia() { return { matches: false, addEventListener() {} }; } };
const ctx = vm.createContext({ window, document: { documentElement: { dataset: {} } } });
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'src/switchboard/web/static/diagram.js'), 'utf8'), ctx);
const sources = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(sources.map((s) => window.SBDiagram.withoutHandlers(s))));
