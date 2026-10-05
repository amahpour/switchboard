"""Room rules may be changed only through an authorized human path."""

from __future__ import annotations

import pytest

from switchboard import cli
from switchboard.broker.rpc import build_methods
from switchboard.remote.proto import REMOTE_METHODS


def test_room_rules_rpc_is_human_cli_only() -> None:
    """An MCP member or remote satellite cannot call the room-rules method."""
    methods = build_methods(None)  # type: ignore[arg-type]  # handlers resolve state only when called
    assert methods["room.rules"].role == "human_cli"
    assert "room.rules" not in REMOTE_METHODS


def test_rooms_rules_cli_uses_the_human_rpc_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI parses a set command and sends only its room and bounded text to the broker."""
    sent = []
    monkeypatch.setattr(cli, "_satellite_home", lambda args: False)
    monkeypatch.setattr(
        cli,
        "_call",
        lambda args, method, params: sent.append((method, params)) or {"rules": params.get("text", "")},
    )
    args = cli.build_parser().parse_args(["rooms", "rules", "#build", "--set", "Post a PR link."])
    assert args.func(args) == cli.EXIT_OK
    assert sent == [("room.rules", {"room": "#build", "text": "Post a PR link."})]
    assert capsys.readouterr().out.strip() == "Post a PR link."
