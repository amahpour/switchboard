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

  // Mermaid's drawing of `source` as a detached <svg>, or a rejection with its message.
  function draw(source) {
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

  // a diagram on show follows the system's light or dark scheme, as the page does
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
      forget();
      for (const [box, v] of Array.from(shown)) render(box, v.source, v.button);
    });
  } catch (e) { /* no matchMedia: the theme is the one at draw time */ }

  window.SBDiagram = Object.freeze({ toggle: toggle });
})();
