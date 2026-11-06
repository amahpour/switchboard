"""The meeting self-test with the bot hosted on a person's machine (DESIGN.md §54, #623, phase 3).

Opt-in, with ``SWITCHBOARD_LIVE=meeting``:

    uv run pytest -m live tests/live/test_meeting_selftest_hosted.py -s -q

Two tests:

1. **A hosted bot starts and stops cleanly without a call.** No link, no meeting, no model, no network but
   the loopback. The plugin's ``meeting-host start --no-join`` registers a call at a throwaway broker and
   starts the real joinly and the relay (switchboard's image, built from this checkout). The relay reaches
   joinly and is heard from. A person's End call makes it leave and exit 0 and takes joinly down with it,
   and ``stop`` removes what is left. This is the test to run on any machine with Docker and the joinly
   image.
2. **Every meetings feature with a hosted bot in a real call.** The same six steps as
   ``test_meeting_selftest.py`` against a real call (``SWITCHBOARD_MEETING_URL_FILE``, mode 0600, as
   there), with one difference in how the bot gets there: step 1 is the host script, not ``/meeting
   <link>``. The scripted agent ``claude-1`` hears the call and answers as before. The hosting person's
   machine is a second agent session and the agent token the script registers with. The broker is
   throwaway, and the second joinly is the test speaker. Step 5 is that a hosted call has no snapshot yet
   (the room says so). In step 6 the containers must also be gone. The meeting-self-test skill makes the
   Zoom meeting; **this test never makes or deletes one.**

Where the bot runs (``SWITCHBOARD_SELFTEST_HOSTED``):

- ``local`` (the default): on this machine. The script runs here. The relay, in a container, reaches the
  throwaway broker (which listens on this machine's loopback only) through a forwarder on Docker's bridge
  address and ``--add-host``.
- ``remote``: on another machine, such as one with an NVIDIA GPU. The test starts the broker, writes the
  agent token the script registers with to ``<out>/host-token`` (mode 0600), **prints the exact commands
  to run on the other machine**, and waits for the call to appear and go live. The broker is not opened to
  the network: the other machine reaches its loopback through an SSH tunnel (``ssh -L``), once for the
  script's own requests and once on Docker's bridge address for the relay's container. That machine needs
  this checkout (or the plugin), Docker with Compose, ``curl``, the joinly image, and switchboard's image
  (``docker build -f deploy/image/Dockerfile .``, or the release that has the relay). For the GPU it also
  needs the NVIDIA Container Toolkit and ``ghcr.io/joinly-ai/joinly:v0.5.4-cuda``. The device the script
  picks, GPU or CPU, is in the report. If the broker is better reached by address than by a tunnel, it
  would need ``--listen`` and ``--public-url``, and a plain-http public URL is only taken for
  ``localhost``, ``*.localhost``, ``*.test`` and ``127.0.0.1`` (DESIGN.md §30), so it would be a name in
  both machines' hosts files. The tunnel needs neither.

Other settings: ``SWITCHBOARD_SELFTEST_DEVICE`` (``cpu`` or ``gpu``, to force what the script would pick),
``SWITCHBOARD_SELFTEST_MODEL``, ``SWITCHBOARD_SELFTEST_OUT`` (the report folder),
``SWITCHBOARD_LIVE_RELAY_IMAGE`` (an image for the relay; otherwise one is built from the checkout) and
``SWITCHBOARD_LIVE_FIXES_DIR`` (a folder that has the two pinned joinly fixes, so nothing is fetched; each
is checked by sha256).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_meeting_selftest as base
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from harness import relay, selftest
from harness.selftest import SecretLink, StepResult
from harness.world import REAL_PATH, live_set

from switchboard.broker import agent_tokens
from switchboard.config import Config, MeetingCfg
from switchboard.meeting.links import PLATFORM_LABEL
from switchboard.models import session_key

pytestmark = pytest.mark.live

if "meeting" not in live_set():
    pytest.skip(
        "set SWITCHBOARD_LIVE=meeting to run the live tests of a hosted meeting bot "
        "(the first needs only Docker and the joinly image; the second needs a call's link too)",
        allow_module_level=True,
    )

MODE = os.environ.get("SWITCHBOARD_SELFTEST_HOSTED", "local")
DEVICE = os.environ.get("SWITCHBOARD_SELFTEST_DEVICE", "")
MODEL = os.environ.get("SWITCHBOARD_SELFTEST_MODEL", "")
PLUGIN = relay.REPO / "plugins" / "claude"
SCRIPT = PLUGIN / "bin" / "meeting-host"
BAR_JOIN_S = float(os.environ.get("SWITCHBOARD_SELFTEST_JOIN_S") or "300")  # images, a model, then the join
READY_S = 240.0  # joinly's start, in the container
STOP_S = 60.0
REMOTE_WAIT_S = 1800.0

# The six steps as a hosted bot has them: the same, but for where the bot comes from and what step 5 can do.
HOSTED_STEPS = {
    **base.STEPS,
    1: (
        "the host script starts the bot and the bar goes live",
        "meeting-host start with the link on stdin: the room's call bar is live on the {platform} call,"
        " and shows the fingerprint the script printed",
    ),
    5: (
        "a hosted call has no snapshot yet, and says so",
        "/meeting snapshot is answered with a sentence that says a hosted call has none, and posts no card",
    ),
    6: (
        "the bots leave and the containers stop",
        "the bar ends and the speaker no longer sees the bot; the speaker leaves; relay and joinly are gone",
    ),
}


class Machine:
    """The hosting machine as the script sees it: a folder for the plugin's data, the agent's token as the
    credential, and where the relay's image and the broker are. The script is run as the agent runs it."""

    def __init__(self, broker: InProcBroker, token: str, image: str, root: Path) -> None:
        self.broker, self.token, self.image = broker, token, image
        self.data = root / "data"
        self.data.mkdir(mode=0o700)
        self.seeded = relay.seed_fixes(self.data / "joinly-fixes")

    def env(self) -> dict[str, str]:
        env = {
            "PATH": REAL_PATH,
            "HOME": relay.REAL_HOME,  # docker finds its config there
            "LANG": "C.UTF-8",
            "SWITCHBOARD_AGENT_TOKEN": self.token,
            "SWITCHBOARD_RELAY_IMAGE": self.image,
            "SWITCHBOARD_RELAY_ADD_HOST": "switchboard.localhost:host-gateway",
        }
        for k in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR"):
            if k in os.environ:
                env[k] = os.environ[k]
        return env

    def args(self) -> list[str]:
        out = ["--server", self.broker.base, "--data", str(self.data)]
        if DEVICE in ("cpu", "gpu"):
            out.append(f"--{DEVICE}")
        if MODEL:
            out += ["--model", MODEL]
        return out

    def start(self, link: SecretLink | None) -> subprocess.CompletedProcess[str]:
        """``meeting-host start``: the link on stdin, never an argument. With none, a setup check."""
        flags = [] if link is not None else ["--no-join"]
        return subprocess.run(
            ["sh", str(SCRIPT), "start", *flags, *self.args(), base.ROOM.lstrip("#")],
            input=(link.reveal() + "\n") if link is not None else "",
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=900,
        )

    def stop(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(SCRIPT), "stop", "--data", str(self.data)],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=180,
        )


def project_of(stdout: str) -> str:
    found = re.search(r"project (switchboard-call-[0-9a-f]{8})", stdout)
    assert found, f"the script did not name its project:\n{stdout}"
    return found.group(1)


def fingerprint_of(stdout: str) -> str:
    found = re.search(r"Key fingerprint: ([0-9a-f]{4}·[0-9a-f]{4})", stdout)
    assert found, f"the script did not print the key's fingerprint:\n{stdout}"
    return found.group(1)


def meeting_rows(broker: InProcBroker) -> list[dict[str, Any]]:
    import sqlite3

    con = sqlite3.connect(broker.paths.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM meetings ORDER BY id")]
    finally:
        con.close()


def add_machine(broker: InProcBroker, room: str = base.ROOM, device: str = "selftest-laptop") -> str:
    """The hosting person's machine: an agent token for ``device`` (the credential the script registers with)
    and one agent session of that machine in the room, as the broker keeps it when a Claude Code on that
    laptop has
    joined (a call can only be hosted into a room that has an agent on the hosting machine). Returns the
    token."""

    def go() -> str:
        made = agent_tokens.make(broker.state, None, device, "alice")
        store = broker.state.store
        r = store.get_room(room)
        assert r is not None
        p = store.upsert_participant("claude", session_key("claude", device, "selftest-host"), host=device)
        store.create_membership(r.id, p.id, "laptop-agent")
        return str(made["token"])

    return str(broker.on_loop(go))


def hosted_broker(home: Path) -> InProcBroker:
    cfg = Config(human_name="alice").replace(meeting=MeetingCfg(joinly_url="http://127.0.0.1:9/mcp/"))
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0, rate_limit_s=0.0)
    return InProcBroker(home, cfg).start()


# ------------------------------------------------------------------------ 1: no call, nothing joined
async def test_a_hosted_bot_starts_and_stops_cleanly_without_a_call(tmp_home: Path) -> None:
    """The script starts the real joinly and the relay for a registered call and returns; the relay reaches
    joinly and says so; End call makes it leave (exit 0) and joinly goes with it; ``stop`` removes everything.
    No call is joined. Fails if either container is left running, a project or folder survives ``stop``, or
    a secret is in an argument."""
    if MODE != "local":
        pytest.skip("the setup check runs on the machine that hosts; use the local mode there")
    why = relay.skip_reason()
    if why:
        pytest.skip(why)
    before = set(relay.projects())
    image = relay.relay_image()
    broker = hosted_broker(tmp_home)
    web = broker.web_client()
    forwarder = relay.Forwarder(relay.gateway_ip(), broker.port).start()
    root = Path(tempfile.mkdtemp(prefix="sb-hosted-selftest-"))
    try:
        assert (
            web.post("/api/rooms", json={"name": base.ROOM}, headers=broker.write_headers()).status_code
            == 200
        )
        machine = Machine(broker, add_machine(broker), image, root)
        began = time.monotonic()
        started = await asyncio.to_thread(machine.start, None)
        assert started.returncode == 0, started.stderr[-600:]
        project = project_of(started.stdout)
        fp = fingerprint_of(started.stdout)
        print(f"started in {time.monotonic() - began:.1f} s: {started.stdout.strip().splitlines()[0]} ({fp})")
        row = meeting_rows(broker)[-1]
        assert (row["source"], row["state"], row["platform"]) == ("hosted", "joining", "unknown"), row
        assert f"{str(row['key_hash'])[:4]}·{str(row['key_hash'])[4:8]}" == fp  # the bar and the script agree
        # the relay reaches joinly (its own wait for a container that is still starting)
        deadline = time.monotonic() + READY_S
        while "joinly is ready" not in relay.logs(project):
            assert relay.running(project), "the relay stopped early:\n" + relay.logs(project)
            assert time.monotonic() < deadline, "the relay never reached joinly:\n" + relay.logs(project)
            await asyncio.sleep(1.0)
        assert "joining no call" in relay.logs(project)
        containers = {c["service"] for c in relay.projects()[project]}
        assert containers == {"relay", "joinly"}
        assert machine.data.joinpath("calls", project.rsplit("-", 1)[1], "key").exists()
        # End call, from the room: the relay is told by its poll and leaves
        ended = web.post(
            f"/api/rooms/{base.SLUG}/command", json={"text": "/meeting leave"}, headers=broker.write_headers()
        )
        assert ended.status_code == 200, ended.text
        deadline = time.monotonic() + STOP_S
        while relay.running(project):
            assert time.monotonic() < deadline, "the containers were still running:\n" + relay.logs(project)
            await asyncio.sleep(0.5)
        cs = {c["service"]: c for c in relay.projects()[project]}
        assert cs["relay"]["exit"] == "0" and cs["joinly"]["state"] == "exited", cs
        stopped = await asyncio.to_thread(machine.stop)
        assert stopped.returncode == 0 and "stopped the call" in stopped.stdout, stopped
        assert set(relay.projects()) == before, "a project outlived stop"
        assert not list((machine.data / "calls").iterdir())
        print(
            f"LIVE RESULTS hosted setup check: relay exit {cs['relay']['exit']},"
            f" joinly {cs['joinly']['state']}"
        )
    finally:
        forwarder.stop()
        web.close()
        broker.stop()
        relay.remove([p for p in relay.projects() if p not in before])
        shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------------- 2: a real call, six steps
class HostedWorld(base.World):
    """``World`` with step 1 done by the host script (or by a person on the other machine), step 5 asking
    what a
    hosted call does about a snapshot, and step 6 also looking at the containers."""

    def __init__(self, *a: Any, machine: Machine | None, token_file: Path | None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.machine = machine
        self.token_file = token_file
        self.project = ""
        self.fingerprint = ""
        self.device = ""

    async def step(
        self, n: int, run: Any, needs: Any = ()
    ) -> None:  # the same as World.step, with this table
        saved = base.STEPS[n]
        base.STEPS[n] = HOSTED_STEPS[n]
        try:
            await super().step(n, run, needs)
        finally:
            base.STEPS[n] = saved

    def instructions(self) -> str:
        port = self.broker.port
        return (
            "\nRun this on the machine that hosts the bot. It needs Docker with Compose, curl, this checkout"
            " (or the plugin), the joinly image, switchboard's image, and the call's link in a file of"
            " mode 0600 (called LINK here).\n"
            "  1. A tunnel to this broker, left running. The bridge address is the gateway that"
            " `docker network inspect bridge` shows there, usually 172.17.0.1:\n"
            f"     ssh -N -L 127.0.0.1:{port}:127.0.0.1:{port} -L 172.17.0.1:{port}:127.0.0.1:{port}"
            " <this machine>\n"
            f"  2. Copy {self.token_file} there (mode 0600). Then, in plugins/claude/bin:\n"
            "     SWITCHBOARD_AGENT_TOKEN=$(cat <the copy>) \\\n"
            "       SWITCHBOARD_RELAY_ADD_HOST=switchboard.localhost:host-gateway \\\n"
            "       SWITCHBOARD_RELAY_IMAGE=<switchboard's image there> \\\n"
            f"       sh ./meeting-host start --server http://switchboard.localhost:{port} --data <a folder>"
            f" {base.ROOM.lstrip('#')} < LINK\n"
            "The test waits for the call to appear and go live (up to 30 minutes).\n"
        )

    async def join(self) -> base.Outcome:
        if self.machine is not None:
            began = time.monotonic()
            ran = await asyncio.to_thread(self.machine.start, self.link)
            out = selftest.scrub(ran.stdout + ran.stderr, self.pieces)
            if ran.returncode != 0:
                return base.Outcome(False, f"meeting-host start failed ({ran.returncode}): {out[-300:]}")
            self.project, self.fingerprint = project_of(ran.stdout), fingerprint_of(ran.stdout)
            chose = re.search(r"Speech: (\w+), model ([\w.-]+)", ran.stdout)
            self.device = f"{chose.group(1)} with {chose.group(2)}" if chose else "?"
            note = f"the script returned in {time.monotonic() - began:.1f} s; {self.device}"
            wait_s = BAR_JOIN_S
        else:
            print(self.instructions())
            note = "started on another machine"
            wait_s = REMOTE_WAIT_S

        def settled() -> str | None:
            state = self.call_state()
            if state is None:
                return "ended" if any(r["source"] == "hosted" for r in meeting_rows(self.broker)) else None
            return "live" if state["state"] == "live" else None

        got = await base.until(settled, wait_s)
        state = self.call_state() or {}
        if got != "live":
            return base.Outcome(
                False, f"{note}; the bar did not go live: {got or 'still joining'}: {self.notices()}"
            )
        shown = str(state.get("fingerprint") or "")
        if self.fingerprint and shown != self.fingerprint:
            return base.Outcome(
                False, f"{note}; the bar shows key {shown} and the script printed {self.fingerprint}"
            )
        self.fingerprint = shown
        self.bot_live = True
        return base.Outcome(
            True,
            f"state=live platform={self.link.platform} host={state.get('host')} key {shown}; {note}",
        )

    async def snapshot(self) -> base.Outcome:
        cursor = self.last_id()
        r = self.command("/meeting snapshot")
        said = selftest.scrub(r.text, self.pieces)
        cards = [m for m in self.messages(cursor) if m.get("files")]
        refused = "isn't available yet for a call hosted on a person's machine" in said
        return base.Outcome(refused and not cards, (said or f"HTTP {r.status_code}")[:300])

    async def leave(self) -> base.Outcome:
        out = await super().leave()
        if self.machine is None:
            return base.Outcome(
                out.ok,
                out.seen + "; the containers are on the other machine: check that `docker ps -a --filter"
                " label=com.switchboard.call` lists none there",
            )
        if not out.ok:
            return out
        deadline = time.monotonic() + STOP_S
        while relay.running(self.project) and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        cs = {c["service"]: c for c in relay.projects().get(self.project, [])}
        if relay.running(self.project):
            return base.Outcome(False, out.seen + "; but the relay or joinly is still running")
        exit_code = cs.get("relay", {}).get("exit")
        ok = exit_code == "0" and cs.get("joinly", {}).get("state") == "exited"
        return base.Outcome(
            ok, out.seen + f"; relay exit {exit_code}, joinly {cs.get('joinly', {}).get('state')}"
        )

    async def run(self) -> None:
        await self.step(1, self.join)
        await self.step(2, self.speak)
        await self.step(3, self.wake, [self.needs_bot, self.needs_speaker])
        await self.step(4, self.hear, [self.needs_bot, self.needs_speaker])
        await self.step(5, self.snapshot, [self.needs_bot])
        await self.step(6, self.leave, [self.needs_bot, self.needs_speaker])
        if self.observer is not None:
            with contextlib.suppress(Exception):
                await self.observer.leave()


def speaker_fixes(link: SecretLink, containers: dict[str, base.Joinly], stock: bool) -> None:
    """The fixes the second joinly (the speaker, and the observer) runs with, as the broker's own bot's test
    gives them: Zoom's speaker names on a Zoom call, and the first-word fix. From the folder
    ``SWITCHBOARD_LIVE_FIXES_DIR`` when it has the pinned files, else fetched and checked by sha256."""
    if stock:
        return
    wanted = [(base.PREROLL_FIX_URL, base.PREROLL_FIX_SHA256, "transcription.py", base.CONTROLLER_PY)]
    if link.platform == "zoom":
        wanted.append((base.ZOOM_FIX_URL, base.ZOOM_FIX_SHA256, "zoom.py", base.ZOOM_PY))
    local = os.environ.get(relay.FIXES_ENV)
    for url, sha, name, target in wanted:
        fix = Path(tempfile.mkdtemp(prefix="sb-selftest-fix-")) / name
        have = Path(local) / name if local else None
        if have is not None and have.is_file():
            data = have.read_bytes()
        else:
            data = httpx.get(url, timeout=30.0).content
        fix.write_bytes(selftest.check_pinned(data, sha))
        fix.chmod(0o644)
        for box in containers.values():
            box.mounts.append((fix, target))


async def test_every_meetings_feature_against_a_real_call_with_a_hosted_bot(
    tmp_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The six steps with the bot hosted: the same words, the same call, but the bot is the script's joinly
    and relay (here, or on another machine in ``remote`` mode). Fails (naming the steps) like the other
    self-test, and also if a container is left behind or any piece of the link is found in the broker's
    database, messages
    or log, or in anything the script printed."""
    if base.LINK_ENV not in os.environ:
        pytest.skip(
            f"{base.LINK_ENV} names no file with a call's link: only the setup check runs without one"
        )
    link = base.read_link()
    base.check_docker()
    why = relay.skip_reason()
    if why:
        pytest.skip(why)
    pieces = link.parts()
    out_dir = Path(os.environ.get(base.OUT_ENV) or tempfile.mkdtemp(prefix="sb-selftest-hosted-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    containers = {"speaker": base.Joinly("speaker")}
    if base.OBSERVER:
        containers["observer"] = base.Joinly("observer")
    steps: list[StepResult] = []
    problems: list[str] = []
    before = set(relay.projects())
    speaker_fixes(link, containers, os.environ.get(base.STOCK_ENV, "") == "1")
    image = relay.relay_image() if MODE == "local" else ""
    try:
        with base.sigterm_is_an_interrupt():
            try:
                for box in containers.values():
                    box.start()
                for box in containers.values():
                    box.wait_healthy()
                await run_hosted_in_a_broker(
                    tmp_home, containers, link, steps, problems, caplog, image, out_dir
                )
            except Exception as e:
                problems.append(selftest.scrub(f"{type(e).__name__}: {e}", pieces)[:300])
    finally:
        removed = [box.remove() for box in containers.values()]
        relay.remove(
            [p for p in relay.projects() if p not in before]
        )  # (a local run's; the other machine is its own)
    problems += [w for w in removed if w]
    for n, (name, expected) in HOSTED_STEPS.items():
        if all(s.n != n for s in steps):
            steps.append(
                StepResult.not_attempted(
                    n, name, expected.format(platform=PLATFORM_LABEL[link.platform]), "the run stopped first"
                )
            )
    steps.sort(key=lambda s: s.n)
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": link.platform,
        "bot": f"hosted ({MODE})",
        "joinly_image": base.IMAGE,
        "device": DEVICE or "chosen by the script",
        "callouts": base.CALLOUTS,
        "bot_name": base.BOT_NAME,
        "speaker_name": base.SPEAKER_NAME,
        "containers_removed": not any(removed),
        "problems": problems,
    }
    try:
        _, md = selftest.write_report(out_dir, steps, pieces, meta)
        print("\n" + md.read_text(encoding="utf-8"))
        print(f"report: {out_dir / selftest.REPORT_JSON}")
    except (selftest.ReportLeak, OSError) as e:
        problems.append(f"the report was not written: {type(e).__name__}")
    failed = selftest.failed_steps(steps)
    if failed or problems:
        base.refuse(
            "hosted meeting self-test failed"
            + (f"; steps: {', '.join(failed)}" if failed else "")
            + (f"; problems: {' | '.join(problems)}" if problems else "")
            + f"; report in {out_dir}"
        )


async def run_hosted_in_a_broker(
    home: Path,
    containers: dict[str, base.Joinly],
    link: SecretLink,
    steps: list[StepResult],
    problems: list[str],
    caplog: pytest.LogCaptureFixture,
    image: str,
    out_dir: Path,
) -> None:
    broker = hosted_broker(home)
    web = broker.web_client()
    forwarder = relay.Forwarder(relay.gateway_ip(), broker.port).start() if MODE == "local" else None
    kept_messages = ""
    try:
        assert (
            web.post("/api/rooms", json={"name": base.ROOM}, headers=broker.write_headers()).status_code
            == 200
        )
        observing = (
            base.Speaker(containers["observer"].url, base.OBSERVER_NAME)
            if "observer" in containers
            else contextlib.nullcontext()
        )
        async with (
            FakeAgent(broker.home, "selftest-claude") as agent,
            base.Speaker(containers["speaker"].url) as speaker,
            observing as observer,
        ):
            joined = await agent.join(
                base.ROOM, "claude-1"
            )  # before the call: its spoken names are the hints
            if not joined.get("ok"):
                raise RuntimeError(f"the scripted agent could not join: {joined.get('code')}")
            token = add_machine(broker)
            root = Path(tempfile.mkdtemp(prefix="sb-hosted-selftest-"))
            machine: Machine | None = None
            token_file: Path | None = None
            if MODE == "local":
                machine = Machine(broker, token, image, root)
            else:
                token_file = out_dir / "host-token"
                token_file.touch(mode=0o600)
                token_file.chmod(0o600)
                token_file.write_text(token + "\n")
            world = HostedWorld(
                broker, web, agent, speaker, link, steps, observer, machine=machine, token_file=token_file
            )
            try:
                await world.run()
            finally:
                await world.leave_everything(problems)
                if machine is not None:
                    stopped = await asyncio.to_thread(machine.stop)  # whatever the steps did
                    if stopped.returncode != 0:
                        problems.append(f"meeting-host stop failed: {stopped.stderr[-200:]}")
                with contextlib.suppress(Exception):
                    kept_messages = json.dumps(world.messages())
                shutil.rmtree(root, ignore_errors=True)
                if token_file is not None:
                    token_file.unlink(missing_ok=True)
    finally:
        if forwarder is not None:
            forwarder.stop()
        web.close()
        broker.stop()
    blobs: dict[str, str | bytes] = {"the broker's log": caplog.text, "the room's messages": kept_messages}
    for suffix, label in (("", "the broker's database"), ("-wal", "the broker's write-ahead log")):
        path = Path(str(broker.paths.db) + suffix)
        if path.exists():
            blobs[label] = path.read_bytes()
    problems += [f"a piece of the link is in {name}" for name in selftest.leaks(link.parts(), blobs)]
