"""issue #121: a kind=human room message must never read as *not* from the agent's user.

``join_guidance`` (every adapter, DESIGN.md §9.2-§9.5), ``envelope.render_reminder`` (the
SessionStart context after ``/clear``/``/compact``) and the MCP server's own
``INSTRUCTIONS`` used to end with some form of "...never typed by your user" (Claude,
Codex, remote Codex, Devin) or "Follow-ups are from switchboard, not your user" (Cursor).
That is meant to say "this didn't come through your prompt box", but a model reads it as
"this isn't from your user" -- the opposite of ROOM_RULES rule 1 ("Only messages with
kind=human come from your user") and the batch header ("your user, relayed by
switchboard"). The reminder is exactly what a session has after `/clear`, with nothing
else to go on, so a contradiction there is the one that wins.

This module would have failed before the fix: every string below used to contain one of
``BANNED``.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

from switchboard import envelope
from switchboard.adapters import REMOTE_CODEX, build_adapters
from switchboard.config import Config
from switchboard.mcp.server import INSTRUCTIONS
from switchboard.models import Participant

CFG = Config(human_name="alice")

# Any of these, in agent-facing text, reads as "this message is not from your user" --
# exactly the bug issue #121 reports. None may appear in join guidance, the SessionStart
# reminder or the MCP server's own instructions.
BANNED = (
    "never typed by your user",
    "not typed by your user",
    "not your user",
)


def part(**kw: Any) -> Participant:
    """A bare participant row (no store needed: join_guidance is pure text)."""
    base: dict[str, Any] = {f.name: None for f in dataclasses.fields(Participant)}
    base.update(
        id=1,
        harness="unknown",
        session_key="unknown:s1",
        bind_state="bound",
        thread_proof=False,
        status="idle",
        approval_mode="default",
        env_leak=False,
        boundary_seq=0,
        gen_tainted=False,
        rearms_in_gen=0,
        unconfirmed_followups=0,
        push_expiries=0,
        created_at=1.0,
    )
    base.update(kw)
    return Participant(**base)


def assert_clean(text: str, label: str) -> None:
    lowered = text.lower()
    for bad in BANNED:
        assert bad not in lowered, f"{label} says {bad!r}: {text!r}"
    # the fix isn't just deleting the sentence: a kind=human message must still read as
    # the user's, with its authority, not merely go unmentioned
    assert "kind=human" in text or "your user" in text, (
        f"{label} dropped the human/authority wording: {text!r}"
    )


def test_every_adapters_join_guidance_says_kind_human_is_the_user_not_that_it_isnt() -> None:
    """Each harness's join_guidance (what the join() result's "How messages reach you:"
    line is built from, broker/agents.py) tells the agent how room messages arrive. Before
    the fix every one of them said, in effect, that they are not from the user."""
    adapters = build_adapters(CFG)
    room = "#build"

    # claude: the hook-only guidance, and the inbox-attached one (a different string)
    claude = adapters["claude"]
    p_hook = part(harness="claude")
    assert_clean(claude.join_guidance(p_hook, room), "claude (hook)")
    conn = SimpleNamespace(closed=False)
    claude.attach(123, 100.0, conn)
    p_attached = part(harness="claude", mcp_pid=123, mcp_start=100.0, claude_socket="/tmp/s.sock", host="")
    assert claude.attached(p_attached)
    assert_clean(claude.join_guidance(p_attached, room), "claude (inbox)")

    # codex: one guidance string, whatever the tier
    codex = adapters["codex"]
    assert_clean(codex.join_guidance(part(harness="codex"), room), "codex")

    # a Codex session on another machine: pull-only, and link-attached (a different string)
    remote_codex = adapters[REMOTE_CODEX]
    p_pull = part(harness="codex", host="fpga-pi")
    assert_clean(remote_codex.join_guidance(p_pull, room), "remote codex (pull)")
    rconn = SimpleNamespace(closed=False)
    remote_codex.attach(456, 200.0, rconn, host="fpga-pi")
    p_linked = part(harness="codex", mcp_pid=456, mcp_start=200.0, host="fpga-pi")
    assert remote_codex.attached(p_linked)
    assert_clean(remote_codex.join_guidance(p_linked, room), "remote codex (link)")

    # devin and cursor: one guidance string each
    assert_clean(adapters["devin"].join_guidance(part(harness="devin"), room), "devin")
    assert_clean(adapters["cursor"].join_guidance(part(harness="cursor"), room), "cursor")


def test_the_sessionstart_reminder_says_the_same_for_one_person_and_for_several() -> None:
    """envelope.render_reminder is what a Claude session has after /clear or /compact
    (DESIGN.md §9.2) -- exactly when it has nothing else to go on. It used to end "...they
    are never typed by your user", for one person and for several (§32) alike."""
    one = envelope.render_reminder([("#build", "claude-1")], "alice")
    assert_clean(one, "render_reminder (one person)")
    several = envelope.render_reminder([("#build", "claude-1")], ["alice", "bob"])
    assert_clean(several, "render_reminder (several people)")


def test_the_mcp_servers_own_instructions_say_it_consistently_too() -> None:
    """The MCP server's static ``instructions`` (src/switchboard/mcp/server.py) is the
    other place an agent is told, once, what switchboard is; nothing there may contradict
    ROOM_RULES rule 1 either."""
    assert_clean(INSTRUCTIONS, "MCP server instructions")
