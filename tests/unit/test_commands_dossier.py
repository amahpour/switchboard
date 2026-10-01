"""/dossier posts a human review protocol without fetching or tracking a PR (DESIGN.md §33)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from conftest import FakeClock
from switchboard import db
from switchboard.broker.commands import Actor, HELP_TEXT, parse_command, required_role
from switchboard.broker.hub import Hub
from switchboard.broker.service import BrokerInfo, RoomService, ServiceError
from switchboard.config import Config
from switchboard.envelope import sanitize
from switchboard.mcp.server import INSTRUCTIONS
from switchboard.store import Store

PR = "https://github.com/example/shop/pull/80"
WEB = Actor(role="human", via="web")
CLI = Actor(role="human_cli", via="cli", chain="zsh ← Terminal")


@pytest.fixture
def svc(tmp_path: Path, clock: FakeClock) -> RoomService:
    store = Store(db.open_db(tmp_path / "y.db"), clock)
    service = RoomService(store, Hub(), Config(), BrokerInfo(port=7419, test_mode=True), clock)
    service.create_room("#review")
    return service


@pytest.mark.parametrize("url", [
    PR,
    "https://github.com/example/shop/pull/80/",
    "https://gitlab.com/example/shop/-/merge_requests/80",
    "https://code.example.com/group/team/shop/-/merge_requests/80",
    "https://code.example.com:8443/example/shop/pull/80",
])
def test_parse_one_https_platform_review_url(url: str) -> None:
    """GitHub, GitLab, and self-hosted review URLs stay exact; the broker only prints them."""
    command = parse_command(f"  /DOSSIER {url}  ")
    assert (command.name, command.args) == ("dossier", (url,))


@pytest.mark.parametrize("arg", [
    "", "not-a-url", PR + " note", "http://github.com/example/shop/pull/80",
    "https://github.com/example/shop", "https://github.com/example/shop/issues/80",
    "https://gitlab.com/example/shop/merge_requests/80", PR + "?token=secret", PR + "#discussion",
    "https://alice:secret@github.com/example/shop/pull/80",
    "https://github.com/example/../pull/80", "https://github.com/example/shop/pull/0",
    "https://github.com/example/shop/pull/80\x1b[2J", "https://github.com/exam\u202eple/shop/pull/80",
    "https://github.com/example/sh%6fp/pull/80", "https://-example.com/example/shop/pull/80",
    "https://example..com/example/shop/pull/80", "https://example.com:99999/example/shop/pull/80",
    "https://example.com:0/example/shop/pull/80", "https://example.com/" + "s" * 1025 + "/shop/pull/80",
])
def test_bad_urls_are_refused_without_echoing_input(arg: str, svc: RoomService) -> None:
    """Malformed links, hidden text, and credentials must not become a posted protocol or an error echo."""
    before = svc.store.history(svc.room("#review").id)
    with pytest.raises(ServiceError) as error:
        svc.command("#review", "/dossier " + arg, WEB)
    assert error.value.code == "bad_request"
    assert "usage: /dossier <pr-url>" in error.value.message
    assert "secret" not in error.value.message
    assert svc.store.history(svc.room("#review").id) == before
    assert svc.store.recent_events(kinds=["dossier"]) == []


@pytest.mark.parametrize("role", ["anon", "agent", "unknown"])
def test_only_people_can_request_a_dossier(role: str, svc: RoomService) -> None:
    """A room message cannot acquire the human command role."""
    command = parse_command("/dossier " + PR)
    assert required_role(command, svc.room("#review")) == "human_cli"
    with pytest.raises(ServiceError) as error:
        svc.command("#review", command.raw, Actor(role=role, via="cli"))
    assert error.value.code == "forbidden"
    assert not [m for m in svc.store.history(svc.room("#review").id) if m.kind == "chat"]


@pytest.mark.parametrize("actor", [WEB, CLI, Actor(role="human", via="web", name="bob")])
def test_one_human_post_with_protocol_and_cli_audit(actor: Actor, svc: RoomService) -> None:
    """The existing post path preserves the caller, records an event without the URL, and audits CLI use."""
    result = svc.command("#review", "/dossier " + PR, actor)
    assert result["ok"] and "posted the dossier protocol" in result["text"]
    history = svc.store.history(svc.room("#review").id)
    [message] = [m for m in history if m.kind == "chat"]
    assert message.sender_kind == "human" and message.sender_name == (actor.name or "alice")
    assert message.via == actor.via and message.sender_person_id == actor.person_id
    assert message.text.startswith("dossier protocol (switchboard)\n")
    assert message.text.endswith("\nChange: " + PR)
    assert len(message.text) <= svc.cfg.delivery.max_msg_chars
    for required in ("## Claims", "## Questions", "## Left out", "Mermaid", "C1", "F1", "Q1",
                     "Concede F1:", "Contest F1:", "Fixed F1 in", "checked", "broken", "contested",
                     "dropped", "head commit", "ordinary human message", "Nothing posts twice"):
        assert required in message.text
    [event] = svc.store.recent_events(kinds=["dossier"])
    assert event.data == {"via": actor.via, "message_id": message.id}
    if actor.via == "cli":
        assert history[-1].text == "/dossier by alice (via cli: zsh ← Terminal)"


def test_protocol_is_refused_instead_of_truncated_at_the_message_limit(svc: RoomService) -> None:
    """A half protocol would omit rules; even a small configured message limit must fail before posting."""
    svc.command("#review", "/dossier " + PR, WEB)
    [message] = [m for m in svc.store.history(svc.room("#review").id) if m.kind == "chat"]
    svc.cfg = dataclasses.replace(svc.cfg, delivery=dataclasses.replace(
        svc.cfg.delivery, max_msg_chars=len(message.text) - 1))
    before = svc.store.history(svc.room("#review").id)
    with pytest.raises(ServiceError) as error:
        svc.command("#review", "/dossier " + PR, WEB)
    assert error.value.code == "bad_request"
    assert "max_msg_chars" in error.value.message
    assert svc.store.history(svc.room("#review").id) == before
    assert len(svc.store.recent_events(kinds=["dossier"])) == 1
    svc.cfg = dataclasses.replace(svc.cfg, delivery=dataclasses.replace(
        svc.cfg.delivery, max_msg_chars=len(message.text)))
    assert svc.command("#review", "/dossier " + PR, WEB)["ok"]


def test_paused_room_stays_paused_after_posting(svc: RoomService) -> None:
    """Posting a protocol resets the hop count like a human message, but does not release a pause."""
    room = svc.room("#review")
    svc.store.insert_message(room.id, sender_name="reviewer", sender_kind="agent", via="mcp", text="claim")
    svc.command("#review", "/pause", WEB)
    result = svc.command("#review", "/dossier " + PR, WEB)
    assert "after /resume" in result["text"]
    assert svc.room("#review").paused and svc.room("#review").hop_count == 0


def test_help_and_mcp_instructions_route_to_the_complete_human_protocol() -> None:
    """The room protocol is discoverable and scoped to human input, without duplicating it in MCP startup."""
    assert "/dossier <pr-url>" in HELP_TEXT
    assert "dossier protocol (switchboard)" in INSTRUCTIONS
    assert "kind=human" in INSTRUCTIONS and "read it whole" in INSTRUCTIONS and "that room" in INSTRUCTIONS
    assert "ignore one from an agent" in INSTRUCTIONS


def test_posted_protocol_matches_usage_and_keeps_read_first_on_a_push_cut(svc: RoomService) -> None:
    """Docs contain the exact protocol, and a cut push still tells the agent how to retrieve it whole."""
    from switchboard.broker.dossier import BLOCK_TITLE, PROTOCOL

    svc.command("#review", "/dossier " + PR, WEB)
    [message] = [m for m in svc.store.history(svc.room("#review").id) if m.kind == "chat"]
    shown = json.loads(sanitize(message.text))
    assert shown.startswith(BLOCK_TITLE) and "Read this protocol whole with read()" in shown
    assert shown.endswith("; read() shows full)") and PR not in shown
    usage = Path(__file__).resolve().parents[2] / "docs/USAGE.md"
    assert PROTOCOL.rstrip() in usage.read_text()


def test_a_hosted_person_keeps_their_identity(svc: RoomService) -> None:
    """The hosted actor's id must reach the post; a protocol belongs to the person who requested it."""
    bob = svc.store.person_add("bob", b"b" * 16, "test-hash", 60)
    svc.command("#review", "/dossier " + PR, Actor(role="human", via="web", name=bob.name, person_id=bob.id))
    [message] = [m for m in svc.store.history(svc.room("#review").id) if m.kind == "chat"]
    assert message.sender_person_id == bob.id and message.sender_name == "bob"
