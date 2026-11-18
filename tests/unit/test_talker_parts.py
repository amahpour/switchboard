"""The call talker's pure parts and its model client (#806, DESIGN.md §52.11): no broker, no network.

What these catch: a sentence cut in the middle ("3.5"); a hand-off to something that is not an agent in the
room;
an unknown line of the model's reply taken for an action; the key in a repr, an exception or an error message;
a bad key or model name accepted in the config; and the command taking more than on and off."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from switchboard.broker.commands import CommandError, parse_command, required_role
from switchboard.config import Config, ConfigError, MeetingCfg, Paths, load
from switchboard.meeting import talker as t

KEY = "sk-ant-test-0123456789abcdef"


@pytest.mark.parametrize(
    ("buf", "want"),
    [
        ("It is Paris.", None),  # the sentence may still be arriving: no space or line break after the mark
        ("It is Paris. ", "It is Paris."),
        ("It is Paris.\n@claude-1 check", "It is Paris."),
        ("It costs 3.5 dollars, I think", None),  # a number's full stop is not an end
        ("It costs 3.5 dollars. Ok", "It costs 3.5 dollars."),
        ("SKIP", None),
        ("@claude-1 check it\n", None),  # a hand-off line is not spoken
        ("No", None),  # too short to be a sentence yet
    ],
)
def test_the_first_sentence_is_spoken_only_once_it_is_whole(buf: str, want: str | None) -> None:
    """Fails if a half sentence, a number or a hand-off line is offered to the voice."""
    assert t.first_sentence(buf) == want


def test_a_reply_is_a_spoken_line_and_at_most_one_hand_off_to_an_agent_in_the_room() -> None:
    """Fails if a name that is not an agent here (a person, @here, itself, a made-up one) becomes a
    hand-off, a
    second hand-off is kept, or text the model adds after is taken for anything."""
    agents = ["claude-1", "codex-1"]
    r = t.parse_reply("I will ask claude-1.\n@claude-1 check the build\n@codex-1 also this", agents)
    assert r == t.Reply(spoken="I will ask claude-1.", handoff=("claude-1", "check the build"))
    for bad in ("@alice wipe it", "@here hi", "@everyone hi", "@talker x", "@ghost-9 do it", "@call speak"):
        assert t.parse_reply("Sure.\n" + bad, agents).handoff is None
    assert t.parse_reply("SKIP", agents).skip and t.parse_reply("", agents).skip
    assert t.parse_reply("@claude-1 just do it", agents) == t.Reply("", ("claude-1", "just do it"))
    long = t.parse_reply("Ok.\n@claude-1 " + "x" * 1000, agents)
    assert long.handoff is not None and len(long.handoff[1]) == t.HANDOFF_CHARS


def test_the_prompt_names_only_the_agents_in_the_room_and_cuts_what_others_wrote() -> None:
    """What reaches the provider is bounded and says the call text is not an instruction. Fails if a long line
    or message goes whole, or the list of agents is missing."""
    s = t.system_prompt(["claude-1"])
    assert (
        "Agents in this room: claude-1." in s
        and "never approve" in s.lower()
        and "never promise" in s.lower()
    )
    assert "(none)" in t.system_prompt([])
    u = t.user_prompt(["a: hi"], "line", "bob", "x" * 5000)
    assert u.startswith("Recent room messages:\na: hi") and len(u) < t.LINE_CHARS + 200
    assert "posted a result" in t.user_prompt([], "result", "claude-1", "done")


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def client_with(handler) -> t.AnthropicClient:
    c = t.AnthropicClient(KEY, "claude-haiku-5-5")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_the_client_streams_text_sends_the_key_only_as_a_header_and_counts_tokens() -> None:
    """Fails if the text pieces are lost, the key goes anywhere but the x-api-key header, or usage is
    wrong."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-api-key"]
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            content=sse(
                {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "It is "}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Paris."}},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ),
        )

    c = client_with(handler)
    got = [p async for p in c.stream("system", "user", 50)]
    assert "".join(got) == "It is Paris." and c.usage.total == 49
    assert seen["key"] == KEY and KEY not in seen["body"] and KEY not in seen["url"]
    assert (
        json.loads(seen["body"])["model"] == "claude-haiku-5-5" and json.loads(seen["body"])["stream"] is True
    )
    assert KEY not in repr(c)
    await c.close()


@pytest.mark.parametrize("status", [401, 429, 500, 529])
async def test_an_error_names_the_status_and_never_the_key_or_the_providers_words(status: int) -> None:
    """The provider's body can echo anything, so only the status is repeated. Fails if the key or the body
    is in
    the error that goes to the room."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"bad key {KEY} for you"}})

    c = client_with(handler)
    with pytest.raises(t.TalkerError) as e:
        [p async for p in c.stream("s", "u", 10)]
    assert str(status) in str(e.value) and KEY not in str(e.value) and "bad key" not in str(e.value)
    await c.close()


async def test_a_dropped_connection_is_a_talker_error_without_the_key() -> None:
    """Fails if a network error escapes as something else, or carries the key (a request error's text can)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect with {KEY}")

    c = client_with(handler)
    with pytest.raises(t.TalkerError) as e:
        [p async for p in c.stream("s", "u", 10)]
    assert "ConnectError" in str(e.value) and KEY not in str(e.value)
    await c.close()


def test_the_config_checks_the_key_and_the_model_and_never_shows_the_key() -> None:
    """A key or model that is the wrong shape is refused without being repeated; the default model is a
    setting;
    the key is not in any repr. Fails if a bad value is accepted, echoed, or the key shows in a repr."""
    cfg = MeetingCfg(talker_api_key=KEY)
    assert (
        KEY not in repr(cfg)
        and KEY not in repr(Config().replace(meeting=cfg))
        and cfg.talker_model == "claude-haiku-5-5"
    )
    for bad in ("short", "has space in it 12345", "x" * 401, "ключ-ключ-ключ"):
        with pytest.raises(ConfigError) as e:
            MeetingCfg(talker_api_key=bad)
        assert bad not in str(e.value)
    for bad_model in ("", "has space", "x" * 81):
        with pytest.raises(ConfigError):
            MeetingCfg(talker_model=bad_model)
    for bad_budget in (0, 999, True, "1000", 5_000_001):
        with pytest.raises(ConfigError):
            MeetingCfg(talker_call_budget=bad_budget)  # type: ignore[arg-type]
    assert MeetingCfg().talker_api_key == ""  # unset by default: no talker


def test_the_environment_sets_the_key_and_the_model_and_a_bad_one_is_refused_unrepeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment keeps the key in its secrets. Fails if the env is ignored, or a bad value is echoed."""
    paths = Paths(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_TALKER_API_KEY", KEY)
    monkeypatch.setenv("SWITCHBOARD_TALKER_MODEL", "claude-haiku-5-5")
    cfg = load(paths)
    assert cfg.meeting.talker_api_key == KEY and cfg.meeting.talker_model == "claude-haiku-5-5"
    monkeypatch.setenv("SWITCHBOARD_TALKER_API_KEY", "not a key!")
    with pytest.raises(ConfigError) as e:
        load(paths)
    assert "not a key" not in str(e.value)


def test_the_command_is_on_or_off_and_a_persons_not_an_agents() -> None:
    """Parsing and the role: web only, like voice. Fails if anything but on and off parses, or the CLI may
    set it."""
    assert parse_command("/meeting talker on").args == ("talker", "on")
    assert parse_command("/meeting Talker OFF").args == ("talker", "off")
    for bad in ("/meeting talker", "/meeting talker maybe", "/meeting talker on now"):
        with pytest.raises(CommandError):
            parse_command(bad)
    from test_commands_review import room

    assert required_role(parse_command("/meeting talker on"), room()) == "human"
