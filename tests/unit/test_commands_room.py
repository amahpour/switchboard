"""/room: a room's hands-free call settings are a person's (#697, DESIGN.md §52.3). Parsing and roles."""

from __future__ import annotations

import pytest
from test_commands_review import room

from switchboard.broker.commands import Actor, CommandError, check_role, parse_command, required_role


@pytest.mark.parametrize(
    "text, args",
    [
        ("/room", ()),
        ("/room voice on", ("voice", "on")),
        ("/room LISTEN Off", ("listen", "off")),
    ],
)
def test_room_parses_only_the_two_settings(text: str, args: tuple[str, ...]) -> None:
    assert parse_command(text).args == args


@pytest.mark.parametrize("text", ["/room voice", "/room speak on", "/room voice 1", "/room listen on now"])
def test_room_refuses_anything_else(text: str) -> None:
    with pytest.raises(CommandError):
        parse_command(text)


def test_changing_a_setting_needs_a_web_session_and_reading_does_not() -> None:
    """Both settings raise what agents do on a call, so the CLI person cannot set either on or off, as
    for ``/meeting voice``; reading changes nothing. Fails if the CLI can set one, or an anonymous caller
    (an agent is never a command caller at all) can do anything."""
    r = room()
    assert required_role(parse_command("/room"), r) == "human_cli"
    for text in ("/room voice on", "/room voice off", "/room listen on", "/room listen off"):
        cmd = parse_command(text)
        assert required_role(cmd, r) == "human"
        check_role(cmd, r, Actor(role="human", via="web"))
        for role in ("human_cli", "anon"):
            with pytest.raises(CommandError) as e:
                check_role(cmd, r, Actor(role=role, via="cli"))
            assert e.value.code == "forbidden"
