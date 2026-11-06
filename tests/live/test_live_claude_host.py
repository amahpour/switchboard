"""Live Claude Code hosting the meeting bot on this machine (DESIGN.md §54, #623, phase 3).

Opt-in with the same switch as the other Claude runs: ``SWITCHBOARD_LIVE=claude uv run pytest -m live
tests/live/test_live_claude_host.py -s -q``.

One real ``claude --model haiku`` session, signed in through the browser (no token typed anywhere, a
throwaway ``CLAUDE_CONFIG_DIR``), with the plugin from this checkout and a throwaway hosted broker. It
joins a room and is asked to host the meeting bot for the room as a setup check (``--no-join``, no link:
**no call is joined, and no meeting is made or touched**). Then the real chain runs:

1. The model reads the plugin's skill, with Claude Code having filled in its ``${CLAUDE_PLUGIN_ROOT}``,
   ``${CLAUDE_PLUGIN_DATA}`` and ``${user_config.server_url}``.
2. The model runs ``bin/meeting-host`` in its Bash tool.
3. The script finds the listener key ``bin/headers`` made for that Claude Code, registers the call, and
   starts the Compose project: joinly (the real image, on the CPU path) and the relay (switchboard's
   image, built from this checkout). They reach each other on the relay's loopback, and the broker through
   a forwarder on Docker's bridge.
4. The person presses End call. The relay leaves and exits 0, the joinly container goes with it, and
   ``meeting-host stop`` removes what is left.

What it shows about how a command the agent runs reaches the credential (the open question of #628): the
exact Bash command the model ran, taken from its transcript, and the key file the script used. The
recorded payloads are ``tests/fixtures/payloads/claude/PostToolUse_bash_plugin_env.json`` (a Bash
command's variables) and ``PostToolUse_bash_plugin_skill.json`` (a skill's text, filled in).

It needs ``claude`` logged in, ``tmux``, Docker with Compose, the joinly image
(``ghcr.io/joinly-ai/joinly:v0.5.4``, never pulled by the test), a headless Chromium for the consent card,
and switchboard's own image, which it builds from the checkout (or ``SWITCHBOARD_LIVE_RELAY_IMAGE`` names
one). The two joinly fixes the script wants are fetched from their pinned addresses and checked by sha256
unless ``SWITCHBOARD_LIVE_FIXES_DIR`` names a folder that already has the two pinned files (the test
checks their hashes). It costs a few cents and a minute of CPU.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from harness import relay, scenarios
from harness.claude import ALLOW, plugin_dir, project_dir_name, settings_json
from harness.claude_signin import ClaudeSigninRun, browser_missing
from harness.world import ROOM, skip_reason, wait_for

pytestmark = pytest.mark.live

_why = skip_reason("claude", "claude", "curl") or browser_missing() or relay.skip_reason()
if _why:
    pytest.skip(_why, allow_module_level=True)

SKILL = "host-meeting-bot"


class HostRun(ClaudeSigninRun):
    """The sign-in run, with what a host needs: rules that let the model run this plugin's script and read its
    skill (and nothing else more than the other runs allow), the relay's image, and a way for a container to
    reach the throwaway broker."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.image = ""
        self.forwarder: relay.Forwarder | None = None
        self.before = set(relay.projects())

    def prepare(self) -> None:
        super().prepare()
        self.image = relay.relay_image()
        # the broker listens on this machine's loopback; a container reaches it at Docker's bridge address
        self.forwarder = relay.Forwarder(relay.gateway_ip(), self.broker.port).start()
        settings = settings_json(self.broker.url, None)
        settings["permissions"]["allow"] = [*ALLOW, f"Skill({SKILL})", "Bash(sh *meeting-host*)"]
        self.write_private(self.settings, json.dumps(settings))

    def env(self) -> dict[str, str]:
        return {
            **super().env(),
            # what the script reads from the environment Claude Code was started in (a Bash command inherits
            # it)
            "SWITCHBOARD_RELAY_IMAGE": self.image,
            # the broker's public name, as a container sees it
            "SWITCHBOARD_RELAY_ADD_HOST": "localhost:host-gateway",
        }

    def data_dir(self) -> Path:
        """The plugin's data folder for this run: where ``bin/headers`` put the listener key."""
        return self.key_file().parent

    def new_projects(self) -> list[str]:
        return [p for p in relay.projects() if p not in self.before]

    def host_commands(self) -> list[str]:
        """The Bash commands the model ran that start or stop the script, from Claude Code's transcript."""
        found = []
        folder = self.claude_home / "projects" / project_dir_name(self.ws)
        for f in sorted(folder.glob("*.jsonl")):
            for line in f.read_text(errors="replace").splitlines():
                try:
                    content = json.loads(line).get("message", {}).get("content")
                except ValueError:
                    continue
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        cmd = str(block.get("input", {}).get("command", ""))
                        if "meeting-host" in cmd:
                            found.append(cmd)
        return found

    def stop(self) -> None:
        try:
            super().stop()
        finally:
            if self.forwarder is not None:
                self.forwarder.stop()
            relay.remove(self.new_projects())  # whatever a failed run left


@pytest.fixture(scope="module")
def run() -> Iterator[HostRun]:
    r = HostRun("claude-3")
    try:
        r.start()
        yield r
    finally:
        r.stop()
    assert r.results["drift_fail"] == [], f"the user's own Claude config changed: {r.results['drift_fail']}"
    assert r.results["leftover_processes"] == [], "processes of the run outlived it"
    assert r.new_projects() == [], "a hosted call's containers outlived the run"


def hosted_row(r: HostRun) -> dict[str, object] | None:
    rows = r.broker.q("SELECT * FROM meetings WHERE source = 'hosted' ORDER BY id DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def test_1_sign_in_through_the_browser_then_join(run: HostRun) -> None:
    """No token anywhere: the session signs in, joins the room and speaks as ``claude@<device>``."""
    scenarios.join_and_say(run)


def test_2_the_model_hosts_the_bot_through_the_plugins_skill_and_script(run: HostRun) -> None:
    """The person asks in words; the model uses the skill, runs the script, and a hosted call exists with this
    machine's name on it, the key's fingerprint in the room's settings, and two containers behind it. Fails if
    the skill's text reaches the model with its placeholders unfilled, if the script cannot find the
    credential
    from a Bash command, or if the containers do not come up."""
    run.wait_settled()
    seeded = relay.seed_fixes(run.data_dir() / "joinly-fixes")
    run.type(
        f"Use the {SKILL} skill to check the meeting bot's setup for {ROOM}. It is only a setup check: no"
        " call, so leave the link out, add --no-join and give the command an empty stdin. Reply with one"
        " word: started."
    )
    row = run.wait(lambda: hosted_row(run), 240, "the host script to register a call")
    assert row is not None
    assert row["host"] == run.device and row["platform"] == "unknown" and row["state"] == "joining", row
    assert row["model"] == "base", row  # the CPU path: no GPU on this machine, or the person forced nothing
    fp8 = str(row["key_hash"])[:8]
    project = f"switchboard-call-{fp8}"

    # what the model ran, as Claude Code had filled in the skill's text for it (point 5 of #623's phase 3)
    commands = run.host_commands()
    assert commands, "the model ran no meeting-host command"
    start = next(c for c in commands if " start " in c)
    assert "${" not in start, f"the skill's placeholders reached the model unfilled: {start}"
    plugin = plugin_dir().resolve()
    assert str(plugin / "bin" / "meeting-host") in start, start  # ${CLAUDE_PLUGIN_ROOT}
    assert str(run.data_dir()) in start, start  # ${CLAUDE_PLUGIN_DATA}
    assert f'--server "{run.broker.url}"' in start or f"--server {run.broker.url}" in start, start
    assert "--no-join" in start and "https://" not in re.sub(re.escape(run.broker.url), "", start)

    # two containers behind it, joinly reachable by the relay alone
    wait_for(lambda: len(relay.projects().get(project, [])) == 2, 120, "both containers to be there")
    wait_for(
        lambda: "joinly is ready" in relay.logs(project) or not relay.running(project),
        240,
        "the relay to reach joinly",
        on_fail=lambda: relay.logs(project),
    )
    assert relay.running(project), relay.logs(project)
    log = relay.logs(project)
    assert f"key {fp8[:4]}·{fp8[4:]}" in log and "joinly is ready" in log, log
    assert "joining no call" in log
    inspect = json.loads(relay.docker("inspect", *[c["id"] for c in relay.projects()[project]]).stdout)
    by_service = {i["Config"]["Labels"]["com.docker.compose.service"]: i for i in inspect}
    joinly = by_service["joinly"]
    assert joinly["HostConfig"]["NetworkMode"].startswith("container:"), (
        "joinly must share the relay's network"
    )
    assert not joinly["NetworkSettings"]["Ports"] or all(
        not v for v in joinly["NetworkSettings"]["Ports"].values()
    )
    assert joinly["HostConfig"]["PidMode"].startswith("container:"), "joinly must end with the relay"
    call_dir = run.data_dir() / "calls" / fp8
    assert (call_dir / "key").stat().st_mode & 0o077 == 0 and not (call_dir / "link").exists()
    scenarios.record(
        run,
        "host",
        project=project,
        command=re.sub(re.escape(str(Path(run.base))), "<run>", start)[:400],
        fixes_seeded=seeded,
        model=row["model"],
        containers=sorted(c["service"] for c in relay.projects()[project]),
    )


def test_3_end_call_makes_the_relay_leave_and_the_containers_stop_by_themselves(run: HostRun) -> None:
    """The person presses End call (``/meeting leave``): the relay is told on its next poll, exits 0, and the
    joinly container ends with it (it shares the relay's process namespace). Nothing is left running, with no
    one running ``stop``. Fails if either container survives."""
    row = hosted_row(run)
    assert row is not None
    project = f"switchboard-call-{str(row['key_hash'])[:8]}"
    reply = run.broker.command("/meeting leave")
    assert "leaving" in reply["text"], reply
    wait_for(
        lambda: not relay.running(project), 60, "both containers to stop", on_fail=lambda: relay.logs(project)
    )
    cs = {c["service"]: c for c in relay.projects()[project]}
    assert cs["relay"]["exit"] == "0", relay.logs(project)  # the call ended: a clean exit
    assert cs["joinly"]["state"] == "exited"
    assert "the call has ended" in relay.logs(project)
    scenarios.record(run, "end_call", relay_exit=cs["relay"]["exit"], joinly_state=cs["joinly"]["state"])


def test_4_stop_removes_what_is_left(run: HostRun) -> None:
    """``meeting-host stop`` (what the skill tells the model to run when the person says leave the call)
    removes
    the exited containers, their network and the call's folder. Run here as the person would from a shell."""
    script = plugin_dir() / "bin" / "meeting-host"
    done = subprocess.run(
        ["sh", str(script), "stop", "--data", str(run.data_dir())],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": run.path, "HOME": str(run.base), "LANG": "C.UTF-8"},
    )
    assert done.returncode == 0, done.stderr
    assert "stopped the call with key" in done.stdout
    assert run.new_projects() == []
    assert not list((run.data_dir() / "calls").glob("*"))
    scenarios.record(run, "stop", stdout=done.stdout.splitlines()[0][:80])
