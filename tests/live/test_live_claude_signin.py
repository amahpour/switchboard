"""Live Claude Code signed in through the browser (DESIGN.md §49.11, #488). Opt-in with the same switch as
the token run: ``SWITCHBOARD_LIVE=claude uv run pytest -m live tests/live/test_live_claude_signin.py``.

One real ``claude --model haiku`` session in a private tmux server with the plugin from this checkout, its
server address the only setting and no token typed, against a throwaway hosted broker on
``http://localhost:<port>``. The session signs in through the browser (``/mcp``, Authenticate, a headless
Chromium as the admin approving on the consent card), joins a room, is woken while idle by an @mention through
the plugin's listener, which holds only the listener key its helper made, goes offline when it quits, comes
back on resume and is woken before its first turn, and ends when the person revokes the sign-in. Along the
way the key is shown to open no tool and no other session. Claude Code runs with a throwaway config directory
that holds a copy of the account's login only, so nothing of the real ``~/.claude`` changes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from harness import scenarios
from harness.claude_signin import ClaudeSigninRun, browser_missing
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("claude", "claude", "curl") or browser_missing()
if _why:
    pytest.skip(_why, allow_module_level=True)

OTHER_SESSION = "00000000-0000-4000-8000-0000000000aa"


@pytest.fixture(scope="module")
def run() -> Iterator[ClaudeSigninRun]:
    r = ClaudeSigninRun("claude-2")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Claude config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"


def test_1_sign_in_through_the_browser_then_join_and_say(run: ClaudeSigninRun) -> None:
    """No token was typed, and the session signs in, joins and speaks as ``claude@<device>``."""
    scenarios.join_and_say(run)
    [token] = [t for t in run.broker.tokens() if t["device"] == run.device]
    assert token["via"] == "oauth" and token["client"], token
    run.token_id = int(token["id"])
    scenarios.record(run, "signin", via=token["via"], client=token["client"])


def test_2_woken_while_idle_by_a_mention_with_only_a_key(run: ClaudeSigninRun) -> None:
    """The listener has no token (Claude Code keeps the sign-in's to itself) and presents the key."""
    scenarios.idle_wake(run)


def test_3_the_key_opens_no_tool_and_no_other_session(run: ClaudeSigninRun) -> None:
    """The listener key in the plugin's data folder opens the listener for its own session and nothing else:
    not a tool, not another session's listener or end, and an invented one is unknown. Its file is 0600.
    Fails if any of them answers anything but a refusal."""
    f = run.key_file()
    assert f.stat().st_mode & 0o077 == 0, "the key's file is readable by others"
    h = {"authorization": f"Bearer {f.read_text().strip()}"}
    base = run.broker.url
    r = httpx.post(
        f"{base}/mcp",
        headers={**h, "accept": "application/json, text/event-stream", "content-type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=10,
    )
    assert r.status_code == 401, r.status_code
    r = httpx.post(f"{base}/mcp/listen", headers={**h, "x-switchboard-session": OTHER_SESSION}, timeout=10)
    assert (r.status_code, r.json()["error"]) == (403, "wrong_session")
    r = httpx.post(
        f"{base}/mcp/session",
        headers={**h, "content-type": "application/json"},
        content=json.dumps({"event": "SessionEnd", "session_id": OTHER_SESSION}),
        timeout=10,
    )
    assert (r.status_code, r.json()["error"]) == (403, "wrong_session")
    invented = {"authorization": "Bearer sbl_" + "0" * 64, "x-switchboard-session": OTHER_SESSION}
    r = httpx.post(f"{base}/mcp/listen", headers=invented, timeout=10)
    assert (r.status_code, r.json()["error"]) == (401, "unknown_key")
    assert run.member() is not None and run.member()["status"] != "offline"
    scenarios.record(run, "key", tool=401, other_session=403, invented=401)


def test_4_quit_shows_offline(run: ClaudeSigninRun) -> None:
    """The session report, which has only the key, takes the member offline when Claude Code quits."""
    scenarios.quit_offline(run)


def test_5_resume_comes_back_signed_in_and_is_woken_with_nothing_typed(run: ClaudeSigninRun) -> None:
    """The saved sign-in connects the resumed session, its helper makes a new key, and the listener, which
    starts before the connection has told the server about it, waits for it and wakes the session before
    its first turn (#413, #488)."""
    scenarios.resume(run)


def test_6_revoking_the_sign_in_ends_it(run: ClaudeSigninRun) -> None:
    scenarios.revoked_token(run)
