// The frame boundary's copy of board.clean_element (DESIGN §56.8). No HTML, URLs or executable fields.
/* global module */
'use strict';
(function (root) {
  const id = v => typeof v === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(v);
  const num = v => typeof v === 'number' && Number.isFinite(v);
  const clamp = (v, lo, hi) => num(v) ? Math.max(lo, Math.min(hi, v)) : null;
  const integer = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi ? v : null;
  const choice = (v, values) => typeof v === 'string' && values.includes(v) ? v : null;
  const text = v => Array.from(v.slice(0, 2000).normalize('NFKC').replace(/[\p{Cc}\p{Cf}\p{Co}\p{Cs}\u2028\u2029]/gu, c => c === '\n' || c === '\t' ? c : '')).slice(0, 200).join('');
  const types = ['rectangle', 'ellipse', 'diamond', 'arrow', 'text', 'frame', 'line', 'freedraw'];
  function clean(raw) {
    if (!raw || typeof raw !== 'object' || !id(raw.id) || !types.includes(raw.type)) return null;
    const x = clamp(raw.x, -20000, 20000), y = clamp(raw.y, -20000, 20000);
    const width = clamp(raw.width, 0, 10000), height = clamp(raw.height, 0, 10000);
    const version = integer(raw.version, 0, 1000000000), versionNonce = integer(raw.versionNonce, -(2 ** 31), 2 ** 31 - 1);
    if ([x, y, width, height, version, versionNonce].includes(null)) return null;
    if (raw.isDeleted !== undefined && typeof raw.isDeleted !== 'boolean') return null;
    const e = { id: raw.id, type: raw.type, x, y, width, height, version, versionNonce, isDeleted: raw.isDeleted || false, link: null };
    const range = { angle: [-100, 100], strokeWidth: [0, 20], roughness: [0, 3], lineHeight: [0.5, 3] };
    e.angle = clamp(raw.angle === undefined ? 0 : raw.angle, -100, 100);
    if (e.angle === null) delete e.angle;
    for (const [k, [lo, hi]] of Object.entries(range)) {
      if (k in raw && clamp(raw[k], lo, hi) !== null) e[k] = clamp(raw[k], lo, hi);
    }
    for (const [k, [lo, hi]] of Object.entries({ opacity: [0, 100], seed: [0, 2 ** 31 - 1], updated: [0, 10 ** 15], fontSize: [8, 48], fontFamily: [1, 3] })) {
      if (integer(raw[k], lo, hi) !== null) e[k] = raw[k];
    }
    const enums = { fillStyle: ['hachure', 'cross-hatch', 'solid', 'zigzag'], strokeStyle: ['solid', 'dashed', 'dotted'], textAlign: ['left', 'center', 'right'], verticalAlign: ['top', 'middle', 'bottom'] };
    for (const [k, values] of Object.entries(enums)) if (choice(raw[k], values) !== null) e[k] = raw[k];
    for (const k of ['strokeColor', 'backgroundColor']) {
      if (typeof raw[k] === 'string' && (raw[k] === 'transparent' || /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(raw[k]))) e[k] = raw[k];
    }
    for (const k of ['text', 'originalText', 'name']) if (typeof raw[k] === 'string') e[k] = text(raw[k]);
    for (const k of ['locked', 'autoResize', 'simulatePressure', 'elbowed']) if (typeof raw[k] === 'boolean') e[k] = raw[k];
    for (const k of ['frameId', 'containerId']) if (k in raw && (raw[k] === null || id(raw[k]))) e[k] = raw[k];
    if (typeof raw.index === 'string' && /^[A-Za-z0-9]{1,120}$/.test(raw.index)) e.index = raw.index;
    if (Array.isArray(raw.groupIds)) e.groupIds = raw.groupIds.slice(0, 16).filter(id);
    if (raw.roundness && integer(raw.roundness.type, 1, 3) !== null) e.roundness = { type: raw.roundness.type };
    if (Array.isArray(raw.boundElements) && raw.boundElements.length <= 40) {
      e.boundElements = raw.boundElements.filter(b => b && id(b.id) && ['arrow', 'text'].includes(b.type)).map(b => ({ id: b.id, type: b.type }));
    }
    if (['arrow', 'line', 'freedraw'].includes(e.type)) {
      if (!Array.isArray(raw.points) || raw.points.length > 500) return null;
      e.points = [];
      for (const p of raw.points) {
        if (!Array.isArray(p) || p.length < 2 || !num(p[0]) || !num(p[1])) return null;
        e.points.push([clamp(p[0], -20000, 20000), clamp(p[1], -20000, 20000)]);
      }
    }
    if (e.type === 'freedraw' && Array.isArray(raw.pressures)) {
      if (raw.pressures.length > 500 || !raw.pressures.every(num)) return null;
      e.pressures = raw.pressures.map(p => clamp(p, 0, 1));
    }
    for (const k of ['startBinding', 'endBinding']) {
      const b = raw[k];
      if (b && id(b.elementId)) {
        const focus = clamp(b.focus === undefined ? 0 : b.focus, -1, 1), gap = clamp(b.gap === undefined ? 0 : b.gap, 0, 100);
        if (focus !== null && gap !== null) e[k] = { elementId: b.elementId, focus, gap };
      }
    }
    for (const k of ['startArrowhead', 'endArrowhead']) {
      if (raw[k] === null) e[k] = null;
      else if (choice(raw[k], ['arrow', 'bar', 'dot', 'triangle', 'diamond', 'triangle_outline']) !== null) e[k] = raw[k];
    }
    return new TextEncoder().encode(JSON.stringify(e)).length <= 32000 ? e : null;
  }
  function batch(raw) {
    if (!Array.isArray(raw) || raw.length > 1500) return null;
    const out = raw.map(clean);
    if (out.includes(null) || new TextEncoder().encode(JSON.stringify(out)).length > 1000000) return null;
    return out;
  }
  function merge(current, incoming) {
    const all = new Map(current.map(e => [e.id, e]));
    for (const e of incoming) {
      const old = all.get(e.id);
      if (!old || e.version > old.version || (e.version === old.version && e.versionNonce < old.versionNonce)) all.set(e.id, e);
    }
    return Array.from(all.values());
  }
  const rules = { clean, batch, merge };
  if (typeof module !== 'undefined') module.exports = rules;
  else root.SBBoardRules = rules;
})(typeof window === 'undefined' ? globalThis : window);
