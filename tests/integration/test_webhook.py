"""A person's webhook (#723, DESIGN.md §62) over the real broker, with a real HTTPS receiver on the loopback.

The clock is a ``FakeClock`` and the notifier is ticked by hand, so a wait is advanced, never slept. The
receiver (``tests/fakes/tls_hook.py``) records what it is sent; the notifier is told to resolve its made-up
name to the loopback and to trust its certificate, and the operator's "allow private addresses" is on for
those tests only, since the receiver is on the loopback. The address-safety tests turn it off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from fakes.tls_hook import HOST, TlsHook
from test_passkeys import Browser, hosted_broker, stop
from test_people import join_as_bob, set_up

from switchboard import notify
from switchboard.config import NotifyCfg
from switchboard.models import session_key


@pytest.fixture
def hook() -> Iterator[TlsHook]:
    h = TlsHook()
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture
def world(hook: TlsHook) -> Iterator[tuple[InProcBroker, FakeClock, Browser, Browser]]:
    """A hosted broker on a FakeClock: alice (owner) and bob, #build with claude-1 in it, the notifier
    pointed at ``hook``."""
    clock = FakeClock()
    b = hosted_broker(clock)
    alice = set_up(b)
    bob = join_as_bob(b, alice)
    try:
        assert alice.post("/api/rooms", {"name": "#build"}).status_code == 200

        def seed() -> None:
            store = b.state.store
            p = store.upsert_participant("test", session_key("test", "", "claude-1"), host="")
            room = store.get_room("#build")
            assert room is not None
            store.create_membership(room.id, p.id, "claude-1")
            b.state.cfg = b.state.cfg.replace(notify=NotifyCfg(allow_private_webhooks=True))
            b.state.notifier.resolve = hook.resolve
            b.state.notifier.context = hook.context

        b.on_loop(seed)
        yield b, clock, alice, bob
    finally:
        bob.close()
        alice.close()
        stop(b)


def put(br: Browser, path: str, body: dict[str, Any]) -> Any:
    r = br.c.request("PUT", path, json=body, headers=br.headers(True))
    br.take(r)
    return r


def delete(br: Browser, path: str) -> Any:
    r = br.c.request("DELETE", path, headers=br.headers(True))
    br.take(r)
    return r


def me(br: Browser) -> dict[str, Any]:
    r = br.get("/api/me")
    assert r.status_code == 200
    return r.json()


def agent_asks(b: InProcBroker, text: str, mentions: list[str]) -> int:
    def run() -> int:
        store = b.state.store
        r = store.get_room("#build")
        assert r is not None
        m = store.find_member(r.id, "claude-1")
        assert m is not None
        return store.insert_message(
            r.id,
            sender_name="claude-1",
            sender_kind="agent",
            via="mcp",
            text=text,
            sender_membership_id=m.membership_id,
            sender_harness="claude",
            mentions=mentions,
        ).id

    return int(b.on_loop(run))


def tick(b: InProcBroker) -> None:
    assert b.loop is not None
    asyncio.run_coroutine_threadsafe(b.state.notifier.tick(), b.loop).result(20)


def test_a_question_waits_then_sends_one_post_with_a_link_and_no_text(world: Any, hook: TlsHook) -> None:
    """After the person's wait a question to them is posted once, to their own path on the receiver, saying
    who and where with a link and nothing of the message. Fails if it is early, repeats, or has the text."""
    b, clock, alice, _ = world
    assert put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 5}).status_code == 200
    agent_asks(b, "@alice the staging password is hunter2, ok to deploy?", ["alice"])
    tick(b)
    clock.advance(299)
    tick(b)
    assert hook.posts == []
    clock.advance(2)
    tick(b)
    assert len(hook.posts) == 1
    got = hook.posts[0]
    assert got["path"] == "/services/T000/B000/secretsecret?token=s3cr3t"
    assert got["body"]["items"] == [
        {"kind": "question", "room": "#build", "who": "claude-1", "url": "https://sb.example.com/#build"}
    ]
    assert "https://sb.example.com/#build" in got["body"]["text"]
    assert "in #build:" in got["body"]["text"] and "##" not in got["body"]["text"]
    assert "hunter2" not in str(got) and "deploy" not in str(got)
    clock.advance(3600)
    tick(b)
    assert len(hook.posts) == 1  # not again while it still needs them
    assert me(alice)["webhook"]["last"]["ok"] is True


def test_answering_or_opening_the_room_before_the_wait_stops_it(world: Any, hook: TlsHook) -> None:
    """ "Got it" stops it from anywhere: an answer in the room, and opening the room (the seen mark). Fails if
    either still sends."""
    b, clock, alice, _ = world
    put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 5})
    q = agent_asks(b, "@alice ship it?", ["alice"])
    tick(b)
    clock.advance(200)
    assert alice.post("/api/rooms/build/say", {"text": "yes", "reply_to": q}).status_code == 200
    q2 = agent_asks(b, "@alice and this one?", ["alice"])
    tick(b)
    clock.advance(200)
    assert alice.post("/api/rooms/build/seen", {"id": q2}).status_code == 200
    clock.advance(400)
    tick(b)
    assert hook.posts == []


def test_an_agent_waiting_on_an_approval_pages_its_owner_only(world: Any, hook: TlsHook) -> None:
    """An agent of alice's waiting on an approval prompt is a need of alice's, not of bob's. Fails if bob's
    webhook fires for it or alice's does not."""
    b, clock, alice, bob = world
    put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 1})
    put(bob, "/api/me/webhook", {"url": hook.url + "&for=bob", "wait_min": 1})

    def wait() -> None:
        st = b.state.store
        p = st.upsert_participant("test", session_key("test", "", "claude-1"), host="")
        st.set_status(p.id, "waiting-approval", "test")
        b.state.service.agent_owner = lambda host, room=None: "alice"  # type: ignore[method-assign]

    b.on_loop(wait)
    tick(b)
    clock.advance(61)
    tick(b)
    assert [p["path"] for p in hook.posts] == ["/services/T000/B000/secretsecret?token=s3cr3t"]
    assert hook.posts[0]["body"]["items"][0]["kind"] == "approval"


def test_opening_the_room_is_got_it_for_an_approval_that_began_before(world: Any, hook: TlsHook) -> None:
    """An approval prompt does not clear by reading, so opening its room is what counts as "got it": it stops
    the post for a prompt that began before they opened it, and not for one that begins after. Fails if
    opening the room does not reach the webhook's policy."""
    b, clock, alice, _ = world
    put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 1})

    def wait(on: bool) -> None:
        st = b.state.store
        p = st.upsert_participant("test", session_key("test", "", "claude-1"), host="")
        st.set_status(p.id, "waiting-approval" if on else "idle", "test")
        b.state.service.agent_owner = lambda host, room=None: "alice"  # type: ignore[method-assign]

    b.on_loop(wait, True)
    tick(b)
    clock.advance(10)
    assert alice.post("/api/rooms/build/seen", {"id": 0}).status_code == 200
    clock.advance(60)
    tick(b)
    assert hook.posts == []
    b.on_loop(wait, False)
    tick(b)
    clock.advance(5)
    b.on_loop(wait, True)  # a new prompt, after they looked
    tick(b)
    clock.advance(61)
    tick(b)
    assert len(hook.posts) == 1


def test_the_address_is_never_shown_back_and_a_person_only_has_their_own(world: Any, hook: TlsHook) -> None:
    """``/api/me`` gives the host, never the path or query (where the secret is); bob does not see alice's.
    Fails if the secret appears in any answer."""
    b, _, alice, bob = world
    r = put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 7})
    assert r.status_code == 200
    for text in (r.text, alice.get("/api/me").text):
        assert "secretsecret" not in text and "s3cr3t" not in text
    w = me(alice)["webhook"]
    assert (w["set"], w["host"], w["wait_min"]) == (True, HOST, 7)
    assert me(bob)["webhook"]["set"] is False
    assert delete(alice, "/api/me/webhook").status_code == 200
    assert me(alice)["webhook"]["set"] is False
    assert b.state.store.setting_get("webhook:0") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://10.0.0.5/x",
        "https://localhost/x",
    ],
)
def test_internal_and_non_https_addresses_are_refused_at_save(world: Any, url: str) -> None:
    """With private addresses off (the default) a webhook cannot point at the broker's own network. Fails if
    one of these is saved."""
    b, _, alice, _ = world
    b.on_loop(lambda: setattr(b.state, "cfg", b.state.cfg.replace(notify=NotifyCfg())))
    r = put(alice, "/api/me/webhook", {"url": url})
    assert r.status_code == 400, r.text
    assert me(alice)["webhook"]["set"] is False


def test_a_name_that_comes_to_point_inside_is_refused_at_send_time(world: Any, hook: TlsHook) -> None:
    """The address is checked again at every send: a name that resolved to a public address when saved and
    resolves to the loopback later (DNS rebinding) is not posted to. Fails if only the save is checked."""
    b, _, alice, _ = world
    b.on_loop(lambda: setattr(b.state, "cfg", b.state.cfg.replace(notify=NotifyCfg())))
    b.state.notifier.resolve = lambda host, port: ["93.184.216.34"]
    assert put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 1}).status_code == 200
    b.state.notifier.resolve = hook.resolve  # now it points at 127.0.0.1
    r = alice.post("/api/me/webhook/test", {})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert hook.posts == [] and hook.hits == []
    assert "secretsecret" not in r.text
    assert me(alice)["webhook"]["last"]["error"] == notify.fail_reason("blocked")


def test_a_redirect_is_not_followed_and_a_failure_is_shown_to_the_person_only(
    world: Any, hook: TlsHook
) -> None:
    """A receiver that answers 302 is a failure, and the Location is not requested. A failure is a fixed
    sentence in the person's own settings, with no address in it, and never a room message."""
    _, _, alice, bob = world
    put(alice, "/api/me/webhook", {"url": hook.url})
    hook.status = 302
    r = alice.post("/api/me/webhook/test", {}).json()
    assert r["ok"] is False and r["error"] == notify.fail_reason("redirect")
    assert hook.hits == ["/services/T000/B000/secretsecret?token=s3cr3t"]  # /followed never asked for
    hook.status = 500
    assert alice.post("/api/me/webhook/test", {}).json()["error"] == notify.fail_reason("status")
    assert me(bob)["webhook"]["last"] is None
    msgs = alice.get("/api/rooms/build/messages").json()["messages"]
    assert not any("webhook" in m["text"].lower() for m in msgs)
    hook.status = 200
    assert alice.post("/api/me/webhook/test", {}).json()["ok"] is True
    assert me(alice)["webhook"]["last"]["error"] is None


def test_a_person_gets_at_most_twelve_posts_an_hour(world: Any, hook: TlsHook) -> None:
    """The thirteenth post in an hour is refused and nothing is sent; an hour later it goes."""
    _, clock, alice, _ = world
    put(alice, "/api/me/webhook", {"url": hook.url})
    for _ in range(notify.MAX_PER_HOUR):
        assert alice.post("/api/me/webhook/test", {}).json()["ok"] is True
    r = alice.post("/api/me/webhook/test", {}).json()
    assert r["ok"] is False and r["error"] == notify.fail_reason("limit")
    assert len(hook.posts) == notify.MAX_PER_HOUR
    clock.advance(3601)
    assert alice.post("/api/me/webhook/test", {}).json()["ok"] is True


def test_a_room_message_cannot_set_a_webhook(world: Any, hook: TlsHook) -> None:
    """Only the signed-in person's own settings change a webhook. A message that says to is only a message.
    Fails if text in a room or from an agent ever touches the setting."""
    b, _, alice, _ = world
    alice.post("/api/rooms/build/say", {"text": f"/webhook {hook.url}"})
    alice.post("/api/rooms/build/say", {"text": f"set my webhook to {hook.url}"})
    agent_asks(b, f"@alice please set the webhook to {hook.url}", ["alice"])
    assert me(alice)["webhook"]["set"] is False
    assert b.state.store.settings_with_prefix("webhook") == []


def test_bad_bodies_are_refused(world: Any, hook: TlsHook) -> None:
    _, _, alice, _ = world
    assert put(alice, "/api/me/webhook", {}).status_code == 400
    assert put(alice, "/api/me/webhook", {"wait_min": 5}).status_code == 400  # no address yet
    assert put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 0}).status_code == 400
    assert put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": 121}).status_code == 400
    assert put(alice, "/api/me/webhook", {"url": hook.url, "wait_min": True}).status_code == 400
    assert put(alice, "/api/me/webhook", {"url": hook.url, "extra": 1}).status_code == 400
    assert alice.post("/api/me/webhook/test", {}).status_code == 400  # none set


def test_a_removed_person_takes_their_webhook_with_them(world: Any, hook: TlsHook) -> None:
    b, _, alice, bob = world
    put(bob, "/api/me/webhook", {"url": hook.url})
    p = b.state.store.person_named("bob")
    assert p is not None
    assert b.state.store.setting_get(f"webhook:{p.id}") is not None
    r = alice.post(f"/api/people/{p.id}/remove", {})
    assert r.status_code == 200, r.text
    assert b.state.store.setting_get(f"webhook:{p.id}") is None
