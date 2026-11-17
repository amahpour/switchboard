"""A secret in a chat message is cut out before it is stored (#819, DESIGN.md §11), for a person and an agent.

An in-process broker, a scripted agent that is a real MCP client of ``/mcp``, the web API and a WebSocket.
What these catch: a secret that reaches the stored row, the search index, a WebSocket frame or another reader
(it has to be absent from all of them); a sender who is never told; and a notice that carries the value."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker, cookie_of, ws_connect
from fakes.fake_agent import FakeAgent
from test_mcp_agent import FAST
from test_ws_fanout import hello, recv_until

SECRET = "ghp" + "_" + "aB3" * 12
PASSWORD = "hunter2" + "hunter2"
WITH_SECRET = f"deploy with {SECRET} and password={PASSWORD} now"


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home, FAST, clock=FakeClock()).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web
    yield b
    web.close()
    b.stop()


def stored(b: InProcBroker, mid: int) -> str:
    m = b.on_loop(lambda: b.state.store.get_message(mid))
    assert m is not None
    return str(m.text)


def found(b: InProcBroker, word: str) -> list[int]:
    r = b.web.get("/api/search", params={"q": word})
    assert r.status_code == 200, r.text
    return [h["id"] for h in r.json()["hits"]]


def assert_gone(b: InProcBroker, mid: int, *, frame: dict[str, Any]) -> None:
    text = stored(b, mid)
    assert SECRET not in text and PASSWORD not in text
    assert "[redacted: github token]" in text and "password=[redacted: password or token]" in text
    assert text.startswith("deploy with ") and text.endswith(" now")  # the rest is as written
    assert (
        frame["msg"]["text"] == text and SECRET not in json.dumps(frame) and PASSWORD not in json.dumps(frame)
    )
    assert found(b, SECRET) == [] and found(b, PASSWORD) == []  # the index never held it
    assert mid in found(b, "deploy")


def test_a_secret_a_person_posts_is_cut_out_of_the_row_the_index_and_the_frame_and_they_are_told(
    broker: InProcBroker,
) -> None:
    """The web post: the stored row, the search index and the WebSocket frame all hold the marker, and the
    reply says what kind was removed (never the value), which the page shows under the message. Fails if the
    text is stored as written."""
    ws = ws_connect(broker, cookie_of(broker.web))
    try:
        hello(ws, ["#build"])
        recv_until(ws, lambda f: f.get("t") == "members")
        r = broker.web.post(
            "/api/rooms/build/say", json={"text": WITH_SECRET}, headers=broker.write_headers()
        )
        assert r.status_code == 200, r.text
        body = r.json()
        frame = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["id"] == body["id"])
    finally:
        ws.close()
    assert_gone(broker, body["id"], frame=frame)
    assert body["redacted"] == ["github token", "password or token"]
    assert (
        "best effort" in body["note"] and SECRET not in json.dumps(body) and PASSWORD not in json.dumps(body)
    )
    plain = broker.web.post(
        "/api/rooms/build/say", json={"text": "nothing here"}, headers=broker.write_headers()
    )
    assert "redacted" not in plain.json() and "note" not in plain.json()


async def test_a_secret_an_agent_says_is_cut_out_everywhere_and_the_agent_is_told_in_the_reply(
    broker: InProcBroker,
) -> None:
    """The agent's ``say``: the same three places, another agent reading the room gets the marker, and the
    tool's own reply carries the kinds and a note. Fails if an agent's say is stored as written, or the
    agent is not told."""
    ws = ws_connect(broker, cookie_of(broker.web))
    try:
        hello(ws, ["#build"])
        recv_until(ws, lambda f: f.get("t") == "members")
        async with FakeAgent(broker.home, "k1") as a, FakeAgent(broker.home, "k2") as other:
            await a.join("#build", "tester")
            await other.join("#build", "reader")
            res = await a.say("#build", WITH_SECRET)
            assert res["ok"] and res["posted_id"]
            mid = res["posted_id"]
            assert res["redacted"] == ["github token", "password or token"]
            assert "removed what looks like a secret" in res["note"]
            assert SECRET not in json.dumps(res) and PASSWORD not in json.dumps(res)
            seen = await other.read("#build")
            assert SECRET not in json.dumps(seen) and "[redacted: github token]" in json.dumps(seen)
            clean_say = await a.say("#build", "all good")
            assert "redacted" not in clean_say
        frame = recv_until(ws, lambda f: f.get("t") == "msg" and f["msg"]["id"] == mid)
    finally:
        ws.close()
    assert_gone(broker, mid, frame=frame)


async def test_text_over_the_length_limit_is_refused_before_anything_searches_it(
    broker: InProcBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is checked first, for a person and an agent, so a huge message costs a length check and not
    a scan. Fails (the patched redact raises) if redact runs on text that is then refused, and shows that a
    text just under the limit that redaction lengthens is not refused."""
    from switchboard.broker import agents, service

    def boom(text: str) -> None:
        raise AssertionError("redact ran on text over the limit")

    big = "x" * (broker.cfg.delivery.max_msg_chars + 1)
    with monkeypatch.context() as mp:
        mp.setattr(service, "redact", boom)
        mp.setattr(agents, "redact", boom)
        r = broker.web.post("/api/rooms/build/say", json={"text": big}, headers=broker.write_headers())
        assert r.status_code == 400 and "too long" in r.text
        async with FakeAgent(broker.home, "k1") as a:
            await a.join("#build", "tester")
            res = await a.say("#build", big)
            assert res["ok"] is False and "too long" in res["error"]
    # just under the limit, with a short secret whose marker is longer: redaction lengthens it, and it posts
    pad = "x" * (broker.cfg.delivery.max_msg_chars - len("AKIA" + "ABCDEFGH12345678") - 1)
    r = broker.web.post(
        "/api/rooms/build/say",
        json={"text": pad + " " + "AKIA" + "ABCDEFGH12345678"},
        headers=broker.write_headers(),
    )
    assert r.status_code == 200, r.text
    assert "[redacted: aws access key]" in stored(broker, r.json()["id"])
