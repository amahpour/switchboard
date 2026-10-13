"""Room renaming (#316): live MCP membership, name validation and web authority."""

from __future__ import annotations

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_link import FakeLink

from switchboard.broker.commands import Actor
from switchboard.broker.service import ServiceError


async def test_rename_keeps_an_existing_agent_and_redirects_new_joins(broker: InProcBroker) -> None:
    """An agent's old MCP room key and credential survive; a new join learns the new name."""
    web = broker.web_client()
    headers = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#old"}, headers=headers).status_code == 200
    async with FakeAgent(broker.home, "room-rename-a") as agent:
        assert (await agent.join("#old", "worker"))["ok"]
        res = web.put("/api/rooms/old/name", json={"name": "#new"}, headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["room"]["name"] == "#new"
        assert "renamed #old to #new" in (await agent.read("#old"))["text"]
        said = await agent.say("#old", "still working")
        assert said["ok"], said
        rows = web.get("/api/rooms/new/messages").json()["messages"]
        assert any(m["text"] == "alice renamed #old to #new" for m in rows)
        assert any(m["text"] == "still working" for m in rows)
        async with FakeAgent(broker.home, "room-rename-b") as newcomer:
            joined = await newcomer.join("#old", "newcomer")
            assert not joined["ok"] and "#new" in joined["error"]
    web.close()


async def test_an_agent_can_use_the_new_name_without_joining_again(broker: InProcBroker) -> None:
    """After a rename, everything an agent reads names the room by its new name, but its MCP
    server held the membership under the name it joined with, so following that text answered
    "you are not in #new". The server now asks its memberships what their rooms are called and
    adopts the new name: both names work, nothing is re-joined, and a room the agent was never
    in is still refused."""
    web = broker.web_client()
    headers = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#old"}, headers=headers).status_code == 200
    async with FakeAgent(broker.home, "room-rename-follow") as agent:
        assert (await agent.join("#old", "worker"))["ok"]
        assert web.put("/api/rooms/old/name", json={"name": "#new"}, headers=headers).status_code == 200
        text = (await agent.read("#new"))["text"]  # the name the notice itself taught it
        assert "renamed #old to #new" in text
        who = await agent.who("#new")
        assert who["ok"] and who["room"] == "#new", who
        assert (await agent.who("#old"))["ok"]  # the old key still works
        said = await agent.say("#new", "found it")
        assert said["ok"], said
        other = await agent.who("#never-joined")
        assert not other["ok"] and other["code"] == "not_member", other
        rows = web.get("/api/rooms/new/messages").json()["messages"]
        assert [m["kind"] for m in rows].count("join") == 1  # it never joined again
        assert any(m["text"] == "found it" for m in rows)
    web.close()


async def test_rename_command_is_web_only_and_aliases_survive_another_rename(broker: InProcBroker) -> None:
    """Only a browser can rename; all old MCP room keys keep their memberships."""
    web = broker.web_client()
    h = broker.write_headers()
    assert web.post("/api/rooms", json={"name": "#first"}, headers=h).status_code == 200
    with pytest.raises(ServiceError, match="web session"):
        broker.on_loop(
            lambda: broker.state.service.command(
                "#first", "/rename second", Actor(role="human_cli", via="cli")
            )
        )
    async with FakeAgent(broker.home, "room-rename-command") as agent:
        assert (await agent.join("#first", "worker"))["ok"]
        first = web.post("/api/rooms/first/command", json={"text": "/rename second"}, headers=h)
        assert first.status_code == 200 and first.json()["ok"]
        second = web.post("/api/rooms/second/command", json={"text": "/rename third"}, headers=h)
        assert second.status_code == 200 and second.json()["ok"]
        assert (await agent.say("#first", "still in third"))["ok"]
        async with FakeAgent(broker.home, "room-rename-newcomer") as newcomer:
            assert "#third" in (await newcomer.join("#first", "other"))["error"]
        assert web.post("/api/rooms", json={"name": "#first"}, headers=h).status_code == 409
    web.close()


def test_rename_refuses_invalid_taken_and_missing_rooms(broker: InProcBroker) -> None:
    """A rename uses creation's validation and cannot overwrite a room or a closed name."""
    web = broker.web_client()
    h = broker.write_headers()
    for name in ("#old", "#taken"):
        assert web.post("/api/rooms", json={"name": name}, headers=h).status_code == 200
    for name, status in (("bad name", 400), ("#taken", 409), ("#old", 409)):
        r = web.put("/api/rooms/old/name", json={"name": name}, headers=h)
        assert r.status_code == status, r.text
    assert web.put("/api/rooms/missing/name", json={"name": "#new"}, headers=h).status_code == 404
    web.close()


async def test_remote_member_keeps_old_allowlisted_name_after_reload() -> None:
    """A remote's old allowlist entry continues to authorize its existing member after rename."""
    with FakeLink(kind="inproc") as link:
        broker = link.broker
        async with FakeAgent(link.pi, "room-rename-remote") as agent:
            assert (await agent.join("#fpga", "bench"))["ok"]
            web = broker.web_client()
            try:
                renamed = web.put(
                    "/api/rooms/fpga/name", json={"name": "#board"}, headers=broker.write_headers()
                )
                assert renamed.status_code == 200, renamed.text
                assert "renamed #fpga to #board" in (await agent.read("#fpga"))["text"]
                entry = broker.state.remotes.links[link.name].entry
                assert broker.on_loop(lambda: broker.state.remotes.enforce(entry)) == 0
                assert (await agent.say("#fpga", "remote work continues"))["ok"]
            finally:
                web.close()
