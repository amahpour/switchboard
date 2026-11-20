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
 *     it (withoutHandlers), so a newer Chromium has nothing to report to the CSP (#917). Only
 *     text that is surely a tag is touched: anything else, a valid diagram included, is left as is.
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

  // `source` without the event-handler attributes (on…) of the HTML tags in it (issue #917).
  // Mermaid's DOMPurify parses each label in this page's documents, and Chromium from 157 reports
  // a handler attribute to the CSP as soon as it is parsed, though nothing runs and DOMPurify then
  // drops it. Dropping it first leaves the same drawing and no report. This is not what keeps a
  // diagram safe (the CSP and DOMPurify are), so it leaves alone anything it can't read for sure:
  // a valid diagram must come out byte for byte as it went in, since a strip that breaks one is
  // worse than the report it avoids. A candidate tag starts only at '<' and an ASCII letter (the
  // HTML tokenizer's own rule), stays on its line, holds no other '<', and ends at the first '>'
  // outside a quoted value it opened; its tag name and every attribute name must be plain, each
  // quoted value must be followed by a space, '/' or '>', and an unquoted one may not hold a quote,
  // a backtick or Mermaid's closing brackets. Anything else (a comparison in a label that runs into
  // an edge's '-->', say) is not a tag here, and the whole candidate is left as written. Only whole
  // attributes named on… go, each with its value.
  const TAG_NAME = /^[A-Za-z][A-Za-z0-9-]*$/;
  const ATTR_NAME = /^[A-Za-z_:][A-Za-z0-9_:.-]*$/;
  const BAD_UNQUOTED = /["'`\]}|]/;

  function space(c) { return c === ' ' || c === '\t' || c === '\f' || c === '\r'; }

  // The candidate tag at source[lt] ('<' and a letter): {end, drop} with `end` just past its '>'
  // and `drop` the [start, end) of each on… attribute, or {stop} where it stopped being one.
  // Every loop stops at a '<', so no '<' lies between lt and either: the caller goes on from
  // there, and each character of a line is read once (#918's review: a rescan per attribute
  // made a long line quadratic).
  function readTag(source, lt) {
    const n = source.length;
    const bad = function (c) { return c === '\n' || c === '<'; };
    let j = lt + 1;
    while (j < n && !space(source[j]) && '/>\n<'.indexOf(source[j]) < 0) j++;
    if (!TAG_NAME.test(source.slice(lt + 1, j))) return { stop: j };
    const drop = [];
    for (;;) {
      if (j >= n || bad(source[j])) return { stop: j };
      const c = source[j];
      if (c === '>') {
        if ('-='.indexOf(source[j - 1]) >= 0) return { stop: j };  // an edge's arrow, not a tag's end
        return { end: j + 1, drop: drop };
      }
      if (space(c) || c === '/') { j++; continue; }
      const start = j;
      while (j < n && !space(source[j]) && '/>=\n<'.indexOf(source[j]) < 0) j++;
      const name = source.slice(start, j);
      if (!ATTR_NAME.test(name)) return { stop: j };
      let k = j;
      while (k < n && space(source[k])) k++;
      if (source[k] === '=') {
        k++;
        while (k < n && space(source[k])) k++;
        const q = source[k];
        if (q === '"' || q === "'") {
          k++;
          while (k < n && source[k] !== q) {
            if (bad(source[k])) return { stop: k };
            k++;
          }
          if (k >= n) return { stop: k };
          k++;
          if (k < n && !space(source[k]) && '/>'.indexOf(source[k]) < 0) return { stop: k };
        } else {
          const v = k;
          while (k < n && !space(source[k]) && '>\n<'.indexOf(source[k]) < 0) k++;
          if (k === v || BAD_UNQUOTED.test(source.slice(v, k))) return { stop: k };
        }
        j = k;
      }
      if (/^on/i.test(name)) drop.push([start, j]);
    }
  }

  function withoutHandlers(source) {
    let out = '';
    let at = 0;  // source[at..] is not yet in out
    let i = 0;
    for (;;) {
      const lt = source.indexOf('<', i);
      if (lt < 0) break;
      if (!/[A-Za-z]/.test(source[lt + 1] || '')) { i = lt + 1; continue; }
      const tag = readTag(source, lt);
      if (tag.end === undefined) { i = Math.max(lt + 1, tag.stop); continue; }  // not a tag: as written
      for (const [s, e] of tag.drop) { out += source.slice(at, s); at = e; }
      i = tag.end;
    }
    return out + source.slice(at);
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
