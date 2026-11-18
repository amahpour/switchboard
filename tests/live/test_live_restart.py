"""Real harness recovery across broker SIGTERM and 30 seconds offline (#823, DESIGN.md §12.4)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import scenarios
from harness.claude_signin import PrivateClaudeRun
from harness.codex import CodexRun
from harness.cursor import CursorRun
from harness.devin import DevinRun
from harness.run import Run
from harness.tmuxdrv import REAL_HOME
from harness.world import skip_reason

pytestmark = pytest.mark.live
DOWNTIME_S = 30


class PrivateCursorRun(CursorRun):
    def prepare(self) -> None:
        super().prepare()
        self.xdg = self.cursor_home / ".config"
        self.auth = self.xdg / "cursor" / "auth.json"
        self.write_private(self.auth, (Path(REAL_HOME) / ".config" / "cursor" / "auth.json").read_text())

    def env(self) -> dict[str, str]:
        return {**super().env(), "XDG_CONFIG_HOME": str(self.xdg)}

    def scrub(self) -> None:
        if hasattr(self, "auth"):
            self.auth.unlink(missing_ok=True)


class PrivateDevinRun(DevinRun):
    def prepare(self) -> None:
        super().prepare()
        self.data_home = self.base / ".local" / "share"
        rel = Path("devin/credentials.toml")
        self.auth = self.data_home / rel
        self.write_private(self.auth, (Path(REAL_HOME) / ".local" / "share" / rel).read_text())

    def env(self) -> dict[str, str]:
        return {**super().env(), "HOME": str(self.base), "XDG_DATA_HOME": str(self.data_home)}

    def scrub(self) -> None:
        if hasattr(self, "auth"):
            self.auth.unlink(missing_ok=True)


@pytest.fixture(
    params=[PrivateClaudeRun, CodexRun, PrivateCursorRun, PrivateDevinRun],
    ids=["claude", "codex", "cursor", "devin"],
)
def run(request: pytest.FixtureRequest) -> Iterator[Run]:
    cls = request.param
    if why := skip_reason(cls.harness, *cls.binaries, "curl"):
        pytest.skip(why)
    r = cls(f"{cls.harness}-1")
    r.tmux.env["HOME"] = str(r.broker.base)
    try:
        try:
            r.start()
        except FileNotFoundError:
            pytest.skip(f"{r.harness} has no account login for the private run")
        if isinstance(r, CursorRun):
            r.enable_server()
            if why := r.model_unavailable():
                pytest.skip(why)
        yield r
    finally:
        r.stop()
        assert r.results["drift_fail"] == []
        assert r.results["leftover_processes"] == []


@pytest.mark.parametrize("phase", ["idle", "mid_turn"])
def test_recovers_without_terminal_input(run: Run, phase: str) -> None:
    """The original CLI must hear a new mention and post one reply after a restart.
    A held Stop/wait exercises the idle path. A held shell command exercises a mid-turn MCP call.
    """
    scenarios.join_and_say(run)
    queued = None
    if phase == "idle":
        if run.harness != "devin":
            run.wait(
                lambda: run.broker.q("SELECT id FROM events WHERE kind='park' LIMIT 1"),
                30,
                "the listener to park at the broker",
            )
    else:
        run.mention(
            'Run "sh hold.sh restart" and wait for it to finish. While it runs call no other tool.'
            ' When it finishes, follow the room request you received. If needed, call read for "#build".'
        )
        run.wait(lambda: run.flag("restart.started"), 120, "the mid-turn command to start")
        assert (run.member() or {}).get("status") == "busy"

        queued = run.mention("Keep this note in context while the shell command runs.")
        run.wait(lambda: (run.member() or {}).get("queued", 0) >= 1, 10, "a queued pre-restart mention")
    typed = run.typed
    pid = run.tmux.pane_pid(run.session)
    before = time.monotonic()
    run.broker.restart(DOWNTIME_S)
    scenarios.record(run, phase, outage_s=round(time.monotonic() - before, 1), replied=False)
    word = f"recovered-{scenarios.nonce()}"
    asked = run.mention(f"{scenarios.say_text(word)} exactly once.")
    record = run.results["scenarios"][phase]
    try:
        if queued is not None:
            assert not run.flag("restart.done"), "mid-turn command ended before the restart finished"
            run.release("restart")
        answer = run.reply(word, after=asked, timeout=90)
    finally:
        row = run.broker.delivery_of(asked, run.name)
        if row is not None:
            record.update(delivery=dict(row))
    receipt = scenarios.delivery(run, asked)
    assert receipt["attempts"] == receipt["redelivered"] == 0, receipt
    if queued is not None:
        assert run.flag("restart.done"), "the reply preceded completion of the mid-turn command"
        scenarios.delivery(run, queued)
    if phase == "idle":
        assert receipt["path"] in run.wake_paths, receipt
    run.wait_settled()
    replies = [m for m in run.broker.messages(asked) if m["from"] == run.name and word in m["text"]]
    assert len(replies) == 1, replies
    assert run.tmux.pane_pid(run.session) == pid and run.typed == typed
    record.update(replied=True, delivery=receipt, response_s=scenarios.latency(run, asked, answer))
