"""A room's hands-free calls: voice on and agents listening, set by a person (#697, DESIGN.md §52.3).

The broker is the real one; joinly is ``FakeJoinly`` for the server's bot, and a scripted relay registers
a call hosted on a person's machine. The point of every test here is who can set the two settings (only a
person, never an agent's ``say``) and that a call starts the way the room's settings say.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_hosted import Host, World, host
from test_meeting_hosted import world as world  # noqa: F401  (a fixture)
from test_meeting_voice import broker as broker  # noqa: F401
from test_meeting_voice import call_state, start_call
from test_meeting_voice import joinly as joinly  # noqa: F401
from test_meetings import command, history, notices, wait_until


def settings(b: InProcBroker) -> dict[str, Any]:
    r = b.web.get("/api/rooms/build/members", headers=b.write_headers())  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return r.json()["settings"]


def listening(b: InProcBroker) -> dict[str, bool]:
    r = b.web.get("/api/rooms/build/members", headers=b.write_headers())  # type: ignore[attr-defined]
    return {m["name"]: m["listen"] for m in r.json()["members"]}


async def test_an_agents_say_of_the_room_commands_changes_nothing(broker: InProcBroker) -> None:
    """Both settings let an agent speak in a call, or be woken by one, so a tricked agent must not be able to
    flip them: its ``say("/room voice on")`` is stored as text like every command. Fails if the text is
    parsed, or the room gains a setting or a notice."""
    async with FakeAgent(broker.home, "k1") as agent:
        assert (await agent.join("#build", "alpha"))["ok"]
        for text in ("/room voice on", "/room listen on"):
            assert (await agent.say("#build", text))["ok"]
        assert settings(broker)["hands_free"] == {"voice": False, "listen": False}
        said = [m["text"] for m in history(broker) if m["kind"] == "chat"]
        assert said == ["/room voice on", "/room listen on"]
        assert "turned" not in notices(broker)


def test_a_person_sets_each_setting_and_the_room_says_who(broker: InProcBroker) -> None:
    """The web session sets them; each change leaves a notice naming the person; the setting is in the room's
    frame for everyone; asking again changes nothing. Fails if a notice is missing or lacks the name."""
    assert "hands-free voice is already off" in command(broker, "/room voice off")["text"]
    command(broker, "/room voice on")
    assert settings(broker)["hands_free"] == {"voice": True, "listen": False}
    assert "alice turned on hands-free voice" in notices(broker)
    assert "already on" in command(broker, "/room voice on")["text"]
    command(broker, "/room listen on")
    assert settings(broker)["hands_free"] == {"voice": True, "listen": True}
    assert "alice turned on hands-free listening" in notices(broker)
    command(broker, "/room voice off")
    command(broker, "/room listen off")
    assert settings(broker)["hands_free"] == {"voice": False, "listen": False}
    assert "alice turned off hands-free voice" in notices(broker)
    assert "voice is off" in command(broker, "/room")["text"]


def test_a_bad_room_command_is_refused(broker: InProcBroker) -> None:
    """Anything but ``voice|listen on|off`` is a usage error, not a guess."""
    for text in ("/room voice", "/room speak on", "/room voice maybe", "/room voice on now"):
        r = broker.web.post(  # type: ignore[attr-defined]
            "/api/rooms/build/command", json={"text": text}, headers=broker.write_headers()
        )
        assert r.status_code == 400 and "usage: /room" in r.text


async def test_a_call_starts_with_voice_on_when_the_room_says_so_and_a_person_can_turn_it_off(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """With the setting on, the bot's call starts with Voice on and its first notice says why; turning Voice
    off on the call is for that call only, so the next one starts on again. Fails if the call starts
    off, the notice is silent, or the person's switch is overridden."""
    command(broker, "/room voice on")
    await start_call(broker, joinly)
    assert (call_state(broker) or {})["voice"] is True
    joining = [t for t in notices(broker).splitlines() if "joining" in t]
    assert joining and "Voice is on: this room's setting." in joining[0]
    command(broker, "/meeting voice off")
    assert (call_state(broker) or {})["voice"] is False
    command(broker, "/meeting leave")
    await wait_until(lambda: bool(joinly.leaves))
    await wait_until(lambda: call_state(broker) is None)
    assert settings(broker)["hands_free"]["voice"] is True  # the room keeps the setting
    await start_call(broker, joinly)
    assert (call_state(broker) or {})["voice"] is True


async def test_a_call_in_a_room_without_the_setting_starts_with_voice_off(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The default is unchanged: Voice is off and the first notice says nothing about a setting."""
    await start_call(broker, joinly)
    assert (call_state(broker) or {})["voice"] is False
    assert "this room's setting" not in notices(broker)


async def test_agents_listen_for_the_whole_call_including_one_that_joins_late(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """With the setting on every agent listens without ``/meeting listen``, one that joins after the call
    started too; a person can turn one off for the call and it stays off. Fails if an agent is missed, a
    late one is, or the person's off is undone."""
    command(broker, "/room listen on")
    async with FakeAgent(broker.home, "k1") as early, FakeAgent(broker.home, "k2") as late:
        assert (await early.join("#build", "alpha"))["ok"]
        await start_call(broker, joinly)
        assert listening(broker) == {"alpha": True}
        assert "Agents listen: this room's setting." in notices(broker)
        assert (await late.join("#build", "beta"))["ok"]
        assert listening(broker) == {"alpha": True, "beta": True}
        command(broker, "/meeting listen alpha off")
        assert listening(broker) == {"alpha": False, "beta": True}
        command(broker, "/meeting leave")
        await wait_until(lambda: not any(listening(broker).values()))  # a call's end clears them


async def test_agents_do_not_listen_when_the_room_has_no_setting(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """Listening stays opt-in per agent in a room without the setting, late joiners included."""
    async with FakeAgent(broker.home, "k1") as agent:
        await start_call(broker, joinly)
        assert (await agent.join("#build", "alpha"))["ok"]
        assert listening(broker) == {"alpha": False}


async def test_turning_a_setting_on_reaches_the_call_that_is_live(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A person on a phone turns it on mid-call and it takes effect at once, for voice and for listening.
    Turning it off leaves that call as it is. Fails if only the next call is affected."""
    async with FakeAgent(broker.home, "k1") as agent:
        assert (await agent.join("#build", "alpha"))["ok"]
        await start_call(broker, joinly)
        assert (call_state(broker) or {})["voice"] is False and listening(broker) == {"alpha": False}
        command(broker, "/room voice on")
        command(broker, "/room listen on")
        assert (call_state(broker) or {})["voice"] is True and listening(broker) == {"alpha": True}
        command(broker, "/room voice off")
        assert (call_state(broker) or {})["voice"] is True


async def test_a_hosted_call_starts_with_voice_on_and_listening_agents(world: World) -> None:  # noqa: F811
    """The same for a call hosted on a person's machine (§54): voice on at registration, every agent
    listening, and the registration notice says so. Fails if only the server's bot honours the room."""
    w = world
    w.command("/room voice on")
    w.command("/room listen on")
    claude = Host(w.b)
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        assert (await host(claude))["ok"]
        assert w.settings()["meeting"]["voice"] is True
        assert w.relay.speech().json()["voice"] is True
        note = w.notices()[-1]
        assert note.startswith("alice is hosting a Zoom call from alice-laptop. Voice is on:")
        assert "Agents listen: this room's setting." in note
        con = sqlite3.connect(w.b.paths.db)
        assert con.execute("SELECT listen FROM memberships").fetchall() == [(1,)]
        con.close()
    finally:
        await claude.close()


async def test_leaving_and_joining_again_does_not_reset_an_agents_wake_cap_for_the_call(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The per-call cap bounds how often call talk, which anyone on the call can make, wakes an agent. With
    the room's setting on, a rejoin listens again at once: it must carry the wakes its earlier membership
    spent on this call, and not listen at all once they reach the cap. Fails if a leave and a join, which an
    agent can do by itself, hands it a fresh 40."""
    cap = broker.state.cfg.delivery.listen_max_wakes  # type: ignore[attr-defined]
    store = broker.state.store  # type: ignore[attr-defined]

    def spend(name: str, n: int) -> None:
        m = next(m for m in store.room_memberships(store.get_room("#build").id) if m.screen_name == name)
        for _ in range(n):
            store.count_listen_wake(m.id, cap)

    def wakes(name: str) -> int:
        m = next(m for m in store.room_memberships(store.get_room("#build").id) if m.screen_name == name)
        return m.listen_wakes

    command(broker, "/room listen on")
    async with FakeAgent(broker.home, "k1") as a:
        assert (await a.join("#build", "alpha"))["ok"]
        await start_call(broker, joinly)
        broker.on_loop(lambda: spend("alpha", 5))
        assert (await a.leave("#build"))["ok"]
        assert (await a.join("#build", "alpha"))["ok"]
        assert listening(broker) == {"alpha": True}
        assert broker.on_loop(lambda: wakes("alpha")) == 5
        broker.on_loop(lambda: spend("alpha", cap - 5))  # the last one turns listening off
        assert listening(broker) == {"alpha": False}
        assert (await a.leave("#build"))["ok"]
        assert (await a.join("#build", "alpha"))["ok"]
        assert listening(broker) == {"alpha": False}
        command(broker, "/meeting leave")
