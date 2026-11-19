"""A review board in a room: findings and questions about one pull request (DESIGN.md §37).

Pure policy, no I/O: what an item is, the moves an agent or a person may make on it, when a
board is settled, what the agents' picks among an item's options add up to (§37.12), who may close
a board (§37.14), and how a board reads as text. The store keeps the rows (``store.py``), the
broker applies the moves (``broker/reviews.py``). switchboard never fetches the pull request,
judges a finding, holds a token or posts anything: the agents and the people do (§37.1).

Every string a board takes from outside is read through ``text`` (``url``, ``proof``, ``head``, ``where``
and ``options`` call it too), which cuts obvious secrets out with ``say``'s patterns (``redact``, §11,
#890), so a card, a move's event, a notice and a line posted in the room never hold one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from switchboard import envelope
from switchboard.redact import redact

# Lengths, cut at the boundary: everything here comes from an agent or a person.
MAX_URL = 500
MAX_TITLE = 200
MAX_DETAIL = 4000
MAX_FILE = 300
MAX_LINES = 40
MAX_REASON = 500
MAX_TICKET = 200  # a follow-up ticket's key or link, for a deferred finding (#371)
MAX_PROOF = 500
MAX_OPTION = 200
MAX_OPTIONS = 4
MAX_ITEMS = 200  # per review: a board, not a log
MAX_OPEN_BOARDS = 8  # open boards in one room (§37.14): a room holds several, not any number
MAX_LISTED = 50  # boards `list` names, open ones first
FIX_CONFIRM_TIMEOUT_S = 24 * 60 * 60

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
# (to conceded, with an owner, or to dropped) and drop anything; only a person answers. A contest
# goes back to its raiser, who drops the finding or insists on it (#424): ``insist`` leaves the
# finding contested, with the raiser's reason, and that is what puts it in front of a person.
AGENT_MOVES: dict[str, tuple[frozenset[str], str | None]] = {
    # from conceded: a handover by its owner, or a takeover once the owner has left (#371)
    "concede": (frozenset({"raised", "contested", "conceded"}), "conceded"),
    "contest": (frozenset({"raised"}), "contested"),
    "insist": (frozenset({"contested"}), "contested"),
    "fix": (frozenset({"conceded", "fixed"}), "fixed"),
    "reopen": (frozenset({"fixed"}), "conceded"),
    "drop": (frozenset({"raised", "contested", "conceded"}), "dropped"),
    "confirm": (frozenset({"fixed"}), "fixed"),
    # None keeps the state: only review responsibility changes (#784).
    "take": (frozenset({"raised", "contested", "conceded", "fixed"}), None),
    # to a follow-up ticket: settled for this pull request, stored as dropped with the ticket (#371)
    "defer": (frozenset({"raised", "contested", "conceded"}), "dropped"),
}
PERSON_MOVES = {
    # from conceded: a person reassigns its owner (#371)
    "concede": (frozenset({"raised", "contested", "conceded"}), "conceded"),
    "drop": (frozenset({"raised", "contested", "conceded", "open"}), "dropped"),
    "answer": (frozenset({"open"}), "answered"),
    # a person's ruling that it's fixed, and so settled: confirmed by them (#371)
    "fix": (frozenset({"conceded"}), "fixed"),
    "defer": (frozenset({"raised", "contested", "conceded"}), "dropped"),
}


class ReviewError(ValueError):
    """A move that isn't allowed, or input that doesn't fit. The message is for the caller."""


class ProofRequired(ReviewError):
    """A fix came without its proof (#251). Its own class so the agent service can tell a session
    whose ``review`` tool has no ``proof`` field to update, not to send one (#340)."""


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
    raised_by: str  # screen name; the responsible reviewer after a takeover (#784)
    owner: str  # screen name, "" when none
    commit: str
    reason: str
    options: tuple[str, ...]
    recommend: int | None  # index into options
    answer: str
    answered_by: str
    created_at: float = 0.0
    updated_at: float = 0.0
    proof: str = ""
    confirmed_by: str = ""
    outdated_head: str = ""
    # a dropped finding deferred to this follow-up ticket (#371): settled, but not withdrawn
    deferred_to: str = ""
    # the picks agents made with ``concur`` (#423), oldest first: (screen name, option index).
    # The asker's own ``recommend`` is their pick too, and is not repeated here (``effective_picks``).
    picks: tuple[tuple[str, int], ...] = ()
    # agents who recorded no view with ``abstain`` (#785), oldest first. A later pick is not listed:
    # the store keeps whichever of the two moves is later. While a name is here, that agent's pick
    # does not count (``effective_picks``).
    abstentions: tuple[str, ...] = ()
    # the raiser's reason for insisting on a contested finding (#424), empty until it does: both
    # sides have then made their case once, and a person decides
    insisted: str = ""

    @property
    def label(self) -> str:
        return ("F" if self.kind == "finding" else "Q") + str(self.n)

    @property
    def needs_person(self) -> bool:
        """Whether a person must act, from the item alone: a contested finding counts, since nothing
        here says its raiser can still answer. ``needs_person()`` (below) knows who is in the room
        and lets a contested finding stay with its raiser (#424)."""
        return (
            (self.kind == "question" and self.state == "open")
            or self.state == "contested"
            or (self.state == "fixed" and not self.confirmed_by and self.raised_by == self.owner)
        )


def _string(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    """A string from outside: stripped, and refused (not cut) when it's too long. Not yet redacted."""
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


def _redacted(value: str, what: str, limit: int) -> tuple[str, list[str]]:
    """``value`` with each obvious secret replaced by its marker, and the kinds removed (#890). After the
    length check, so text over the limit is refused before anything searches it, as for ``say``; and the
    limit again after, since a card's column has it as a CHECK (§37.3) and a marker can be a little
    longer than the secret it replaced. That refusal names the kind, never the value."""
    out, kinds = redact(value)
    if len(out) > limit:
        raise ReviewError(
            f"{what} is too long once what looks like a secret ({', '.join(kinds)}) is cut out"
            f" ({len(out)} > {limit} characters): leave the secret out"
        )
    return out, kinds


def text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    """A string from outside: stripped, refused (not cut) when it's too long, and with every obvious
    secret cut out (``redact``, DESIGN.md §11, #890). Every field of a board, an agent's or a person's,
    is read here, so what is stored, shown on the page and posted in the room holds ``[redacted: <kind>]``
    and never the value."""
    return _redacted(_string(value, what, limit, required=required), what, limit)[0]


def _link(what: str, kinds: list[str]) -> None:
    """A link is refused when it held a secret: a marker can't stand in a link, and a board's URL is the
    board's name (§37.14), so a redacted one would name nothing that opens."""
    if kinds:
        raise ReviewError(
            f"{what} holds what looks like a secret ({', '.join(kinds)}): give the link without it"
        )


# user:password@ in front of a host: a password has no token's shape, so the patterns can't see it
_USERINFO_RE = re.compile(r"(?i)\b(https?://)[^\s/@<>\"'?#]*@")


def scrub(value: str) -> str:
    """``value`` with every obvious secret cut out, and the credentials of any http(s) link in it (#890).
    Never refuses and never fails: it is for text that is only being shown or posted, whatever an older
    row holds, and applying it twice changes nothing. What a board takes in goes through ``text`` (which
    refuses what it can't repair); what it says out loud, a notice, a line in the room, an event and a
    board's own URL as shown, goes through here as well, so a row stored before this check, or a value
    copied from one into a new message, is cut too."""
    return _USERINFO_RE.sub(r"\1[redacted: url credentials]@", redact(value)[0])


def lookup_url(value: Any) -> str:
    """A URL given to find a board that exists (a lookup, never a write): a string of the right length,
    not checked for shape or secrets. A board of an older row may hold a URL ``url`` would now refuse, and
    its owner must still be able to open and close it, by the URL as ``scrub`` shows it or as stored."""
    return _string(value, "url", MAX_URL, required=True)


def _no_credentials(what: str, link: str) -> None:
    """A link with ``user:password@`` in it is refused: it would store a password, show it as a link
    and post it (§37.14, #890)."""
    try:
        netloc = urlsplit(link).netloc
    except ValueError:
        raise ReviewError(f"{what} is not a valid http(s) link") from None
    if "@" in netloc:
        raise ReviewError(
            f"{what} must not carry a user name or password (user:password@host): leave them out"
        )


def url(value: Any) -> str:
    u, kinds = _redacted(_string(value, "url", MAX_URL, required=True), "url", MAX_URL)
    _link("url", kinds)
    if not URL_RE.match(u):
        raise ReviewError("url must be an http(s) link to the pull request or merge request")
    _no_credentials("url", u)
    return u


def proof(value: Any) -> str:
    """A test's name or an http(s) proof link; the broker checks shape, not its contents. A name is
    redacted like any text, and a link that held a secret is refused (``_link``)."""
    p, kinds = _redacted(_string(value, "proof", MAX_PROOF), "proof", MAX_PROOF)
    if not p:
        raise ProofRequired("proof is required")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", p):
        _link("proof link", kinds)
        if not URL_RE.fullmatch(p):
            raise ReviewError("proof link must be an http(s) URL, or give the test's name")
        _no_credentials("proof link", p)
    return p


# What an agent reads off the pull request with its own gh or glab and reports (#291, §37.11):
# switchboard holds no token and polls no forge, so these are the reporting agent's claims, shown
# as theirs. A value it doesn't know is refused, never taken for one it does.
PR_STATES = ("open", "merged", "closed")
CI_STATES = ("passing", "failing", "running", "none")
MAX_THREADS = 999


def report(params: dict[str, Any]) -> dict[str, Any]:
    """A report's checked fields (``pr_state``, ``pr_threads``, ``pr_ci``, the ones given), or
    ReviewError: at least one, each from its list or a count from 0 to 999."""
    out: dict[str, Any] = {}
    state = params.get("state")
    if state not in (None, ""):
        if state not in PR_STATES:
            raise ReviewError(f"state must be one of: {', '.join(PR_STATES)}")
        out["pr_state"] = state
    threads = params.get("threads")
    if threads not in (None, -1):
        if type(threads) is not int or not 0 <= threads <= MAX_THREADS:
            raise ReviewError(f"threads must be the count of unresolved threads, 0 to {MAX_THREADS}")
        out["pr_threads"] = threads
    ci = params.get("ci")
    if ci not in (None, ""):
        if ci not in CI_STATES:
            raise ReviewError(f"ci must be one of: {', '.join(CI_STATES)}")
        out["pr_ci"] = ci
    if not out:
        raise ReviewError("report needs state, threads or ci, as gh or glab shows them")
    return out


def gitlab(review: dict[str, Any]) -> bool:
    """Whether the board is on a GitLab merge request, so its lines say MR, GitLab's own word."""
    f = forge(str(review.get("url") or ""))
    return bool(f and f["kind"] == "gitlab")


def report_line(review: dict[str, Any], now: float) -> str:
    """The latest report, as the agent and the page read it: "2 open on the PR, CI failing (per
    claude-1, 3 min ago)", or "on the MR" for a GitLab merge request. Empty when no agent has
    reported."""
    if not review.get("reported_by"):
        return ""
    pr = "MR" if gitlab(review) else "PR"
    bits = []
    if review.get("pr_state") in ("merged", "closed"):
        bits.append(str(review["pr_state"]))
    if review.get("pr_threads") is not None:
        n = int(review["pr_threads"])
        bits.append(f"{n} open on the {pr}" if n else f"no threads open on the {pr}")
    if review.get("pr_ci"):
        bits.append(f"CI {review['pr_ci']}")
    age = envelope.ago(float(review.get("reported_at") or now), now)
    return f"{', '.join(bits) or review.get('pr_state') or 'reported'} (per {review['reported_by']}, {age})"


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


def where(value: Any) -> tuple[str, str]:
    """Split a compact finding location, then apply the existing file and line checks. It is split before
    either part is redacted: a marker has a colon in it, and the last colon is the one before the lines.
    The whole value is searched too (#890): a secret whose pattern reaches across that colon (``"token":``
    and its value) is in neither part, so the parts alone would keep it. A match across the colon is
    refused, so a marker never moves the split."""
    location = _string(value, "where", MAX_FILE + MAX_LINES + 1)
    if not location:
        return "", ""
    path, sep, span = location.rpartition(":")
    whole, kinds = redact(location)
    if sep and whole != redact(path)[0] + sep + redact(span)[0]:
        raise ReviewError(
            f"where holds what looks like a secret ({', '.join(kinds)}) across its colon: leave it out"
        )
    if not sep:
        return text(location, "file", MAX_FILE), ""
    return text(path, "file", MAX_FILE), lines(span)


# the card text an edit may replace (#786), in the order a history line names them
EDIT_FIELDS = ("title", "detail", "file", "lines")


def edit_changes(item: Item, actor: str, params: dict[str, Any], *, by_person: bool) -> dict[str, str]:
    """The text an edit replaces on ``item``, or ReviewError (#786).

    ``params`` may carry ``title``, ``detail`` and ``where`` (file and lines, the same string
    ``raise`` takes). A key that is absent stays as it is. ``where`` replaces file and lines
    together, and an empty ``where`` clears both. Nothing that differs from the card is a refusal,
    so a save of the same text does not become a history line. An agent may edit only a finding
    they raised, and only while it is raised or contested. A person may edit any card: the page
    is how a wrong title gets corrected after the finding has moved on."""
    if not by_person:
        if actor != item.raised_by:
            raise ReviewError(f"only {item.raised_by}, who raised {item.label}, can edit it")
        if item.kind != "finding" or item.state not in ("raised", "contested"):
            raise ReviewError(
                f"{item.label} is {item.state}: only a raised or contested finding can be edited"
            )
    if not any(key in params for key in ("title", "detail", "where")):
        raise ReviewError(f"{item.label} needs a title, detail or where to edit")
    changes: dict[str, str] = {}
    if "title" in params:
        title = text(params.get("title"), "title", MAX_TITLE, required=True)
        if title != item.title:
            changes["title"] = title
    if "detail" in params:
        detail = text(params.get("detail"), "detail", MAX_DETAIL)
        if detail != item.detail:
            changes["detail"] = detail
    if "where" in params:
        file, span = where(params.get("where"))
        if file != item.file:
            changes["file"] = file
        if span != item.lines:
            changes["lines"] = span
    if not changes:
        raise ReviewError(f"{item.label} already says that")
    return changes


def edit_did(fields: Sequence[str]) -> str:
    """How an edit reads on a card's history: the fields it changed, in ``EDIT_FIELDS`` order."""
    names = [name for name in EDIT_FIELDS if name in fields]
    if len(names) == 1:
        return f"edited the {names[0]}"
    return f"edited the {', the '.join(names[:-1])} and the {names[-1]}"


def options(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_OPTIONS:
        raise ReviewError(f"options must be a list of 2 to {MAX_OPTIONS} choices")
    out = tuple(text(o, "an option", MAX_OPTION, required=True) for o in value)
    if len(set(out)) != len(out):
        raise ReviewError("options must differ")
    return out


def recommend(value: Any, opts: tuple[str, ...], *, required: bool = False) -> int | None:
    """An option's index, or None when none is given and none is required. A question and a
    finding's fix options both carry the asker's recommendation (#423): ``required`` refuses a
    call without it, with what to give."""
    if value is None:
        if required:
            raise ReviewError(
                f"recommend is required with options: the index of the one you pick, 0 to {len(opts) - 1}."
                " The other agents agree or pick another with concur"
            )
        return None
    if type(value) is not int or not 0 <= value < len(opts):
        raise ReviewError(f"recommend must be an option's index, 0 to {len(opts) - 1}")
    return value


def names(who: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c': how a note names the agents behind a pick."""
    who = list(who)
    if len(who) < 2:
        return "".join(who)
    return ", ".join(who[:-1]) + " and " + who[-1]


def effective_picks(item: Item) -> dict[str, int]:
    """Every agent's pick on ``item``'s options, in the order they were made, the asker first. The
    asker's ``recommend`` is their pick; a ``concur`` of their own replaces it. A pick that points
    at no option is dropped, so a row out of range never counts for anything. An agent who abstained
    (#785) is left out, including the asker: no view replaces their recommendation until they pick."""
    abstained = set(item.abstentions)
    out: dict[str, int] = {}
    if item.options and item.recommend is not None and item.raised_by and item.raised_by not in abstained:
        out[item.raised_by] = item.recommend
    for agent, option in item.picks:
        if agent not in abstained:
            out[agent] = option
    return {a: o for a, o in out.items() if 0 <= o < len(item.options)}


@dataclass(frozen=True)
class Consensus:
    """What the agents' picks add up to (#423). Computed from the picks and who is in the room now,
    never stored or claimed: ``state`` is "none" (no one here has picked), "proposed" (those who
    picked agree, and others are still to), "agreed" (every agent in the room picked the same
    option) or "split" (two picks differ: no option is recommended). ``order`` lists the options,
    the recommended one first; ``notes`` says, per option (by index), who is behind it.
    ``abstained`` (#785) names agents in the room who recorded no view: they are in neither
    ``waiting`` nor any option's picks."""

    state: str
    option: int | None
    by: tuple[str, ...]
    waiting: tuple[str, ...]
    order: tuple[int, ...]
    notes: tuple[str, ...]
    abstained: tuple[str, ...] = ()


def consensus(item: Item, present: Sequence[str]) -> Consensus:
    """The agreement on ``item``'s options among ``present``, the agents in the room now (the only
    ones who can still agree: a pick by an agent who left is history, and counts for nothing). An
    option is "recommended" only when every one of them who has not abstained has picked it; an
    agent who hasn't picked or abstained is waited on, an abstention is neither (#785), and a
    split names each pick without recommending any."""
    count = len(item.options)
    base = tuple(range(count))
    here = list(dict.fromkeys(present))
    here_set = set(here)
    abstained = tuple(dict.fromkeys(a for a in item.abstentions if a in here_set))
    abstained_set = set(abstained)
    live = {a: o for a, o in effective_picks(item).items() if a in here_set and a not in abstained_set}
    waiting = tuple(a for a in here if a not in live and a not in abstained_set)
    notes = [""] * count
    if not live:
        return Consensus("none", None, (), waiting, base, tuple(notes), abstained)
    chosen = set(live.values())
    if len(chosen) > 1:
        for o in sorted(chosen):
            notes[o] = "picked by " + names([a for a, p in live.items() if p == o])
        return Consensus("split", None, (), waiting, base, tuple(notes), abstained)
    (option,) = chosen
    by = tuple(live)
    if waiting:
        notes[option] = names(by) + (" recommends this" if len(by) == 1 else " recommend this")
    else:
        notes[option] = "recommended by " + names(by)
    order = (option, *(o for o in base if o != option))
    return Consensus(
        "agreed" if not waiting else "proposed", option, by, waiting, order, tuple(notes), abstained
    )


def concur(item: Item, actor: str, value: Any, here: Sequence[str]) -> int:
    """The option index ``actor`` picks with ``concur``, or ReviewError. Only an agent in the room
    picks (the board's agents are the room's, §37.12); only an item with options that is still being
    decided: an open question, or a finding not yet fixed or dropped; and only an index in range.
    An agent may pick again until then, which replaces their pick."""
    if actor not in here:
        raise ReviewError(f"only an agent in the room can concur: {actor!r} isn't")
    if not item.options:
        raise ReviewError(f"{item.label} has no options to pick")
    if item.state != "open" and item.state not in OPEN_FINDING:
        raise ReviewError(f"{item.label} is {item.state}: its options are settled")
    if type(value) is not int or not 0 <= value < len(item.options):
        raise ReviewError(
            f"concur needs recommend, the index of the option you pick, 0 to {len(item.options) - 1}"
        )
    return value


def abstain(item: Item, actor: str, here: Sequence[str]) -> None:
    """``actor`` records no view on ``item``'s options (#785), or ReviewError. The same gates as
    ``concur``, and no index: an agent in the room, an item with options that is still being
    decided. A later ``concur`` replaces it. ``recommend=-1`` is not this: that value is how a
    caller leaves the field out, and ``concur`` still refuses it."""
    if actor not in here:
        raise ReviewError(f"only an agent in the room can abstain: {actor!r} isn't")
    if not item.options:
        raise ReviewError(f"{item.label} has no options to abstain on")
    if item.state != "open" and item.state not in OPEN_FINDING:
        raise ReviewError(f"{item.label} is {item.state}: its options are settled")


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
        origin = "https://github.com"
    else:
        m = GITLAB_MR_RE.match(parts.path)
        kind, repo, number = ("gitlab", m[1], m[2]) if m else ("", "", "")
        origin = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    if not kind or any(set(seg) == {"."} for seg in repo.split("/")):
        return None
    # base: the repository's own page, which a card's links start from (#250)
    return {"kind": kind, "repo": repo, "number": int(number), "base": f"{origin}/{repo}"}


def _url_path(file: str) -> str | None:
    """``file`` as a path to put in a URL, each segment percent-encoded, or None when it isn't a plain
    relative path: empty, a leading or doubled slash, a ``.`` or ``..`` segment (a browser would
    resolve it out of the repository), a backslash or a control character. It is the one agent-written
    string that reaches a link, so it is refused rather than repaired."""
    if not file or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in file):
        return None
    parts = file.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return "/".join(quote(p, safe="") for p in parts)


def lines_url(f: dict[str, Any] | None, file: str, lines: str, head: str = "") -> str | None:
    """A finding's lines on the forge. On GitHub, in the PR's own diff, where it draws them in color:
    the file is named by the SHA-256 of its path and the new side's lines by ``R<n>``. On GitLab, in
    the file at the board's head commit (#442), highlighted: an MR diff's line anchors are line codes
    that need the old side's position too (``<sha1 of the path>_<old>_<new>``), which a board doesn't
    have, and the diff has no range anchor, so the diff link goes to the file (``diff_url``) instead.
    No link without a head on GitLab. Nothing an agent wrote is in a link but the path, percent-encoded
    by ``_url_path``, and the checked line numbers and head; every other part comes from ``forge``."""
    if f is None or not file:
        return None
    if lines and not LINES_RE.match(lines):
        return None
    if f["kind"] == "github":
        anchor = "diff-" + hashlib.sha256(file.encode()).hexdigest()
        if lines:
            start, _, end = lines.partition("-")
            anchor += f"R{start}" + (f"-R{end}" if end else "")
        return f"{f['base']}/pull/{f['number']}/files#{anchor}"
    path = _url_path(file)
    if path is None or not HEAD_RE.match(head or ""):
        return None
    start, _, end = lines.partition("-")
    anchor = f"#L{start}" + (f"-{end}" if end else "") if lines else ""
    return f"{f['base']}/-/blob/{head}/{path}{anchor}"


def diff_url(f: dict[str, Any] | None, file: str) -> str | None:
    """A GitLab finding's file in the merge request's diff, for the colored view (#442): GitLab names
    a file there by the SHA-1 of its path, so the link opens the file, not the lines. GitHub's
    ``lines_url`` is already the diff, so there is none for it."""
    if f is None or f["kind"] != "gitlab" or not file:
        return None
    digest = hashlib.sha1(file.encode()).hexdigest()  # noqa: S324 - GitLab's own file anchor, not security
    return f"{f['base']}/-/merge_requests/{f['number']}/diffs#{digest}"


def commit_url(f: dict[str, Any] | None, sha: str) -> str | None:
    """A fix's commit on the forge: in the PR on GitHub, the commit's page on GitLab (#250)."""
    if f is None or not sha:
        return None
    if f["kind"] == "github":
        return f"{f['base']}/pull/{f['number']}/commits/{sha}"
    return f"{f['base']}/-/commit/{sha}"


_LINK_RE = re.compile(r"https?://[^\s<>\"'`()\[\]]+")
MAX_LINKS_PER_MESSAGE = 3


def canonical_url(value: str) -> str:
    """The one address of the pull or merge request ``value`` names, whatever tab or suffix it was copied
    with (``/pull/7/files``, ``/-/merge_requests/7/diffs``): ``https://github.com/o/r/pull/7`` or the
    GitLab host's ``…/-/merge_requests/7``. Empty when it names neither (§37.16)."""
    f = forge(value)
    if f is None:
        return ""
    return (
        f"{f['base']}/pull/{f['number']}"
        if f["kind"] == "github"
        else f"{f['base']}/-/merge_requests/{f['number']}"
    )


def trusted_forge(url: str, boards: Sequence[str]) -> bool:
    """May switchboard ask for a review of ``url`` on its own (§37.16)? A GitHub pull request, always; a
    merge request only on a host the room already has a board on (``boards``: the room's boards' addresses),
    since GitLab can be on any host and an agent could otherwise name one that is not the room's. Any other
    host is a person's to open with ``/review``."""
    f = forge(url)
    if f is None:
        return False
    if f["kind"] == "github":
        return True
    host = urlsplit(url).netloc.lower()
    for board in boards:
        g = forge(board)
        if g is not None and g["kind"] == "gitlab" and urlsplit(board).netloc.lower() == host:
            return True
    return False


def pr_links(text: str, limit: int = MAX_LINKS_PER_MESSAGE) -> list[str]:
    """The distinct pull or merge request links in ``text``, as ``canonical_url`` makes them, in the
    order written, at most ``limit``: what an agent's message points at (§37.16). Only a link ``forge``
    recognises counts; any other link, and a word that only looks like one, is ignored. Trailing
    punctuation is not part of a link."""
    out: list[str] = []
    for m in _LINK_RE.finditer(text):
        url = canonical_url(m.group(0).rstrip(".,;:!?*_"))
        if url and url not in out:
            out.append(url)
            if len(out) >= limit:
                break
    return out


def ref(f: dict[str, Any] | None) -> str:
    """A pull request's or merge request's short name, ``#7`` or ``!7``, built from the checked
    number alone; empty when the URL names neither. Safe in a person's message (#177)."""
    return "" if f is None else ("#" if f["kind"] == "github" else "!") + str(f["number"])


def holder(review: dict[str, Any]) -> str:
    """Who holds a board: the agent that opened it, or opened it again last (§37.14). Only the
    holder or a person closes or resets it."""
    return str(review.get("reopened_by") or review.get("opened_by") or "")


def when(ts: float | None) -> str:
    """A moment as an agent reads it: UTC, to the minute, the same wherever the broker runs."""
    if ts is None:
        return "an unknown time"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def list_line(board: dict[str, Any], default: bool = False) -> str:
    """One board in ``review(action="list")`` (§37.14): its URL, state, what is open and settled,
    its head, and who opened it, who opened it again and who closed it, each with when."""
    n = board["counts"]
    settled_n = n["fixed"] + n["dropped"] + n["answered"]
    state = "closed" if board.get("closed_at") is not None else "open"
    if board.get("posted_by"):
        state += f", handed off to {board['posted_by']}"
    head_s = f" at {board['head'][:12]}" if board.get("head") else ""
    bits = [
        f"{n['open']} open, {settled_n} settled",
        f"opened by {board['opened_by']} on {when(board['opened_at'])}",
    ]
    if board.get("reopened_at") is not None:
        bits.append(f"opened again by {board['reopened_by']} on {when(board['reopened_at'])}")
    if board.get("closed_at") is not None:
        who = f" by {board['closed_by']}" if board.get("closed_by") else ""
        bits.append(f"closed{who} on {when(board['closed_at'])}")
    mark = " <- your calls without url go here" if default else ""
    return f"{board['url']}{head_s} [{state}]: {', '.join(bits)}{mark}"


def list_text(boards: Sequence[dict[str, Any]], default_url: str | None = None) -> str:
    """The room's boards, open first, as an agent reads them. ``boards`` come from the broker with
    ``counts`` and the row's own columns. At most ``MAX_LISTED``; the rest are counted."""
    if not boards:
        return 'This room has no review boards. review(action="open", url=...) starts one.'
    shown = boards[:MAX_LISTED]
    out = [f"{len(boards)} review board{'s' if len(boards) != 1 else ''} in this room, open ones first:"]
    out.extend(list_line(b, b["url"] == default_url) for b in shown)
    if len(boards) > len(shown):
        out.append(f"... and {len(boards) - len(shown)} older closed boards")
    out.append(
        'review(action="show", url=...) reads any, closed ones too; open(url) brings a closed one back.'
    )
    return "\n".join(out)


# what each move a board records reads as on a card's history (#250)
HISTORY_VERBS = {
    "conceded": "conceded it",
    "reassigned": "reassigned it",
    "reopened": "reopened it",
    "contested": "contested it",
    "insisted": "insisted on it",
    "fixed": "fixed it",
    "confirmed": "confirmed it",
    "dropped": "dropped it",
    "deferred": "deferred it to a ticket",
    "answered": "answered it",
    "taken": "took over review",
}
MAX_HISTORY = 12


def history(item: Item, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who did what to ``item``, in order: who raised or asked it, then each move the board's
    events record for its label (``{ts, data: {item, to, by}}``). Only the moves a board makes
    are named; anything else in an event is left out. Capped at ``MAX_HISTORY`` lines."""
    original = item.raised_by
    for event in events:
        data = event.get("data") or {}
        if (
            data.get("item") == item.label
            and data.get("to") == "taken"
            and isinstance(data.get("previous_raiser"), str)
            and data["previous_raiser"]
        ):
            original = data["previous_raiser"]
            break
    out: list[dict[str, Any]] = [
        {
            "by": original,
            "did": "raised it" if item.kind == "finding" else "asked it",
            "at": item.created_at,
        }
    ]
    for e in events:
        d = e.get("data") or {}
        did = HISTORY_VERBS.get(d.get("to"))  # type: ignore[arg-type]
        if d.get("to") == "edited":
            # only the field names the broker wrote: anything else is left out, so a stored event
            # cannot put its own words on the card (#786)
            changed = d.get("fields")
            if (
                isinstance(changed, list)
                and changed
                and all(isinstance(name, str) and name in EDIT_FIELDS for name in changed)
            ):
                did = edit_did(changed)
            else:
                did = None
        if d.get("item") == item.label and did and isinstance(d.get("by"), str):
            if d.get("to") == "taken" and isinstance(d.get("previous_raiser"), str):
                did = f"took over from {d['previous_raiser']}"
            out.append({"by": d["by"], "did": did, "at": e.get("ts")})
    return out[:1] + out[1:][-(MAX_HISTORY - 1) :]


def parse_label(value: Any) -> tuple[str, int]:
    """'F3' -> ('finding', 3); 'q1' -> ('question', 1)."""
    m = re.fullmatch(r"([FfQq])([1-9][0-9]{0,3})", value.strip()) if isinstance(value, str) else None
    if m is None:
        raise ReviewError('item must be a board label, like "F3" or "Q1"')
    return ("finding" if m.group(1) in "Ff" else "question"), int(m.group(2))


def agent_move(
    item: Item, move: str, actor: str, owner_here: bool = True, *, raiser_here: bool = True
) -> str:
    """The state an agent's ``move`` takes ``item`` to, or ReviewError. Rules beyond the
    table: a question is a person's to answer or drop; whoever raised a finding can't concede
    or contest it (that's the other side's call). Dropping, deferring and insisting belong to
    its raiser (#424: insisting once, on a contest); only its owner marks it fixed. The owner may
    revise an unconfirmed fix; the raiser may reopen any fixed finding, clearing confirmation (#783).
    A conceded finding moves to another owner (#371) when its owner hands it over, or when the
    owner has left the room (``owner_here`` False: a restart brings an agent back under a new
    name, and the finding would otherwise be no one's to fix). A departed raiser's unsettled
    finding may be taken by an agent other than its owner (#784); raiser_here defaults to True
    so unknown membership cannot authorize a takeover."""
    if move not in AGENT_MOVES:
        raise ReviewError(f"unknown move {move!r}")
    if item.kind == "question":
        raise ReviewError(f"{item.label} is a question: only a person answers or drops it")
    frm, target = AGENT_MOVES[move]
    to = item.state if target is None else target
    if move == "take":
        if item.state not in frm or item.confirmed_by:
            raise ReviewError(f"{item.label} is settled: only an unsettled finding can be taken")
        if raiser_here:
            raise ReviewError(
                f"{item.raised_by} is still in the room: only a departed raiser's finding can be taken"
            )
        if actor == item.owner:
            raise ReviewError(
                f"{item.label}'s owner cannot take its review: the fixer never confirms its own fix"
            )
    if item.state not in frm:
        if move == "insist":
            raise ReviewError(f"{item.label} is {item.state}: only a contested finding can be insisted on")
        raise ReviewError(f"{item.label} is {item.state}: it can't be {to} from there")
    if move == "insist":
        if actor != item.raised_by:
            raise ReviewError(f"only {item.raised_by}, who raised {item.label}, can insist on it")
        if item.insisted:
            raise ReviewError(f"{item.label} is insisted on already: a person decides, or you can drop it")
    if move in ("concede", "contest") and actor == item.raised_by:
        raise ReviewError(f"{item.label} is yours: you can drop it, not {move} it")
    if move == "concede" and item.state == "conceded" and actor != item.owner and owner_here:
        raise ReviewError(
            f"{item.label} is {item.owner}'s: {item.owner} can hand it over, or a person can reassign it"
        )
    if move == "fix" and actor != item.owner:
        if not owner_here:
            raise ReviewError(
                f"{item.label} is {item.owner}'s to fix, and {item.owner} isn't in the room: take it"
                f' over first with review(action="concede", item="{item.label}")'
            )
        raise ReviewError(f"{item.label} is {item.owner}'s to fix")
    if move in ("drop", "defer") and actor != item.raised_by:
        raise ReviewError(f"only {item.raised_by}, who raised {item.label}, or a person can {move} it")
    if move == "reopen" and actor != item.raised_by:
        raise ReviewError(f"only {item.raised_by}, who raised {item.label}, can reopen it")
    if move == "fix" and item.confirmed_by:
        raise ReviewError(
            f"{item.label} is already confirmed by {item.confirmed_by}: its raiser can reopen it"
        )
    if move == "confirm":
        if item.confirmed_by:
            raise ReviewError(f"{item.label} is already confirmed by {item.confirmed_by}")
        if actor != item.raised_by or actor == item.owner:
            raise ReviewError(f"only {item.raised_by}, who raised {item.label}, can confirm its fix")
    return to


# ------------------------------------------------------- who acts next (#884)
# the moves of a board's ``review`` events that answer a finding: whoever made one is on the author's side
SIDE_MOVES = frozenset({"conceded", "reassigned", "contested"})
# an ``open`` that changed the board: it opened it, brought it back, or moved it to a commit just pushed.
# A call that found it open as it was ("kept") says nothing about who wrote the pull request.
SIDE_OPENS = frozenset({"opened", "reopened", "moved"})


def author_side(
    holder: str,
    items: Sequence[Item],
    events: Sequence[dict[str, Any]],
    agents: Sequence[str],
    offline: Collection[str] = (),
) -> tuple[str, ...]:
    """The agents of the room (``agents``, its active ones) that the board records on the author's
    side, the side that concedes or contests a finding (#884), in the room's order. Always: every
    owner of one of its findings, and the agent that posted the link switchboard opened the board for
    (§37.16, its ``open`` event's ``author``). And, unless it has raised a finding here, which makes
    it a reviewer: whoever opened the board (its ``holder``, or an agent an ``open`` event that opened,
    reopened or moved the board names as its ``by``), and every agent that conceded, reassigned or
    contested a finding (an agent's ``review`` event, not a person's). An agent that is ``offline``
    is not counted either: it would not answer. Empty when the board does not know: then a raise asks
    every other agent, as it always did (``acts_next``). ``events`` are the board's, as
    ``{"data": {...}}`` or the data alone."""
    owners = {i.owner for i in items if i.kind == "finding" and i.owner}
    raisers = {i.raised_by for i in items if i.kind == "finding" and i.raised_by}
    sure = set(owners)
    maybe = {holder} if holder else set()
    for event in events:
        data = event.get("data", event) if isinstance(event, dict) else {}
        if not isinstance(data, dict) or data.get("person"):
            continue
        by = data.get("by")
        if not isinstance(by, str) or not by:
            continue
        if data.get("to") in SIDE_MOVES:
            maybe.add(by)
        if "open" in data:
            if data.get("how") in SIDE_OPENS:
                maybe.add(by)
            author = data.get("author")
            if isinstance(author, str) and author:
                sure.add(author)
    side = sure | (maybe - raisers)
    gone = set(offline)
    return tuple(a for a in dict.fromkeys(agents) if a in side and a not in gone)


def _first(item: Item, here: Sequence[str], side: Sequence[str]) -> list[str]:
    """Whose move a finding's state makes next (§37.4): a raised one is the author's side's to
    concede or contest (every other agent's when the board doesn't know that side, #884), a contested
    one its raiser's (#424), a conceded one its owner's, an unconfirmed fix its raiser's."""
    if item.kind != "finding":
        return []
    if item.state == "raised":
        mine = [a for a in side if a in here and a != item.raised_by]
        return mine or [a for a in here if a != item.raised_by]
    if item.state == "contested" and not item.insisted:
        return [item.raised_by] if item.raised_by in here else []
    if item.state == "conceded":
        return [item.owner] if item.owner and item.owner in here else []
    if item.state == "fixed" and not item.confirmed_by:
        return [item.raised_by] if item.raised_by in here else []
    return []


def _pickers(item: Item, here: Sequence[str]) -> list[str]:
    """The agents asked to agree with ``item``'s options who have neither picked nor abstained, while
    it is still being decided (§37.12: every agent in the room counts)."""
    if not item.options:
        return []
    deciding = item.state == "open" if item.kind == "question" else item.state in OPEN_FINDING
    if not deciding:
        return []
    spoken = set(effective_picks(item)) | set(item.abstentions)
    return [a for a in here if a != item.raised_by and a not in spoken]


def acts_next(item: Item, agents: Sequence[str], side: Sequence[str] = ()) -> tuple[str, ...]:
    """Who of ``agents`` (the room's) has a move on ``item`` now (#884): its state's next move
    (``_first``), and every agent still to pick among its options. A board's line about an item stays
    owed by an agent it reached only while that agent is here: once the item has moved on (an agent
    conceded it, the raiser dropped it, a person ruled), its line is answered for the others."""
    here = list(dict.fromkeys(agents))
    who = set(_first(item, here, side)) | set(_pickers(item, here))
    return tuple(a for a in here if a in who)


def answered(
    cards: Sequence[tuple[str, str]],
    items: dict[str, Item],
    recipients: Sequence[str],
    agents: Sequence[str],
    side: Sequence[str],
) -> list[str]:
    """Of the agents a board's line reached (``recipients``), those for whom it is answered (#884),
    though they said nothing, since a move on the board is the answer. A line names ``cards``, each as
    its label and the state it was in when the line was posted (read now in ``items`` by label). An
    agent still owes the line while one of its cards is in that state and the agent has a move on it
    (``acts_next``). Once a card has moved on, what the line asked of it was done by someone, and
    whatever is next has a line of its own (or was the mover's own doing, as a concession to itself
    is); a card the board no longer has is nobody's. A state the line did not record (``""``) asks only
    whether the agent has a move. A raise or an ask with options asked every agent to pick (§37.12),
    and a pick is due until the card is decided, whatever state it has moved to: an agent still to
    pick owes that line until it picks or abstains."""
    here = list(dict.fromkeys(agents))
    owed: set[str] = set()
    for label, state in cards:
        item = items.get(label)
        if item is None:
            continue
        if not state or item.state == state:
            owed.update(acts_next(item, here, side))
        if state in ("raised", "open"):  # the line that asked for the picks
            owed.update(_pickers(item, here))
    return [r for r in recipients if r not in owed]


@dataclass(frozen=True)
class Told:
    """One move's line for whoever acts next (§37.4): ``who`` they are, and its parts. ``body`` is the
    line between its board and its ``show``: "F1 raised: off by one. @claude-1: concede or contest it."
    ``state``: the item's state the line is about; the line is answered once the item moves on."""

    who: tuple[str, ...]
    label: str
    kind: str
    title: str
    body: str
    on: str
    show: str
    state: str = ""

    @property
    def line(self) -> str:
        return f"{self.on} {self.body} {self.show}"


def told(
    item: Item,
    actor: str,
    agents: Sequence[str],
    board_url: str = "",
    *,
    action: str = "",
    side: Sequence[str] = (),
) -> Told | None:
    """After an agent's move on ``item`` (its state now), who has to act next and the parts of the
    line that tells them, or None when it's nobody's move or a person's (§37.4). A raised finding is
    the author's side's to concede or contest (``author_side``, #884), every other agent's while the
    board doesn't know that side; a finding conceded to another agent is that owner's to fix; a fix
    is its raiser's to check. A question just asked, and a finding's fix options, are every other
    agent's to agree with or answer with another pick (#423: "recommended" means they all agree),
    before it is a person's. A contested finding goes back to its raiser (#424), to drop or insist
    on; once insisted on it waits for a person, who sees it on the page, and a move that leaves the
    next step with the mover wakes nobody. ``agents``: the room's active agents. ``board_url``: given
    when the room has several open boards (§37.14), so the line names the board and every call it
    suggests carries its ``url``; the line is the mover's own message, so the board's URL, which the
    mover wrote, may be in it."""
    on = f"Review board {board_url}:" if board_url else "Review board:"
    pass_url = f', url="{board_url}"' if board_url else ""
    show = f'review(action="show"{pass_url}) shows the board.'
    here = list(dict.fromkeys(agents))
    if item.kind == "question":
        who = tuple(a for a in _pickers(item, here) if a != actor)
        if item.state != "open" or not who:
            return None
        concur_s = f'review(action="concur", item="{item.label}", recommend=<index>{pass_url})'
        abstain_s = f'review(action="abstain", item="{item.label}"{pass_url})'
        stop = "" if item.title.endswith((".", "?", "!")) else "."  # a question usually ends in one
        mention = " ".join("@" + a for a in who)
        body = (
            f"{item.label} asked: {item.title}{stop} {mention}: agree with {actor}'s pick"
            f" or pick another option, {concur_s}, or abstain if you have no view, {abstain_s}."
        )
        return Told(who, item.label, item.kind, item.title, body, on, show, state=item.state)
    first = [a for a in _first(item, here, side) if a != actor]
    if item.state == "raised":
        # its fix options are every other agent's to agree with too (#423)
        pick = set(_pickers(item, here))
        who = tuple(a for a in here if (a in first or a in pick) and a != actor)
        verb, then = f"raised: {item.title}", "concede or contest it"
        if item.options:
            then += ", and agree with its fix option, pick another, or abstain (concur or abstain)"
    elif item.state == "contested" and not item.insisted:
        # a contest is the raiser's to answer (#424): drop it, or insist with a reason, and only an
        # insisted finding is a person's. The reason itself is read on the board, not repeated here
        who = tuple(first)
        verb = f"contested: {item.title}"
        then = (
            f'drop it if the contest convinces you, or insist with your reason (review(action="insist", '
            f'item="{item.label}", reason=...{pass_url})): only an insisted finding goes to a person'
        )
    elif item.state == "conceded":
        who = tuple(first)
        verb = (
            f"reopened, owner {item.owner}: {item.reason}"
            if action == "reopen"
            else f"conceded, owner {item.owner}"
        )
        then = "it's yours to fix"
    elif item.state == "fixed" and not item.confirmed_by:
        who = tuple(first)
        verb, then = f"fixed in {item.commit[:12]}", "check the fix"
    else:
        return None
    if not who:
        return None
    body = f"{item.label} {verb}. {' '.join('@' + a for a in who)}: {then}."
    return Told(who, item.label, item.kind, item.title, body, on, show, item.state)


def handoff(
    item: Item,
    actor: str,
    agents: Sequence[str],
    board_url: str = "",
    *,
    action: str = "",
    side: Sequence[str] = (),
) -> tuple[tuple[str, ...], str] | None:
    """Who has to act next after an agent's move on ``item``, and the one line that tells them, or
    None (``told``)."""
    t = told(item, actor, agents, board_url, action=action, side=side)
    return None if t is None else (t.who, t.line)


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
    return not open_items(items)


def open_items(items: list[Item]) -> list[Item]:
    """The labels that must be named if an agent closes a board with work left (#408)."""
    return [
        i
        for i in items
        if i.state in OPEN_FINDING or i.state == "open" or (i.state == "fixed" and not i.confirmed_by)
    ]


def unsettled_finding_ids(items: list[Item]) -> list[int]:
    """Findings whose lines or fix still need checking after the board moves to a new head."""
    return [
        i.id
        for i in items
        if i.kind == "finding" and (i.state in OPEN_FINDING or (i.state == "fixed" and not i.confirmed_by))
    ]


# what a finding contested before agents could insist reads as (the v26 -> v27 migration writes it)
LEGACY_INSISTED = "(contested before agents could insist: a person decides)"


def insist_line(item: Item) -> str:
    """The raiser's answer to a contest as a card shows it: "Insists: <reason>", or the migration's own
    line for a finding that was already waiting on a person. Empty when there is none."""
    if not item.insisted:
        return ""
    return item.insisted if item.insisted == LEGACY_INSISTED else f"Insists: {item.insisted}"


def raiser_answers(item: Item, active: set[str] | None, offline: Collection[str] = ()) -> bool:
    """Whether a contested finding is still its raiser's to answer (#424): not insisted on yet, and
    its raiser is in the room (``active``, who we know) and not offline. Fails closed: with no
    ``active`` to look at, or a raiser who left or is offline, a person has it."""
    return (
        item.state == "contested"
        and not item.insisted
        and active is not None
        and item.raised_by in active
        and item.raised_by not in offline
    )


def needs_person(
    item: Item, active: set[str] | None = None, now: float | None = None, offline: Collection[str] = ()
) -> bool:
    """Whether a person must act now. A contested finding goes back to its raiser first, who drops it
    or insists (#424), so it is a person's only once insisted on, or when its raiser has left the
    room or is offline. A fix waits for a person's check when its independent raiser cannot
    confirm in time."""
    if item.needs_person:
        return not raiser_answers(item, active, offline)
    return bool(
        item.state == "fixed"
        and not item.confirmed_by
        and (
            (active is not None and item.raised_by not in active)
            or (now is not None and now - item.updated_at >= FIX_CONFIRM_TIMEOUT_S)
        )
    )


def counts(
    items: list[Item],
    active: set[str] | None = None,
    now: float | None = None,
    offline: Collection[str] = (),
) -> dict[str, int]:
    out = {"needs_person": 0, "open": 0, "fixed": 0, "dropped": 0, "answered": 0}
    for i in items:
        if needs_person(i, active, now, offline):
            out["needs_person"] += 1
        if i.state in OPEN_FINDING or i.state == "open" or (i.state == "fixed" and not i.confirmed_by):
            out["open"] += 1
        elif i.state in ("fixed", "dropped", "answered"):
            out[i.state] += 1
    return out


def contested_line(item: Item, active: set[str], offline: Collection[str]) -> str:
    """Whose move a contested finding is, as an agent reads it (#424)."""
    base = f"raised by {item.raised_by}, contested"
    if item.insisted:
        who = f"{base}, insisted on: a person decides"
    elif raiser_answers(item, active, offline):
        who = f"{base}: {item.raised_by} drops it or insists (review action insist)"
    else:
        who = f"{base}; {item.raised_by} isn't here to answer: a person decides"
    # Keep the board short; show(item=...) has the uncut argument.
    reason = item.reason[:200].replace("\n", " ").replace("\r", " ")
    return who + (f"; contest reason: {reason}" if reason else "")


def options_text(item: Item, present: Sequence[str]) -> str:
    """An item's options as an agent reads them: the recommended one first, each with its number
    (what ``concur`` takes) and who is behind it, then who abstained and who is still to agree.
    Empty for none."""
    if not item.options:
        return ""
    c = consensus(item, present)
    text = " | ".join(f"{n}. {item.options[n]}" + (f" ({c.notes[n]})" if c.notes[n] else "") for n in c.order)
    if c.abstained:
        text += f"; abstained: {names(c.abstained)}"
    if c.state == "split":
        text += "; the agents are split: no option is recommended"
    if c.waiting:
        text += f"; waiting on {names(c.waiting)} to agree or pick another (concur)"
    return text


def render(
    review: dict[str, Any],
    items: list[Item],
    now: float | None = None,
    present: Sequence[str] = (),
    offline: Collection[str] = (),
) -> str:
    """The board as an agent reads it: one line per item, open work first. Short on purpose:
    the board replaces a wall of text in the room, it mustn't become one. ``present``: the room's
    agents, who must all pick an option for it to read "recommended by" (``consensus``), and whose
    raiser a contested finding waits on; ``offline``: those among them who are not there (#424)."""
    head_s = f" at {review['head'][:12]}" if review.get("head") else ""
    closed = review.get("closed_at") is not None
    state = "settled" if settled(items) else "open"
    if review.get("posted_by"):
        state = f"handed off to {review['posted_by']}" + ("" if closed else ": post one review now, once")
    if closed:  # kept, not gone (§37.14): any member may read it, and open(url) brings it back
        by = f" by {review['closed_by']}" if review.get("closed_by") else ""
        state += f" (closed{by} on {when(review['closed_at'])}; open(url) brings it back)"
    out = [f"Review of {review['url']}{head_s}: {state}"]
    line = report_line(review, now if now is not None else float(review.get("reported_at") or 0))
    if line:
        out.append(f"The {'merge' if gitlab(review) else 'pull'} request: {line}")
    order = {
        s: n
        for n, s in enumerate(("contested", "open", "raised", "conceded", "answered", "fixed", "dropped"))
    }
    for i in sorted(items, key=lambda i: (order.get(i.state, 9), i.kind, i.n)):
        where = f" ({i.file}{':' + i.lines if i.lines else ''})" if i.file else ""
        who = {
            "raised": f"raised by {i.raised_by}",
            "contested": contested_line(i, set(present), offline),
            "conceded": f"owner {i.owner}",
            "fixed": f"fixed by {i.owner}"
            + (f" in {i.commit[:12]}" if i.commit else "")
            + (f", confirmed by {i.confirmed_by}" if i.confirmed_by else ", awaiting confirmation"),
            "dropped": f"deferred to {i.deferred_to}"
            if i.deferred_to
            else "dropped" + (f": {i.reason}" if i.reason else ""),
            "open": "for a person: " + options_text(i, present),
            "answered": f"answered by {i.answered_by}: {i.answer}",
        }[i.state]
        proof_s = f"; proof: {i.proof}" if i.state == "fixed" and i.proof else ""
        outdated = f"; outdated at {i.outdated_head}" if i.outdated_head else ""
        # a finding may offer fix options, in the same shape as a question's (#423)
        fix_options = (
            f"; fix options: {options_text(i, present)}"
            if i.kind == "finding" and i.options and i.state in OPEN_FINDING
            else ""
        )
        out.append(f"{i.label} [{i.state}] {i.title}{where}: {who}{proof_s}{outdated}{fix_options}")
    if review.get("posted_by") and review.get("plan"):
        plan = review["plan"]
        out.extend(("Handoff summary to post:", plan["summary"], "Line comments to leave open:"))
        out.extend(f"- {c['file']}:{c['line']}: {c['body']}" for c in plan["comments"])
        if not plan["comments"]:
            out.append("- none")
    return "\n".join(out)


def render_item(
    review: dict[str, Any],
    item: Item,
    events: list[dict[str, Any]],
    now: float | None = None,
    present: Sequence[str] = (),
    offline: Collection[str] = (),
) -> str:
    """One card in full (#782), with the same bounded history the page reads."""
    out = [render({**review, "plan": None}, [item], now, present, offline)]
    for label, value in (
        ("Detail", item.detail),
        ("Owner", item.owner),
        ("Reason", item.reason),
        ("Insists", item.insisted),
        ("Commit", item.commit),
        ("Proof", item.proof),
        ("Confirmed by", item.confirmed_by),
        ("Outdated at", item.outdated_head),
        ("Deferred to", item.deferred_to),
        ("Answer", item.answer),
        ("Answered by", item.answered_by),
    ):
        if value:
            out.append(f"{label}: {value}")
    if item.options:
        out.append("Options: " + options_text(item, present))
    out.append("History:")
    for entry in history(item, events):
        at = f" ({when(entry['at'])})" if entry["at"] is not None else ""
        out.append(f"- {entry['by']} {entry['did']}{at}")
    return "\n".join(out)


def handoff_plan(
    items: list[Item], events: list[dict[str, Any]], agent_names_ever: set[str] | None = None
) -> dict[str, Any]:
    """The exact summary and line comments one agent is asked to post (§37.6).

    Titles and other agent-written text stay in this board/tool data, never in the person's
    handoff chat message (#177). Older events lack the person marker; a name never held by an
    agent in the room identifies those older person moves conservatively.
    """
    done = [
        i
        for i in sorted(items, key=lambda i: (i.kind, i.n))
        if i.state in ("dropped", "answered") or (i.state == "fixed" and i.confirmed_by)
    ]
    lines = ["Review board summary"]
    for i in done:
        if i.state == "fixed":
            # a person's own ruling has no agent's proof, and may have no commit either (#371)
            outcome = f"Fixed: {i.title}" + (
                f" — commit {i.commit}" if i.commit else f", on {i.confirmed_by}'s say"
            )
            if i.proof:
                outcome += f"; proof: {i.proof}"
        elif i.state == "answered":
            outcome = f"Answered: {i.title} — {i.answer}"
        elif i.deferred_to:
            outcome = f"Deferred: {i.title} — to {i.deferred_to}"
        else:
            outcome = f"Dropped: {i.title}" + (f" — {i.reason}" if i.reason else "")
        lines.append(f"- {i.label} {outcome}")
    decisions: dict[str, list[str]] = {}
    for event in events:
        data = event.get("data") or {}
        if data.get("to") not in ("conceded", "dropped", "confirmed", "deferred"):
            continue
        label, actor = data.get("item"), data.get("by")
        if not isinstance(label, str) or not isinstance(actor, str):
            continue
        person = data.get("person") is True or (
            "person" not in data and agent_names_ever is not None and actor not in agent_names_ever
        )
        if person:
            decisions.setdefault(label, []).append(f"{actor} {data['to']} this finding")
    comments = []
    for i in done:
        if i.kind != "finding" or not i.file or not i.lines or i.label not in decisions:
            continue
        fixed = (
            f"Fixed in {i.commit}; proof: {i.proof}."
            if i.proof
            else f"Fixed in {i.commit}, on {i.confirmed_by}'s say."  # a person's ruling (#371)
            if i.commit
            else f"Fixed, on {i.confirmed_by}'s say."
        )
        result = (
            fixed
            if i.state == "fixed"
            else f"Deferred to {i.deferred_to}."
            if i.deferred_to
            else f"Dropped: {i.reason}."
            if i.state == "dropped"
            else ""
        )
        comments.append(
            {
                "item": i.label,
                "file": i.file,
                "line": int(i.lines.rsplit("-", 1)[-1]),
                "body": f"{i.label}: {'; '.join(decisions[i.label])}. {result}",
            }
        )
    return {"summary": "\n".join(lines), "comments": comments, "labels": [i.label for i in done]}


def render_handoff(plan: dict[str, Any], agent: str, limit: int, ref: str = "") -> str:
    """One human message wakes only the chosen agent, with checked labels and no agent text.
    ``ref``: the board's ``#7`` or ``!7``, given when the room has several open boards (§37.14);
    it is built from a checked number, never from the board's URL (#177)."""
    labels = ", ".join(plan["labels"])
    decisions = ", ".join(c["item"] for c in plan["comments"]) or "none"
    which = f" ({ref})" if ref else ""
    message = (
        f"Hand off: @{agent}, post one review to this board's PR or MR{which} with your own gh or glab, "
        'once. review(action="show") has the exact summary and line comments; leave those '
        f"conversations open. Items: {labels}. Person-decided lines: {decisions}."
    )
    if len(message) > limit:
        raise ReviewError("the board is too big to hand off in one message: close some items first")
    return message
