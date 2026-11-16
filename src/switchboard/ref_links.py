"""Reference links: ``!42`` or ``ABC-123`` becomes a link, from an admin's rules (DESIGN.md §60).

An admin sets up to ``MAX_RULES`` pairs in Settings: a pattern and a URL template, for example ``!(\\d+)`` and
``https://gitlab.example.com/group/project/-/merge_requests/$1``. The page (``md.js``) finds each pattern in a
message and builds the link with ``createElement``, the way it builds every link. With no rules nothing
changes. A room message can never set them: the only door is an admin's ``PUT /api/settings/links``.

The rules are text from outside, so what may be saved is narrow, and checked here, on the server, before
it is stored:

- the template is ``https://`` with a fixed host (no ``$1`` and no credentials in it), of plain URL
  characters, and only ``$1`` is replaced, by the matched text, URL-encoded by the page;
- the pattern is a small, linear subset of regular expressions that means the same in Python and in a
  browser: literals, ``\\d``, ``\\w``, plain classes such as ``[A-Z0-9]``, bounded counts, and exactly
  one group, with no alternation, no group inside a group and no quantifier on a group. That keeps
  matching from backtracking catastrophically in a browser, which can't be told to stop. It is also
  compiled, and tried on long input against a time limit, and refused if either fails.

Pure functions, plus ``load`` and ``save``, which keep each rule as one ``broker_settings`` row
(``store.py`` has the SQL).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from switchboard.store import Store

MAX_RULES = 8
PATTERN_MAX = 60
TEMPLATE_MAX = 100
ROW_MAX = 200  # a broker_settings value (schema): the rule's stored JSON must fit
PREFIX = "ref_link."  # the keys: ref_link.0 … ref_link.7
UNBOUNDED_MAX = 3  # `+`, `*` and `{n,}`: each one multiplies the work a failed match can cost
QUANTIFIERS_MAX = 6
COUNT_MAX = 64  # the largest bound in `{n,m}`
PROBE_LEN = 300
PROBE_LIMIT_S = 0.05  # a refused pattern: one try on a probe took longer than this

# characters that mean themselves outside a class, with no escape: anything else special is escaped or refused
LITERALS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#@_-/:%&=~,")
ESCAPABLE = frozenset(".!#$&*+-/:;<=>?@^_|~(){}[]\\%,")
CLASS_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*")
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


class RuleError(ValueError):
    """A rule that may not be saved; the message says why, for the admin."""


@dataclass(frozen=True)
class Rule:
    pattern: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"pattern": self.pattern, "url": self.url}


# ----------------------------------------------------------------------------- the template
def check_template(template: object) -> str:
    """The template, or ``RuleError``: ``https://host/path…$1…``, with the host fixed."""
    if not isinstance(template, str) or not template:
        raise RuleError("the URL needs to be text, like https://example.com/issues/$1")
    if len(template) > TEMPLATE_MAX:
        raise RuleError(f"the URL is longer than {TEMPLATE_MAX} characters")
    if not template.startswith("https://"):
        raise RuleError("the URL has to start with https://")
    if URL_CHARS.fullmatch(template) is None:
        raise RuleError("the URL has a character that is not allowed in a link (no spaces, quotes or <>)")
    stripped = template.replace("$1", "")
    if "$" in stripped:
        raise RuleError("only $1 is replaced; there is no other $ in a URL here")
    count = template.count("$1")
    if count == 0:
        raise RuleError("the URL has to contain $1, where the matched text goes")
    if count > 3:
        raise RuleError("the URL may contain $1 at most three times")
    parts = urlsplit(template)
    host = parts.netloc
    if "$" in host or "@" in host or not host:
        raise RuleError("the host can't contain $1 or a user name: only the path and query take the match")
    try:
        parts.port  # noqa: B018  (a bad port raises)
    except ValueError:
        raise RuleError("the URL's port is not a number") from None
    name = (parts.hostname or "").lower()
    if HOST_RE.fullmatch(name) is None or len(name) > 253:
        raise RuleError("the URL's host is not a plain host name like gitlab.example.com")
    return template


# ----------------------------------------------------------------------------- the pattern
def _class_end(p: str, i: int) -> int:
    """Index just past the ``[...]`` that opens at ``p[i]``: plain characters and ``a-z`` ranges."""
    j = i + 1
    items = 0
    while j < len(p) and p[j] != "]":
        c = p[j]
        if c not in CLASS_CHARS:
            raise RuleError("a [class] holds only letters, digits, _ and ranges like A-Z")
        if j + 2 < len(p) and p[j + 1] == "-" and p[j + 2] != "]":
            hi = p[j + 2]
            if hi not in CLASS_CHARS or hi < c:
                raise RuleError("a range in a [class] is low-to-high, like A-Z")
            j += 3
        else:
            j += 1
        items += 1
    if j >= len(p) or items == 0:
        raise RuleError("a [class] needs at least one item and a closing ]")
    return j + 1


def _quantifier_end(p: str, i: int) -> tuple[int, bool] | None:
    """If a quantifier starts at ``p[i]``: (index past it, whether it has no upper bound)."""
    if i >= len(p):
        return None
    c = p[i]
    if c in "+*":
        return i + 1, True
    if c == "?":
        return i + 1, False
    if c == "{":
        m = re.compile(r"\{([0-9]{1,2})(,([0-9]{0,2}))?\}").match(p, i)
        if m is None:
            raise RuleError("a count is {n}, {n,m} or {n,}, with numbers up to 64")
        lo = int(m.group(1))
        hi_text = m.group(3)
        unbounded = m.group(2) is not None and hi_text == ""
        hi = int(hi_text) if hi_text else lo
        if lo > COUNT_MAX or hi > COUNT_MAX or hi < lo:
            raise RuleError(f"a count is at most {COUNT_MAX}, low to high")
        return m.end(), unbounded
    return None


def check_pattern(pattern: object) -> str:
    """The pattern, or ``RuleError``. See the module's description of what is allowed and why."""
    if not isinstance(pattern, str) or not pattern:
        raise RuleError("the pattern needs to be text, like !(\\d+)")
    if len(pattern) > PATTERN_MAX:
        raise RuleError(f"the pattern is longer than {PATTERN_MAX} characters")
    if not pattern.isascii() or not pattern.isprintable():
        raise RuleError("the pattern is plain ASCII, with no control characters")
    i, groups, in_group, atoms_in_group = 0, 0, False, 0
    quantifiers = unbounded = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "(":
            if groups:
                raise RuleError("exactly one ( ) group: the part that $1 stands for")
            if pattern[i + 1 : i + 2] == "?":
                raise RuleError("no (?...) here: the one group is a plain ( )")
            groups, in_group = 1, True
            i += 1
            continue
        if c == ")":
            if not in_group or atoms_in_group == 0:
                raise RuleError("a ) has to close the ( group, and the group can't be empty")
            in_group = False
            i += 1
            if i < len(pattern) and pattern[i] in "+*?{":
                raise RuleError("a ( ) group can't be repeated: repeat what is inside it")
            continue
        if c == "\\":
            nxt = pattern[i + 1 : i + 2]
            if nxt not in ("d", "w") and not (nxt and nxt in ESCAPABLE):
                raise RuleError("a backslash goes before d, w or a punctuation mark: \\d \\w \\. \\#")
            i += 2
        elif c == "[":
            i = _class_end(pattern, i)
        elif c in LITERALS:
            i += 1
        elif c in "+*?{":
            raise RuleError("a repeat (+ * ? {n}) follows a letter, \\d, \\w or a [class]")
        else:
            raise RuleError(f"{c!r} is not allowed in a pattern: write \\{c} for the character itself")
        if in_group:
            atoms_in_group += 1
        q = _quantifier_end(pattern, i)
        if q is not None:
            i, open_ended = q
            quantifiers += 1
            unbounded += 1 if open_ended else 0
            if i < len(pattern) and pattern[i] in "+*?{":
                raise RuleError("one repeat per item (no lazy ?, no a+*)")
    if in_group:
        raise RuleError("the ( group is never closed")
    if groups != 1:
        raise RuleError("the pattern needs one ( ) group around what $1 stands for, like !(\\d+)")
    if unbounded > UNBOUNDED_MAX or quantifiers > QUANTIFIERS_MAX:
        raise RuleError("too many repeats (+ * {n,}): at most three open-ended and six in all")
    try:
        compiled = re.compile(pattern, re.ASCII)
    except re.error as e:
        raise RuleError(f"the pattern doesn't compile: {e}") from None
    if compiled.fullmatch("") is not None:
        raise RuleError("the pattern can match nothing at all; it needs at least one character")
    _try_it(compiled)
    return pattern


def _try_it(compiled: re.Pattern[str]) -> None:
    """Run the pattern on long, awkward input and refuse one that is slow. The grammar already rules out
    the shapes that backtrack without bound; this is the second lock, and the answer to a pattern that
    does not finish in time."""
    for ch in ("a", "0", "!", "-", "A"):
        probe = (ch * PROBE_LEN) + "~"
        start = time.perf_counter()
        for _m in compiled.finditer(probe):
            pass
        if time.perf_counter() - start > PROBE_LIMIT_S:
            raise RuleError("the pattern takes too long to match: make it simpler")


# ----------------------------------------------------------------------------- the whole list
def check_rules(raw: object) -> list[Rule]:
    """What a ``PUT`` may send: a list of up to ``MAX_RULES`` ``{pattern, url}``, each valid, no two alike."""
    if not isinstance(raw, list):
        raise RuleError("rules is a list")
    if len(raw) > MAX_RULES:
        raise RuleError(f"at most {MAX_RULES} rules")
    out: list[Rule] = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict) or set(item) != {"pattern", "url"}:
            raise RuleError(f"rule {n} is a pattern and a url, and nothing else")
        try:
            rule = Rule(check_pattern(item["pattern"]), check_template(item["url"]))
        except RuleError as e:
            raise RuleError(f"rule {n}: {e}") from None
        if len(_encode(rule)) > ROW_MAX:
            raise RuleError(f"rule {n}: the pattern and the URL together are too long to keep")
        if any(r.pattern == rule.pattern for r in out):
            raise RuleError(f"rule {n}: the same pattern is used twice")
        out.append(rule)
    return out


def _encode(rule: Rule) -> str:
    return json.dumps([rule.pattern, rule.url], separators=(",", ":"), ensure_ascii=True)


def _decode(raw: str) -> Rule | None:
    """A stored row as a rule, or None for anything that doesn't pass the checks again: what is on disk is
    trusted no more than what arrives."""
    try:
        got: Any = json.loads(raw)
        if isinstance(got, list) and len(got) == 2:
            return Rule(check_pattern(got[0]), check_template(got[1]))
    except (ValueError, RuleError):
        pass
    return None


def load(store: Store) -> list[Rule]:
    rules = []
    for _key, raw in store.settings_with_prefix(PREFIX):
        rule = _decode(raw)
        if rule is not None:
            rules.append(rule)
    return rules[:MAX_RULES]


def save(store: Store, raw: object) -> list[Rule]:
    """Check ``raw`` and replace the stored rules with it, all or nothing. Returns what is now kept."""
    rules = check_rules(raw)
    store.settings_replace_prefix(PREFIX, {f"{PREFIX}{n}": _encode(r) for n, r in enumerate(rules)})
    return rules
