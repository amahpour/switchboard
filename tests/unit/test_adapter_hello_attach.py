"""What each harness takes from ``mcp.hello`` and what its MCP connection can become through
``mcp.attach`` (DESIGN.md §6.3, §6.4, §9.1), asked of the adapters directly (#170): the claims a
hello keeps, the test-only and Claude-socket facts a remote hello needs, which connection may be
a push or wake channel and why not, and the channels there are. ``AgentService`` asks these
questions and names no harness."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.adapters import REMOTE_CODEX, build_adapters
from switchboard.adapters.base import Adapter, HelloClaims, PullAdapter
from switchboard.adapters.claude import TIER_INBOX, ClaudeAdapter
from switchboard.adapters.remote_codex import TIER_LINK, RemoteCodexAdapter
from switchboard.adapters.testagent import TestAgentAdapter
from switchboard.broker.agents import AgentService, McpConn
from switchboard.broker.peer import McpIdentity
from switchboard.config import Config

CFG = Config(human_name="alice")
ADAPTERS = build_adapters(CFG)


def ident(harness: str, socket: str | None = None) -> McpIdentity:
    return McpIdentity(
        harness=harness,
        mcp_pid=10,
        mcp_start=1.0,
        agent_pid=9,
        agent_start=0.5,
        evidence="stub",
        claude_socket=socket,
    )


# ------------------------------------------------------------------ hello claims
@pytest.mark.parametrize("name", sorted(set(ADAPTERS) - {"claude", "test"}))
def test_a_harness_keeps_the_env_leak_its_mcp_server_claimed(name: str) -> None:
    """Every harness but Claude and the test agent keeps just the leak claim, and none of the
    test agent's fields. Fails if a harness stops keeping it (the member list's warning) or keeps
    a test session it never asked for."""
    a = ADAPTERS[name]
    assert a.hello_claims({"env_leak": True}) == HelloClaims(env_leak=True)
    assert a.hello_claims({"env_leak": 1, "test_session": "x", "test_ack": "never"}) == HelloClaims(
        env_leak=True
    )
    assert a.hello_claims({}) == HelloClaims()
    assert a.hello_claims({"env_leak": False}) == HelloClaims()


def test_claude_never_has_an_env_leak() -> None:
    """Claude's own environment carries Claude's markers, so a leak claimed for it means nothing.
    Fails if a claimed leak is kept for Claude (it was always dropped, in ``AgentService``)."""
    assert ADAPTERS["claude"].hello_claims({"env_leak": True}) == HelloClaims()


def test_the_test_agents_hello_names_its_session_and_ack_mode() -> None:
    a = TestAgentAdapter(CFG)
    assert a.hello_claims({"test_session": "bot-1.a", "test_ack": "immediate"}) == HelloClaims(
        test_session="bot-1.a", ack_mode="immediate"
    )
    # an --ack it doesn't know, or none, is next_call; a leak claim is kept as for any harness
    assert a.hello_claims({"test_session": "s", "test_ack": "bogus", "env_leak": True}) == HelloClaims(
        env_leak=True, test_session="s", ack_mode="next_call"
    )
    assert a.hello_claims({"test_session": "s"}).ack_mode == "next_call"


@pytest.mark.parametrize("bad", [None, 7, ["s"], "", "has space", "x" * 65, "a/b"])
def test_the_test_agents_hello_without_a_usable_session_key_is_refused(bad: Any) -> None:
    """The session key is what the test agent's membership is keyed by (§9.6), so a hello with
    none, or one that isn't ``[A-Za-z0-9_.-]{1,64}``, is refused with the sentence the MCP
    server's user reads. Fails if a bad key gets through."""
    with pytest.raises(ValueError, match=r"--harness test needs --test-session KEY"):
        TestAgentAdapter(CFG).hello_claims({"test_session": bad})
    with pytest.raises(ValueError):
        TestAgentAdapter(CFG).hello_claims({})


def test_only_the_test_agent_is_test_only() -> None:
    assert {n for n, a in ADAPTERS.items() if a.test_only} == {"test"}


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_only_claude_has_an_attested_socket(name: str) -> None:
    att = {"claude_socket": "/tmp/yk-claude.sock"}
    assert ADAPTERS[name].attested_socket(att) == ("/tmp/yk-claude.sock" if name == "claude" else None)
    assert ADAPTERS["claude"].attested_socket({"claude_socket": None}) is None


# ---------------------------------------------------------------------- channels
def test_the_channels_there_are() -> None:
    """Claude's inbox, and a remote Codex's wake channel; no other adapter has one. Fails if a
    harness gains a channel without ``AgentService`` and its tiers hearing of it, or if two
    adapters name the same channel."""
    got = {a.channel: (n, a.channel_flag, a.channel_tier) for n, a in ADAPTERS.items() if a.channel}
    assert got == {
        "inbox": ("claude", "", TIER_INBOX),
        "codex": (REMOTE_CODEX, "codex", TIER_LINK),
    }
    assert TIER_INBOX == "claude:inbox" and TIER_LINK == "codex:link"


def test_an_adapter_with_no_channel_refuses_and_has_nothing_to_detach() -> None:
    a = PullAdapter("unknown", CFG)
    assert a.channel == "" and a.channel_tier == ""
    assert a.channel_refusal(ident("unknown"), has_token=True, guard_ok=True, remote=True) == (
        "unknown has no push channel"
    )
    assert a.detach(object()) == []
    assert a.posted(1, object(), {"ok": True}) is False
    with pytest.raises(NotImplementedError):
        a.attach(10, 1.0, object(), "")
    assert isinstance(a, Adapter)


CLAUDE_CASES = [
    ("claude", "/tmp/s.sock", True, True, None),
    ("codex", "/tmp/s.sock", True, True, "not a verified Claude session"),
    ("claude", None, True, True, "not a verified Claude session"),
    ("claude", "", True, True, "not a verified Claude session"),
    ("claude", "/tmp/s.sock", False, True, "no session messaging token"),
    ("claude", "/tmp/s.sock", True, False, "the MCP server's inbox guard refused"),
]


@pytest.mark.parametrize(("harness", "socket", "token", "guard", "why"), CLAUDE_CASES)
@pytest.mark.parametrize("remote", [False, True])
def test_claude_says_which_connection_may_be_its_inbox(
    harness: str, socket: str | None, token: bool, guard: bool, why: str | None, remote: bool
) -> None:
    """A direct child of a claude with the registry's inbox socket, the session token and a
    passing guard, here or over a link; each missing piece is its own refusal, in this order."""
    got = ClaudeAdapter(CFG).channel_refusal(
        ident(harness, socket), has_token=token, guard_ok=guard, remote=remote
    )
    assert got == why


@pytest.mark.parametrize(
    ("harness", "guard", "remote", "why"),
    [
        ("codex", True, True, None),
        ("claude", True, True, "not a verified Codex session"),
        ("codex", True, False, "a Codex session on this machine is woken by the broker itself"),
        ("codex", False, True, "the MCP server's own check refused"),
        ("claude", False, False, "not a verified Codex session"),
        ("codex", False, False, "a Codex session on this machine is woken by the broker itself"),
    ],
)
def test_a_remote_codex_says_which_connection_may_wake_its_threads(
    harness: str, guard: bool, remote: bool, why: str | None
) -> None:
    got = RemoteCodexAdapter(CFG).channel_refusal(
        ident(harness), has_token=False, guard_ok=guard, remote=remote
    )
    assert got == why


# ----------------------------------------------------------------- McpConn state
def test_a_connection_is_the_channels_it_attached() -> None:
    """``inbox_attached`` and ``codex_attached`` are the two channels' old names, and read and
    set what ``attached`` holds (tests and the connection's own checks use both)."""
    mc = McpConn(ident=ident("claude", "/tmp/s.sock"))
    assert mc.attached == set() and not mc.inbox_attached and not mc.codex_attached
    mc.inbox_attached = True
    assert mc.attached == {"inbox"} and mc.inbox_attached and not mc.codex_attached
    mc.codex_attached = True
    assert mc.attached == {"inbox", "codex"}
    mc.inbox_attached = False
    mc.inbox_attached = False  # again: nothing to remove
    assert mc.attached == {"codex"} and mc.codex_attached
    mc.codex_attached = False
    assert mc.attached == set()
    assert McpConn(ident=ident("claude")).attached is not McpConn(ident=ident("claude")).attached


def test_the_adapters_keep_their_own_connections() -> None:
    """Each channel adapter is its own object with its own table: attaching to Claude's leaves
    the remote Codex's empty, and ``detach`` on one never touches the other."""
    claude, remote = ADAPTERS["claude"], ADAPTERS[REMOTE_CODEX]
    assert isinstance(claude, ClaudeAdapter) and isinstance(remote, RemoteCodexAdapter)
    conn = SimpleNamespace(closed=False)
    claude.attach(10, 1.0, conn, host="")
    assert claude.conns == {("", 10): (1.0, conn)} and remote.conns == {}
    assert remote.detach(conn) == [] and claude.detach(conn) == [10]


# ------------------------------------------------------------------ the guard
HARNESS_NAMES = ("claude", "codex", "cursor", "devin", "test")
MENTIONS = re.compile(r"\b(claude|codex|cursor|devin)\b", re.IGNORECASE)
ASKS_ADAPTERS = (
    "hello",
    "_remote_ident",
    "conn_closed",
    "attach",
    "_detach",
    "posted",
    "_channel_asked",
    "_channel_named",
    "join",  # the test agent's starting status: its adapter's sink_status (#360)
    "hook_event",  # the test agent's test-mode gate: its adapter's test_only (#360)
)


def harness_strings(src: str) -> list[str]:
    """Every string in ``src``, outside a docstring, that names a harness: a literal that picks
    one adapter, harness or session-key prefix."""
    tree = ast.parse(textwrap.dedent(src))
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            v = n.value.lower()
            if v in HARNESS_NAMES or v.startswith(tuple(f"{h}{sep}" for h in HARNESS_NAMES for sep in ":@")):
                out.append(n.value)
            elif MENTIONS.search(v):
                out.append(n.value)
    return out


def test_the_guard_sees_a_harness_named_in_a_string_and_not_in_a_docstring() -> None:
    src = '''
def f(self, p):
    """Claude's inbox, or Codex's wake (a docstring)."""
    a = self.adapters.get("claude")
    b = self.adapters["codex@remote"]
    if p.harness == "test":
        note = "a Codex session"
    return {"harness": p.harness, "what": "attach"}
'''
    assert harness_strings(src) == ["claude", "codex@remote", "test", "a Codex session"]


@pytest.mark.parametrize("name", ASKS_ADAPTERS)
def test_agent_service_names_no_harness_in_hello_and_attach(name: str) -> None:
    """``AgentService`` answers a hello and an attach by asking the adapter (§6.4), so a harness
    is added or changed in its adapter alone, as the engine does (#169). A new guard, scoped to
    these methods: it only tightens. Fails if one of them names a harness again, as the
    ``harness == "test"`` and ``adapters.get("claude")`` it replaced did."""
    got = harness_strings(inspect.getsource(getattr(AgentService, name)))
    assert got == [], "move the harness's rule into its adapter (adapters/base.py, §9.1)"
