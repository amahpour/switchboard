"""The standing guidance, once, through a real broker (#543, DESIGN.md §8.6).

``tests/unit/test_guidance_once.py`` checks the rule and the engine with a clock and no broker. These
drive what Claude Code does: join over HTTPS, then the plugin's ``bin/session`` posting SessionStart with
its `source` to ``/mcp/session``, and read the answer it would print. They are what show that a join is
remembered (the engine's own tests have to say so), that the reply keeps the one shape a hook may have, and
that a room's rules edited by a person bring the full text back.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import InProcBroker
from test_agents_http_claude import SESSION_1
from test_agents_http_wake import joined_claude, session_hook

from switchboard import envelope
from switchboard.broker.hook_replies import reply_for

FULL_ONLY = (envelope.PASS_ADVICE, "Call read() for any marked", "again=yes and reminder=yes", "authority")


@pytest.fixture
def build(broker: InProcBroker) -> InProcBroker:
    broker.call("room.create", {"name": "#build"})
    return broker


def start(b: InProcBroker, token: str, source: str | None) -> str:
    """What the plugin's ``bin/session`` prints for a SessionStart of this `source` (the context text),
    after checking that it is the one shape a hook may be answered with."""
    body: dict[str, Any] = {"event": "SessionStart", "session_id": SESSION_1}
    if source is not None:
        body["source"] = source
    r = session_hook(b, token, body)
    assert r.status_code == 200, r.text
    reply = r.json()
    assert set(reply) == {"hookSpecificOutput"} and set(reply["hookSpecificOutput"]) == {
        "hookEventName",
        "additionalContext",
    }, reply
    assert reply["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    text = str(reply["hookSpecificOutput"]["additionalContext"])
    assert reply_for("claude", "SessionStart", {"kind": "context", "text": text}) == reply
    assert envelope.PEER_WARNING in text  # in every reminder, whole
    return text


def is_pointer(text: str) -> bool:
    return envelope.GUIDANCE_POINTER in text and not any(x in text for x in FULL_ONLY)


def is_full(text: str) -> bool:
    return all(x in text for x in FULL_ONLY) and envelope.GUIDANCE_POINTER not in text


def test_a_join_is_remembered_so_a_clear_is_pointed_and_a_compaction_is_full(build: InProcBroker) -> None:
    """The maintainer's scope on #543, end to end. The join gave the session the guidance in full, so /clear
    (or a fresh start) gets the pointer; a compaction and a resume get it in full each time; a SessionStart
    that says nothing of its source, as an older plugin's does, gets it in full too. The untrusted-peer line
    is in every one, and every one is the shape a hook may be answered with. Fails if the join is not
    remembered (the first /clear would be full), or any of the four is decided the other way."""
    _claude, token = joined_claude(build)
    assert is_pointer(start(build, token, "clear"))
    assert is_pointer(start(build, token, "startup"))
    assert is_full(start(build, token, "compact"))
    assert is_full(start(build, token, "resume"))
    assert is_full(start(build, token, None))
    assert is_full(start(build, token, "something-new"))
    assert is_pointer(start(build, token, "clear"))  # a compaction does not make the broker forget


def test_the_pointer_still_names_the_rooms_and_a_fresh_join_code(build: InProcBroker) -> None:
    """The pointer is shorter only by the standing guidance: the rooms, the people and a join code that
    replaces the earlier one stay, since they are different every time (#304). Fails if the pointer drops
    them."""
    _claude, token = joined_claude(build)
    first = start(build, token, "clear")
    second = start(build, token, "clear")
    assert is_pointer(first) and is_pointer(second)
    assert "you are in #build as claude-1" in first and "resume=<code>" in first
    codes = [t.rsplit("yk:j", 1)[1] for t in (first, second)]
    assert all(len(c.rstrip(".")) == 16 for c in codes) and codes[0] != codes[1]


def test_a_person_changing_the_rooms_rules_brings_the_full_text_back(build: InProcBroker) -> None:
    """Rules edited by a person (§35) are rules the session has not seen, so the pointer is not sent until
    the delivery that carries them has reached it; after that the pointer is allowed again. Here the
    delivery is the agent's own read(). Fails if the pointer is sent while the session is behind, or if
    reading the rules does not end that."""
    claude, token = joined_claude(build)
    assert is_pointer(start(build, token, "clear"))
    build.call("room.rules", {"room": "#build", "text": "Keep answers under three lines."})
    assert is_full(start(build, token, "clear"))
    assert is_full(start(build, token, "startup"))  # still behind: nothing has carried the rules yet
    build.call("human.say", {"room": "#build", "text": "@claude-1 what do you think?"})
    got = claude.call("read", room="#build")
    assert envelope.CUSTOM_RULES_HEADING in got["text"], got
    assert is_pointer(start(build, token, "clear"))
