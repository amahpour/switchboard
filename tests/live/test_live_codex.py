"""Live Codex over HTTPS (DESIGN.md §12.4). Opt-in: ``SWITCHBOARD_LIVE=codex uv run pytest -m live``.

One real ``codex`` session (the cheapest model, low effort) in a private tmux server, on a **temp
CODEX_HOME** that holds a copy of the login and the plugin from this checkout, against a throwaway broker
in a temp home. The scenarios are ``harness/scenarios.py``'s, in order. The copy of the login is deleted
when the run ends, and the real ``~/.codex`` must not change.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator

import httpx
import pytest
from harness import scenarios
from harness.codex import CodexRun
from harness.world import skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("codex", "codex")
if _why:
    pytest.skip(_why, allow_module_level=True)


@pytest.fixture(scope="module")
def run() -> Iterator[CodexRun]:
    r = CodexRun("codex-1")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Codex config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"
    assert not (r.codex_home / "auth.json").exists(), "the copy of the login was not deleted"


def test_1_join_and_say(run: CodexRun) -> None:
    scenarios.join_and_say(run)
    # The installed CLI, not a stand-in, executes the local helper and claims its hash (#637).
    files = list((run.base / ".cache" / "switchboard").glob("codex-listener-key-*"))
    assert files, "Codex did not run its HTTP header helper"
    keys = {}
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600, "the script key must be private"
        key = file.read_text().strip()
        keys[hashlib.sha256(key.encode()).hexdigest()] = key
    rows = run.broker.q("SELECT key_hash,session_id FROM listener_keys WHERE harness='codex'")
    bound = next((row for row in rows if row["key_hash"] in keys and row["session_id"]), None)
    assert bound is not None, "the real helper's hash was not bound to an authenticated Codex thread"
    # No Docker or meeting is needed to verify the live credential's registration scope.
    with httpx.Client(
        base_url=run.broker.url, headers={"Authorization": f"Bearer {keys[bound['key_hash']]}"}
    ) as client:
        body = {"room": "#build", "key_hash": secrets.token_hex(32), "platform": "zoom"}
        refused = client.post("/mcp/meeting/host", json={**body, "session": "another-thread"})
        assert (refused.status_code, refused.json().get("error")) == (403, "wrong_session")
        registered = client.post("/mcp/meeting/host", json={**body, "session": bound["session_id"]})
        try:
            assert registered.status_code == 200, registered.text
        finally:
            run.broker.command("/meeting leave")
    scenarios.record(
        run, "host_key", mode="0600", claimed=True, harness="codex", own_thread=200, other_thread=403
    )


def test_2_woken_while_idle_by_a_mention(run: CodexRun) -> None:
    scenarios.idle_wake(run)


def test_3_a_mention_mid_task_arrives_after_the_next_tool_call(run: CodexRun) -> None:
    scenarios.mid_task(run)


def test_4_a_subagent_does_not_take_the_sessions_message(run: CodexRun) -> None:
    scenarios.subagent(run)


def test_5_quit_shows_offline(run: CodexRun) -> None:
    scenarios.quit_offline(run)


def test_6_resume_comes_back_and_is_woken(run: CodexRun) -> None:
    scenarios.resume(run)


def test_7_a_revoked_token_ends_it(run: CodexRun) -> None:
    scenarios.revoked_token(run)
