"""A review board in a room: findings and questions about one pull request (DESIGN.md §37).

Pure policy, no I/O: what an item is, the moves an agent or a person may make on it, when a
board is settled, and how a board reads as text. The store keeps the rows (``store.py``), the
broker applies the moves (``broker/reviews.py``). switchboard never fetches the pull request,
judges a finding, holds a token or posts anything: the agents and the people do (§37.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# Lengths, cut at the boundary: everything here comes from an agent or a person.
MAX_URL = 500
MAX_TITLE = 200
MAX_DETAIL = 4000
MAX_FILE = 300
MAX_LINES = 40
MAX_REASON = 500
MAX_OPTION = 200
MAX_OPTIONS = 4
MAX_ITEMS = 200  # per review: a board, not a log

HEAD_RE = re.compile(r"^[0-9a-f]{7,64}$")
LINES_RE = re.compile(r"^[1-9][0-9]{0,6}(-[1-9][0-9]{0,6})?$")
URL_RE = re.compile(r"^https?://[^\s<>\"']+$")
# The forge a board's URL names, from its path (forge()): a GitHub pull request, or a GitLab
# merge request on any host (the "/-/" segment is GitLab's). A name is plain ASCII, never
# percent-encoded, and never only dots.
_NAME = r"[A-Za-z0-9_.-]{1,100}"
GITHUB_PR_RE = re.compile(rf"^/({_NAME})/({_NAME})/pull/([1-9][0-9]{{0,8}})(?:/[^?#]*)?$")
GITLAB_MR_RE = re.compile(
    rf"^/((?:{_NAME}/){{1,20}}{_NAME})/-/merge_requests/([1-9][0-9]{{0,8}})(?:/[^?#]*)?$"
)

KINDS = ("finding", "question")
FINDING_STATES = ("raised", "contested", "conceded", "fixed", "dropped")
QUESTION_STATES = ("open", "answered", "dropped")
# a finding still needs work in these; a question in "open"
OPEN_FINDING = frozenset({"raised", "contested", "conceded"})

# An agent's moves on a finding, from -> to. A person may also rule on a contested finding
# (to conceded, with an owner, or to dropped) and drop anything; only a person answers.
AGENT_MOVES = {
    "concede": (frozenset({"raised", "contested"}), "conceded"),
    "contest": (frozenset({"raised"}), "contested"),
    "fix": (frozenset({"conceded"}), "fixed"),
    "drop": (frozenset({"raised", "contested", "conceded"}), "dropped"),
}
PERSON_MOVES = {
    "concede": (frozenset({"raised", "contested"}), "conceded"),
    "drop": (frozenset({"raised", "contested", "conceded", "open"}), "dropped"),
    "answer": (frozenset({"open"}), "answered"),
}


class ReviewError(ValueError):
    """A move that isn't allowed, or input that doesn't fit. The message is for the caller."""


@dataclass(frozen=True)
class Item:
    id: int
    n: int  # its number on the board: F<n> for a finding, Q<n> for a question
    kind: str
    state: str
    title: str
    detail: str
    file: str
    lines: str
    raised_by: str  # screen name
    owner: str  # screen name, "" when none
    commit: str
    reason: str
    options: tuple[str, ...]
    recommend: int | None  # index into options
    answer: str
    answered_by: str

    @property
    def label(self) -> str:
        return ("F" if self.kind == "finding" else "Q") + str(self.n)

    @property
    def needs_person(self) -> bool:
        return (self.kind == "question" and self.state == "open") or self.state == "contested"


def text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    """A string from outside: stripped, and refused (not cut) when it's too long."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ReviewError(f"{what} must be a string")
    value = value.strip()
    if required and not value:
        raise ReviewError(f"{what} is required")
    if len(value) > limit:
        raise ReviewError(f"{what} is too long ({len(value)} > {limit} characters)")
    return value


def url(value: Any) -> str:
    u = text(value, "url", MAX_URL, required=True)
    if not URL_RE.match(u):
        raise ReviewError("url must be an http(s) link to the pull request or merge request")
    return u


def head(value: Any) -> str:
    h = text(value, "head", 64).lower()
    if h and not HEAD_RE.match(h):
        raise ReviewError("head must be a commit hash (7 to 64 hex digits)")
    return h


def lines(value: Any) -> str:
    v = text(value, "lines", MAX_LINES)
    if v and not LINES_RE.match(v):
        raise ReviewError('lines must be a line or a range, like "42" or "40-58"')
    if "-" in v:
        a, b = (int(x) for x in v.split("-"))
        if b < a:
            raise ReviewError("lines must go from the lower number to the higher")
    return v


def options(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_OPTIONS:
        raise ReviewError(f"options must be a list of 2 to {MAX_OPTIONS} choices")
    out = tuple(text(o, "an option", MAX_OPTION, required=True) for o in value)
    if len(set(out)) != len(out):
        raise ReviewError("options must differ")
    return out


def recommend(value: Any, opts: tuple[str, ...]) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value < len(opts):
        raise ReviewError(f"recommend must be an option's index, 0 to {len(opts) - 1}")
    return value


def forge(value: str) -> dict[str, Any] | None:
    """The pull request or merge request a board's URL names, from the URL's shape alone (it is
    never fetched, §37.1): ``{"kind": "github" | "gitlab", "repo", "number"}``. None for anything
    else, which the page shows as the URL itself, with no mark (#247)."""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a port that isn't a number
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if host in ("github.com", "www.github.com"):
        m = GITHUB_PR_RE.match(parts.path)
        kind, repo, number = ("github", f"{m[1]}/{m[2]}", m[3]) if m else ("", "", "")
    else:
        m = GITLAB_MR_RE.match(parts.path)
        kind, repo, number = ("gitlab", m[1], m[2]) if m else ("", "", "")
    if not kind or any(set(seg) == {"."} for seg in repo.split("/")):
        return None
    return {"kind": kind, "repo": repo, "number": int(number)}


def parse_label(value: Any) -> tuple[str, int]:
    """'F3' -> ('finding', 3); 'q1' -> ('question', 1)."""
    m = re.fullmatch(r"([FfQq])([1-9][0-9]{0,3})", value.strip()) if isinstance(value, str) else None
    if m is None:
        raise ReviewError('item must be a board label, like "F3" or "Q1"')
    return ("finding" if m.group(1) in "Ff" else "question"), int(m.group(2))


def agent_move(item: Item, move: str, actor: str) -> str:
    """The state an agent's ``move`` takes ``item`` to, or ReviewError. Rules beyond the
    table: a question is a person's to answer or drop; whoever raised a finding can't concede
    or contest it (that's the other side's call), only drop it; only its owner marks it fixed."""
    if move not in AGENT_MOVES:
        raise ReviewError(f"unknown move {move!r}")
    if item.kind == "question":
        raise ReviewError(f"{item.label} is a question: only a person answers or drops it")
    frm, to = AGENT_MOVES[move]
    if item.state not in frm:
        raise ReviewError(f"{item.label} is {item.state}: it can't be {to} from there")
    if move in ("concede", "contest") and actor == item.raised_by:
        raise ReviewError(f"{item.label} is yours: you can drop it, not {move} it")
    if move == "fix" and actor != item.owner:
        raise ReviewError(f"{item.label} is {item.owner}'s to fix")
    if move == "drop" and actor != item.raised_by:
        raise ReviewError(f"only {item.raised_by}, who raised {item.label}, or a person can drop it")
    return to


def person_move(item: Item, move: str) -> str:
    if move not in PERSON_MOVES:
        raise ReviewError(f"unknown move {move!r}")
    frm, to = PERSON_MOVES[move]
    if (move == "answer") != (item.kind == "question") and move != "drop":
        raise ReviewError(f"{item.label} is a {item.kind}: it can't be {to}")
    if item.state not in frm:
        raise ReviewError(f"{item.label} is {item.state}: it can't be {to} from there")
    return to


def settled(items: list[Item]) -> bool:
    """Nothing left to do: every finding fixed or dropped, every question answered or dropped."""
    return not any(i.state in OPEN_FINDING or i.state == "open" for i in items)


def counts(items: list[Item]) -> dict[str, int]:
    out = {"needs_person": 0, "open": 0, "fixed": 0, "dropped": 0, "answered": 0}
    for i in items:
        if i.needs_person:
            out["needs_person"] += 1
        if i.state in OPEN_FINDING or i.state == "open":
            out["open"] += 1
        elif i.state in ("fixed", "dropped", "answered"):
            out[i.state] += 1
    return out


def render(review: dict[str, Any], items: list[Item]) -> str:
    """The board as an agent reads it: one line per item, open work first. Short on purpose:
    the board replaces a wall of text in the room, it mustn't become one."""
    head_s = f" at {review['head'][:12]}" if review.get("head") else ""
    state = "settled" if settled(items) else "open"
    if review.get("posted_by"):
        state = f"posted by {review['posted_by']}: post your items now, once"
    out = [f"Review of {review['url']}{head_s}: {state}"]
    order = {
        s: n
        for n, s in enumerate(("contested", "open", "raised", "conceded", "answered", "fixed", "dropped"))
    }
    for i in sorted(items, key=lambda i: (order.get(i.state, 9), i.kind, i.n)):
        where = f" ({i.file}{':' + i.lines if i.lines else ''})" if i.file else ""
        who = {
            "raised": f"raised by {i.raised_by}",
            "contested": f"raised by {i.raised_by}, contested: a person decides",
            "conceded": f"owner {i.owner}",
            "fixed": f"fixed by {i.owner}" + (f" in {i.commit[:12]}" if i.commit else ""),
            "dropped": "dropped" + (f": {i.reason}" if i.reason else ""),
            "open": "for a person: "
            + " | ".join(
                f"{n}. {o}{' (recommended)' if n == i.recommend else ''}" for n, o in enumerate(i.options)
            ),
            "answered": f"answered by {i.answered_by}: {i.answer}",
        }[i.state]
        out.append(f"{i.label} [{i.state}] {i.title}{where}: {who}")
    return "\n".join(out)


def post_plan(items: list[Item]) -> dict[str, list[Item]]:
    """What each agent posts to the pull request once the board is settled (§37.6): a fixed
    finding goes to its owner, an answered question to whoever asked it. Dropped items are never
    posted. Owners in the order they first appear; items in board order."""
    plan: dict[str, list[Item]] = {}
    for i in sorted(items, key=lambda i: (i.kind, i.n)):
        if i.state == "fixed" and i.owner:
            plan.setdefault(i.owner, []).append(i)
        elif i.state == "answered":
            plan.setdefault(i.raised_by, []).append(i)
    return plan


def _post_line(i: Item) -> str:
    """An item in the Post message: its label and what switchboard checked (a commit's hex, an
    option's number), never its title or an option's text, which are an agent's (#177)."""
    if i.kind == "question":
        options = [n for n, o in enumerate(i.options) if o == i.answer]
        return f"{i.label} (option {options[0]})" if options else i.label
    return i.label + (f" (fixed in {i.commit[:12]})" if i.commit else "")


def render_post(plan: dict[str, list[Item]], limit: int) -> str:
    """The one message a person's Post sends: it @mentions each owner (so each is woken once)
    and lists the labels of what that owner posts, in its own name, with its own gh or glab.
    It carries no agent's text: not a title, an option or the pull request's URL. It goes out
    as the person's own message, so anything an agent wrote in it would speak for them (#177);
    the agents read those from the board itself, as other agents' text."""
    lines = [
        "The review board in this room is settled. Post your items to its pull request now, once,"
        " in your own name with your own gh or glab: one review comment per item, on its lines"
        ' where it has them. Dropped items aren\'t posted. review(action="show") has each item.'
    ]
    for owner, items in plan.items():
        lines.append(f"- @{owner}: " + ", ".join(_post_line(i) for i in items))
    text = "\n".join(lines)
    if len(text) > limit:
        raise ReviewError("the board is too big to post in one message: drop or close some items first")
    return text
