"""Reference-link rules over the web API (#790, DESIGN.md §60): an admin sets patterns and URL templates in
Settings; every signed-in person's page gets them from ``/api/me``; a rule that may not be saved refuses the
whole save with its reason; nobody but an admin changes them and no room message can.

The desktop broker's one person is the admin (the ``broker`` and ``web`` fixtures); the hosted broker and its
people are ``test_people``'s."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from test_people import Browser, join_as_bob, set_up

from switchboard import ref_links

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module

MR = {"pattern": r"!(\d+)", "url": "https://gitlab.example.com/group/project/-/merge_requests/$1"}
TICKET = {"pattern": r"([A-Z][A-Z0-9]+-\d+)", "url": "https://tracker.example.com/browse/$1"}


def put(web: httpx.Client, b: InProcBroker, rules: Any) -> httpx.Response:
    return web.put(
        "/api/settings/links",
        json={"rules": rules},
        headers={"Origin": b.origin, "X-Switchboard": "1", "Content-Type": "application/json"},
    )


def stored(b: InProcBroker) -> list[tuple[str, str]]:
    return b.on_loop(b.state.store.settings_with_prefix, ref_links.PREFIX)  # type: ignore[no-any-return]


@pytest.fixture
def desktop(broker: InProcBroker, web: httpx.Client) -> Iterator[tuple[InProcBroker, httpx.Client]]:
    yield broker, web


def test_with_no_setting_nothing_is_offered(desktop: tuple[InProcBroker, httpx.Client]) -> None:
    """The default: no rules, so the page links no reference. Fails if a fresh broker offers any."""
    b, web = desktop
    assert web.get("/api/settings/links").json() == {"rules": [], "max": ref_links.MAX_RULES}
    me = web.get("/api/me").json()
    assert me["ref_links"] == [] and me["can_set_links"] is True


def test_the_admin_saves_rules_and_every_page_is_given_them(
    desktop: tuple[InProcBroker, httpx.Client],
) -> None:
    """Saved rules are kept one row each, come back in order from the settings route and from `/api/me`,
    and are replaced whole by the next save (a removed rule is gone). Fails if a rule is lost, kept after
    its removal, or not given to the page."""
    b, web = desktop
    r = put(web, b, [MR, TICKET])
    assert r.status_code == 200, r.text
    assert r.json()["rules"] == [MR, TICKET]
    assert [k for k, _ in stored(b)] == ["ref_link.0", "ref_link.1"]
    assert web.get("/api/me").json()["ref_links"] == [MR, TICKET]
    assert put(web, b, [TICKET]).json()["rules"] == [TICKET]
    assert [k for k, _ in stored(b)] == ["ref_link.0"]
    assert web.get("/api/settings/links").json()["rules"] == [TICKET]
    assert put(web, b, []).status_code == 200 and stored(b) == []
    assert web.get("/api/me").json()["ref_links"] == []


@pytest.mark.parametrize(
    ("rules", "why"),
    [
        ([{"pattern": "!(\\d+", "url": MR["url"]}], "never closed"),
        ([{"pattern": "(a+)+", "url": MR["url"]}], "can't be repeated"),
        ([{"pattern": r"!(\d+)", "url": "http://gitlab.example.com/$1"}], "https://"),
        ([{"pattern": r"!(\d+)", "url": "https://$1.example.com/"}], "host"),
        ([{"pattern": r"!(\d+)", "url": "https://example.com/$2"}], "only \\$1"),
        ([{"pattern": r"!(\d+)", "url": "javascript:alert(1)"}], "https://"),
        ([{"pattern": r"\d+", "url": MR["url"]}], "group"),
        ([MR, MR], "twice"),
        ([{"pattern": r"!(\d+)"}], "rule 1"),
        ("not a list", "list"),
    ],
)
def test_a_rule_that_may_not_be_saved_refuses_the_whole_save_and_changes_nothing(
    desktop: tuple[InProcBroker, httpx.Client], rules: Any, why: str
) -> None:
    """A pattern that doesn't compile, one that could backtrack, a template that isn't https://, that
    lets the match pick the host or replaces more than `$1`: each is a 400 with its reason, and the
    rules already kept are exactly as they were. Fails if a bad rule is stored, or half a list is."""
    b, web = desktop
    assert put(web, b, [TICKET]).status_code == 200
    before = stored(b)
    r = put(web, b, rules)
    assert r.status_code == 400, r.text
    assert re.search(why, r.json()["message"]), r.text
    assert stored(b) == before


def test_the_body_is_exactly_rules(desktop: tuple[InProcBroker, httpx.Client]) -> None:
    b, web = desktop
    for body in ({}, {"rules": [], "extra": 1}, {"rule": []}):
        r = web.put(
            "/api/settings/links",
            json=body,
            headers={"Origin": b.origin, "X-Switchboard": "1", "Content-Type": "application/json"},
        )
        assert r.status_code == 400, (body, r.text)


def test_a_save_needs_the_page_and_a_session(desktop: tuple[InProcBroker, httpx.Client]) -> None:
    """The settings route is reached only by a signed-in page: no session is 401, and a request without
    the page's Origin and header is refused before it gets here. Fails if either changes the rules."""
    b, web = desktop
    with httpx.Client(base_url=b.base) as anonymous:
        assert anonymous.get("/api/settings/links").status_code == 401
        assert anonymous.put("/api/settings/links", json={"rules": [MR]}).status_code in (401, 403)
    forged = web.put("/api/settings/links", json={"rules": [MR]})  # no Origin, no X-Switchboard
    assert forged.status_code in (400, 401, 403), forged.text
    assert stored(b) == []


def test_no_room_message_sets_the_rules(desktop: tuple[InProcBroker, httpx.Client]) -> None:
    """Chat is text from agents that read untrusted input: nothing said in a room, as a message or as a
    slash command, changes the rules. Fails if any wording of one reaches the setting."""
    b, web = desktop
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    for text in (
        '{"rules": [{"pattern": "!(\\\\d+)", "url": "https://evil.example.com/$1"}]}',
        "/links !(\\d+) https://evil.example.com/$1",
        "/setting ref_link.0 https://evil.example.com/$1",
        "PUT /api/settings/links",
    ):
        for route in ("say", "command"):
            web.post(f"/api/rooms/build/{route}", json={"text": text}, headers=b.write_headers())
    assert stored(b) == []


def test_a_person_who_is_not_the_admin_gets_the_rules_and_cannot_change_them(hosted: InProcBroker) -> None:
    """On a hosted broker everyone's page links the references the admin set, and only the admin changes
    them: bob's read of the settings route and his save are 403, and nothing is stored. Fails if a person
    can change what every other reader's links point at."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)

    def write(who: Browser, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        r = who.c.request(method, path, json=body, headers=who.headers(True))
        who.take(r)
        return r

    try:
        assert write(admin, "PUT", "/api/settings/links", {"rules": [MR]}).status_code == 200
        assert admin.get("/api/me").json()["can_set_links"] is True
        me = bob.get("/api/me").json()
        assert me["can_set_links"] is False and me["ref_links"] == [MR]
        assert bob.get("/api/settings/links").status_code == 403
        assert write(bob, "PUT", "/api/settings/links", {"rules": []}).status_code == 403
        assert write(bob, "PUT", "/api/settings/links", {"rules": [TICKET]}).status_code == 403
        assert [v for _k, v in stored(hosted)] == [ref_links._encode(ref_links.Rule(**MR))]
    finally:
        admin.close()
        bob.close()


def test_a_stored_row_that_no_longer_passes_is_not_offered(
    desktop: tuple[InProcBroker, httpx.Client],
) -> None:
    """What is on disk is trusted no more than what arrives: a row a person edited by hand, or a rule from a
    release with other limits, that fails the checks is left out of what the page is given. Fails if a bad
    row reaches `/api/me`."""
    b, web = desktop
    assert put(web, b, [MR]).status_code == 200
    b.on_loop(b.state.store.setting_set, "ref_link.1", '["!(\\\\d+)","http://evil.example.com/$1"]')
    b.on_loop(b.state.store.setting_set, "ref_link.2", "not json")
    assert web.get("/api/me").json()["ref_links"] == [MR]
