"""A review board in a room: findings and questions about one pull request (DESIGN.md §37).

Pure policy, no I/O: what an item is, the moves an agent or a person may make on it, when a
board is settled, what the agents' picks among an item's options add up to (§37.12), and how a
board reads as text. The store keeps the rows (``store.py``), the
broker applies the moves (``broker/reviews.py``). switchboard never fetches the pull request,
judges a finding, holds a token or posts anything: the agents and the people do (§37.1).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Sequence
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
MAX_TICKET = 200  # a follow-up ticket's key or link, for a deferred finding (#371)
MAX_PROOF = 500
MAX_OPTION = 200
MAX_OPTIONS = 4
MAX_ITEMS = 200  # per review: a board, not a log
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
AGENT_MOVES = {
    # from conceded: a handover by its owner, or a takeover once the owner has left (#371)
    "concede": (frozenset({"raised", "contested", "conceded"}), "conceded"),
    "contest": (frozenset({"raised"}), "contested"),
    "insist": (frozenset({"contested"}), "contested"),
    "fix": (frozenset({"conceded"}), "fixed"),
    "drop": (frozenset({"raised", "contested", "conceded"}), "dropped"),
    "confirm": (frozenset({"fixed"}), "fixed"),
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
    raised_by: str  # screen name
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


def proof(value: Any) -> str:
    """A test's name or an http(s) proof link; the broker checks shape, not its contents."""
    p = text(value, "proof", MAX_PROOF)
    if not p:
        raise ProofRequired("proof is required")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", p) and not URL_RE.fullmatch(p):
        raise ReviewError("proof link must be an http(s) URL, or give the test's name")
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


def report_line(review: dict[str, Any], now: float) -> str:
    """The latest report, as the agent and the page read it: "2 open on the PR, CI failing (per
    claude-1, 3 min ago)". Empty when no agent has reported."""
    if not review.get("reported_by"):
        return ""
    bits = []
    if review.get("pr_state") in ("merged", "closed"):
        bits.append(str(review["pr_state"]))
    if review.get("pr_threads") is not None:
        n = int(review["pr_threads"])
        bits.append(f"{n} open on the PR" if n else "no threads open on the PR")
    if review.get("pr_ci"):
        bits.append(f"CI {review['pr_ci']}")
    mins = max(0, int((now - float(review.get("reported_at") or now)) // 60))
    age = "just now" if mins == 0 else f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"
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
    """Split a compact finding location, then apply the existing file and line checks."""
    location = text(value, "where", MAX_FILE + MAX_LINES + 1)
    if not location:
        return "", ""
    path, sep, span = location.rpartition(":")
    if not sep:
        return text(location, "file", MAX_FILE), ""
    return text(path, "file", MAX_FILE), lines(span)


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
    at no option is dropped, so a row out of range never counts for anything."""
    out: dict[str, int] = {}
    if item.options and item.recommend is not None and item.raised_by:
        out[item.raised_by] = item.recommend
    out.update(item.picks)
    return {a: o for a, o in out.items() if 0 <= o < len(item.options)}


@dataclass(frozen=True)
class Consensus:
    """What the agents' picks add up to (#423). Computed from the picks and who is in the room now,
    never stored or claimed: ``state`` is "none" (no one here has picked), "proposed" (those who
    picked agree, and others are still to), "agreed" (every agent in the room picked the same
    option) or "split" (two picks differ: no option is recommended). ``order`` lists the options,
    the recommended one first; ``notes`` says, per option (by index), who is behind it."""

    state: str
    option: int | None
    by: tuple[str, ...]
    waiting: tuple[str, ...]
    order: tuple[int, ...]
    notes: tuple[str, ...]


def consensus(item: Item, present: Sequence[str]) -> Consensus:
    """The agreement on ``item``'s options among ``present``, the agents in the room now (the only
    ones who can still agree: a pick by an agent who left is history, and counts for nothing). An
    option is "recommended" only when every one of them has picked it; an agent who hasn't is
    waited on, and a split names each pick without recommending any."""
    count = len(item.options)
    base = tuple(range(count))
    here = list(dict.fromkeys(present))
    live = {a: o for a, o in effective_picks(item).items() if a in here}
    waiting = tuple(a for a in here if a not in live)
    notes = [""] * count
    if not live:
        return Consensus("none", None, (), waiting, base, tuple(notes))
    chosen = set(live.values())
    if len(chosen) > 1:
        for o in sorted(chosen):
            notes[o] = "picked by " + names([a for a, p in live.items() if p == o])
        return Consensus("split", None, (), waiting, base, tuple(notes))
    (option,) = chosen
    by = tuple(live)
    if waiting:
        notes[option] = names(by) + (" recommends this" if len(by) == 1 else " recommend this")
    else:
        notes[option] = "recommended by " + names(by)
    order = (option, *(o for o in base if o != option))
    return Consensus("agreed" if not waiting else "proposed", option, by, waiting, order, tuple(notes))


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


def lines_url(f: dict[str, Any] | None, file: str, lines: str) -> str | None:
    """A finding's lines in the PR's or MR's own diff, where the forge draws them in color (#250).
    GitHub names a file in a PR's diff by the SHA-256 of its path and the new side's lines by
    ``R<n>``; GitLab names a file by the SHA-1 of its path, and its line anchors need the old
    side's number too, which a board doesn't have, so a GitLab link opens the file. The path is
    only hashed, and every other part comes from ``forge``, so nothing an agent wrote is in it."""
    if f is None or not file:
        return None
    if f["kind"] == "github":
        anchor = "diff-" + hashlib.sha256(file.encode()).hexdigest()
        if lines:
            start, _, end = lines.partition("-")
            anchor += f"R{start}" + (f"-R{end}" if end else "")
        return f"{f['base']}/pull/{f['number']}/files#{anchor}"
    digest = hashlib.sha1(file.encode()).hexdigest()  # noqa: S324 - GitLab's own file anchor, not security
    return f"{f['base']}/-/merge_requests/{f['number']}/diffs#{digest}"


def commit_url(f: dict[str, Any] | None, sha: str) -> str | None:
    """A fix's commit on the forge: in the PR on GitHub, the commit's page on GitLab (#250)."""
    if f is None or not sha:
        return None
    if f["kind"] == "github":
        return f"{f['base']}/pull/{f['number']}/commits/{sha}"
    return f"{f['base']}/-/commit/{sha}"


# what each move a board records reads as on a card's history (#250)
HISTORY_VERBS = {
    "conceded": "conceded it",
    "reassigned": "reassigned it",
    "contested": "contested it",
    "insisted": "insisted on it",
    "fixed": "fixed it",
    "confirmed": "confirmed it",
    "dropped": "dropped it",
    "deferred": "deferred it to a ticket",
    "answered": "answered it",
}
MAX_HISTORY = 12


def history(item: Item, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who did what to ``item``, in order: who raised or asked it, then each move the board's
    events record for its label (``{ts, data: {item, to, by}}``). Only the moves a board makes
    are named; anything else in an event is left out. Capped at ``MAX_HISTORY`` lines."""
    out: list[dict[str, Any]] = [
        {
            "by": item.raised_by,
            "did": "raised it" if item.kind == "finding" else "asked it",
            "at": item.created_at,
        }
    ]
    for e in events:
        d = e.get("data") or {}
        did = HISTORY_VERBS.get(d.get("to"))  # type: ignore[arg-type]
        if d.get("item") == item.label and did and isinstance(d.get("by"), str):
            out.append({"by": d["by"], "did": did, "at": e.get("ts")})
    return out[:1] + out[1:][-(MAX_HISTORY - 1) :]


def board_reason(name: str, version: str | None, tools: tuple[str, ...] | None) -> str | None:
    """Why the member ``name`` can't use the board, from what its MCP server said in ``mcp.hello``
    (#249), or None when it can. A server that lists no tools predates the list, and an unknown
    counts as unable (it fails closed). It's a list, not a version comparison: it says exactly
    what's missing."""
    fix = "Upgrade it, then restart its session."
    if tools is None:
        why = "its switchboard is older than this one and doesn't list its tools"
    elif "review" not in tools:
        why = f"its switchboard ({version or 'version unknown'}) has no review tool"
    else:
        return None
    return f"{name} can't use the board: {why}. {fix}"


def parse_label(value: Any) -> tuple[str, int]:
    """'F3' -> ('finding', 3); 'q1' -> ('question', 1)."""
    m = re.fullmatch(r"([FfQq])([1-9][0-9]{0,3})", value.strip()) if isinstance(value, str) else None
    if m is None:
        raise ReviewError('item must be a board label, like "F3" or "Q1"')
    return ("finding" if m.group(1) in "Ff" else "question"), int(m.group(2))


def agent_move(item: Item, move: str, actor: str, owner_here: bool = True) -> str:
    """The state an agent's ``move`` takes ``item`` to, or ReviewError. Rules beyond the
    table: a question is a person's to answer or drop; whoever raised a finding can't concede
    or contest it (that's the other side's call), only drop it, or insist on it once it is
    contested (#424: only its raiser, and once); only its owner marks it fixed.
    A conceded finding moves to another owner (#371) when its owner hands it over, or when the
    owner has left the room (``owner_here`` False: a restart brings an agent back under a new
    name, and the finding would otherwise be no one's to fix)."""
    if move not in AGENT_MOVES:
        raise ReviewError(f"unknown move {move!r}")
    if item.kind == "question":
        raise ReviewError(f"{item.label} is a question: only a person answers or drops it")
    frm, to = AGENT_MOVES[move]
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
    if move == "confirm":
        if item.confirmed_by:
            raise ReviewError(f"{item.label} is already confirmed by {item.confirmed_by}")
        if actor != item.raised_by or actor == item.owner:
            raise ReviewError(f"only {item.raised_by}, who raised {item.label}, can confirm its fix")
    return to


def handoff(item: Item, actor: str, agents: Sequence[str]) -> tuple[tuple[str, ...], str] | None:
    """After an agent's move on ``item`` (its state now), who has to act next and the line
    that tells them, or None when it's nobody's move or a person's (§37.4). A raised finding
    is the other agents' to concede or contest; a finding conceded to another agent is that
    owner's to fix; a fix is its raiser's to check. A question just asked is the other agents' to
    agree with or answer with another pick (#423: "recommended" means they all agree), before it
    is a person's. A contested finding goes back to its raiser (#424), to drop or insist on; once
    insisted on it waits for a person, who sees it on the page, and a move that leaves the next
    step with the mover wakes nobody. ``agents``: the room's active agents."""
    show = 'review(action="show") shows the board.'
    if item.kind == "question":
        who = tuple(a for a in agents if a != actor) if item.state == "open" and item.options else ()
        if not who:
            return None
        concur_s = f'review(action="concur", item="{item.label}", recommend=<index>)'
        stop = "" if item.title.endswith((".", "?", "!")) else "."  # a question usually ends in one
        mention = " ".join("@" + a for a in who)
        return who, (
            f"Review board: {item.label} asked: {item.title}{stop} {mention}: agree with {actor}'s pick"
            f" or pick another option, {concur_s}. {show}"
        )
    if item.state == "raised":
        who = tuple(a for a in agents if a != actor)
        verb, then = f"raised: {item.title}", "concede or contest it"
        if item.options:  # its fix options are theirs to agree with too (#423)
            then += ", and agree with its fix option or pick another (concur)"
    elif item.state == "contested" and not item.insisted:
        # a contest is the raiser's to answer (#424): drop it, or insist with a reason, and only an
        # insisted finding is a person's. The reason itself is read on the board, not repeated here
        who = (item.raised_by,) if item.raised_by != actor and item.raised_by in agents else ()
        verb = f"contested: {item.title}"
        then = (
            f'drop it if the contest convinces you, or insist with your reason (review(action="insist", '
            f'item="{item.label}", reason=...)): only an insisted finding goes to a person'
        )
    elif item.state == "conceded":
        who = (item.owner,) if item.owner and item.owner != actor and item.owner in agents else ()
        verb, then = f"conceded, owner {item.owner}", "it's yours to fix"
    elif item.state == "fixed" and not item.confirmed_by:
        who = (item.raised_by,) if item.raised_by != actor and item.raised_by in agents else ()
        verb, then = f"fixed in {item.commit[:12]}", "check the fix"
    else:
        return None
    if not who:
        return None
    return who, f"Review board: {item.label} {verb}. {' '.join('@' + a for a in who)}: {then}. {show}"


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
        return f"{base}, insisted on: a person decides"
    if raiser_answers(item, active, offline):
        return f"{base}: {item.raised_by} drops it or insists (review action insist)"
    return f"{base}; {item.raised_by} isn't here to answer: a person decides"


def options_text(item: Item, present: Sequence[str]) -> str:
    """An item's options as an agent reads them: the recommended one first, each with its number
    (what ``concur`` takes) and who is behind it, then who is still to agree. Empty for none."""
    if not item.options:
        return ""
    c = consensus(item, present)
    text = " | ".join(f"{n}. {item.options[n]}" + (f" ({c.notes[n]})" if c.notes[n] else "") for n in c.order)
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
    state = "settled" if settled(items) else "open"
    if review.get("posted_by"):
        state = f"handed off to {review['posted_by']}: post one review now, once"
    out = [f"Review of {review['url']}{head_s}: {state}"]
    line = report_line(review, now if now is not None else float(review.get("reported_at") or 0))
    if line:
        out.append(f"The pull request: {line}")
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


def render_handoff(plan: dict[str, Any], agent: str, limit: int) -> str:
    """One human message wakes only the chosen agent, with checked labels and no agent text."""
    labels = ", ".join(plan["labels"])
    decisions = ", ".join(c["item"] for c in plan["comments"]) or "none"
    message = (
        f"Hand off: @{agent}, post one review to this board's PR or MR with your own gh or glab, "
        'once. review(action="show") has the exact summary and line comments; leave those '
        f"conversations open. Items: {labels}. Person-decided lines: {decisions}."
    )
    if len(message) > limit:
        raise ReviewError("the board is too big to hand off in one message: close some items first")
    return message
