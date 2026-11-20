/*
 * diagram.js: shows a ```mermaid code block as its diagram, on request (issue #57, DESIGN.md §33).
 * Exposed as window.SBDiagram, which md.js uses for the "Show diagram" button.
 *
 *   SBDiagram.toggle(box, source, button)  -> Promise: draw the diagram, or go back to the code
 *
 * SECURITY MODEL. Diagram source is message text, so it is as hostile as any message (md.js).
 *   - Mermaid (vendor/mermaid/mermaid.min.js, 11.17.2, unmodified; the static lint pins its
 *     sha256) is loaded only when someone first clicks "Show diagram". The script element this
 *     file creates for it is the only one any static JS creates, for that one fixed same-origin
 *     path, and the CSP's script-src 'self' (no inline script, no eval) still holds.
 *   - Mermaid runs at securityLevel 'strict' (labels are text, click callbacks are off) with HTML
 *     labels off. A diagram's own config (%%{init}%% or front matter) can't change any SECURE key:
 *     the security level, labels, theme, colours, fonts or CSS.
 *   - Mermaid draws in an off-screen box in the page (it measures text there), then the drawing
 *     moves into a shadow root, so the <style> it carries applies inside that shadow root only:
 *     a diagram can't restyle the page (hide a warning, fake a line). CSS containment keeps what
 *     it paints inside its box. Its styles are inline, so the app page's CSP allows inline styles
 *     (style-src 'unsafe-inline', auth.app_csp); scripts stay 'self' only.
 *   - Links in a drawing lose their href: md.js's mdLink stays the one link path.
 *   - Event-handler attributes in tag-like text are dropped from the source before Mermaid sees
 *     it (withoutHandlers), so a newer Chromium has nothing to report to the CSP (#917).
 *   - Nothing comes back from Mermaid but the drawing, and errors are shown as text.
 */
'use strict';

(function () {
  const SRC = '/static/vendor/mermaid/mermaid.min.js';
  const MAX_ERROR = 400;
  // Config keys a diagram can't set for itself. Mermaid's own list starts the array; the rest
  // keep a diagram from injecting CSS (theme, colours, fonts), turning HTML labels back on (top
  // level, flowchart, journey's text placement) or changing how DOMPurify cleans it.
  const SECURE = ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering', 'maxEdges',
    'htmlLabels', 'flowchart', 'journey', 'theme', 'themeVariables', 'themeCSS', 'fontFamily', 'altFontFamily',
    'darkMode', 'dompurifyConfig', 'look', 'handDrawnSeed', 'layout', 'elk', 'arrowMarkerAbsolute',
    'deterministicIds', 'deterministicIDSeed', 'logLevel'];

  let loading = null;    // the one load of Mermaid, shared by every diagram
  let queue = Promise.resolve();  // one drawing at a time: Mermaid's config is global
  const shown = new Map();  // box -> {source, button} of each diagram on show (redrawn on a scheme change)

  function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function dark() {
    const chosen = document.documentElement.dataset.theme;
    if (chosen === 'dark') return true;
    if (chosen === 'light') return false;
    try {
      return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (e) { return false; }
  }

  function config() {
    return {
      startOnLoad: false,
      securityLevel: 'strict',
      theme: dark() ? 'dark' : 'neutral',
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      journey: { textPlacement: 'tspan' },
      maxTextSize: 20000,
      maxEdges: 500,
      suppressErrorRendering: true,
      logLevel: 'fatal',
      secure: SECURE,
    };
  }

  function load() {
    const ready = function () { return window.mermaid && typeof window.mermaid.run === 'function'; };
    if (ready()) return Promise.resolve(window.mermaid);
    if (!loading) {
      loading = new Promise(function (resolve, reject) {
        const s = document.createElement('script');
        s.src = SRC;
        s.addEventListener('load', function () {
          if (ready()) resolve(window.mermaid);
          else reject(new Error('the diagram renderer did not start'));
        });
        s.addEventListener('error', function () {
          loading = null;  // a later click tries again
          s.remove();
          reject(new Error('the diagram renderer did not load'));
        });
        document.head.append(s);
      });
    }
    return loading;
  }

  // `source` without the event-handler attributes (on…) of any complete, one-line HTML tag in it
  // (issue #917). Mermaid's DOMPurify parses each label in this page's documents, and Chromium
  // from 157 reports a handler attribute to the CSP as soon as it is parsed, though nothing runs
  // and DOMPurify then drops it. Dropping it first leaves the same drawing and no report. This
  // is not what keeps a diagram safe (the CSP and DOMPurify are), so it may miss an odd tag; it
  // never touches text outside a tag that closes on its own line, or a tag with no on… attribute.
  // The states follow the HTML tokenizer's tag states.
  function withoutHandlers(source) {
    const space = function (c) { return c === ' ' || c === '\t' || c === '\f' || c === '\r'; };
    let out = '';
    let i = 0;
    while (i < source.length) {
      const lt = source.indexOf('<', i);
      if (lt < 0) break;
      const after = source[lt + 1] === '/' ? lt + 2 : lt + 1;
      if (!/[A-Za-z]/.test(source[after] || '')) {
        out += source.slice(i, lt + 1);
        i = lt + 1;
        continue;
      }
      const drop = [];  // [start, end) of each on… attribute
      let j = after;
      while (j < source.length && !space(source[j]) && '/>\n'.indexOf(source[j]) < 0) j++;  // the name
      let closed = false;
      while (j < source.length && source[j] !== '\n') {
        const c = source[j];
        if (c === '>') { closed = true; break; }
        if (space(c) || c === '/') { j++; continue; }
        const start = j;
        j++;  // an attribute name's first character may be '='
        while (j < source.length && !space(source[j]) && '/>=\n'.indexOf(source[j]) < 0) j++;
        const name = source.slice(start, j).toLowerCase();
        let k = j;
        while (k < source.length && space(source[k])) k++;
        if (source[k] === '=') {
          k++;
          while (k < source.length && space(source[k])) k++;
          const q = source[k];
          if (q === '"' || q === "'") {
            const end = source.indexOf(q, k + 1);
            const nl = source.indexOf('\n', k + 1);
            if (end < 0 || (nl >= 0 && nl < end)) { j = nl < 0 ? source.length : nl; break; }
            k = end + 1;
          } else {
            while (k < source.length && !space(source[k]) && '>\n'.indexOf(source[k]) < 0) k++;
          }
          j = k;
        }
        if (name.startsWith('on')) drop.push([start, j]);
      }
      if (!closed) {  // not a tag that ends on its line: left as it is
        out += source.slice(i, lt + 1);
        i = lt + 1;
        continue;
      }
      let at = i;
      for (const [s, e] of drop) { out += source.slice(at, s); at = e; }
      out += source.slice(at, j + 1);
      i = j + 1;
    }
    return out + source.slice(i);
  }

  // Mermaid's drawing of `source` as a detached <svg>, or a rejection with its message.
  function draw(source) {
    source = withoutHandlers(source);
    const job = queue.then(function () {
      return load().then(function (mermaid) {
        mermaid.initialize(config());
        return mermaid.parse(source).then(function () {
          const stage = el('div', 'md-diagram-stage');
          stage.textContent = source;
          document.body.append(stage);
          return mermaid.run({ nodes: [stage] }).then(function () {
            const svg = stage.querySelector('svg');
            if (!svg) throw new Error('nothing was drawn');
            for (const a of svg.querySelectorAll('a')) {
              a.removeAttribute('href');
              a.removeAttribute('xlink:href');
              a.removeAttribute('target');
            }
            svg.remove();
            return svg;
          }).finally(function () { stage.remove(); });
        });
      });
    });
    queue = job.catch(function () {});
    return job;
  }

  function errorText(err) {
    const msg = String((err && (err.message || err.str)) || err || 'unknown error').trim();
    return msg.length > MAX_ERROR ? msg.slice(0, MAX_ERROR - 1) + '…' : msg;
  }

  function safeDiagnostic(err) {
    const raw = String((err && (err.message || err.str)) || err || '').trim();
    if (raw.startsWith('No diagram type detected matching given configuration for text:')) {
      return 'Mermaid found no diagram type';
    }
    const line = raw.match(/(?:^|\n)Parse error on line ([1-9][0-9]{0,5})\b/);
    if (!line) return 'Mermaid could not parse this diagram';
    // Only parser token names may cross into a human post. The source line and `got` token do not.
    const expected = Array.from(raw.matchAll(/(?:^|\n)Expecting ((?:'[A-Z][A-Z0-9_]*'(?:, )?)+), got /g)).pop();
    return 'Parse error on line ' + line[1] +
      (expected ? ': Expecting ' + expected[1].slice(0, 240) : '');
  }

  function parts(box) {
    let fig = box.querySelector('.md-diagram');
    if (!fig) {
      fig = el('div', 'md-diagram');
      fig.attachShadow({ mode: 'open' });
      box.querySelector('.md-pre-head').after(fig);
      if (typeof fig.requestFullscreen === 'function') {
        const full = el('button', 'md-copy md-fullscreen');
        full.type = 'button';
        full.textContent = 'Full screen';
        full.addEventListener('click', function () { fig.requestFullscreen().catch(function () {}); });
        box.querySelector('.md-show-diagram').after(full);
      }
    }
    let note = box.querySelector('.md-diagram-error');
    if (!note) {
      note = el('div', 'md-diagram-error');
      note.setAttribute('role', 'status');
      fig.after(note);
    }
    return { fig: fig, note: note };
  }

  function showCode(box, button) {
    box.classList.remove('md-showing-diagram');
    shown.delete(box);
    button.textContent = 'Show diagram';
  }

  // Draw `source` into `box` (a first draw, or a redraw in the other scheme). A failure shows
  // Mermaid's message as text, with the code.
  function render(box, source, button) {
    const p = parts(box);
    return draw(source).then(function (svg) {
      svg.setAttribute('part', 'drawing');
      p.fig.shadowRoot.replaceChildren(svg);
      p.note.textContent = '';
      box.classList.remove('md-diagram-failed');
      box.classList.add('md-showing-diagram');
      shown.set(box, { source: source, button: button });
      button.textContent = 'Show code';
    }, function (err) {
      const message = errorText(err);
      p.fig.shadowRoot.replaceChildren();
      p.note.textContent = "Can't draw this diagram: " + message;
      box.classList.add('md-diagram-failed');
      showCode(box, button);
      box.dispatchEvent(new CustomEvent('diagramerror',
        { bubbles: true, detail: { diagnostic: safeDiagnostic(err) } }));
    });
  }

  function forget() {  // drawings whose message has left the log
    for (const box of Array.from(shown.keys())) if (!box.isConnected) shown.delete(box);
  }

  function toggle(box, source, button) {
    forget();
    if (box.classList.contains('md-showing-diagram')) {
      showCode(box, button);
      return Promise.resolve();
    }
    button.disabled = true;
    button.textContent = 'Drawing…';
    return render(box, source, button).finally(function () { button.disabled = false; });
  }

  function redrawShown() {
    forget();
    for (const [box, v] of Array.from(shown)) render(box, v.source, v.button);
  }

  // A diagram on show follows either a chosen theme or the system's scheme.
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redrawShown);
  } catch (e) { /* no matchMedia: the theme is the one at draw time */ }
  window.addEventListener('switchboard-theme-change', redrawShown);

  window.SBDiagram = Object.freeze({ toggle: toggle, withoutHandlers: withoutHandlers });
})();
