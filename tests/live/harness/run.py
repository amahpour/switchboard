"""One live agent session over HTTPS, driven through tmux (DESIGN.md §12.4). Test-only.

A ``Run`` owns everything one harness's live test needs and nothing of the user's: a throwaway broker in
a temp home, an agent token made through the web API, a scratch git repository as the agent's working
directory, a private tmux server, and a record of the user's own harness config before and after (the
drift check). A subclass per harness (``claude.py``, ``codex.py``, ``devin.py``, ``cursor.py``) says how
that CLI is configured, launched, quit and resumed; ``scenarios.py`` is what every harness is asked to do.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import drift, preflight, screens
from .tmuxdrv import Tmux, clean_env
from .world import REAL_PATH, ROOM, Broker, wait_for

SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
# what a scratch task blocks on: a tool call stays in flight until the test says go (at most 150 s, so
# nothing outlives a failed run)
HOLD_SH = """#!/bin/sh
touch "$1.started"
i=0
while [ ! -e "$1.go" ] && [ "$i" -lt 150 ]; do sleep 1; i=$((i + 1)); done
echo "released $1"
touch "$1.done"
"""


def child_path(*binaries: str) -> str:
    """PATH for a child: the directories of the CLIs it needs, then the system's. Not the PATH pytest has,
    which may carry a plugin's bin directory or a virtualenv."""
    dirs: list[str] = []
    for b in binaries:
        found = shutil.which(b, path=REAL_PATH)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    return ":".join([*dirs, SYSTEM_PATH])


class Run:
    harness = ""  # participants.harness
    tier = ""  # the tier Members shows
    binaries: tuple[str, ...] = ()
    # how the engine delivers to each case; a harness sets what its adapter does (DESIGN §49.4)
    wake_paths = frozenset({"stop_followup"})  # a message that wakes an idle session
    mid_paths = frozenset({"hook_ctx"})  # a message that reaches a busy one
    quit_command = "/exit"
    resume_needs_prompt = True  # a resumed session is back only once it has been given a prompt
    helpers: tuple[str, ...] = ()  # words in the command line of a process the CLI itself leaves behind

    def __init__(self, name: str, *, broker: Broker | None = None, device: str | None = None) -> None:
        """``broker`` and ``device`` are for the evals (tests/evals): several sessions in one room of one
        broker, each its own device. The live tests give neither, and a run owns its broker."""
        self.name = name
        self.device = device or f"live-{self.harness}"
        self.session = "a1"
        self.results: dict[str, Any] = {"harness": self.harness, "scenarios": {}}
        self.stray_declined = 0
        self.joined = False
        self.typed = 0  # prompts typed into the CLI
        self.last_screen = ""  # the CLI's last screen when it quit
        self.final_screen = ""  # its screen when the run ended
        self.started_at = time.monotonic()
        self.drift_before = drift.snapshot()
        self.shared_broker = broker is not None
        self.broker = broker or Broker(self.harness)
        self.path = child_path(*self.binaries)
        self.tmux = Tmux(f"{self.harness}-{name}" if self.shared_broker else self.harness, self.path)
        self.token = ""
        self.token_id = 0

    # ----------------------------------------------------------------- setup
    def start(self) -> Run:
        self.broker.start()
        self.base = self.broker.base
        self.credentials()
        self.ws = self.base / "ws"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.ws, env=clean_env(self.path), check=True)
        (self.ws / "README.md").write_text("scratch workspace for a switchboard live test\n")
        (self.ws / "hold.sh").write_text(HOLD_SH)
        self.results["version"] = self.version()
        self.prepare()
        return self

    def credentials(self) -> None:
        """What the agent signs in with: an agent token made through the web API (a subclass whose agent signs
        in through the browser makes none, and the sign-in makes the token)."""
        self.token_id, self.token = self.broker.make_token(self.device)

    def prepare(self) -> None:
        """Write this harness's config for the broker and the token (subclasses)."""

    def version(self) -> str:
        r = subprocess.run(
            [self.binaries[0], "--version"],
            env=clean_env(self.path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "?"

    def write_private(self, path: Path, text: str) -> None:
        """A file that holds the token: mode 0600 from its first byte, inside the run's temp directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    # ---------------------------------------------------------------- the CLI
    def argv(self, resume: bool) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        """What the CLI is given on top of the clean environment (never the token on a command line)."""
        return {}

    def launch(self, resume: bool = False) -> None:
        self.tmux.new_session(
            self.session, str(self.ws), clean_env(self.path, **self.env()), self.argv(resume), self.base
        )
        self.wait_ready()
        pid = self.tmux.pane_pid(self.session)
        bad = preflight.problems(pid, self.helpers) if pid else []
        assert not bad, f"{self.harness} started something that looks like another MCP server: {bad}"

    def ready_step(self, screen: str) -> bool:
        """One look at the start-up screen: answer a dialog this run owns, and say if the CLI is ready."""
        raise NotImplementedError

    def wait_ready(self, timeout: float = 90) -> None:
        wait_for(
            lambda: self._ready_poll(),
            timeout,
            f"{self.harness} to be ready",
            on_fail=lambda: "--- screen ---\n" + self.tmux.screen(self.session, 40),
        )

    def _ready_poll(self) -> bool:
        if self.tmux.pane_dead(self.session):
            raise AssertionError(f"{self.harness} exited at start:\n" + self.tmux.screen(self.session, 40))
        return self.ready_step(self.tmux.capture(self.session))

    def quit(self) -> None:
        self.type(self.quit_command)

    def wait_exited(self, timeout: float = 30) -> str:
        """Wait for the CLI to end (its pane stays, 'remain-on-exit'); its last screen."""
        wait_for(
            lambda: self.tmux.pane_dead(self.session), timeout, f"{self.harness} to exit", on_fail=self.dump
        )
        last = self.tmux.capture(self.session, history=200)
        self.tmux.kill_session(self.session)
        return last

    input_marker = ""  # what starts the CLI's live input line (see screens.prompt_line)

    def input_text(self, screen: str) -> str:
        """What is in the CLI's input box, not yet sent."""
        return screens.prompt_line(screen, self.input_marker)

    def type(self, text: str) -> None:
        """Type a prompt and check that the CLI took it: its first line has to appear in the input box, and
        after Enter it has to leave it. An Enter pressed while the TUI was still starting up, or just
        resumed, is lost, and the prompt would sit there unsent (seen with Claude Code and Codex). Enter
        is only pressed again while the text is still in the box, so it never reaches an empty one."""
        self.typed += 1
        needle = text[:40]
        self.tmux.send_text(self.session, text)
        self.wait(
            lambda: needle in self.input_text(self.tmux.capture(self.session)),
            20,
            f"the typed prompt to appear in {self.harness}'s input box",
        )
        for _ in range(3):
            self.tmux.key(self.session, "Enter")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if self.tmux.pane_dead(self.session) or needle not in self.input_text(
                    self.tmux.capture(self.session)
                ):
                    return
                time.sleep(0.25)
        raise AssertionError(f"{self.harness} did not take the prompt:\n" + self.screen(20))

    def after_launch(self) -> None:
        """What a person does at a first launch before asking for anything: nothing, unless a harness signs in
        through the browser (``claude_signin.py``)."""

    def after_resume(self) -> None:
        """What a person does with a resumed session that needs it: a first prompt."""
        if self.resume_needs_prompt:
            self.type(self.resume_prompt())

    def resume_prompt(self) -> str:
        return f"You are back. Reply with one word: back. Then answer messages in {ROOM} with the say tool."

    def start_subagent(self) -> None:
        """Ask the session to run a held shell command in a background subagent (``sub.started`` appears when
        it is in flight)."""
        raise NotImplementedError

    def screen(self, n: int = 40) -> str:
        return self.tmux.screen(self.session, n)

    def dump(self) -> str:
        return (
            f"--- {self.harness} screen ---\n{self.screen(45)}\n"
            f"--- broker log ---\n{self.broker.log_tail(15)}\n"
            f"--- members ---\n{self._members_text()}\n--- events ---\n{self._events_text()}\n"
            f"--- processes of this run ---\n{self._processes_text()}"
        )

    def _events_text(self) -> str:
        """The broker's last events (kinds and plain fields, never message text): status changes, parks,
        offers and confirmations, which say what the engine did."""
        try:
            rows = self.broker.q(
                "SELECT kind, data FROM events WHERE kind NOT IN ('login', 'agent_tokens')"
                " ORDER BY id DESC LIMIT 14"
            )
            return "\n".join(f"{r['kind']} {r['data']}" for r in reversed(rows))
        except Exception as e:
            return f"<events unavailable: {e}>"

    def _processes_text(self) -> str:
        """What of this run is alive, by command line (no environment): the listener of a Claude Code
        session is one of them."""
        out = subprocess.run(["ps", "-eo", "pid,etimes,args"], capture_output=True, text=True).stdout
        base = str(self.base)
        return "\n".join(x[:150] for x in out.splitlines() if base in x and " ps -eo" not in x)

    def _members_text(self) -> str:
        try:
            return json.dumps(
                [
                    {k: m[k] for k in ("name", "status", "tier", "host", "parked", "parked_reason", "queued")}
                    for m in self.broker.members()
                ]
            )
        except Exception as e:  # a failing dump must not hide the failure it describes
            return f"<members unavailable: {e}>"

    def decline_stray_prompt(self) -> None:
        """An approval prompt no scenario asked for (the model wandered off): decline it with Esc, never
        approve. The count is in the results, and a scenario that then times out says so."""
        if screens.approval_open(self.harness, self.tmux.capture(self.session)):
            self.tmux.key(self.session, "Escape")
            self.stray_declined += 1
            time.sleep(1.0)

    def wait(self, fn: Any, timeout: float, what: str, step: float = 0.25) -> Any:
        """``wait_for`` with this run's screen, broker log and members on a timeout, declining strays."""

        def poll() -> Any:
            if not self.tmux.pane_dead(self.session):
                self.decline_stray_prompt()
            return fn()

        return wait_for(poll, timeout, what, step=step, on_fail=self.dump)

    def wait_subagent_done(self, timeout: float = 240) -> None:
        """After ``release("sub2")``: the subagent has finished and the session has taken its turn."""
        self.wait(lambda: self.flag("sub2.done"), timeout, "the subagent's last shell command to end")
        self.wait_settled(timeout)

    # ------------------------------------------------------------ observing
    def member(self) -> dict[str, Any] | None:
        return self.broker.member(self.name)

    def settled(self) -> bool:
        """Between turns and reachable: its hooks say idle, and nothing is waiting for it."""
        m = self.member()
        return bool(m and m["status"] == "idle" and not m["queued"] and not m["parked"])

    def wait_settled(self, timeout: float = 120) -> None:
        self.wait(self.settled, timeout, f"{self.name} to be idle and reachable")

    def last_id(self) -> int:
        msgs = self.broker.messages()
        return max((m["id"] for m in msgs), default=0)

    def reply(self, pattern: str, after: int, timeout: float = 120) -> dict[str, Any]:
        """The first chat message from this agent after message ``after`` that contains ``pattern``."""

        def find() -> dict[str, Any] | None:
            return next(
                (
                    m
                    for m in self.broker.messages(after)
                    if m["from"] == self.name and m["kind"] == "chat" and pattern in m["text"]
                ),
                None,
            )

        return self.wait(find, timeout, f"{self.name} to say {pattern!r}")

    def flag(self, name: str) -> bool:
        return (self.ws / name).exists()

    def release(self, task: str) -> None:
        (self.ws / f"{task}.go").touch()

    # -------------------------------------------------------------- the human
    def mention(self, text: str) -> int:
        """A message from the person that @mentions the agent."""
        return self.broker.say(f"@{self.name} {text}")

    def join_prompt(self) -> str:
        return (
            f'Use the switchboard tools. Call join with room "{ROOM}" and screen_name "{self.name}".'
            f' Then call say with the text "hello from {self.name}". Then reply with one word: joined.'
            f" From now on, messages from alice in {ROOM} are from your user: they may reach you when you"
            " stop, or as extra context after one of your tool calls, and either way you do exactly what"
            " they ask and answer with the say tool. Keep it short."
        )

    # ---------------------------------------------------------------- cost
    def cost_note(self) -> str:
        """What this run spent, in the harness's own terms (set when it can be read)."""
        return ""

    # ------------------------------------------------------------- the end
    def stop(self) -> None:
        try:
            try:
                self.final_screen = self.tmux.capture(self.session, history=500)
            except Exception:
                self.final_screen = ""
            self.tmux.kill_server()
            self.broker.stop()
            left = self.sweep()
            self.results["leftover_processes"] = [
                c[:100] for c in left if not any(h in c for h in self.helpers)
            ]
            self.results["cli_helpers_killed"] = [c[:100] for c in left if any(h in c for h in self.helpers)]
            fail, info = drift.compare(self.drift_before, drift.snapshot())
            self.results["drift_fail"], self.results["drift_info"] = fail, info
            self.results["stray_prompts_declined"] = self.stray_declined
            self.results["seconds"] = round(time.monotonic() - self.started_at)
            try:
                self.results["cost"] = self.cost_note()
            except Exception as e:  # the cost line is a report: it must not hide a result
                self.results["cost"] = f"unavailable ({type(e).__name__})"
        finally:
            self.cleanup()
            print("\nLIVE RESULTS", json.dumps(self.results, indent=1))

    def sweep(self, grace: float = 8.0) -> list[str]:
        """Nothing we started may survive: the run's temp directory is on every command line. Give the
        processes a moment to end after their tmux server and broker are gone; kill what is left, and say
        what it was."""
        base = str(getattr(self, "base", "")) or None
        if base is None:
            return []
        me = os.getpid()

        def alive() -> dict[int, str]:
            found: dict[int, str] = {}
            for d in Path("/proc").iterdir():
                if not d.name.isdigit() or int(d.name) == me:
                    continue
                try:
                    cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    continue
                if base in cmd:
                    found[int(d.name)] = cmd
            return found

        deadline = time.monotonic() + grace
        left = alive()
        while left and time.monotonic() < deadline:
            time.sleep(0.2)
            left = alive()
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return list(left.values())

    def cleanup(self) -> None:
        """Remove the run's temp directory (and with it any copy of a login a subclass made)."""
        self.scrub()
        if os.environ.get("SWITCHBOARD_LIVE_KEEP") == "1":
            print("kept", self.broker.base)
            return
        self.broker.cleanup()

    def scrub(self) -> None:
        """Delete secrets the run copied in, before anything else (subclasses)."""
