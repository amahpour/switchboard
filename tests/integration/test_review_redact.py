"""A secret written on a review board is cut out before it is stored, shown or posted (#890, DESIGN.md §11,
§37.2), for an agent's ``review`` move and a person's move from the page, as ``say`` and a person's post are.

Two scripted agents that are real MCP clients of ``/mcp``, and the web API, against an in-process broker. One
case per field a board stores or posts: the secret goes in that field alone, and then it must be absent from
every file the broker keeps (the database and its log, so the card, the move's event, the room's messages and
the search index), from the search, from what the page reads (open and past boards), from the room's messages
and from what each agent reads; and where the field is shown, its marker is there instead. A link that holds
a secret is refused, so nothing of it is kept. What these catch: any field, of any move, stored or posted as
written, which is how a board's handoff line used to carry a secret into the room."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from test_review_board import FAST, PR

from switchboard.broker.reviews import ACTIONS

SECRET = "ghp" + "_" + "aB3" * 12  # a GitHub token's shape, built from pieces so no scanner reads it
MARK = "[redacted: github token]"
PATH = "/api/rooms/build/review"

# The broker's ``review`` actions that take no text an agent writes: a concede's owner must be an agent in
# the room, a report's values come from fixed lists, and the others take a label or an index.
NO_TEXT = frozenset({"list", "show", "concur", "abstain", "concede", "confirm", "verdict", "take", "report"})


class World:
    """The room #build with two agents: claude-1 wrote the pull request, codex-1 reviews it."""

    def __init__(self, b: InProcBroker, web: httpx.Client, author: FakeAgent, reviewer: FakeAgent):
        self.b, self.web, self.author, self.reviewer = b, web, author, reviewer

    async def review(self, agent: FakeAgent, *, ok: bool = True, **kw: Any) -> dict[str, Any]:
        r = await agent.call("review", room="#build", **kw)
        if ok:
            assert r.get("ok") is not False, r
        return r

    async def raised(self, **kw: Any) -> None:
        """codex-1 opens the board and raises F1."""
        await self.review(self.reviewer, action="open", url=PR)
        await self.review(self.reviewer, action="raise", **{"title": "wrong total", **kw})

    async def fixed(self, proof: str = "test_total") -> None:
        await self.raised()
        await self.review(self.author, action="concede", item="F1")
        await self.review(self.author, action="fix", item="F1", commit="abc1234", proof=proof)

    def person(self, **body: Any) -> None:
        r = self.web.post(PATH, json={"url": PR, **body}, headers=self.b.write_headers())
        assert r.status_code == 200, r.text


async def raise_title(w: World) -> None:
    await w.raised(title=f"leaks {SECRET}")


async def raise_detail(w: World) -> None:
    await w.raised(detail=f"the log prints {SECRET}")


async def raise_where(w: World) -> None:
    await w.raised(where=f"shop/{SECRET}.py:40-58")


async def raise_options(w: World) -> None:
    await w.raised(options=[f"rotate {SECRET}", "remove it"], recommend=1)


async def ask_title(w: World) -> None:
    await w.review(w.reviewer, action="open", url=PR)
    await w.review(w.reviewer, action="ask", title=f"keep {SECRET}?", options=["yes", "no"], recommend=1)


async def ask_options(w: World) -> None:
    await w.review(w.reviewer, action="open", url=PR)
    await w.review(w.reviewer, action="ask", title="keep it?", options=[f"keep {SECRET}", "no"], recommend=1)


async def agent_edit(w: World) -> None:
    await w.raised()
    await w.review(
        w.reviewer, action="edit", item="F1", title=f"t {SECRET}", detail=SECRET, where=f"{SECRET}.py:3"
    )


async def contest_reason(w: World) -> None:
    await w.raised()
    await w.review(w.author, action="contest", item="F1", reason=f"it is only {SECRET}")


async def insist_reason(w: World) -> None:
    await w.raised()
    await w.review(w.author, action="contest", item="F1", reason="not a bug")
    await w.review(w.reviewer, action="insist", item="F1", reason=f"it prints {SECRET}")


async def drop_reason(w: World) -> None:
    await w.raised()
    await w.review(w.reviewer, action="drop", item="F1", reason=f"fine with {SECRET}")


async def defer_ticket(w: World) -> None:
    await w.raised()
    await w.review(w.reviewer, action="defer", item="F1", reason=f"SHOP-1 {SECRET}")


async def fix_proof(w: World) -> None:
    await w.fixed(proof=f"test_total {SECRET}")


async def reopen_reason(w: World) -> None:
    await w.fixed()
    await w.review(w.reviewer, action="reopen", item="F1", reason=f"still prints {SECRET}")


async def close_reason(w: World) -> None:
    await w.raised()
    await w.review(w.reviewer, action="close", state="unknown", reason=f"moving on, {SECRET}")


async def close_asked_by_another(w: World) -> None:
    """Not the holder's close: refused, and a person is asked in the room with the reason."""
    await w.raised()
    r = await w.review(w.author, ok=False, action="close", state="unknown", reason=f"close it, {SECRET}")
    assert r.get("ok") is False and "A person has been asked" in r["error"], r


async def reset_reason(w: World) -> None:
    await w.raised()
    await w.review(w.reviewer, action="reset", url=PR, reason=f"start over, {SECRET}")


async def open_url(w: World) -> None:
    r = await w.review(w.reviewer, ok=False, action="open", url=f"{PR}?t={SECRET}")
    assert r.get("ok") is False and "looks like a secret" in r["error"], r


async def fix_proof_link(w: World) -> None:
    await w.raised()
    await w.review(w.author, action="concede", item="F1")
    r = await w.review(
        w.author,
        ok=False,
        action="fix",
        item="F1",
        commit="abc1234",
        proof=f"https://ci.example.com/?t={SECRET}",
    )
    assert r.get("ok") is False and "looks like a secret" in r["error"], r


async def person_edit(w: World) -> None:
    await w.raised()
    w.person(action="edit", item="F1", title=f"t {SECRET}", detail=SECRET, where=f"{SECRET}.py:3")


async def person_drop(w: World) -> None:
    await w.raised()
    w.person(action="drop", item="F1", reason=f"fine with {SECRET}")


async def person_defer(w: World) -> None:
    await w.raised()
    w.person(action="defer", item="F1", reason=f"SHOP-1 {SECRET}")


async def person_close(w: World) -> None:
    await w.raised()
    w.person(action="close", reason=f"moving on, {SECRET}")


Case = Callable[[World], Awaitable[None]]
# (the move, the agent's ``review`` action it is, or "" for a person's, and where its marker must be seen:
# "card" on what the page reads, "room" in the room's messages; a refused link is kept nowhere)
CASES: list[tuple[Case, str, set[str]]] = [
    (raise_title, "raise", {"card", "room"}),  # the line that asks the author's side to answer it
    (raise_detail, "raise", {"card"}),
    (raise_where, "raise", {"card"}),
    (raise_options, "raise", {"card"}),
    (ask_title, "ask", {"card", "room"}),
    (ask_options, "ask", {"card"}),
    (agent_edit, "edit", {"card"}),
    (contest_reason, "contest", {"card"}),
    (insist_reason, "insist", {"card"}),
    (drop_reason, "drop", {"card"}),
    (defer_ticket, "defer", {"card", "room"}),
    (fix_proof, "fix", {"card"}),
    (reopen_reason, "reopen", {"card", "room"}),  # the line that hands it back to its owner
    (close_reason, "close", {"room"}),
    (close_asked_by_another, "close", {"room"}),
    (reset_reason, "reset", {"room"}),
    (open_url, "open", set()),
    (fix_proof_link, "fix", set()),
    (person_edit, "", {"card"}),
    (person_drop, "", {"card", "room"}),
    (person_defer, "", {"card", "room"}),
    (person_close, "", {"room"}),
]


def kept_files(home: Path) -> dict[str, bytes]:
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


@pytest.mark.parametrize(("case", "action", "seen"), CASES, ids=[c[0].__name__ for c in CASES])
async def test_a_secret_on_the_board_is_kept_nowhere(
    tmp_home: Path, case: Case, action: str, seen: set[str]
) -> None:
    """The secret in one field of one move: in no file of the broker's home (lower-cased too, as the search
    index keeps a word), not found by search, not on the page, not in the room and not in what either agent
    reads; its marker is on the card and in the room where the field is shown there. Fails if that field is
    stored, shown or posted as written."""
    b = InProcBroker(tmp_home, FAST).start()
    web = b.web_client()
    try:
        assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
        async with FakeAgent(b.home, "ka") as author, FakeAgent(b.home, "kr") as reviewer:
            await author.join("#build", "claude-1")
            await reviewer.join("#build", "codex-1")
            w = World(b, web, author, reviewer)
            await case(w)
            read = [
                json.dumps(await author.read("#build")),
                json.dumps(await reviewer.read("#build")),
                *[
                    json.dumps(await w.review(reviewer, ok=False, action="show", item=i))
                    for i in ("F1", "Q1")
                ],
                json.dumps(await w.review(reviewer, ok=False, action="show")),
                json.dumps(await w.review(reviewer, ok=False, action="list")),
            ]
        page = web.get(PATH).json()["boards"]
        past = web.get(PATH + "/past").json()["boards"]
        page += [web.get(PATH + "/past", params={"url": rv["url"]}).json()["board"] for rv in past]
        messages = web.get("/api/rooms/build/messages").json()["messages"]
        hits = web.get("/api/search", params={"q": SECRET}).json()["hits"]
    finally:
        web.close()
        b.stop()
    files = kept_files(b.home)  # once the broker has stopped, so no file is being written or removed
    body = SECRET.split("_", 1)[1]
    leaks = [name for name, data in files.items() if SECRET.encode() in data or body.lower().encode() in data]
    assert leaks == [], f"the secret is kept in {leaks}"
    assert hits == []
    shown = {"card": json.dumps(page), "room": json.dumps(messages)}
    for where, text in [*shown.items(), *(("agent", r) for r in read)]:
        assert SECRET not in text, f"the secret is in {where}"
    for where in seen:
        assert MARK in shown[where], f"no marker in {where}: the field was not shown redacted"


def test_every_board_action_that_takes_text_has_a_case() -> None:
    """A new ``review`` action is classified here before it ships: it takes no text an agent writes, or it
    has a case above that puts a secret in its text. Fails when an action is added to ``ACTIONS`` with
    neither, so a new move can't skip the redaction unseen."""
    covered = {action for _case, action, _seen in CASES if action}
    assert set(ACTIONS) == covered | NO_TEXT, sorted(set(ACTIONS) ^ (covered | NO_TEXT))
    assert not covered & NO_TEXT
