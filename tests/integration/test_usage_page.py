"""The usage page on a broker (#818, DESIGN.md §61): a person sees the rooms they can see, and
nothing of a private room they are not in. A missing session is refused. An offset the page does
not know is UTC."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from conftest import InProcBroker
from test_passkeys import hosted_broker, stop
from test_people import join_as_bob, set_up
from test_private_rooms import make_private

from switchboard.models import session_key


@pytest.fixture
def hosted() -> Iterator[InProcBroker]:
    b = hosted_broker()
    try:
        yield b
    finally:
        stop(b)


def _names(page: dict, key: str) -> list[str]:
    return [row["name"] for row in page[key]]


def test_a_person_sees_only_rooms_they_can_see(hosted: InProcBroker) -> None:
    """The bug this catches: the page counts a private room for someone who is not in it, or
    hides a public room. The counts are the batches and the posts that were stored."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)
    make_private(bob, "#secret")
    made = admin.post("/api/rooms", {"name": "#plaza"})
    assert made.status_code == 200, made.text

    def plant() -> None:
        store = hosted.state.store
        secret = store.get_room("#secret")
        plaza = store.get_room("#plaza")
        assert secret is not None and plaza is not None

        def member(room_id: int, name: str) -> int:
            person = store.upsert_participant("claude", session_key("claude", "", name))
            joined = store.create_membership(room_id, person.id, name)
            store.set_held(joined.id, True)
            store.set_status(person.id, "offline", "test")
            return joined.id

        def wake(mid: int, text: str) -> None:
            got = store.get_membership(mid)
            assert got is not None
            msg = store.insert_message(
                got.room_id, sender_name="alice", sender_kind="human", via="web", text=text
            )
            store.create_batch(mid, path="wait", kind="wake", counted=True, items=[(msg.id, 1)])

        quiet = member(secret.id, "quiet-1")
        for n in range(3):
            wake(quiet, f"secret {n}")
        store.insert_message(
            secret.id,
            sender_name="quiet-1",
            sender_kind="agent",
            via="mcp",
            text="noted",
            sender_membership_id=quiet,
            sender_harness="claude",
        )
        shown = member(plaza.id, "plaza-1")
        wake(shown, "in the open")
        for room, who in ((secret, "quiet-1"), (plaza, "plaza-1")):
            rv = store.open_review(room.id, "https://github.com/example/shop/pull/7", "", who)
            store.add_review_item(rv["id"], "finding", "raised", title="t", raised_by=who)

    hosted.on_loop(plant)

    bob_page = bob.get("/api/usage").json()
    admin_page = admin.get("/api/usage").json()
    assert "#secret" in _names(bob_page, "rooms")
    assert "quiet-1" in _names(bob_page, "agents")
    secret_row = next(r for r in bob_page["rooms"] if r["name"] == "#secret")
    assert secret_row["private"] is True
    assert (secret_row["wakes_week"], secret_row["posts_week"]) == (3, 1)
    assert "#secret" not in _names(admin_page, "rooms")
    assert "quiet-1" not in _names(admin_page, "agents")
    assert all(a["room"] != "#secret" for a in admin_page["agents"])
    for page in (bob_page, admin_page):
        assert "#plaza" in _names(page, "rooms")
        plaza_row = next(r for r in page["rooms"] if r["name"] == "#plaza")
        assert plaza_row["wakes_week"] == 1 and plaza_row["posts_week"] == 0
    # the review figures follow the same rule: a private room's boards are its people's alone (§44)
    assert sorted(r["name"] for r in bob_page["reviews"]["rooms"]) == ["#plaza", "#secret"]
    assert [r["name"] for r in admin_page["reviews"]["rooms"]] == ["#plaza"]
    assert {a["name"] for a in admin_page["reviews"]["agents"]} == {"plaza-1"}
    assert all(r["week"]["raised"] == 1 for r in bob_page["reviews"]["rooms"])


def test_usage_needs_a_session(broker: InProcBroker) -> None:
    got = httpx.get(broker.base + "/api/usage", timeout=10)
    assert got.status_code == 401


def test_a_bad_offset_is_read_as_utc(web: httpx.Client) -> None:
    """An offset that is not a whole number of minutes inside ±14 hours is UTC, never a wider window."""
    assert web.get("/api/usage").json()["offset_min"] == 0
    assert web.get("/api/usage?offset=nope").json()["offset_min"] == 0
    assert web.get("/api/usage?offset=99999").json()["offset_min"] == 0
    assert web.get("/api/usage?offset=-480").json()["offset_min"] == -480
