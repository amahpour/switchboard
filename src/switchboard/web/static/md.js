/*
 * md.js: the web UI's Markdown renderer, exposed as window.SBMarkdown.
 *
 *   SBMarkdown.render(text, {mentions, localHost})  -> DocumentFragment
 *   SBMarkdown.firstLine(text, max)                 -> plain string (reply snippets)
 *   SBMarkdown.safeUrl(raw, localHost)              -> {ok: true, href} | {ok: false, why}
 *
 * SECURITY MODEL. Message text comes from agents, and an agent reads untrusted input (web pages,
 * tool output), so every message is treated as hostile. This file therefore:
 *   - never builds HTML from strings. Text is parsed into a small tree of plain objects, and the
 *     tree becomes DOM through createElement / createTextNode / textContent only. Raw HTML such as
 *     <b> or <script> is ordinary text, shown exactly as typed. Entities (&amp;, &#106;) are never
 *     decoded, so they cannot smuggle characters past the checks below;
 *   - creates only these elements: p br div span strong em code pre ul ol li blockquote hr table
 *     thead tbody tr th td a button. Never img, iframe, script, form, svg or style: images are not
 *     rendered at all (![alt](url) stays literal text), so nothing is fetched just by viewing;
 *   - sets a link target in exactly one place, mdLink(), after safeUrl() has approved it. Only
 *     absolute http: and https: URLs pass. Links back to this switchboard page (localhost,
 *     *.localhost such as switchboard.localhost, 127.x, [::1], 0.0.0.0, or the page's own host)
 *     are refused, so a message cannot make a click do something to the broker (a GET /logout,
 *     say). A refused link is inert text plus a "link blocked" pill that says why;
 *   - shows the real, normalized URL beside every link text, so "[CI run](https://evil.example)"
 *     cannot hide where it goes, and an IDN look-alike host shows as its xn-- form;
 *   - refuses URLs with a user name or password (https://github.com@evil.example/): the part
 *     before '@' can make the shown URL read as a trusted host while the browser goes to the
 *     host after it;
 *   - opens links in a new tab with rel="noopener noreferrer nofollow" and no referrer;
 *   - gives a ```mermaid code block a "Show diagram" button, when diagram.js (window.SBDiagram)
 *     is loaded, which hands that file the block's raw text. md.js itself never draws a diagram.
 *   The static lint (tests/unit/test_web_static_lint.py) pins the single link path and bans the
 *   HTML sinks, so a later edit cannot quietly add another way in.
 *
 * GRAMMAR (a small, fixed subset; every sender, human or agent, gets the same rules).
 *   Preprocess: CRLF and CR become LF; a tab counts as 4 columns (tab stops) for indentation.
 *   Blocks, tried in this order at each line:
 *     1. Fenced code: up to 3 spaces, then ``` or ~~~ (3 or more), then an optional info word.
 *        A backtick fence whose line holds another backtick is inline code, not a fence (as in
 *        CommonMark). Closed by the same character, a run at least as long, alone on its line;
 *        an unclosed fence runs to the end of its container. The content is raw text. The label
 *        is the info word filtered to [A-Za-z0-9+#._-], at most 20 characters, else "code".
 *     2. ATX heading: 1 to 6 '#' then a space or tab. "#build" is not a heading. An optional
 *        closing run of '#' is dropped. h1 and h2 keep their size; h3 to h6 share one size.
 *     3. Thematic break: three or more of the same '-', '*' or '_', spaces between allowed.
 *     4. Blockquote: consecutive lines starting "> " (up to 3 spaces first). The marker is
 *        stripped and the rest parsed again, one level deeper. No lazy continuation lines.
 *     5. List: items "- ", "* ", "+ ", "1. " or "1) " indented 0 to 12 columns. One list is
 *        consecutive items of the same kind at the same indent; lines indented to the item's
 *        content column belong to the item (so lists nest); a single blank line between items
 *        keeps the list together. An ordered list keeps its first number. "[ ]" stays literal.
 *     6. Table: a line with an unescaped '|' followed by a delimiter row (---, :--, :-:, --:)
 *        with the same cell count. Body rows continue while a line holds '|' and is not blank.
 *        Cells split on '|' that is neither escaped nor inside a code span; outer pipes are
 *        trimmed; missing cells are empty and extra cells dropped.
 *     7. Paragraph: consecutive non-blank lines that start no other block; each line break
 *        inside it is kept as a <br>.
 *   Not supported: indented code, setext headings, HTML blocks, reference links, footnotes,
 *   images, task lists, strikethrough, syntax highlighting.
 *   Inlines, one left-to-right scan:
 *     \ + ASCII punctuation is that character, literal. `code spans` (a run of n backticks
 *     closed by the next run of exactly n). [text](url "title") links, with no link inside a
 *     link. <http(s)://...> autolinks, and bare http(s):// URLs (trailing punctuation and an
 *     unbalanced ')' stay text). A bare URL is taken whole by the same scan, before emphasis,
 *     code spans or mentions can see its characters, so `.../__init__.py`, `/a*b*c` and
 *     `/@types/node` stay inside it. *em*, _em_, **strong**, __strong__ by CommonMark's
 *     flanking rules, except that '_' needs a non-alphanumeric neighbour outside, so
 *     snake_case_name stays literal. @name becomes a highlighted mention only when the broker
 *     listed it in the message's mentions (the same pattern as delivery/rules.py MENTION_RE),
 *     so the highlight never claims a wake-up that did not happen. Everything else is text.
 *
 * LIMITS. The broker caps a message at 4000 characters, but this file does not rely on that:
 *   - input longer than MAX_INPUT (20,000) is shown as one plain paragraph, unparsed;
 *   - block nesting (quotes plus lists) stops at MAX_DEPTH (8); deeper markers stay text;
 *   - a table has at most MAX_COLS (50) columns and MAX_ROWS (500) body rows;
 *   - at most MAX_DELIMS (500) emphasis delimiter runs per inline scan; later runs are text;
 *   - inline nesting (emphasis inside emphasis) stops at MAX_NEST (16) levels: a pair that
 *     would go deeper stays literal, so one "***...a...***" run pair cannot build a tree
 *     thousands of levels deep;
 *   - a link destination is scanned for at most MAX_DEST (2048) characters.
 *   Parsing is linear: block and inline scanners are hand-written loops, and no regular
 *   expression with a nested quantifier ever runs on message text. If anything still throws,
 *   render() falls back to the plain paragraph, so a message is never lost.
 */
(function () {
  'use strict';

  const MAX_INPUT = 20000;
  const MAX_DEPTH = 8;
  const MAX_COLS = 50;
  const MAX_ROWS = 500;
  const MAX_DELIMS = 500;
  const MAX_NEST = 16;
  const MAX_DEST = 2048;
  const MAX_LANG = 20;

  const TITLE_SCHEME = 'Not a link: only http(s) URLs are followed';
  const TITLE_LOCAL = 'Not a link: links to this switchboard page are never followed';
  const TITLE_CREDS = 'Not a link: a URL with a user name or password can hide its real host';

  const ASCII_PUNCT = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~';
  const URL_TRAIL = '.,;:!?*_';        // trimmed from the end of a bare URL
  const URL_STOP = '<>"\'`';           // a bare URL never contains these
  // Same shape as rules.MENTION_RE (Python): the look-behind is checked by hand in the scanner.
  const MENTION = /@([a-z][a-z0-9_-]{0,23})(?![a-z0-9_-])/iy;
  const WORD_BEFORE_MENTION = /[\p{L}\p{N}_@]/u;
  const ALNUM = /[\p{L}\p{N}]/u;
  const PUNCT = /[\p{P}\p{S}]/u;
  const SPACE = /\s/;

  // ------------------------------------------------------------------ small helpers

  function isSpace(c) { return c === undefined || c === '' || SPACE.test(c); }
  function isPunct(c) { return c !== undefined && c !== '' && PUNCT.test(c); }
  function isAlnum(c) { return c !== undefined && c !== '' && ALNUM.test(c); }
  function isAsciiPunct(c) { return c !== undefined && c !== '' && ASCII_PUNCT.indexOf(c) >= 0; }

  function isBlank(line) {
    for (let k = 0; k < line.length; k++) if (line[k] !== ' ' && line[k] !== '\t') return false;
    return true;
  }

  // Trim spaces and tabs by hand (a /[ \t]+$/ regex backtracks quadratically on long space runs).
  function trimST(s) {
    let a = 0;
    let b = s.length;
    while (a < b && (s[a] === ' ' || s[a] === '\t')) a++;
    while (b > a && (s[b - 1] === ' ' || s[b - 1] === '\t')) b--;
    return s.slice(a, b);
  }

  // Columns of leading whitespace (tab stops of 4).
  function leadCols(line) {
    let c = 0;
    for (let k = 0; k < line.length; k++) {
      if (line[k] === ' ') c++;
      else if (line[k] === '\t') c += 4 - (c % 4);
      else break;
    }
    return c;
  }

  // Remove n columns of leading whitespace; a tab that straddles the cut leaves its remainder.
  function stripCols(line, n) {
    let c = 0;
    let k = 0;
    while (k < line.length && c < n) {
      if (line[k] === ' ') { c++; k++; }
      else if (line[k] === '\t') {
        const w = 4 - (c % 4);
        if (c + w > n) return ' '.repeat(c + w - n) + line.slice(k + 1);
        c += w;
        k++;
      } else break;
    }
    return line.slice(k);
  }

  function unescapePunct(s) {
    let out = '';
    for (let k = 0; k < s.length; k++) {
      if (s[k] === '\\' && isAsciiPunct(s[k + 1])) { out += s[k + 1]; k++; } else out += s[k];
    }
    return out;
  }

  // Code-span closers for one string, found in linear time: all backtick runs are indexed by
  // length once; a query "the next run of exactly n after p" only moves a per-length pointer
  // forward (callers query with increasing p). Returns the closing run's start, or -1.
  function spanMatcher(s) {
    const byLen = new Map();
    let k = s.indexOf('`');
    while (k >= 0) {
      let e = k;
      while (e < s.length && s[e] === '`') e++;
      const n = e - k;
      if (!byLen.has(n)) byLen.set(n, []);
      byLen.get(n).push(k);
      k = s.indexOf('`', e);
    }
    const ptr = new Map();
    return function (p, n) {
      const arr = byLen.get(n);
      if (!arr) return -1;
      let q = ptr.get(n) || 0;
      while (q < arr.length && arr[q] <= p) q++;
      ptr.set(n, q);
      return q < arr.length ? arr[q] : -1;
    };
  }

  function runLen(s, k, ch) {
    let e = k;
    while (e < s.length && s[e] === ch) e++;
    return e - k;
  }

  // ------------------------------------------------------------------ block recognizers

  function fenceOpen(line) {
    let k = 0;
    while (k < 3 && line[k] === ' ') k++;
    const ch = line[k];
    if (ch !== '`' && ch !== '~') return null;
    const len = runLen(line, k, ch);
    if (len < 3) return null;
    let j = k + len;
    if (ch === '`' && line.indexOf('`', j) >= 0) return null;
    while (line[j] === ' ' || line[j] === '\t') j++;
    let w = j;
    while (w < line.length && !isSpace(line[w]) && line[w] !== '`') w++;
    const lang = line.slice(j, w).replace(/[^A-Za-z0-9+#._-]/g, '').slice(0, MAX_LANG) || 'code';
    return { ch: ch, len: len, indent: k, lang: lang };
  }

  function fenceCloses(line, f) {
    let k = 0;
    while (k < 3 && line[k] === ' ') k++;
    if (line[k] !== f.ch) return false;
    const len = runLen(line, k, f.ch);
    return len >= f.len && isBlank(line.slice(k + len));
  }

  function atxHeading(line) {
    let k = 0;
    while (k < 3 && line[k] === ' ') k++;
    const level = runLen(line, k, '#');
    if (level < 1 || level > 6) return null;
    const after = line[k + level];
    if (after !== ' ' && after !== '\t') return null;
    let body = trimST(line.slice(k + level));
    let q = body.length;
    while (q > 0 && body[q - 1] === '#') q--;
    if (q === 0) body = '';
    else if (q < body.length && (body[q - 1] === ' ' || body[q - 1] === '\t')) body = trimST(body.slice(0, q));
    return { level: level, text: body };
  }

  function isThematicBreak(line) {
    let k = 0;
    while (k < 3 && line[k] === ' ') k++;
    const ch = line[k];
    if (ch !== '-' && ch !== '*' && ch !== '_') return false;
    let n = 0;
    for (let j = k; j < line.length; j++) {
      if (line[j] === ch) n++;
      else if (line[j] !== ' ' && line[j] !== '\t') return false;
    }
    return n >= 3;
  }

  // The rest of a "> " line, or null.
  function quoteStrip(line) {
    let k = 0;
    while (k < 3 && line[k] === ' ') k++;
    if (line[k] !== '>') return null;
    k++;
    if (line[k] === ' ') k++;
    return line.slice(k);
  }

  function listItem(line) {
    const cols = leadCols(line);
    if (cols > 12) return null;
    let idx = 0;
    while (line[idx] === ' ' || line[idx] === '\t') idx++;
    const c = line[idx];
    let e;
    let kind;
    let start = 1;
    if (c === '-' || c === '*' || c === '+') {
      e = idx + 1;
      kind = 'b';
    } else {
      e = idx;
      while (e < line.length && e - idx < 10 && line[e] >= '0' && line[e] <= '9') e++;
      if (e === idx || e - idx > 9) return null;
      const d = line[e];
      if (d !== '.' && d !== ')') return null;
      start = parseInt(line.slice(idx, e), 10);
      kind = 'o' + d;
      e++;
    }
    if (line[e] !== ' ' && line[e] !== '\t') return null;
    const markerEnd = cols + (e - idx);
    let col = markerEnd;
    let s = e;
    while (line[s] === ' ' || line[s] === '\t') {
      col += line[s] === '\t' ? 4 - (col % 4) : 1;
      s++;
    }
    const content = line.slice(s);
    const contentCol = content === '' || col - markerEnd > 4 ? markerEnd + 1 : col;
    return { kind: kind, start: start, indent: cols, contentCol: contentCol, content: content };
  }

  // Split one table row into trimmed cell strings (see GRAMMAR, Table).
  function splitCells(line) {
    const s = trimST(line);
    const closer = spanMatcher(s);
    let k = s[0] === '|' ? 1 : 0;
    let end = s.length;
    if (end > k && s[end - 1] === '|' && s[end - 2] !== '\\') end--;
    const cells = [];
    let cur = '';
    while (k < end) {
      const c = s[k];
      if (c === '\\' && k + 1 < end) { cur += c + s[k + 1]; k += 2; continue; }
      if (c === '`') {
        const n = runLen(s, k, '`');
        const close = closer(k, n);
        const stop = close >= 0 ? close + n : k + n;
        cur += s.slice(k, stop);
        k = stop;
        continue;
      }
      if (c === '|') { cells.push(trimST(cur)); cur = ''; k++; continue; }
      cur += c;
      k++;
    }
    cells.push(trimST(cur));
    return cells;
  }

  function hasUnescapedPipe(line) {
    for (let k = 0; k < line.length; k++) {
      if (line[k] === '\\') k++;
      else if (line[k] === '|') return true;
    }
    return false;
  }

  function tableHead(lines, i) {
    if (i + 1 >= lines.length || !hasUnescapedPipe(lines[i])) return null;
    const delim = lines[i + 1];
    if (delim.indexOf('-') < 0) return null;
    const dc = splitCells(delim);
    if (dc.length > MAX_COLS) return null;
    const aligns = [];
    for (const c of dc) {
      if (!/^:?-+:?$/.test(c)) return null;
      const l = c[0] === ':';
      const r = c[c.length - 1] === ':';
      aligns.push(l && r ? 'c' : r ? 'r' : l ? 'l' : '');
    }
    const head = splitCells(lines[i]);
    if (head.length !== dc.length) return null;
    return { head: head, aligns: aligns };
  }

  function startsBlock(lines, j, depth) {
    const line = lines[j];
    if (fenceOpen(line) || atxHeading(line) || isThematicBreak(line)) return true;
    if (depth < MAX_DEPTH && (quoteStrip(line) !== null || listItem(line))) return true;
    return !!tableHead(lines, j);
  }

  // ------------------------------------------------------------------ block parser

  // lines -> [{t:'p'|'h'|'code'|'hr'|'quote'|'ul'|'ol'|'table', ...}]
  function parseBlocks(lines, depth) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }

      const f = fenceOpen(line);
      if (f) {
        const body = [];
        i++;
        while (i < lines.length && !fenceCloses(lines[i], f)) {
          body.push(stripCols(lines[i], Math.min(f.indent, leadCols(lines[i]))));
          i++;
        }
        i++;  // the closing fence (or past the end)
        out.push({ t: 'code', lang: f.lang, text: body.join('\n') });
        continue;
      }

      const h = atxHeading(line);
      if (h) { out.push({ t: 'h', level: h.level, text: h.text }); i++; continue; }

      if (isThematicBreak(line)) { out.push({ t: 'hr' }); i++; continue; }

      if (depth < MAX_DEPTH && quoteStrip(line) !== null) {
        const inner = [];
        while (i < lines.length) {
          const q = quoteStrip(lines[i]);
          if (q === null) break;
          inner.push(q);
          i++;
        }
        out.push({ t: 'quote', c: parseBlocks(inner, depth + 1) });
        continue;
      }

      const item = depth < MAX_DEPTH ? listItem(line) : null;
      if (item) {
        const r = parseList(lines, i, depth, item);
        out.push(r.node);
        i = r.next;
        continue;
      }

      const tbl = tableHead(lines, i);
      if (tbl) {
        const width = tbl.head.length;
        const rows = [];
        i += 2;
        while (i < lines.length && rows.length < MAX_ROWS && !isBlank(lines[i]) && lines[i].indexOf('|') >= 0) {
          const cells = splitCells(lines[i]).slice(0, width);
          while (cells.length < width) cells.push('');
          rows.push(cells);
          i++;
        }
        out.push({ t: 'table', head: tbl.head, aligns: tbl.aligns, rows: rows });
        continue;
      }

      const para = [trimST(line)];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i, depth)) {
        para.push(trimST(lines[i]));
        i++;
      }
      out.push({ t: 'p', text: para.join('\n') });
    }
    return out;
  }

  function parseList(lines, i, depth, first) {
    const items = [];
    let cur = first;
    let j = i;
    for (;;) {
      const body = [cur.content];
      j++;
      while (j < lines.length) {
        if (isBlank(lines[j])) {
          let k = j;
          while (k < lines.length && isBlank(lines[k])) k++;
          if (k < lines.length && leadCols(lines[k]) >= cur.contentCol) {
            for (; j < k; j++) body.push('');
            continue;
          }
          break;
        }
        if (leadCols(lines[j]) < cur.contentCol) break;
        body.push(stripCols(lines[j], cur.contentCol));
        j++;
      }
      items.push(parseBlocks(body, depth + 1));

      let k = j;
      while (k < lines.length && isBlank(lines[k])) k++;
      if (k - j > 1 || k >= lines.length || isThematicBreak(lines[k])) break;
      const next = listItem(lines[k]);
      if (!next || next.kind !== first.kind || next.indent !== first.indent) break;
      j = k;
      cur = next;
    }
    return { node: { t: first.kind === 'b' ? 'ul' : 'ol', start: first.start, items: items }, next: j };
  }

  // ------------------------------------------------------------------ inline parser

  // Parse one link destination starting just after "](". Returns {url, end, bad} or null.
  // A destination with whitespace or a control character inside (a tab or a newline in the
  // middle of a scheme, say) is kept as a link that is never followed ("bad"), so the reader
  // sees a blocked link rather than half-parsed text.
  function parseDest(s, p) {
    const lim = Math.min(s.length, p + MAX_DEST);
    let k = p;
    while (k < lim && isSpace(s[k])) k++;
    let url;
    if (s[k] === '<') {
      let e = k + 1;
      while (e < lim && s[e] !== '>' && s[e] !== '<' && s[e] !== '\n') e += s[e] === '\\' ? 2 : 1;
      if (e >= lim || s[e] !== '>') return badDest(s, p, lim);
      url = unescapePunct(s.slice(k + 1, e));
      k = e + 1;
    } else {
      let e = k;
      let depth = 0;
      while (e < lim) {
        const c = s[e];
        if (c === '\\' && isAsciiPunct(s[e + 1])) { e += 2; continue; }
        if (c === '(') { if (++depth > 3) return null; }
        else if (c === ')') { if (depth === 0) break; depth--; }
        else if (isSpace(c) || c < ' ') break;
        e++;
      }
      url = unescapePunct(s.slice(k, e));
      k = e;
    }
    while (k < lim && isSpace(s[k])) k++;
    if (s[k] === '"') {  // an optional "title", ignored
      k++;
      while (k < lim && s[k] !== '"') k += s[k] === '\\' ? 2 : 1;
      if (k >= lim) return badDest(s, p, lim);
      k++;
      while (k < lim && isSpace(s[k])) k++;
    }
    if (k < lim && s[k] === ')') return { url: url, end: k + 1, bad: false };
    return badDest(s, p, lim);
  }

  function badDest(s, p, lim) {
    let depth = 0;
    for (let e = p; e < lim; e++) {
      const c = s[e];
      if (c === '\\') { e++; continue; }
      if (c === '(') depth++;
      else if (c === ')') {
        if (depth === 0) return { url: s.slice(p, e), end: e + 1, bad: true };
        depth--;
      }
    }
    return null;
  }

  function startsWithCI(s, k, word) {
    return s.slice(k, k + word.length).toLowerCase() === word;
  }

  // A bare http(s):// URL starting at s[k], or null. The caller has checked that no letter or
  // digit comes right before it. It runs inside the inline scan, ahead of the emphasis, code
  // span, mention and bracket handlers, so none of them can cut a URL short: `__init__.py`,
  // `a*b*c` and `/@types/node` stay part of it. The run ends at whitespace, at one of URL_STOP,
  // at a backslash that escapes nothing (a backslash before other ASCII punctuation keeps that
  // character, as it would in text) and, while a '[' is open (inBracket), at ']', so
  // "[https://a.example](https://b.example)" is still a link to b. Then trailing punctuation
  // (URL_TRAIL) and a ')' with no '(' to match come off the end, and are scanned again as
  // ordinary text, so "(see https://x.y/z)." and "*https://x.y/z*" work. Linear: each character
  // is looked at once here, and when nothing is left after the trim every character past the
  // scheme was trailing punctuation, so no other URL can start inside the run.
  // Returns {url, end}: the URL (escapes resolved) and the index in s just past it.
  function bareUrl(s, k, inBracket) {
    const m = s.startsWith('https://', k) ? 8 : s.startsWith('http://', k) ? 7 : 0;
    if (!m) return null;
    const chars = [];  // the URL so far, one character per entry
    const ends = [];   // ends[j]: the index in s just past chars[j] (an escape is 2 long)
    for (let q = k; q < k + m; q++) { chars.push(s[q]); ends.push(q + 1); }
    let opens = 0;
    let closes = 0;
    let e = k + m;
    while (e < s.length) {
      let c = s[e];
      let next = e + 1;
      if (c === '\\') {
        const x = s[e + 1];
        if (!isAsciiPunct(x) || URL_STOP.indexOf(x) >= 0) break;
        c = x;
        next = e + 2;
      } else if (isSpace(c) || URL_STOP.indexOf(c) >= 0 || (inBracket && c === ']')) {
        break;
      }
      if (c === '(') opens++;
      else if (c === ')') closes++;
      chars.push(c);
      ends.push(next);
      e = next;
    }
    let len = chars.length;
    while (len > m) {
      const c = chars[len - 1];
      if (URL_TRAIL.indexOf(c) >= 0) len--;
      else if (c === ')' && closes > opens) { len--; closes--; }
      else break;
    }
    if (len <= m) return null;
    return { url: chars.slice(0, len).join(''), end: ends[len - 1] };
  }

  // One inline scan over s (see GRAMMAR, Inlines). Returns a tree of plain objects:
  // {t:'text', v} {t:'br'} {t:'code', v} {t:'mention', v} {t:'strong'|'em', c}
  // {t:'link', c, url, bad}. Nodes live in a doubly linked list while scanning so emphasis and
  // links can wrap a stretch in O(1) relinking; `d` is a node's inline nesting depth, and a
  // {t:'group', c} (emphasis past MAX_NEST) is dissolved again by finish().
  function parseInline(s, o) {
    const head = { t: 'head', prev: null, next: null };
    let tail = head;
    let buf = '';
    const delims = [];   // emphasis delimiter stack: {node, ch, canOpen, canClose, orig}
    let delimRuns = 0;
    const brackets = []; // '[' and '![' openers: {node, image, pos, bottom}
    let floor = 0;       // '[' openers below this index are inactive (no link inside a link)
    const closer = spanMatcher(s);

    function add(n) { n.prev = tail; n.next = null; tail.next = n; tail = n; return n; }
    function flush() { if (buf) { add({ t: 'text', v: buf }); buf = ''; } }
    function cutAfter(node) {
      const out = [];
      for (let n = node.next; n; n = n.next) out.push(n);
      node.next = null;
      tail = node;
      return out;
    }

    // CommonMark's "process emphasis", over delimiter entries at index >= bottom. The stack
    // holds at most MAX_DELIMS entries, so the backward opener search is bounded.
    function processEmphasis(bottom) {
      let ci = bottom;
      while (ci < delims.length) {
        const cl = delims[ci];
        if (!cl.canClose) { ci++; continue; }
        let oi = ci - 1;
        for (; oi >= bottom; oi--) {
          const op = delims[oi];
          if (op.ch !== cl.ch || !op.canOpen) continue;
          const bothWays = op.canClose || cl.canOpen;
          if (bothWays && (op.orig + cl.orig) % 3 === 0 && !(op.orig % 3 === 0 && cl.orig % 3 === 0)) continue;
          break;
        }
        if (oi < bottom) {
          if (!cl.canOpen) delims.splice(ci, 1); else ci++;
          continue;
        }
        const op = delims[oi];
        const kids = [];
        let d = 0;
        for (let n = op.node.next; n !== cl.node; n = n.next) { kids.push(n); d = Math.max(d, n.d || 0); }
        let w;
        if (d >= MAX_NEST) {
          // Too deep: every remaining match of this pair would be too deep as well, so all of
          // it becomes literal text at once, and the stretch is grouped (a 'group' renders its
          // children in place) so a later pass over it sees one node, keeping this linear.
          const use = Math.min(op.node.len, cl.node.len);
          op.node.len -= use;
          cl.node.len -= use;
          const lit = { t: 'text', v: op.ch.repeat(use) };
          w = { t: 'group', c: [lit].concat(kids, [{ t: 'text', v: op.ch.repeat(use) }]), d: d };
        } else {
          const use = op.node.len >= 2 && cl.node.len >= 2 ? 2 : 1;
          op.node.len -= use;
          cl.node.len -= use;
          w = { t: use === 2 ? 'strong' : 'em', c: kids, d: d + 1 };
        }
        w.prev = op.node;
        w.next = cl.node;
        op.node.next = w;
        cl.node.prev = w;
        delims.splice(oi + 1, ci - oi - 1);
        ci = oi + 1;
        if (op.node.len === 0) { delims.splice(oi, 1); ci--; }
        if (cl.node.len === 0) delims.splice(ci, 1);
      }
    }

    let i = 0;
    const n = s.length;
    while (i < n) {
      const ch = s[i];

      // A bare URL, before anything else can take its characters (see bareUrl).
      if (ch === 'h' && !isAlnum(s[i - 1])) {
        const u = bareUrl(s, i, brackets.length > 0);
        if (u) {
          flush();
          add({ t: 'link', c: [{ t: 'text', v: u.url }], url: u.url, bad: false });
          i = u.end;
          continue;
        }
      }

      if (ch === '\\') {
        if (isAsciiPunct(s[i + 1])) { buf += s[i + 1]; i += 2; }
        else if (s[i + 1] === '\n') i++;  // a hard break: the newline below becomes <br>
        else { buf += ch; i++; }
        continue;
      }

      if (ch === '\n') {
        flush();
        add({ t: 'br' });
        i++;
        while (s[i] === ' ' || s[i] === '\t') i++;
        continue;
      }

      if (ch === '`') {
        const len = runLen(s, i, '`');
        const close = closer(i, len);
        if (close < 0) { buf += s.slice(i, i + len); i += len; continue; }
        let v = s.slice(i + len, close).replace(/\n/g, ' ');
        if (v.length > 1 && v[0] === ' ' && v[v.length - 1] === ' ' && trimST(v) !== '') v = v.slice(1, -1);
        flush();
        add({ t: 'code', v: v });
        i = close + len;
        continue;
      }

      if (ch === '*' || ch === '_') {
        const len = runLen(s, i, ch);
        const before = i > 0 ? s[i - 1] : '';
        const after = s[i + len];
        const lf = !isSpace(after) && (!isPunct(after) || isSpace(before) || isPunct(before));
        const rf = !isSpace(before) && (!isPunct(before) || isSpace(after) || isPunct(after));
        const canOpen = ch === '*' ? lf : lf && !isAlnum(before);
        const canClose = ch === '*' ? rf : rf && !isAlnum(after);
        if ((canOpen || canClose) && delimRuns < MAX_DELIMS) {
          delimRuns++;
          flush();
          const node = add({ t: 'delim', ch: ch, len: len });
          delims.push({ node: node, ch: ch, canOpen: canOpen, canClose: canClose, orig: len });
        } else {
          buf += s.slice(i, i + len);
        }
        i += len;
        continue;
      }

      if (ch === '[' || (ch === '!' && s[i + 1] === '[')) {
        const image = ch === '!';
        flush();
        const node = add({ t: 'text', v: image ? '![' : '[' });
        brackets.push({ node: node, image: image, pos: i, bottom: delims.length });
        i += image ? 2 : 1;
        continue;
      }

      if (ch === ']') {
        const top = brackets.length - 1;
        const b = brackets[top];
        const d = b && (b.image || top >= floor) && s[i + 1] === '(' ? parseDest(s, i + 2) : null;
        if (!d) {
          if (b) { brackets.pop(); floor = Math.min(floor, brackets.length); }
          buf += ']';
          i++;
          continue;
        }
        flush();
        brackets.pop();
        if (b.image) {
          // Images are never rendered: the whole source stays literal, and nothing inside it
          // (a nested link, a bare URL) becomes a link: what was built for it is dropped.
          cutAfter(b.node.prev);
          delims.length = b.bottom;
          add({ t: 'text', v: s.slice(b.pos, d.end) });
        } else {
          processEmphasis(b.bottom);
          delims.length = b.bottom;
          const kids = cutAfter(b.node);
          cutAfter(b.node.prev);
          let depth = 0;
          for (const k of kids) depth = Math.max(depth, k.d || 0);
          add({ t: 'link', c: kids, url: d.url, bad: d.bad, d: depth + 1 });
          floor = brackets.length;
        }
        floor = Math.min(floor, brackets.length);
        i = d.end;
        continue;
      }

      if (ch === '<') {
        const m = startsWithCI(s, i + 1, 'https://') ? 8 : startsWithCI(s, i + 1, 'http://') ? 7 : 0;
        if (m) {
          const lim = Math.min(n, i + 1 + MAX_DEST);
          let e = i + 1 + m;
          while (e < lim && s[e] !== '>' && s[e] !== '<' && !isSpace(s[e])) e++;
          if (e < lim && s[e] === '>' && e > i + 1 + m) {
            const url = s.slice(i + 1, e);
            flush();
            add({ t: 'link', c: [{ t: 'text', v: url }], url: url, bad: false });
            i = e + 1;
            continue;
          }
        }
        buf += ch;
        i++;
        continue;
      }

      if (ch === '@' && (i === 0 || !WORD_BEFORE_MENTION.test(s[i - 1]))) {
        MENTION.lastIndex = i;
        const m = MENTION.exec(s);
        if (m) {
          if (o.mentions.has(m[1].toLowerCase())) { flush(); add({ t: 'mention', v: m[0] }); }
          else buf += m[0];
          i += m[0].length;
          continue;
        }
      }

      buf += ch;
      i++;
    }
    flush();
    processEmphasis(0);
    const all = [];
    for (let x = head.next; x; x = x.next) all.push(x);
    return finish(all);
  }

  // Linked nodes -> clean tree: leftover delimiters become text and neighbouring text merges.
  // (Bare URLs are links already: the scan takes them whole, see bareUrl.)
  function finish(arr) {
    const out = [];
    for (const n of arr) {
      let m;
      if (n.t === 'delim') {
        if (!n.len) continue;
        m = { t: 'text', v: n.ch.repeat(n.len) };
      } else if (n.t === 'group') {
        for (const g of finish(n.c)) push(out, g);
        continue;
      } else if (n.t === 'strong' || n.t === 'em') {
        m = { t: n.t, c: finish(n.c) };
      } else if (n.t === 'link') {
        m = { t: 'link', c: finish(n.c), url: n.url, bad: n.bad };
      } else {
        m = { t: n.t, v: n.v };
      }
      push(out, m);
    }
    return out;
  }

  function push(out, m) {
    const last = out[out.length - 1];
    if (m.t === 'text' && last && last.t === 'text') last.v += m.v;
    else out.push(m);
  }

  function plainText(nodes) {
    let out = '';
    for (const n of nodes) {
      if (n.t === 'br') out += '\n';
      else if (n.c) out += plainText(n.c);
      else out += n.v;
    }
    return out;
  }

  // ------------------------------------------------------------------ links (the one path)

  function safeUrl(raw, localHost) {
    let u;
    try { u = new URL(String(raw).trim()); } catch (e) { return { ok: false, why: 'relative' }; }
    if (!(u.protocol === 'http:' || u.protocol === 'https:')) return { ok: false, why: 'scheme' };
    // userinfo (https://github.com@evil.example/): the text before '@' reads like a host, in the
    // link text and in the md-url span, while the browser goes to the host after it. Refused.
    if (u.username || u.password) return { ok: false, why: 'credentials' };
    const h = u.hostname.toLowerCase().replace(/\.$/, '');
    if (!h) return { ok: false, why: 'invalid' };
    if (h === 'localhost' || h.endsWith('.localhost') || h === '[::1]' || h === '0.0.0.0' ||
        /^127\./.test(h) || (localHost && h === String(localHost).toLowerCase())) return { ok: false, why: 'local' };
    // Beyond the literal list: the unspecified IPv6 address and IPv4-mapped loopback.
    if (h === '[::]' || h.startsWith('[::ffff:7f')) return { ok: false, why: 'local' };
    return { ok: true, href: u.href };  // normalized: punycode hosts are shown as xn--
  }

  // The only place a link target is ever set. `visible` is the link's plain text; the real URL
  // is shown after it unless the two already read the same.
  function mdLink(textNodes, raw, opts, visible) {
    const v = safeUrl(raw, opts.localHost);
    if (!v.ok) return blocked(textNodes, v.why);
    const a = document.createElement('a');
    a.className = 'md-link';
    a.href = v.href;
    a.rel = 'noopener noreferrer nofollow';
    a.target = '_blank';
    a.referrerPolicy = 'no-referrer';
    a.title = v.href;
    for (const t of textNodes) a.append(t);
    const out = [a, ext()];
    if (visible !== v.href && visible + '/' !== v.href) out.push(realUrl(v.href));
    return out;
  }

  function blocked(textNodes, why) {
    const title = why === 'local' ? TITLE_LOCAL : why === 'credentials' ? TITLE_CREDS : TITLE_SCHEME;
    const span = el('span', 'md-blocked');
    span.title = title;
    for (const t of textNodes) span.append(t);
    const pill = el('span', 'md-blocked-pill');
    pill.title = title;
    pill.textContent = 'link blocked';
    return [span, pill];
  }

  function ext() {
    const s = el('span', 'md-ext');
    s.setAttribute('aria-hidden', 'true');
    s.textContent = '↗';
    return s;
  }

  function realUrl(href) {
    const s = el('span', 'md-url');
    s.textContent = href;
    return s;
  }

  // ------------------------------------------------------------------ tree -> DOM

  function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }

  function appendAll(parent, nodes) {
    for (const x of nodes) parent.append(x);
    return parent;
  }

  // Inline tree -> array of DOM nodes. Inside a link, a nested link keeps only its text.
  function inlineDom(nodes, o, inLink) {
    const out = [];
    for (const n of nodes) {
      if (n.t === 'text') out.push(document.createTextNode(n.v));
      else if (n.t === 'br') out.push(el('br'));
      else if (n.t === 'code') { const c = el('code', 'md-code'); c.textContent = n.v; out.push(c); }
      else if (n.t === 'mention') { const m = el('span', 'md-mention'); m.textContent = n.v; out.push(m); }
      else if (n.t === 'strong' || n.t === 'em') out.push(appendAll(el(n.t), inlineDom(n.c, o, inLink)));
      else if (n.t === 'link') {
        const kids = inlineDom(n.c, o, true);
        if (inLink) { out.push.apply(out, kids); continue; }
        if (n.bad) {
          const v = safeUrl(n.url, o.localHost);
          out.push.apply(out, blocked(kids, v.ok ? 'invalid' : v.why));
        } else {
          out.push.apply(out, mdLink(kids, n.url, o, plainText(n.c)));
        }
      }
    }
    return out;
  }

  function inline(text, o) { return inlineDom(parseInline(text, o), o, false); }

  function blockDom(b, o) {
    switch (b.t) {
      case 'p':
        return appendAll(el('p', 'md-p'), inline(b.text, o));
      case 'h': {
        const h = el('div', 'md-h md-h' + Math.min(b.level, 3));
        h.setAttribute('role', 'heading');
        h.setAttribute('aria-level', String(Math.min(6, b.level + 2)));
        return appendAll(h, inline(b.text, o));
      }
      case 'hr':
        return el('hr', 'md-hr');
      case 'code':
        return codeBlock(b);
      case 'quote':
        return appendAll(el('blockquote', 'md-quote'), b.c.map(function (x) { return blockDom(x, o); }));
      case 'ul':
      case 'ol': {
        const list = el(b.t, b.t === 'ul' ? 'md-ul' : 'md-ol');
        if (b.t === 'ol' && b.start !== 1) list.start = b.start;
        for (const blocks of b.items) {
          const li = el('li');
          blocks.forEach(function (x, k) {
            // a tight item: its leading paragraph sits directly in the <li>
            if (k === 0 && x.t === 'p') appendAll(li, inline(x.text, o));
            else li.append(blockDom(x, o));
          });
          list.append(li);
        }
        return list;
      }
      case 'table':
        return tableDom(b, o);
      default:
        return appendAll(el('p', 'md-p'), [document.createTextNode('')]);
    }
  }

  function codeBlock(b) {
    const wrap = el('div', 'md-pre');
    const head = el('div', 'md-pre-head');
    const lang = el('span', 'md-lang');
    lang.textContent = b.lang;
    const btn = el('button', 'md-copy');
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code');
    btn.addEventListener('click', function () {
      if (typeof navigator === 'undefined' || !navigator.clipboard) return;
      navigator.clipboard.writeText(b.text).then(function () {
        btn.textContent = 'Copied';
        setTimeout(function () { btn.textContent = 'Copy'; }, 1600);
      }, function () {});
    });
    head.append(lang);
    // a ```mermaid block can also be shown as its diagram, on request (diagram.js, issue #57)
    const sb = window.SBDiagram;
    if (b.lang.toLowerCase() === 'mermaid' && sb && typeof sb.toggle === 'function') {
      const show = el('button', 'md-copy md-show-diagram');
      show.type = 'button';
      show.textContent = 'Show diagram';  // "Show code" while the diagram is on show
      show.addEventListener('click', function () { sb.toggle(wrap, b.text, show); });
      head.append(show);
    }
    head.append(btn);
    const pre = el('pre', 'md-pre-body');
    pre.tabIndex = 0;
    const code = el('code');
    code.textContent = b.text;
    pre.append(code);
    wrap.append(head, pre);
    return wrap;
  }

  function tableDom(b, o) {
    const wrap = el('div', 'md-table-wrap');
    wrap.tabIndex = 0;
    const table = el('table', 'md-table');
    const thead = el('thead');
    const hr = el('tr');
    b.head.forEach(function (text, k) {
      const th = el('th', b.aligns[k] ? 'md-al-' + b.aligns[k] : '');
      th.setAttribute('scope', 'col');
      hr.append(appendAll(th, inline(text, o)));
    });
    thead.append(hr);
    table.append(thead);
    if (b.rows.length) {
      const tbody = el('tbody');
      for (const row of b.rows) {
        const tr = el('tr');
        row.forEach(function (text, k) {
          tr.append(appendAll(el('td', b.aligns[k] ? 'md-al-' + b.aligns[k] : ''), inline(text, o)));
        });
        tbody.append(tr);
      }
      table.append(tbody);
    }
    wrap.append(table);
    return wrap;
  }

  // ------------------------------------------------------------------ API

  function plainParagraph(src) {
    const frag = document.createDocumentFragment();
    const p = el('p', 'md-p');
    p.append(document.createTextNode(src));
    frag.append(p);
    return frag;
  }

  function render(text, opts) {
    const src = String(text == null ? '' : text);
    if (src.length > MAX_INPUT) return plainParagraph(src);
    const list = opts && Array.isArray(opts.mentions) ? opts.mentions : [];
    const o = {
      mentions: new Set(list.map(function (x) { return String(x).toLowerCase(); })),
      localHost: (opts && opts.localHost) || '',
    };
    try {
      const blocks = parseBlocks(src.replace(/\r\n?/g, '\n').split('\n'), 0);
      const frag = document.createDocumentFragment();
      for (const b of blocks) frag.append(blockDom(b, o));
      return frag;
    } catch (e) {
      return plainParagraph(src);
    }
  }

  const NO_OPTS = { mentions: new Set(), localHost: '' };

  function firstLine(text, max) {
    const src = String(text == null ? '' : text).slice(0, MAX_INPUT).replace(/\r\n?/g, '\n');
    let out = '';
    for (const line of src.split('\n')) {
      let t = trimST(line);
      if (!t || isThematicBreak(t) || fenceOpen(t)) continue;
      for (let g = 0; g < 16; g++) {  // peel block markers: "> ", "## ", "- ", "1. "
        const q = quoteStrip(t);
        if (q !== null) { t = trimST(q); continue; }
        const h = atxHeading(t);
        if (h) { t = h.text; continue; }
        const li = listItem(t);
        if (li) { t = li.content; continue; }
        break;
      }
      try { out = plainText(parseInline(t, NO_OPTS)); } catch (e) { out = t; }
      out = out.replace(/\s+/g, ' ').trim();
      if (out) break;
    }
    const cps = Array.from(out);
    if (max > 0 && cps.length > max) out = cps.slice(0, Math.max(0, max - 1)).join('').trim() + '…';
    return out;
  }

  window.SBMarkdown = Object.freeze({ render: render, firstLine: firstLine, safeUrl: safeUrl });
})();
