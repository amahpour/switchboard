"""A claim's note is the room's board (#545, DESIGN.md §47).

Two real MCP clients. The holder sets a note; ``who()`` shows it quoted and cleaned.
A renewal with no note keeps it, a renewal with one replaces it, and release clears it.
Another member cannot set it. The join says only how many items are on the board.
A note posts nothing and creates no delivery.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.config import Config
from switchboard.envelope import sanitize

NOTE = "review https://example.com/pr/56 <see>"


def _broker(home: Path) -> InProcBroker:
    b = InProcBroker(home, Config(human_name="alice")).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    return b


def _close(b: InProcBroker) -> None:
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def _line(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"- {name} "))


def _messages(b: InProcBroker) -> int:
    def read() -> int:
        room = b.state.store.get_room("#build")
        assert room is not None
        return b.state.store.count_messages(room.id)

    return b.on_loop(read)


def _pending(b: InProcBroker, name: str) -> int:
    def read() -> int:
        room = b.state.store.get_room("#build")
        assert room is not None
        member = b.state.store.find_member(room.id, name)
        assert member is not None
        return b.state.store.membership_delivery_counts(member.membership_id).get("pending", 0)

    return b.on_loop(read)


@pytest.fixture
def broker(tmp_home: Path):
    b = _broker(tmp_home)
    yield b
    _close(b)


async def test_who_shows_a_note_a_renewal_keeps_it_and_only_the_holder_sets_it(
    broker: InProcBroker,
) -> None:
    """The note is peer text, so ``who()`` quotes it like a claim key and escapes ``<``.
    An empty renewal leaves it; a new one replaces it; release removes the claim.
    The other member's attempt is a conflict and the note stays. Setting a note posts
    no message and creates no delivery. Fails if ``who()`` omits the note, a blank
    renewal clears it, or the other member can write it."""
    async with FakeAgent(broker.home, "note-a") as holder, FakeAgent(broker.home, "note-b") as other:
        await holder.join("#build", "claude-1")
        first = await holder.join("#build", "claude-1")  # rejoin, still no claims
        assert "on the board" not in first["text"]
        await other.join("#build", "codex-1")
        messages, pending = _messages(broker), _pending(broker, "codex-1")
        held = await holder.call("claim", room="#build", key="build", ttl=120, note=NOTE)
        assert held["ok"] and held["note"]
        assert _messages(broker) == messages and _pending(broker, "codex-1") == pending
        seen = await other.who("#build")
        line = _line(seen["text"], "claude-1")
        assert f'claim="build" note={sanitize(NOTE, 160)} expires=' in line
        assert "<" not in seen["text"] and "yk:" not in seen["text"]
        kept = await holder.call("claim", room="#build", key="build", ttl=180)
        assert kept["ok"] and kept["note"] == held["note"]
        still = _line((await other.who("#build"))["text"], "claude-1")
        assert f"note={sanitize(NOTE, 160)}" in still
        replaced = await holder.call("claim", room="#build", key="build", ttl=180, note="done")
        assert replaced["ok"] and replaced["note"] == "done"
        done = _line((await other.who("#build"))["text"], "claude-1")
        assert 'note="done"' in done and "example.com" not in done
        refused = await other.call("claim", room="#build", key="build", ttl=60, note="stolen")
        assert not refused["ok"] and "claude-1" in refused["error"]
        same = _line((await other.who("#build"))["text"], "claude-1")
        assert 'note="done"' in same
        released = await holder.call("claim", room="#build", key="build", ttl=0)
        assert released["ok"] and released["released"]
        gone = await other.who("#build")
        assert "claim=" not in _line(gone["text"], "claude-1")


async def test_join_counts_the_board_and_leaves_the_words_out(broker: InProcBroker) -> None:
    """With no claims the join has no board line. One claim says "1 item"; two say
    "2 items". The line names no key, note or holder: only a person may put text in
    front of every joining agent. Fails if the join prints the note, or the plural
    is wrong."""
    async with FakeAgent(broker.home, "join-a") as holder, FakeAgent(broker.home, "join-b") as newcomer:
        alone = await holder.join("#build", "claude-1")
        assert "on the board" not in alone["text"]
        await holder.call(
            "claim", room="#build", key="piece-4", ttl=300, note="queued https://example.com/pr/56"
        )
        one = await newcomer.join("#build", "codex-1")
        assert "1 item on the board; call who()." in one["text"]
        assert "1 items" not in one["text"]
        assert (
            "piece-4" not in one["text"] and "queued" not in one["text"] and "example.com" not in one["text"]
        )
        await holder.call("claim", room="#build", key="piece-5", ttl=300, note="review")
        two = await holder.join("#build", "claude-1")
        assert "2 items on the board; call who()." in two["text"]
        assert "piece-5" not in two["text"] and "review" not in two["text"]
