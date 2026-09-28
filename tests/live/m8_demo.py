"""M8: the FPGA bench demo as a rehearsal driver (DESIGN.md §27.13 T3, docs/DEMO-FPGA.md).

Opt-in (the file name doesn't match ``test_*.py``, so a plain ``pytest`` never runs it).

**The fake remote machine, unattended** (``SWITCHBOARD_LIVE=fakepi``, ``--scripted``): the
demo container (``sandbox/twohost/compose.yaml``, profile ``demo``) must be running::

    docker compose -f sandbox/twohost/compose.yaml --profile demo up -d --build pi
    SWITCHBOARD_LIVE=fakepi uv run python tests/live/m8_demo.py --scripted

The driver starts a test-mode broker in a rehearsal home under ``SWITCHBOARD_LIVE_DIR``
(default: the temp dir), pairs it with the container for real (``remote add`` against a
temp ssh config and known_hosts holding the host key the container printed, ``remote
accept --yes`` inside the container into a home of its own, ``remote enable``), and runs
stand-ins on both sides over the real SSH link: in the container ``tests/twohost/actors.py
bench``, a stand-in Claude session that the satellite attests (``claude:inbox``) and that
asks for approval before flashing (its registry says ``waiting`` for a few seconds); on
this machine a scripted ``vivado`` (a ``test`` member) that builds a stand-in bitstream,
pushes it with a key restricted to ``rrsync -wo ~/fpga/in`` and posts the ``artifact:``
line. At the end it removes what it added in the container (its link line, its push-key
line, its home there) and stops the broker.

**The real remote machine, with real Claude sessions** (``SWITCHBOARD_LIVE=pi``, the owner's
rehearsal before recording)::

    SWITCHBOARD_LIVE=pi SWITCHBOARD_M8_HOME=<a rehearsal home you paired by hand> \\
      [SWITCHBOARD_M8_REMOTE=fpga-pi] [SWITCHBOARD_M8_SSH=<your ssh destination for it>] \\
      uv run python tests/live/m8_demo.py

The rehearsal home must be a test home (under the temp dir, with a ``.switchboard-test``
file) that you paired once (``remote add`` there, ``remote accept`` on the remote); disable
the remote on your real home first (one broker dials a remote at a time). The driver
starts the broker there, checks the link, prints the two join prompts for you to paste
(docs/DEMO-FPGA.md §5), waits for both members, then posts the human's lines through the
web API and checks what comes back. You approve the flash on the remote yourself. With
``SWITCHBOARD_M8_SSH`` it also reads, over your own ssh config and read-only, the
remote's switchboard version and the md5s of its harness config before and after.

Both modes: the preflight (versions on both machines from ``remote status``, the link up,
this machine's harness-config md5s before and after, ``/hops`` at least 20), the human's
lines through the web API, and checks of the tiers, the approval hold (``waiting-approval``
and a held delivery), the ``artifact:`` and ``result:`` lines and a second cycle. It writes
``tests/live/_runs/m8-<time>.md`` with the timings. It never copies a login and never adds a
key anywhere but the fake remote container.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import httpx
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from harness import drift  # noqa: E402
from harness.tmuxdrv import REAL_HOME  # noqa: E402

pytestmark = pytest.mark.live

if __name__ == "__main__" and "--scripted" in sys.argv[1:]:
    os.environ["SWITCHBOARD_M8_SCRIPTED"] = "1"  # `python tests/live/m8_demo.py --scripted`
LIVE = {x.strip() for x in os.environ.get("SWITCHBOARD_LIVE", "").split(",") if x.strip()}
SCRIPTED = os.environ.get("SWITCHBOARD_M8_SCRIPTED") == "1"
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
# the docker CLI's env, taken at import (the suite's clean-env fixture moves HOME to a temp
# dir later, where the CLI would find no compose plugin)
DOCKER_ENV = {k: v for k, v in os.environ.items()
              if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")
              or k.startswith(("DOCKER_", "BUILDX_", "COMPOSE_"))}
COMPOSE = ["docker", "compose", "-f", str(ROOT / "sandbox" / "twohost" / "compose.yaml"), "--profile", "demo"]
ACTORS_HOST = ROOT / "tests" / "twohost" / "actors.py"
ACTORS_CT = "/opt/switchboard-src/tests/twohost/actors.py"
CT_PY = "/opt/switchboard/bin/python"
CT_HOME = "/home/pi/sb-scripted"  # the scripted run's switchboard home in the container (not ~/.switchboard)
PUSH_COMMENT = "switchboard-m8-scripted-push"
ROOM = "#fpga"
HOLD_S = 6.0
STEP_S = 60.0 if SCRIPTED else 1800.0  # how long one step may take: a stand-in, or a real build and flash
# a relative path whose parts never start with a dot (no "..", no leading "/"), and a plain
# board name: the bench prompt puts nothing else from the line into a command (§27.9)
SEG = r"[A-Za-z0-9_-][A-Za-z0-9_.-]*"
ART_RE = re.compile(rf"artifact: (?P<rel>{SEG}(?:/{SEG})*) sha256:(?P<sha>[0-9a-f]{{64}}) size:(?P<size>\d+)"
                    r" board:(?P<board>[A-Za-z0-9_-]+)(?=\s|$)")
RESULT_RE = re.compile(r"^result: (?P<sha12>[0-9a-f]{12}) pull=(ok|fail|skip) verify=(ok|fail) flash=(ok|fail|skip)"
                       r" uart=(?P<uart>pass\(\d+/\d+\)|fail\(\d+/\d+\)|fail|skip) t=\d+s")
TOKEN_RE = re.compile(r"switchboard remote accept '([^']+)'")

# the human's lines (docs/DEMO-FPGA.md §6)
LINE_BUILD = "@vivado build blinky with the UART echo at 115200 and hand it to @bench"
LINE_HELD = "@bench no rush: tell me when the flash is done"
LINE_REVERSE = "@vivado reverse the LED pattern"

# the join prompts: docs/DEMO-FPGA.md §5 is their one source (read at run time), with its
# bracketed bench values filled from the environment where given
DEMO_DOC = ROOT / "docs" / "DEMO-FPGA.md"
BENCH_VALUES = {
    "[board]": os.environ.get("SWITCHBOARD_M8_BOARD") or ("fake" if "fakepi" in LIVE else None),
    "[/dev/ttyUSB1]": os.environ.get("SWITCHBOARD_M8_UART") or ("~/bench/ttyFAKE0" if "fakepi" in LIVE else None),
    "[115200]": os.environ.get("SWITCHBOARD_M8_BAUD") or "115200",
}


def demo_prompts() -> dict[str, str]:
    """``{"vivado": …, "bench": …}``: the quoted blocks under **vivado** and **bench** in
    §5 of docs/DEMO-FPGA.md, joined into one paragraph each, bench values filled in."""
    text = DEMO_DOC.read_text(encoding="utf-8")
    sec = text.split("\n## 5. Prompts\n", 1)[1].split("\n## ", 1)[0]
    out: dict[str, str] = {}
    for who in ("vivado", "bench"):
        block = sec.split(f"\n**{who}**", 1)[1].split("\n\n", 2)[1]
        lines = [ln[1:].strip() for ln in block.splitlines() if ln.startswith(">")]
        prompt = " ".join(ln for ln in lines if ln)
        for k, v in BENCH_VALUES.items():
            if v:
                prompt = prompt.replace(k, v)
        out[who] = prompt
    assert "Join switchboard room #fpga as vivado" in out["vivado"], out
    assert "Join switchboard room #fpga as bench" in out["bench"], out
    return out


def _skip_reason() -> str | None:
    if not LIVE & {"fakepi", "pi"}:
        return "set SWITCHBOARD_LIVE=fakepi (with --scripted) or SWITCHBOARD_LIVE=pi to run the M8 rehearsal"
    if "fakepi" in LIVE and not SCRIPTED:
        return ("SWITCHBOARD_LIVE=fakepi runs --scripted only; for real Claude sessions in the container, pair"
                " a rehearsal home with it and use SWITCHBOARD_LIVE=pi")
    if "pi" in LIVE and not os.environ.get("SWITCHBOARD_M8_HOME"):
        return "SWITCHBOARD_LIVE=pi needs SWITCHBOARD_M8_HOME, a rehearsal home paired with the remote by hand"
    return None


if __name__ != "__main__" and _skip_reason():
    pytest.skip(_skip_reason() or "", allow_module_level=True)


def ts() -> str:
    return time.strftime("%H:%M:%S")


class Actor:
    """A long-running ``actors.py``: JSON lines out, JSON commands in."""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env, start_new_session=True)
        self.q: Queue[dict[str, Any]] = Queue()
        self.log: list[dict[str, Any]] = []
        self.err: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(self.p.stderr or []), daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout or []:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ev.setdefault("at", time.time())
            self.log.append(ev)
            self.q.put(ev)

    def next(self, ev: str, timeout: float, pred: Any = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {ev} in {timeout:.0f} s; events {self.log[-6:]}; {''.join(self.err)[-2000:]}")
            try:
                e = self.q.get(timeout=left)
            except Empty:
                continue
            if e.get("ev") == ev and (pred is None or pred(e)):
                return e

    def send(self, obj: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                assert self.p.stdin is not None
                self.p.stdin.write('{"op": "quit"}\n')
                self.p.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.p.wait(15)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)


class Rehearsal:
    def __init__(self) -> None:
        base = os.environ.get("SWITCHBOARD_LIVE_DIR") or tempfile.gettempdir()
        self.fake = "fakepi" in LIVE
        if self.fake:
            self.home = Path(tempfile.mkdtemp(prefix="yk-home-m8-", dir=base))
            (self.home / ".switchboard-test").touch()
            self.remote = "fakepi"
        else:
            self.home = Path(os.environ["SWITCHBOARD_M8_HOME"]).expanduser()
            self.remote = os.environ.get("SWITCHBOARD_M8_REMOTE", "fpga-pi")
        self.run = Path(tempfile.mkdtemp(prefix="yk-run-m8-", dir=base))
        self.timeline: list[dict[str, Any]] = []
        self.checks: dict[str, Any] = {}
        self.t0: float | None = None
        self.broker: subprocess.Popen[bytes] | None = None
        self.actors: list[Actor] = []
        self.drift_before = drift.snapshot()
        self.remote_before: dict[str, str] | None = None
        # the fake run's tags (its dirs under ~/fpga/in there) and what the container held
        # before it, so its teardown removes exactly what the run added
        self.rid = f"{os.getpid() % 10000:04d}{int(time.time()) % 10000:04d}"
        self.tags: list[str] = []
        self.ct_before: dict[str, Any] | None = None
        # the fake run's processes get a HOME of their own: nothing of this user's harness config,
        # Codex daemon or Claude sessions is in reach; a real run's broker keeps the real HOME
        # (its rehearsal home's config.toml names what it uses)
        self.benv = {"HOME": str(self.run) if self.fake else REAL_HOME, "PATH": REAL_PATH, "LANG": "en_US.UTF-8",
                     "SWITCHBOARD_TEST": "1", "TMPDIR": tempfile.gettempdir()}

    # ------------------------------------------------------------- log
    def note(self, what: str, **kw: Any) -> None:
        t = time.time()
        rel = round(t - self.t0, 2) if self.t0 else None
        self.timeline.append({"t0_s": rel, "what": what, **kw})
        print(f"[m8 {ts()} T0{'+' if rel is not None else ''}{rel if rel is not None else '-'}] {what}"
              f" {kw if kw else ''}", flush=True)

    # --------------------------------------------------------- commands
    def sb(self, *args: str, check: bool = True, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        r = subprocess.run([sys.executable, "-m", "switchboard", "--home", str(self.home), *args], env=self.benv,
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if check and r.returncode != 0:
            raise AssertionError(f"switchboard {' '.join(args)}: {r.stdout}{r.stderr}")
        return r

    def dc(self, *args: str, input: str | None = None, check: bool = True,
           timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        r = subprocess.run([*COMPOSE, *args], input=input, capture_output=True, text=True, timeout=timeout,
                           stdin=None if input is not None else subprocess.DEVNULL, env=DOCKER_ENV)
        if check and r.returncode != 0:
            raise AssertionError(f"docker compose {' '.join(args[:6])}: {r.stdout[-2000:]}{r.stderr[-2000:]}")
        return r

    def ct(self, *cmd: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        """A command in the fake remote container, as its user."""
        return self.dc("exec", "-T", "-u", "pi", "-w", "/home/pi", "pi", *cmd, **kw)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        from switchboard.mcp.client import call_sync
        from switchboard.paths import Paths

        return call_sync(Paths.from_home(self.home).sock, method, params or {}, timeout)

    def status(self) -> dict[str, Any]:
        return self.call("remote.status", {"name": self.remote})["remotes"][0]

    def members(self) -> dict[str, dict[str, Any]]:
        r = self.web.get("/api/rooms/fpga/members")
        return {m["name"]: m for m in r.json()["members"]}

    def messages(self, after: int = 0) -> list[dict[str, Any]]:
        return self.web.get("/api/rooms/fpga/messages", params={"after": after, "limit": 500}).json()["messages"]

    def say(self, text: str) -> int:
        r = self.web.post("/api/rooms/fpga/say", json={"text": text}, headers=self.hdr)
        assert r.status_code == 200, r.text
        self.note("human", text=text)
        return r.json()["id"]

    def wait_msg(self, pred: Any, timeout: float, what: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self.messages():
                if pred(m):
                    return m
            time.sleep(0.2)
        raise AssertionError(f"no {what} in {timeout:.0f} s")

    def wait_member(self, name: str, pred: Any, timeout: float, what: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        m: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            m = self.members().get(name)
            if m is not None and pred(m):
                return m
            time.sleep(0.1)
        raise AssertionError(f"{name} never {what}: {m}")

    # ------------------------------------------------------------ set-up
    def start_broker(self) -> None:
        if self.fake:
            # never this user's Codex daemon or Claude sessions: a socket and a sessions dir of its own
            (self.home / "config.toml").write_text(
                'human_name = "alice"\n\n'
                f'[claude]\nsessions_dir = "{self.run / "claude-sessions"}"\n\n'
                f'[codex]\ncontrol_socket = "{self.run / "cx.sock"}"\nbin = "/usr/bin/false"\n')
        out = open(self.run / "broker.out", "ab")
        self.broker = subprocess.Popen(
            [sys.executable, "-m", "switchboard", "start", "--foreground", "--test-mode", "--home", str(self.home),
             "--port", "0"], env=self.benv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True)
        from switchboard.mcp.client import ping
        from switchboard.paths import Paths

        paths = Paths.from_home(self.home)
        deadline = time.monotonic() + 30
        port = 0
        while time.monotonic() < deadline and not port:
            info = ping(paths.sock, timeout=1.0)  # a long home puts its socket elsewhere (sun_path)
            port = info["port"] if info else 0
            time.sleep(0.1)
        assert port, (self.run / "broker.out").read_text()[-3000:]
        base = f"http://switchboard.localhost:{port}"
        self.web = httpx.Client(base_url=base, timeout=30.0, follow_redirects=False)
        tok = paths.test_login_token.read_text().strip()
        assert self.web.get(f"/login?t={tok}").status_code == 303
        self.hdr = {"Origin": base, "X-Switchboard": "1", "Content-Type": "application/json"}
        rooms = [r["name"] for r in self.web.get("/api/rooms").json()["rooms"]]
        if ROOM not in rooms:
            assert self.web.post("/api/rooms", json={"name": ROOM}, headers=self.hdr).status_code == 200
        hops = next(r for r in self.web.get("/api/rooms").json()["rooms"] if r["name"] == ROOM)["settings"]["hop_limit"]
        if hops < 20:
            r = self.web.post("/api/rooms/fpga/command", json={"text": "/hops 30"}, headers=self.hdr)
            assert r.status_code == 200 and r.json().get("ok"), r.text
            hops = 30
        self.checks["hop_limit"] = hops

    def pair_container(self) -> None:
        """``remote add`` here, ``remote accept`` in the container, ``remote enable``: for real,
        against temp ssh files here (never ~/.ssh) and a home of its own in the container."""
        running = self.dc("ps", "--status", "running", "--services", check=False).stdout.split()
        assert "pi" in running, ("the demo container isn't running: docker compose -f sandbox/twohost/compose.yaml"
                                 " --profile demo up -d --build pi")
        pub = self.ct("cat", "/home/pi/.sshd/host_ed25519.pub").stdout.split()
        host_key = f"{pub[0]} {pub[1]}"
        fp = self.dc("logs", "pi", check=False).stdout
        assert host_key in fp, "the container printed another host key than it serves"
        snap = self.ct("python3", "-c", "import json, os; h = '/home/pi'; f = h + '/bench/flash.log';"
                                        " print(json.dumps({'ssh': sorted(os.listdir(h + '/.ssh')),"
                                        " 'flash_log': os.path.getsize(f) if os.path.exists(f) else None}))")
        self.ct_before = json.loads(snap.stdout.strip().splitlines()[-1])
        (self.run / "ssh_config").write_text("")
        (self.run / "desk_ak").write_text("")
        (self.run / "ssh-dir").mkdir(mode=0o700)
        (self.run / "known_hosts").write_text(f"[127.0.0.1]:2222 {host_key}\n")
        r = self.sb("remote", "add", self.remote, "pi@127.0.0.1", "--port", "2222", "--rooms", ROOM,
                    "--ssh-config", str(self.run / "ssh_config"), "--known-hosts", str(self.run / "known_hosts"),
                    "--authorized-keys", str(self.run / "desk_ak"), "--label", "desk")
        token = TOKEN_RE.search(r.stdout)
        assert token, r.stdout + r.stderr
        self.ct("python3", "-c", f"import os; os.umask(0o077); os.makedirs({CT_HOME!r} + '/claude-sessions',"
                                 " exist_ok=True);"
                                 f" open({CT_HOME!r} + '/config.toml', 'w').write('[claude]\\nsessions_dir = "
                                 f"\"{CT_HOME}/claude-sessions\"\\n')")
        r = self.ct("switchboard", "--home", CT_HOME, "remote", "accept", token.group(1), "--yes")
        self.note("accept", out=r.stdout.strip().splitlines()[-1:])
        # the bitstream push key (§27.8.4), limited to writing ~/fpga/in there
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", PUSH_COMMENT, "-f",
                        str(self.run / "fpga_push")], check=True, capture_output=True, env=self.benv, timeout=30)
        push = (self.run / "fpga_push.pub").read_text().strip()
        self.ct("bash", "-c", "cat >> ~/.ssh/authorized_keys",
                input=f'restrict,command="rrsync -wo /home/pi/fpga/in" {push}\n')
        self.push_ssh = (f"/usr/bin/ssh -F /dev/null -i {self.run / 'fpga_push'} -o IdentitiesOnly=yes"
                         f" -o IdentityAgent=none -o UserKnownHostsFile={self.run / 'known_hosts'}"
                         " -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes"
                         " -o LogLevel=ERROR -p 2222")
        # consent from the web session (the UI's Enable button): the human's, whoever runs this driver,
        # for the config the remotes panel shows (its config_hash)
        [shown] = [x for x in self.web.get("/api/remotes").json()["remotes"] if x["name"] == self.remote]
        self.note("remote", dest=shown["dest"], host_keys=shown["host_keys"])
        r = self.web.post(f"/api/remotes/{self.remote}/enable", json={"config_hash": shown["config_hash"]},
                          headers=self.hdr, timeout=60)
        assert r.status_code == 200 and r.json()["text"].startswith("link ok: satellite "), r.text
        self.note("enable", text=r.json()["text"])

    def doctors(self) -> None:
        args = ["remote", "doctor"]
        if self.fake:
            args += ["--authorized-keys", str(self.run / "desk_ak"), "--ssh-dir", str(self.run / "ssh-dir")]
        r = self.sb(*args, check=False)
        self.checks["doctor_desktop"] = r.stdout.strip().splitlines()
        assert "FAIL" not in r.stdout, r.stdout
        if self.fake:
            r = self.ct("switchboard", "--home", CT_HOME, "remote", "doctor", check=False)
            self.checks["doctor_remote"] = r.stdout.strip().splitlines()
            assert "FAIL" not in r.stdout, r.stdout

    def remote_facts(self) -> dict[str, str] | None:
        """The remote's switchboard version and harness-config md5s, read-only, over the
        owner's own ssh config (pi mode with SWITCHBOARD_M8_SSH only)."""
        dest = os.environ.get("SWITCHBOARD_M8_SSH")
        if self.fake or not dest:
            return None
        cmd = ("switchboard --version 2>/dev/null || ~/.local/bin/switchboard --version; "
               "md5sum ~/.claude/settings.json ~/.codex/config.toml ~/.codex/hooks.json 2>/dev/null; true")
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", dest, cmd], capture_output=True,
                           text=True, timeout=60, env={"HOME": REAL_HOME, "PATH": REAL_PATH,
                                                       **({"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
                                                          if "SSH_AUTH_SOCK" in os.environ else {})})
        return {"out": r.stdout.strip(), "rc": str(r.returncode)}

    def start_actors(self) -> None:
        if self.fake:
            self.bench = Actor([*COMPOSE, "exec", "-T", "-u", "pi", "-w", "/home/pi", "pi", CT_PY, ACTORS_CT, "bench",
                                "--home", CT_HOME, "--hold-s", str(HOLD_S)], env=DOCKER_ENV)
            self.actors.append(self.bench)
            j = self.bench.next("joined", 60)
            assert j["ok"] and j["tier"] == "claude:inbox", j
            self.vivado = Actor([sys.executable, str(ACTORS_HOST), "agent", "--home", str(self.home), "--name",
                                 "vivado", "--test-session", "vivado"], env=self.benv)
            self.actors.append(self.vivado)
            assert self.vivado.next("joined", 60)["ok"]
            return
        prompts = demo_prompts()
        print("\n=== Paste these into the two Claude sessions (docs/DEMO-FPGA.md §5) ===\n")
        print(f"vivado (this machine, in the FPGA project):\n  {prompts['vivado']}\n")
        print(f"bench (the remote machine, in ~/bench):\n  {prompts['bench']}\n")
        left = sorted({k for k in BENCH_VALUES if k in prompts["vivado"] + prompts["bench"]})
        if left:
            print(f"(fill in {', '.join(left)}, or set SWITCHBOARD_M8_BOARD / _UART / _BAUD)\n")
        sys.stdout.flush()
        for name in ("vivado", "bench"):
            m = self.wait_member(name, lambda m: True, 900, "joined")
            self.note("joined", name=name, tier=m["tier"], host=m["host"])

    # --------------------------------------------------- scripted vivado
    def vivado_turn(self, tag: str) -> dict[str, Any]:
        """The scripted vivado: wait() for your instruction, build, push, post the artifact
        line as a reply to it (a reply to the human is exempt from the say rate limit)."""
        deadline = time.monotonic() + STEP_S
        while True:
            assert time.monotonic() < deadline, "vivado never got its instruction"
            self.vivado.send({"op": "wait", "room": ROOM, "timeout_s": 30})
            got = self.vivado.next("wait", 45)
            ids = [int(x) for x in re.findall(r"^- id=(\d+) .*kind=human", got.get("text") or "", re.M)]
            if ids:
                break
        tag = f"{tag}-{self.rid}"  # a dir of this run's own under ~/fpga/in there
        self.tags.append(tag)
        out = self.run / "out" / tag / "top.bit"
        b = subprocess.run([sys.executable, str(ACTORS_HOST), "build", "--out", str(out), "--tag", tag],
                           capture_output=True, text=True, timeout=60, env=self.benv, check=True)
        bit = json.loads(b.stdout.strip().splitlines()[-1])
        r = subprocess.run(["/usr/bin/rsync", "-t", "-e", self.push_ssh, str(out), f"pi@127.0.0.1:{tag}/"],
                           capture_output=True, text=True, timeout=60, env=self.benv)
        assert r.returncode == 0, r.stderr
        self.note("vivado pushed", tag=tag, sha12=bit["sha256"][:12])
        text = f"@bench artifact: {tag}/top.bit sha256:{bit['sha256']} size:{bit['size']} board:fake via:push"
        self.vivado.send({"op": "say", "room": ROOM, "text": text, "reply_to": ids[-1]})
        said = self.vivado.next("say", 30)
        assert said.get("posted_id"), said
        return bit

    # ------------------------------------------------------------ script
    def cycle(self, n: int, line: str, tag: str) -> dict[str, Any]:
        t_say = time.time()
        self.say(line)
        if self.fake:
            self.vivado_turn(tag)
        art = self.wait_msg(lambda m: m["from"] == "vivado" and ART_RE.search(m["text"] or "") is not None
                            and m["ts"] >= t_say - 1, STEP_S, f"artifact line (cycle {n})")
        t_art = art["ts"]
        self.note("artifact", cycle=n, id=art["id"], text=art["text"][:120])
        if self.fake:
            # the stand-in's own record: the inbox frame or hook context that listed the artifact
            # line (the human's line may have woken it first: it @mentions bench too), else the job
            aid = art["id"]
            seen = self.bench.next("job", STEP_S, pred=lambda e: e["id"] == aid)
            first = [e for e in self.bench.log if e.get("ev") in ("woken", "context") and aid in e.get("ids", [])]
            t_woke = min([e["at"] for e in first] + [seen["at"]])
            status = self.members()["bench"]["status"]
        else:
            status = self.wait_member("bench", lambda m: m["status"] in ("busy", "waiting-approval"), STEP_S,
                                      "woke (busy)")["status"]
            t_woke = time.time()
        self.note("bench woke", cycle=n, status=status, after_s=round(t_woke - t_art, 2))
        held = None
        if n == 1:
            held = self.check_hold()
        res = self.wait_msg(lambda m: m["from"] == "bench" and m["text"].startswith("result:")
                            and m["reply_to"] == art["id"], STEP_S, f"result line (cycle {n})")
        self.note("result", cycle=n, text=res["text"].splitlines()[0])
        m = RESULT_RE.match(res["text"])
        assert m, res["text"]
        sha = ART_RE.search(art["text"]).group("sha")  # type: ignore[union-attr]
        assert m.group("sha12") == sha[:12] and res["host"] == self.remote
        return {"cycle": n, "say_to_artifact_s": round(t_art - t_say, 2), "artifact_to_woke_s": round(t_woke - t_art, 2),
                "artifact_to_result_s": round(res["ts"] - t_art, 2), "result": res["text"].splitlines()[0],
                "uart": m.group("uart"), "held": held}

    def check_hold(self) -> dict[str, Any] | None:
        """While bench's approval prompt is open, the buddy list says waiting-approval and a
        message for it is held (§27.5.6): the broker posts nothing into an open prompt."""
        try:
            m = self.wait_member("bench", lambda m: m["status"] == "waiting-approval", 20 if self.fake else STEP_S,
                                 "waiting-approval")
        except AssertionError:
            if self.fake:
                raise
            self.note("no approval prompt seen (bench ran the flash without asking)")
            return None
        t_wait = time.time()
        self.note("bench waiting-approval")
        mid = self.say(LINE_HELD)
        time.sleep(2.0)
        still = self.members()["bench"]
        # the stand-in's inbox watcher reports every frame as it arrives, even while its turn
        # sits in the approval prompt: none may arrive during the hold
        frames_during = ([e for e in self.bench.log if e.get("ev") == "inbox" and e["at"] > t_wait]
                         if self.fake else [])
        ok = still["status"] == "waiting-approval" and (still["held"] or still["queued"]) and not frames_during
        self.note("hold", status=still["status"], held=still["held"], queued=still["queued"], ok=bool(ok))
        assert ok or not self.fake, (still, frames_during)
        return {"message_id": mid, "status": still["status"], "held": still["held"], "queued": still["queued"]}

    # ------------------------------------------------------------ report
    def report(self, cycles: list[dict[str, Any]]) -> Path:
        r = self.sb("report", "--room", ROOM, "--last", "30m", check=False, timeout=120)
        (self.run / "report.md").write_text(r.stdout)
        runs = HERE / "_runs"
        runs.mkdir(exist_ok=True)
        out = runs / f"m8-{time.strftime('%Y%m%d-%H%M%S')}.md"
        lines = [f"# M8 rehearsal ({'fake remote, scripted' if self.fake else 'real remote'})", "",
                 f"- remote: `{self.remote}`; link: {self.checks.get('link')}",
                 f"- versions: this broker {self.checks.get('broker_version')}, satellite {self.checks.get('sat_version')}",
                 f"- tiers: {self.checks.get('tiers')}", f"- /hops: {self.checks.get('hop_limit')}",
                 f"- harness config on this machine unchanged: {self.checks.get('drift_ok')}"
                 f" (info: {self.checks.get('drift_info')})", ""]
        lines += ["| cycle | say → artifact | artifact → bench has it | artifact → result | result | hold |",
                  "|---|---|---|---|---|---|"]
        for c in cycles:
            hold = "—" if not c["held"] else f"{c['held']['status']}, held={c['held']['held']}"
            lines.append(f"| {c['cycle']} | {c['say_to_artifact_s']} s | {c['artifact_to_woke_s']} s |"
                         f" {c['artifact_to_result_s']} s | `{c['result']}` | {hold} |")
        lines += ["", "## Timeline", "", "```"]
        lines += [json.dumps(e) for e in self.timeline]
        lines += ["```", "", f"Room report: `{self.run / 'report.md'}`", ""]
        out.write_text("\n".join(lines))
        return out

    # ---------------------------------------------------------- teardown
    def close(self) -> None:
        for a in self.actors:
            a.stop()
        if self.fake:
            for cmd in (["switchboard", "--home", CT_HOME, "remote", "remove", self.remote, "--yes"],
                        ["python3", "-c", "p='/home/pi/.ssh/authorized_keys'; L=open(p).read().splitlines(True);"
                                          f" open(p,'w').writelines(l for l in L if {PUSH_COMMENT!r} not in l)"],
                        ["python3", "-c", f"import shutil; shutil.rmtree({CT_HOME!r}, ignore_errors=True)"]):
                with_rc = self.ct(*cmd, check=False)
                if with_rc.returncode != 0:
                    print(f"[m8] teardown: {' '.join(cmd[:4])}: {with_rc.stderr[-300:]}", flush=True)
            if self.ct_before is not None:
                # the authorized_keys backups `remote accept` and `remote remove` made, the pushed
                # bitstreams, and flash.log back to its length before the run
                code = ("import json, os, shutil, sys; b = json.loads(sys.argv[1]); h = '/home/pi'\n"
                        "for n in os.listdir(h + '/.ssh'):\n"
                        "    if n.startswith('authorized_keys.bak-switchboard-') and n not in b['ssh']:\n"
                        "        os.unlink(h + '/.ssh/' + n)\n"
                        "for t in b['tags']:\n"
                        "    shutil.rmtree(h + '/fpga/in/' + t, ignore_errors=True)\n"
                        "f = h + '/bench/flash.log'\n"
                        "if os.path.exists(f):\n"
                        "    os.truncate(f, b['flash_log']) if b['flash_log'] is not None else os.unlink(f)\n")
                r = self.ct("python3", "-c", code, json.dumps({**self.ct_before, "tags": self.tags}), check=False)
                if r.returncode != 0:
                    print(f"[m8] teardown: container files: {r.stderr[-300:]}", flush=True)
        if self.broker is not None and self.broker.poll() is None:
            self.broker.send_signal(signal.SIGTERM)
            try:
                self.broker.wait(15)
            except subprocess.TimeoutExpired:
                self.broker.kill()
        if self.fake:
            shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture(scope="module")
def reh() -> Any:
    r = Rehearsal()
    try:
        yield r
    finally:
        r.close()


def test_m8_rehearsal(reh: Rehearsal) -> None:
    reh.start_broker()
    if reh.fake:
        reh.pair_container()
    st = reh.status()
    assert st["state"] == "up", f"the link isn't up: {st.get('text')}"
    from switchboard import __version__

    reh.checks.update(link=st["text"], sat_version=st["version"], broker_version=__version__)
    reh.note("preflight", link=st["text"], hooks=st["hooks"], harden=st["harden"])
    reh.doctors()
    reh.remote_before = reh.remote_facts()
    reh.start_actors()
    mem = reh.members()
    reh.checks["tiers"] = {n: f"{m['tier']}{'@' + m['host'] if m['host'] else ''}" for n, m in mem.items()}
    assert mem["bench"]["host"] == reh.remote and mem["bench"]["tier"] == "claude:inbox", mem["bench"]
    reh.t0 = time.time()
    cycles = [reh.cycle(1, LINE_BUILD, "blinky")]
    if reh.fake:
        # the next say to vivado: an agent may say() once per 10 s unless it replies to the human
        time.sleep(1.0)
    cycles.append(reh.cycle(2, LINE_REVERSE, "blinky-rev"))
    for c in cycles:
        assert c["uart"].startswith("pass"), c
    after = drift.snapshot()
    fail, info = drift.compare(reh.drift_before, after)
    reh.checks.update(drift_ok=not fail, drift_info=info)
    assert not fail, f"harness config changed on this machine: {fail}"
    remote_after = reh.remote_facts()
    if reh.remote_before is not None:
        assert remote_after == reh.remote_before, (reh.remote_before, remote_after)
    out = reh.report(cycles)
    print(f"\n[m8] report: {out}\n", flush=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-m", "live", "-s", "-p", "no:cacheprovider", "-q",
                          "-o", "addopts=", *[a for a in sys.argv[1:] if a != "--scripted"]]))
