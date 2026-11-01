"""``who()`` shows each member's claims and status line, which only the web page showed (#545).

An agent that joins or resumes cannot tell what is taken unless it tries the same claim key and is
refused. The page already lists active claims (key and expiry) and the one-line status (stale after
30 minutes). ``who()`` now prints both, cleaned and cut with ``sanitize`` the way an away message is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent

from switchboard.claims import KEY_MAX
from switchboard.config import Config
from switchboard.envelope import sanitize
from switchboard.store import STATUS_LINE_STALE_S

# the cap ``status()`` already stores, and the cut ``who()`` applies on the way out (like away's 80)
STATUS_LINE_MAX = 160


def _broker(home: Path, clock: FakeClock | None = None) -> InProcBroker:
    b = InProcBroker(home, Config(human_name="alice"), clock=clock).start()
    web = b.web_client()
    for room in ("#build", "#docs"):
        assert web.post("/api/rooms", json={"name": room}, headers=b.write_headers()).status_code == 200
    b.web = web
    return b


def _close(b: InProcBroker) -> None:
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def _line(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"- {name} "))


def _expiry(ts: float) -> str:
    """A whole second is printed without a trailing ``.0``, matching ``who()``."""
    whole = int(ts)
    return str(whole) if ts == whole else str(ts)


@pytest.fixture
def broker(tmp_home: Path):
    b = _broker(tmp_home)
    yield b
    _close(b)


@pytest.fixture
def timed(tmp_home: Path, clock: FakeClock):
    b = _broker(tmp_home, clock)
    yield b
    _close(b)


async def test_who_lists_each_members_claims_and_status_line(broker: InProcBroker) -> None:
    """A newcomer sees another member's active claims, each key with its expiry, and its status
    line, quoted like ``away``. Claims stay in the room they were taken in; the status line follows
    the session. Fails if ``who()`` still prints only harness, status, tier and away, or if a claim
    from #build shows up in #docs."""
    async with FakeAgent(broker.home, "who-a") as holder, FakeAgent(broker.home, "who-b") as newcomer:
        await holder.join("#build", "claude-1")
        await holder.join("#docs", "claude-1")
        await newcomer.join("#build", "codex-1")
        await newcomer.join("#docs", "codex-1")
        status = "editing <parser> yk:note"
        assert (await holder.call("status", text=status))["status_line"] == status
        build = await holder.call("claim", room="#build", key="build", ttl=60)
        piece = await holder.call("claim", room="#build", key="piece <4>", ttl=120)
        assert build["ok"] and piece["ok"]
        seen = await newcomer.who("#build")
        line = _line(seen["text"], "claude-1")
        assert f"status_line={sanitize(status, STATUS_LINE_MAX)}" in line
        assert "stale=" not in line
        build_bit = f'claim="build" expires={_expiry(build["expires_at"])}'
        piece_bit = f"claim={sanitize('piece <4>', KEY_MAX)} expires={_expiry(piece['expires_at'])}"
        assert build_bit in line and piece_bit in line
        assert line.index(build_bit) < line.index(piece_bit)  # the store's key order
        other = _line(seen["text"], "codex-1")
        assert "status_line=" not in other and "claim=" not in other
        assert "<" not in seen["text"] and ">" not in seen["text"]
        docs = await newcomer.who("#docs")
        docs_line = _line(docs["text"], "claude-1")
        assert f"status_line={sanitize(status, STATUS_LINE_MAX)}" in docs_line
        assert "claim=" not in docs_line


async def test_who_marks_a_stale_status_and_drops_an_expired_claim(
    timed: InProcBroker, clock: FakeClock
) -> None:
    """A status line is fresh until 30 minutes, then ``stale=yes``; a claim is listed until its
    expiry and gone at that instant (``expires_at > now``). Fails if ``who()`` keeps a finished
    claim or never says the line has gone stale."""
    async with FakeAgent(timed.home, "who-a") as holder, FakeAgent(timed.home, "who-b") as newcomer:
        await holder.join("#build", "claude-1")
        await newcomer.join("#build", "codex-1")
        assert (await holder.call("status", text="on the parser"))["status_line"] == "on the parser"
        held = await holder.call("claim", room="#build", key="build", ttl=30)
        assert held["ok"]
        bit = f'claim="build" expires={_expiry(held["expires_at"])}'

        async def current() -> str:
            seen = await newcomer.who("#build")
            return _line(seen["text"], "claude-1")

        fresh = await current()
        assert 'status_line="on the parser"' in fresh and "stale=" not in fresh and bit in fresh
        clock.advance(29)
        still = await current()
        assert "stale=" not in still and bit in still
        clock.advance(1)  # exactly the claim's deadline
        ended = await current()
        assert "claim=" not in ended and "stale=" not in ended
        clock.advance(STATUS_LINE_STALE_S - 30 - 1)
        almost = await current()
        assert 'status_line="on the parser"' in almost and "stale=" not in almost
        clock.advance(1)
        stale = await current()
        assert 'status_line="on the parser"' in stale and "stale=yes" in stale
        assert "claim=" not in stale


async def test_who_cuts_an_overlong_status_line_the_way_away_is_cut(broker: InProcBroker) -> None:
    """``status()`` already stores at most 160 characters, but the column does not, so ``who()``
    cuts with ``sanitize`` at that cap, the way ``away`` is cut at 80. A raw ``<`` or ``yk:`` in
    the row is quoted, not copied. Fails if the line is pasted whole or left unquoted."""
    raw = "<see yk:secret>\n" + ("x" * 200)
    async with FakeAgent(broker.home, "who-a") as holder, FakeAgent(broker.home, "who-b") as newcomer:
        await holder.join("#build", "claude-1")
        await newcomer.join("#build", "codex-1")

        def plant() -> None:
            store = broker.state.store
            room = store.get_room("#build")
            assert room is not None
            member = store.find_member(room.id, "claude-1")
            assert member is not None
            store.update_participant(member.participant_id, status_line=raw, status_line_at=store.clock.now())

        broker.on_loop(plant)
        seen = await newcomer.who("#build")
        line = _line(seen["text"], "claude-1")
        quoted = f"status_line={sanitize(raw, STATUS_LINE_MAX)}"
        assert quoted in line
        assert raw not in line and "<" not in line
        assert "read() shows full" in line  # the same truncation note ``away`` uses
