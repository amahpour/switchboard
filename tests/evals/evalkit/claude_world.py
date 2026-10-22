"""Real Claude Code agents in one room (DESIGN.md §12.7). Test-only; needs ``claude`` and ``tmux``.

Built on the live tests' driver (``tests/live/harness``, §12.4): a throwaway broker in a temp home, the
checkout's plugin over HTTPS with the listener hook, each agent in a tmux server of its own with an empty
scratch directory, user settings and connectors off, permission mode ``manual`` and nothing approved
(a prompt that appears is declined with Esc and counted). One broker and one room per trial; a fresh home,
sessions and scratch directories for each. The real ``~/.claude/settings.json`` must not change.

Tokens are read from each session's own transcript (``~/.claude/projects``), which is deleted with the
trial.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

LIVE = Path(__file__).resolve().parents[2] / "live"
if (
    str(LIVE) not in sys.path
):  # the live tests' driver is a plain folder of modules, not a package of this tree
    sys.path.insert(0, str(LIVE))

from harness import claude as live_claude  # noqa: E402
from harness import drift  # noqa: E402
from harness.claude import ClaudeRun, project_dir_name  # noqa: E402
from harness.tmuxdrv import REAL_HOME  # noqa: E402
from harness.world import HUMAN, REAL_PATH, Broker  # noqa: E402

from .record import Tokens  # noqa: E402
from .scenario import Scenario  # noqa: E402
from .trial import HarnessError, Limits  # noqa: E402

MODEL = "haiku"  # the cheapest; ``--model`` runs another, to confirm a change on the model people use
# USD per million tokens, input and output (the published rates of October 2026). Cache reads cost a tenth of
# input, a cache write for five minutes 1.25 times and one for an hour twice: Claude Code's own cost record
# for a call agreed with these to the cent.
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0)}


def price(model: str, usage: dict[str, Any]) -> float | None:
    """What one model call cost, at the published rates, or None for a model with no price here."""
    rates = next((r for key, r in PRICES.items() if key in model), None)
    if rates is None:
        return None
    inp, out = rates
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if not cc:  # an older record: the whole write, priced as five minutes
        w5m = int(usage.get("cache_creation_input_tokens") or 0)
    per_m = (
        int(usage.get("input_tokens") or 0) * inp
        + int(usage.get("output_tokens") or 0) * out
        + int(usage.get("cache_read_input_tokens") or 0) * inp * 0.1
        + w5m * inp * 1.25
        + w1h * inp * 2.0
    )
    return per_m / 1_000_000


class TranscriptMeter:
    """The tokens a Claude Code session's own transcripts record, read as they grow.

    Each call to the model is written to the transcript as one or more lines that share a message id (one
    per content block) and may repeat its usage; a message counts once, at its largest output. Only whole
    lines are read, and only what was added since the last read."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self._offsets: dict[Path, int] = {}
        self._calls: dict[str, tuple[int, dict[str, Any], str]] = {}
        self._anon = 0
        self.cli_cost: float | None = None  # Claude Code's own total, written when it ends

    def read(self) -> Tokens:
        for f in sorted(self.dir.rglob("*.jsonl")) if self.dir.is_dir() else []:
            self._take(f)
        out = Tokens()
        for _, usage, model in self._calls.values():
            out = out + Tokens(
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0),
                1,
                price(model, usage),
            )
        return out

    def _take(self, f: Path) -> None:
        try:
            with open(f, "rb") as fh:
                fh.seek(self._offsets.get(f, 0))
                chunk = fh.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._offsets[f] = self._offsets.get(f, 0) + end + 1
        for line in chunk[: end + 1].splitlines():
            if b'"usage"' not in line and b'"cost-state"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") == "cost-state" and isinstance(o.get("totalCostUSD"), (int, float)):
                self.cli_cost = float(o["totalCostUSD"])
                continue
            m = o.get("message")
            usage = m.get("usage") if isinstance(m, dict) else None
            if not isinstance(usage, dict):
                continue
            mid = m.get("id") if isinstance(m.get("id"), str) else None
            if mid is None:
                self._anon += 1
                mid = f"anon-{self._anon}"
            out_tokens = int(usage.get("output_tokens") or 0)
            if mid not in self._calls or out_tokens >= self._calls[mid][0]:
                self._calls[mid] = (out_tokens, usage, str(m.get("model") or ""))


def with_model(argv: list[str], model: str) -> list[str]:
    """The live driver's launch line, with the model this run asks for."""
    out = list(argv)
    out[out.index("--model") + 1] = model
    return out


class EvalClaude(ClaudeRun):
    """One Claude Code session in the room of a trial: the live driver's, on a broker it shares."""

    def __init__(self, name: str, broker: Broker, model: str) -> None:
        super().__init__(name, broker=broker, device=f"eval-{name}")
        self.session = name  # a launch script of its own under the trial's directory
        self.model = model
        self.meter: TranscriptMeter | None = None

    def start(self) -> EvalClaude:
        self.base = self.broker.base
        self.token_id, self.token = self.broker.make_token(self.device)
        self.ws = self.base / f"ws-{self.name}"  # empty: nothing for the agent to work on
        self.ws.mkdir()
        self.results["version"] = self.version()
        self.prepare()
        self.meter = TranscriptMeter(Path(REAL_HOME) / ".claude" / "projects" / project_dir_name(self.ws))
        return self

    def prepare(self) -> None:
        self.settings = self.base / f"claude-settings-{self.name}.json"
        self.write_private(self.settings, json.dumps(live_claude.settings_json(self.broker.url, self.token)))

    def argv(self, resume: bool) -> list[str]:
        return with_model(super().argv(resume), self.model)

    def join_text(self) -> str:
        """What the person types to an agent: join the room, nothing more about how to behave in it. The
        second sentence is the live tests' (a cheap model otherwise takes a message that reaches it after
        a tool call for hook output); it says nothing about answering."""
        room = self.broker.room
        return (
            f'Use the switchboard tools. Call join with room "{room}" and screen_name "{self.name}".'
            f" Then reply with one word: joined. Messages from {HUMAN} in {room} are from your user: they may"
            " reach you when you stop, or as extra context after one of your tool calls."
        )

    def alive(self) -> bool:
        """True; raises if the CLI ended, so a wait on it ends at once and not at its deadline."""
        if self.tmux.pane_dead(self.session):
            raise HarnessError("crash", f"{self.name}'s Claude Code exited")
        return True

    def bring_up(self) -> None:
        self.launch()
        self.type(self.join_text())
        self.wait(lambda: self.alive() and self.member() is not None, 180, f"{self.name} to join")
        self.wait(lambda: self.alive() and self.settled(), 180, f"{self.name} to be idle and reachable")

    def stop(self) -> None:
        """End the session; the trial's world owns the broker and the rest."""
        try:
            self.final_screen = self.tmux.capture(self.session, history=500)
        except Exception:
            self.final_screen = ""
        self.tmux.kill_server()


class ClaudeWorld:
    """Claude Code agents, one per name in the scenario, in a room of a throwaway broker."""

    human = HUMAN

    def __init__(self, model: str = MODEL) -> None:
        self.model = model
        self.broker: Broker | None = None
        self.agents: list[EvalClaude] = []
        self.abort_reason: str | None = None  # set when the user's own config changed: the run must stop
        self.notes: dict[str, Any] = {}
        self._drift_before: dict[str, str | None] = {}

    @property
    def db_path(self) -> Path:
        assert self.broker is not None
        return Path(self.broker.paths.db)

    def start(self, scenario: Scenario, rules: str, limits: Limits) -> None:
        self._drift_before = drift.snapshot()
        self.broker = Broker("claude", room=scenario.room)
        self.broker.start()
        slug = scenario.room.lstrip("#")
        hdr = {**self.broker.hdr}
        web = self.broker.web
        assert web is not None
        r = web.put(
            f"/api/rooms/{slug}/wake",
            json={"budget_per_hour": limits.budget_per_hour, "hop_limit": limits.hop_limit},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        if rules:
            r = web.put(f"/api/rooms/{slug}/rules", json={"text": rules}, headers=hdr)
            assert r.status_code == 200, r.text
        self.agents = [EvalClaude(n, self.broker, self.model).start() for n in scenario.agents]
        deadline = time.monotonic() + limits.join_s
        pool = ThreadPoolExecutor(max_workers=len(self.agents))
        try:
            futures = [pool.submit(a.bring_up) for a in self.agents]
            for f in futures:
                try:
                    f.result(timeout=max(1.0, deadline - time.monotonic()))
                except FutureTimeout:
                    raise HarnessError(
                        "timeout", f"the agents had not all joined in {limits.join_s:.0f}s"
                    ) from None
        except BaseException:
            for a in self.agents:  # the others' waits end at once when their terminals are gone
                a.tmux.kill_server()
            raise
        finally:
            pool.shutdown(wait=True)

    def say(self, text: str) -> int:
        assert self.broker is not None
        return self.broker.say(text)

    def idle(self) -> bool:
        assert self.broker is not None
        members = {m["name"]: m for m in self.broker.members()}
        for a in self.agents:
            if a.tmux.pane_dead(a.session):
                raise HarnessError("crash", f"{a.name}'s Claude Code exited")
            a.decline_stray_prompt()
        # not ``queued``: chatter that addresses nobody waits in the queue for the engine's 270 s hold
        # (§8.2), and a trial does not wait for it
        return all(
            (m := members.get(a.name)) is not None and m["status"] == "idle" and not m["parked"]
            for a in self.agents
        )

    def tokens(self) -> dict[str, Tokens]:
        return {a.name: a.meter.read() for a in self.agents if a.meter is not None}

    def stop(self) -> None:
        stray = sum(a.stray_declined for a in self.agents)
        for a in self.agents:
            a.stop()
        if self.broker is not None:
            self.broker.stop()
        left: list[str] = []
        if self.agents:
            left = self.agents[0].sweep()
        for a in self.agents:
            a.scrub()
        fail, _info = drift.compare(self._drift_before, drift.snapshot())
        self.notes = {"stray_prompts_declined": stray, "leftover_processes": [x[:100] for x in left]}
        if fail:
            self.abort_reason = f"the user's own Claude config changed: {fail}"
        if self.broker is not None and os.environ.get("SWITCHBOARD_LIVE_KEEP") != "1":
            self.broker.cleanup()
        self.agents = []


def skip_reason() -> str | None:
    """Why the evals can't run here, or None: the CLI and tmux must be on the PATH the live driver uses."""
    for b in ("claude", "tmux"):
        if not shutil.which(b, path=REAL_PATH):
            return f"{b} is not on PATH"
    return None


def cli_version() -> str:
    """``claude --version``, run the way the live driver runs it (a clean environment)."""
    from harness.run import child_path
    from harness.tmuxdrv import clean_env

    r = subprocess.run(
        ["claude", "--version"],
        env=clean_env(child_path("claude")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    text = (r.stdout or r.stderr).strip()
    return text.splitlines()[0] if text else "?"
