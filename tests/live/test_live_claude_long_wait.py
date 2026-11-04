"""Claude's long wait over HTTPS, in a private home (#558, DESIGN.md §6.1).

Opt in with SWITCHBOARD_LIVE=claude and -m live. Only a copy of the account login is read;
all CLI configuration, transcripts and plugin data belong to the run's temporary home.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from harness import scenarios, screens
from harness.claude import ClaudeRun, project_dir_name
from harness.claude_signin import login_only
from harness.tmuxdrv import REAL_HOME
from harness.world import ROOM, skip_reason

pytestmark = pytest.mark.live

_why = skip_reason("claude", "claude", "curl")
if _why:
    pytest.skip(_why, allow_module_level=True)


class PrivateClaudeRun(ClaudeRun):
    @property
    def claude_home(self) -> Path:
        return self.base / "claude-config"

    def prepare(self) -> None:
        self.claude_home.mkdir(mode=0o700)
        try:
            login = login_only(json.loads((Path(REAL_HOME) / ".claude" / ".credentials.json").read_text()))
        except (OSError, ValueError, KeyError):
            pytest.skip("Claude Code is not signed in: no account login available for the private run")
        self.write_private(self.claude_home / ".credentials.json", json.dumps(login))
        super().prepare()

    def env(self) -> dict[str, str]:
        return {
            **super().env(),
            "HOME": str(self.base),
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
        }

    def ready_step(self, screen: str) -> bool:
        key = screens.claude_onboarding(screen)
        if key is not None:
            self.tmux.key(self.session, key)
            return False
        return super().ready_step(screen)

    def scrub(self) -> None:
        shutil.rmtree(self.claude_home, ignore_errors=True)


@pytest.fixture(scope="module")
def run() -> Iterator[PrivateClaudeRun]:
    r = PrivateClaudeRun("claude-long")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == []
    assert r.results["leftover_processes"] == []


def wait_call(run: PrivateClaudeRun) -> dict[str, Any] | None:
    """Find the actual model call, so the message's delay starts at the call, not at our prompt."""
    for f in (run.claude_home / "projects" / project_dir_name(run.ws)).glob("*.jsonl"):
        for line in f.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # the CLI may be writing the final line
            for block in entry.get("message", {}).get("content", []):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and str(block.get("name", "")).endswith("__wait")
                    and block.get("input", {}).get("timeout_s") == 600
                ):
                    return entry
    return None


def test_message_after_315_seconds_of_wait_is_delivered_once(run: PrivateClaudeRun) -> None:
    """Cross both the 120 s background and 300 s idle cutoffs. Fail if the call ends early, the
    broker offers the message twice, or the agent answers twice; inspect the settled turn."""
    scenarios.join_and_say(run)
    after = run.last_id()
    run.type(
        f'Call wait with room "{ROOM}" and timeout_s 600, once. Call no other tool while it runs.'
        ' When it returns, follow any message it contains, then call say with text "finished-long-wait".'
        " If it runs in the background, let its result arrive before doing that. Then end your turn."
    )
    entry = run.wait(lambda: wait_call(run), 60, "the real wait(600) call")
    started = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()

    def time_to_post() -> bool:
        assert not run.broker.said_by(run.name, "finished-long-wait", after), "wait ended before 315 s"
        return time.time() - started >= 315

    # This delay is the experimental stimulus, not a sleep guessing that some work has finished.
    run.wait(time_to_post, 330, "315 seconds into the real wait", step=1)
    word = "long-wait-" + scenarios.nonce()
    asked = run.mention(f'call say with text "{word}" exactly once.')
    answer = run.reply(word, after=asked)
    run.reply("finished-long-wait", after=asked)
    run.wait_settled()
    d = scenarios.delivery(run, asked)
    assert d["path"] == "wait", dict(d)
    [receipt] = run.broker.q(
        "SELECT d.attempts, d.redelivered, b.evidence, b.created_at FROM deliveries d"
        " JOIN memberships m ON m.id = d.membership_id JOIN batches b ON b.id = d.batch_id"
        " WHERE d.message_id = ? AND m.screen_name = ?",
        asked,
        run.name,
    )
    assert receipt["attempts"] == receipt["redelivered"] == 0, dict(receipt)
    replies = [m for m in run.broker.messages(asked) if m["from"] == run.name and word in m["text"]]
    assert len(replies) == 1, replies
    scenarios.record(
        run,
        "long_wait",
        requested_s=600,
        arrived_after_s=round(receipt["created_at"] - started, 1),
        path=d["path"],
        evidence=receipt["evidence"],
        attempts=receipt["attempts"],
        replies=len(replies),
        response_s=scenarios.latency(run, asked, answer),
    )
