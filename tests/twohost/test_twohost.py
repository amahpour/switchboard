"""Two hosts in containers (DESIGN.md §27.13 T2; marker ``twohost``, opt-in).

``sandbox/twohost/compose.yaml``'s ``test`` profile: ``desk`` (user dev, uid 1000, a
test-mode broker) and ``testpi`` (user pi, uid 1001, a user-level sshd on 2222, reached
as ``pi``) on an internal network, in separate PID namespaces, with no capabilities. The
pairing is the real one: ``remote add`` on desk (the host key pinned from the one the
remote printed), ``remote accept`` on the remote (non-editable install, into its own
``authorized_keys``), ``remote enable``; the link is the production ssh argv. The remote's
agent is ``tests/twohost/actors.py bench``, a stand-in Claude session that the remote's
satellite attests and the broker wakes through its inbox; the desktop's is a ``test``
member (``vivado``). Bitstreams move by the agents' own restricted keys (``rrsync -wo`` for
a push, ``rrsync -ro`` for a pull), never by switchboard (§27.8.4).

Run (Docker needed; builds the image, starts the containers, removes them after)::

    uv run pytest -m twohost tests/twohost

Nothing touches this machine's ``~/.ssh``, sshd or switchboard home.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "sandbox" / "twohost" / "compose.yaml"
PY = "/opt/switchboard/bin/python"
ACTORS = "/opt/switchboard-src/tests/twohost/actors.py"
DESK_HOME = "/tmp/sb-desk"  # a test-mode home: under the temp dir, with the marker
WORK = "/tmp/sb-work"
PI_HOME = "/home/pi/.switchboard"
NAME = "fpga-pi"
ROOM = "#fpga"
TOKEN_RE = re.compile(r"switchboard remote accept '([^']+)'")
# The docker CLI's env, taken at import (before the suite's clean-env fixture moves HOME to a
# temp dir, where the CLI would find no compose plugin): what it needs to reach the daemon,
# and nothing of any harness (§0).
DOCKER_ENV = {
    k: v
    for k, v in os.environ.items()
    if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")
    or k.startswith("DOCKER_")
    or k.startswith("BUILDX_")
    or k.startswith("COMPOSE_")
}
PI_SSH = (
    "ssh -F /dev/null -i {key} -o IdentitiesOnly=yes -o IdentityAgent=none -o UserKnownHostsFile={kh}"
    " -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o BatchMode=yes -o LogLevel=ERROR"
    " -p {port}"
)


def _docker_missing() -> str | None:
    if shutil.which("docker") is None:
        return "no docker"
    try:
        r = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=20, env=DOCKER_ENV
        )
        if r.returncode != 0:
            return "no docker compose"
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
            env=DOCKER_ENV,
        )
        return None if r.returncode == 0 else "the docker daemon isn't running"
    except (OSError, subprocess.TimeoutExpired):
        return "docker doesn't answer"


pytestmark = [pytest.mark.twohost]


class Actor:
    """A long-running ``actors.py`` in a container: JSON lines out, stdin to stop it."""

    def __init__(self, argv: list[str]):
        self.p = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=DOCKER_ENV,
        )
        self.q: Queue[dict[str, Any]] = Queue()
        self.log: list[dict[str, Any]] = []
        self.err: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        assert self.p.stdout is not None
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self.log.append(ev)
            self.q.put(ev)

    def _read_err(self) -> None:
        assert self.p.stderr is not None
        for line in self.p.stderr:
            self.err.append(line)

    def next(self, ev: str, timeout: float = 60.0, pred: Any = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(
                    f"no {ev} event in {timeout} s; last events: {self.log[-8:]};"
                    f" stderr: {''.join(self.err)[-3000:]}"
                )
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


class TwoHost:
    def __init__(self) -> None:
        self.project = f"sbth-{secrets.token_hex(3)}"
        self.base = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, "--profile", "test"]
        self.actors: list[Actor] = []

    # ----------------------------------------------------------- docker
    def compose(
        self, *args: str, timeout: float = 900.0, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            [*self.base, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=DOCKER_ENV,
        )
        if check and r.returncode != 0:
            raise AssertionError(f"docker compose {' '.join(args)}: {r.stdout[-2000:]}{r.stderr[-2000:]}")
        return r

    def exec_argv(
        self, service: str, user: str, *cmd: str, env: dict[str, str] | None = None, detach: bool = False
    ) -> list[str]:
        e = []
        for k, v in (env or {}).items():
            e += ["-e", f"{k}={v}"]
        home = "/home/dev" if user == "dev" else "/home/pi"
        return [
            *self.base,
            "exec",
            "-T",
            *(["-d"] if detach else []),
            "-u",
            user,
            "-w",
            home,
            *e,
            service,
            *cmd,
        ]

    def run(
        self,
        service: str,
        user: str,
        *cmd: str,
        input: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            self.exec_argv(service, user, *cmd, env=env),
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=None if input is not None else subprocess.DEVNULL,
            env=DOCKER_ENV,
        )
        if check and r.returncode != 0:
            raise AssertionError(
                f"{service}$ {' '.join(cmd)} -> {r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
            )
        return r

    def desk(self, *cmd: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.run("desk", "dev", *cmd, env={"SWITCHBOARD_TEST": "1", **kw.pop("env", {})}, **kw)

    def pi(self, *cmd: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.run("testpi", "pi", *cmd, **kw)

    def sh(self, service: str, script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        user = "dev" if service == "desk" else "pi"
        return self.run(service, user, "bash", "-euo", "pipefail", "-c", script, **kw)

    def container(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()

    def network(self) -> str:
        return f"{self.project}_lan"

    def ip(self, service: str) -> str:
        cid = self.container(service)
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=DOCKER_ENV,
        )
        return r.stdout.split()[0]

    # ------------------------------------------------------- switchboard
    def sb(self, *args: str, check: bool = True, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return self.desk("switchboard", "--home", DESK_HOME, *args, check=check, timeout=timeout)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        r = self.desk(PY, ACTORS, "rpc", "--home", DESK_HOME, method, json.dumps(params or {}))
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert out["ok"], out
        return out["result"]

    def status(self) -> dict[str, Any]:
        return self.rpc("remote.status", {"name": NAME})["remotes"][0]

    def wait_state(self, state: str, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        st: dict[str, Any] = {}
        while time.monotonic() < deadline:
            st = self.status()
            if st["state"] == state:
                return st
            time.sleep(0.5)
        raise AssertionError(f"the link never reached {state}: {st}")

    def messages(self) -> list[dict[str, Any]]:
        return self.rpc("room.history", {"room": ROOM, "limit": 500})["messages"]

    def members(self) -> list[dict[str, Any]]:
        return self.rpc("room.who", {"room": ROOM})["members"]

    def member(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.members() if m["name"] == name), None)

    def actor(self, service: str, user: str, *args: str) -> Actor:
        env = {"SWITCHBOARD_TEST": "1"} if service == "desk" else None
        a = Actor(self.exec_argv(service, user, PY, ACTORS, *args, env=env))
        self.actors.append(a)
        return a

    # ------------------------------------------------------------ set-up
    def up(self) -> None:
        self.compose("up", "-d", "--build", timeout=1800.0)
        deadline = time.monotonic() + 60
        while "Server listening" not in self.compose("logs", "testpi", check=False).stdout:
            assert time.monotonic() < deadline, self.compose("logs", check=False).stdout[-3000:]
            time.sleep(0.5)

    def start_broker(self) -> None:
        self.desk(
            "bash",
            "-c",
            f"umask 077; mkdir -p {DESK_HOME} {WORK} /home/dev/fpga/out"
            f" && touch {DESK_HOME}/.switchboard-test"
            f" && printf 'human_name = \"alice\"\\n' > {DESK_HOME}/config.toml",
        )
        # the broker in the foreground of a detached exec: it lives as long as the container
        subprocess.run(
            self.exec_argv(
                "desk",
                "dev",
                "bash",
                "-c",
                f"exec switchboard start --foreground --test-mode --test-trust-uds"
                f" --home {DESK_HOME} --port 0 >{WORK}/broker.out 2>&1",
                env={"SWITCHBOARD_TEST": "1"},
                detach=True,
            ),
            check=True,
            capture_output=True,
            timeout=60,
            env=DOCKER_ENV,
        )
        deadline = time.monotonic() + 30
        while True:
            r = self.desk(PY, ACTORS, "rpc", "--home", DESK_HOME, "sys.ping", check=False)
            if r.returncode == 0:
                break
            assert time.monotonic() < deadline, self.desk("cat", f"{WORK}/broker.out", check=False).stdout
            time.sleep(0.3)
        self.sb("create", ROOM)
        self.sb("cmd", ROOM, "/hops", "40")

    def pair(self) -> str:
        pub = self.pi("cat", "/home/pi/.sshd/host_ed25519.pub").stdout.split()
        self.host_key = f"{pub[0]} {pub[1]}"
        # the owner ssh'd there once and compared the fingerprint the container printed
        self.sh(
            "desk",
            f": > {WORK}/ssh_config; : > {WORK}/desk_ak;"
            f" printf '%s\\n' '[pi]:2222 {self.host_key}' > {WORK}/known_hosts",
        )
        r = self.sb(
            "remote",
            "add",
            NAME,
            "pi@pi",
            "--port",
            "2222",
            "--rooms",
            ROOM,
            "--ssh-config",
            f"{WORK}/ssh_config",
            "--known-hosts",
            f"{WORK}/known_hosts",
            "--authorized-keys",
            f"{WORK}/desk_ak",
            "--label",
            "desk",
        )
        m = TOKEN_RE.search(r.stdout)
        assert m, r.stdout + r.stderr
        self.desk_ip = self.ip("desk")
        self.pi_ip = self.ip("testpi")
        r = self.pi("switchboard", "remote", "accept", m.group(1), "--from", self.desk_ip, "--yes")
        self.accept_out = r.stdout
        r = self.sb("remote", "enable", NAME, timeout=60)
        assert r.stdout.startswith("link ok: satellite "), r.stdout + r.stderr
        return r.stdout

    def keys(self) -> None:
        """The bitstream keys (§27.8.4), by hand as the owner would: a push key whose line on
        the remote is ``rrsync -wo ~/fpga/in``, and (for the pull variant) a user-level sshd
        on the desktop with the remote's pull key limited to ``rrsync -ro ~/fpga/out``."""
        self.desk("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fpga-push", "-f", f"{WORK}/fpga_push")
        push = self.desk("cat", f"{WORK}/fpga_push.pub").stdout.strip()
        self.pi(
            "bash",
            "-c",
            "cat >> /home/pi/.ssh/authorized_keys",
            input=f'restrict,command="rrsync -wo /home/pi/fpga/in" {push}\n',
        )
        self.push_ssh = PI_SSH.format(key=f"{WORK}/fpga_push", kh=f"{WORK}/known_hosts", port=2222)
        # the desktop's sshd for pulls: this user's own, on 2223, never a system one
        self.sh(
            "desk",
            f"""
            mkdir -p {WORK}/dsshd && chmod 700 {WORK}/dsshd
            ssh-keygen -q -t ed25519 -N '' -C desk-host -f {WORK}/dsshd/host_ed25519
            : > {WORK}/dsshd/authorized_keys
            cat > {WORK}/dsshd/sshd_config <<EOF
Port 2223
ListenAddress 0.0.0.0
HostKey {WORK}/dsshd/host_ed25519
PidFile {WORK}/dsshd/sshd.pid
AuthorizedKeysFile {WORK}/dsshd/authorized_keys
AllowUsers dev
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
            /usr/sbin/sshd -t -f {WORK}/dsshd/sshd_config""",
        )
        subprocess.run(
            self.exec_argv(
                "desk", "dev", "/usr/sbin/sshd", "-D", "-e", "-f", f"{WORK}/dsshd/sshd_config", detach=True
            ),
            check=True,
            capture_output=True,
            timeout=60,
            env=DOCKER_ENV,
        )
        self.pi("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fpga-pull", "-f", "/home/pi/fpga_pull")
        pull = self.pi("cat", "/home/pi/fpga_pull.pub").stdout.strip()
        self.desk(
            "bash",
            "-c",
            f"cat >> {WORK}/dsshd/authorized_keys",
            input=f'restrict,from="{self.pi_ip}",command="rrsync -ro /home/dev/fpga/out" {pull}\n',
        )
        dkey = " ".join(self.desk("cat", f"{WORK}/dsshd/host_ed25519.pub").stdout.split()[:2])
        self.pi("bash", "-c", "cat > /home/pi/pull_known_hosts", input=f"[desk]:2223 {dkey}\n")
        self.pull_ssh = PI_SSH.format(key="/home/pi/fpga_pull", kh="/home/pi/pull_known_hosts", port=2223)

    def build(self, *, broken: bool = False, tag: str = "") -> dict[str, Any]:
        args = [PY, ACTORS, "build", "--out", "/home/dev/fpga/out/blinky/top.bit", "--tag", tag]
        if broken:
            args.append("--broken")
        return json.loads(self.desk(*args).stdout.strip().splitlines()[-1])

    def push(self, path: str) -> None:
        self.desk("rsync", "-t", "-e", self.push_ssh, path, "pi@pi:blinky/")

    def down(self) -> None:
        for a in self.actors:
            a.stop()
        self.compose("down", "-v", "--remove-orphans", "--timeout", "5", check=False, timeout=300.0)


@pytest.fixture(scope="module")
def th() -> Iterator[TwoHost]:
    why = _docker_missing()
    if why:
        pytest.skip(why)
    t = TwoHost()
    try:
        t.up()
        t.start_broker()
        t.enable_text = t.pair()
        t.keys()
        t.vivado = t.actor(
            "desk", "dev", "agent", "--home", DESK_HOME, "--name", "vivado", "--test-session", "vivado"
        )
        assert t.vivado.next("joined")["ok"]
        t.bench = t.actor(
            "testpi", "pi", "bench", "--home", PI_HOME, "--pull-src", "dev@desk", "--pull-ssh", t.pull_ssh
        )
        j = t.bench.next("joined")
        assert j["ok"] and j["tier"] == "claude:inbox", j
        yield t
    finally:
        t.down()


def handoff(th: TwoHost, bit: dict[str, Any], via: str = "push") -> dict[str, Any]:
    """vivado posts the artifact line; the bench wakes, works and answers."""
    text = f"@bench artifact: blinky/top.bit sha256:{bit['sha256']} size:{bit['size']} board:fake via:{via}"
    for _ in range(3):
        th.vivado.send({"op": "say", "room": ROOM, "text": text})
        said = th.vivado.next("say")
        if said.get("reason") != "rate_limited":
            break
        time.sleep(
            said["retry_after_s"] + 0.3
        )  # an agent may say() once per 10 s (§8.4), as a real one learns
    assert said["ok"] and said["posted_id"], said
    res = th.bench.next("result", timeout=90, pred=lambda e: e["facts"]["sha256"] == bit["sha256"])
    posted = [m for m in th.messages() if m["text"].startswith("result: ") and m["from"] == "bench"]
    assert posted and posted[-1]["text"] == res["text"] and posted[-1]["host"] == NAME
    return res


# --------------------------------------------------------------------------- tests
def test_fpga_handoff_push_rrsync_wo(th: TwoHost) -> None:
    assert "remote hooks ok" in th.enable_text and "rtt " in th.enable_text
    st = th.status()
    assert (
        st["state"] == "up" and st["transport"] == "ssh" and st["harden"] == "prctl" and not st["test_mode"]
    )
    who = th.member("bench")
    assert who is not None and who["host"] == NAME and who["tier"] == "claude:inbox"
    bit = th.build(tag="push")
    th.push(bit["path"])
    t0 = time.monotonic()
    res = handoff(th, bit)
    woke = next(e for e in th.bench.log if e.get("ev") == "woken")
    assert woke["via"] == "inbox"
    assert re.fullmatch(
        rf"result: {bit['sha256'][:12]} pull=skip verify=ok flash=ok uart=pass\(12/12\) t=\d+s", res["text"]
    ), res
    assert time.monotonic() - t0 < 60
    # openFPGALoader logged what it flashed; the push key can do nothing but write the drop dir
    log = th.pi("cat", "/home/pi/bench/flash.log").stdout
    assert bit["sha256"] in log
    r = th.desk("ssh", *th.push_ssh.split()[1:], "pi@pi", "cat /etc/passwd", check=False)
    assert "root:" not in r.stdout
    r = th.desk("rsync", "-e", th.push_ssh, "pi@pi:blinky/top.bit", "/tmp/sb-work/readback.bit", check=False)
    assert r.returncode != 0  # -wo: no reading back
    # both machines' doctors are clean
    r = th.sb("remote", "doctor", check=False)
    assert "FAIL" not in r.stdout, r.stdout
    r = th.pi("switchboard", "remote", "doctor", check=False)
    assert "FAIL" not in r.stdout, r.stdout


def test_fpga_handoff_pull_rrsync_ro(th: TwoHost) -> None:
    bit = th.build(tag="pull")
    res = handoff(th, bit, via="pull")
    assert re.fullmatch(
        rf"result: {bit['sha256'][:12]} pull=ok verify=ok flash=ok uart=pass\(12/12\) t=\d+s", res["text"]
    ), res
    # the pull key reads only the out dir, and writes nothing
    r = th.pi("rsync", "-e", th.pull_ssh, "dev@desk:../../../etc/passwd", "/tmp/x", check=False)
    assert r.returncode != 0
    th.pi("bash", "-c", "echo x > /tmp/intruder")
    r = th.pi("rsync", "-e", th.pull_ssh, "/tmp/intruder", "dev@desk:blinky/", check=False)
    assert r.returncode != 0
    assert th.desk("test", "-e", "/home/dev/fpga/out/blinky/intruder", check=False).returncode != 0


def test_broken_bitstream_fails_uart_then_fixed_passes(th: TwoHost) -> None:
    bad = th.build(broken=True, tag="broken")
    th.push(bad["path"])
    res = handoff(th, bad)
    assert "flash=ok uart=fail(8/12)" in res["text"] and "line 3: sent" in res["text"], res
    good = th.build(tag="fixed")
    th.push(good["path"])
    res = handoff(th, good)
    assert "uart=pass(12/12)" in res["text"], res
    # a hash that doesn't match what was pushed is never flashed
    wrong = dict(good, sha256="0" * 64)
    flashed = th.pi("cat", "/home/pi/bench/flash.log").stdout.count("\n")
    res = handoff(th, wrong)
    assert "verify=fail flash=skip" in res["text"], res
    assert th.pi("cat", "/home/pi/bench/flash.log").stdout.count("\n") == flashed


def test_partition_offline_then_delivery(th: TwoHost) -> None:
    net, cid = th.network(), th.container("testpi")
    subprocess.run(
        ["docker", "network", "disconnect", net, cid],
        check=True,
        capture_output=True,
        timeout=60,
        env=DOCKER_ENV,
    )
    try:
        st = th.wait_state("down", timeout=40)
        assert st["reason"] in ("no_pong", "keepalive", "timeout", "unreachable", "eof", "closed") or st[
            "reason"
        ].startswith("exit"), st
        deadline = time.monotonic() + 30
        while (m := th.member("bench")) is not None and m["status"] != "offline":
            assert time.monotonic() < deadline, m
            time.sleep(0.5)
        assert any("link down" in m["text"] for m in th.messages() if m["kind"] == "notice")
        # the owner says something while the bench is unreachable: it waits for it
        mid = th.rpc(
            "human.say", {"room": ROOM, "text": "@bench are you back? (sent while you were offline)"}
        )["id"]
        time.sleep(3)
        assert not [e for e in th.bench.log if e.get("ev") == "woken" and "sent while" in json.dumps(e)]
    finally:
        subprocess.run(
            ["docker", "network", "connect", "--alias", "pi", net, cid],
            check=True,
            capture_output=True,
            timeout=60,
            env=DOCKER_ENV,
        )
    th.wait_state("up", timeout=60)
    deadline = time.monotonic() + 60
    while (m := th.member("bench")) is None or m["status"] == "offline":
        assert time.monotonic() < deadline, m
        time.sleep(0.5)
    woke = th.bench.next("woken", timeout=60, pred=lambda e: "alice" in e.get("head", ""))
    assert woke["via"] == "inbox"
    row = th.rpc("room.history", {"room": ROOM, "limit": 50})["messages"]
    assert any(x["id"] == mid for x in row)
    # the same member, back with the same credentials: no second join, no leave line
    joins = [x for x in th.messages() if x["kind"] in ("join", "leave") and x["from"] == "bench"]
    assert [x["kind"] for x in joins] == ["join"], joins


@pytest.mark.parametrize("half", ["satellite", "sshd_session"])
def test_pi_user_cannot_open_satellite_or_sshd_session_fds(th: TwoHost, half: str) -> None:
    """Gate G2 (§27.15): another process of the remote's user can't reach the link's fds.

    ``satellite``: the satellite makes itself non-dumpable (``prctl``), so the remote's own
    user is refused its ``/proc/<pid>/fd`` and ``environ``. ``sshd_session``: sshd's session
    process above it holds the other ends of the link's pipes; under a system sshd it
    changed uid without exec, which leaves it non-dumpable too. This container's sshd is
    user-level (no root, no capabilities), so its session never changes uid and the kernel
    leaves it open: that half skips here (it was measured live against a system sshd,
    DESIGN.md §27.16) and runs wherever the listener is another user's."""
    probe = r"""
import json, os, sys
home = sys.argv[1]
sat = int(open(os.path.join(home, "run", "satellite.pid")).read().split()[0])
def ppid(p):
    return int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
def name(p):
    try:
        return open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return "?"
chain, p = [], sat
while p > 1 and len(chain) < 8:
    chain.append(p)
    p = ppid(p)
out = {
    "uid": os.getuid(),
    "satellite": sat,
    "chain": [[q, name(q), os.stat(f"/proc/{q}").st_uid] for q in chain],
}
res = {}
for q in chain:
    r = {}
    for what, fn in (("fd1", lambda: open(f"/proc/{q}/fd/1", "rb").close()),
                     ("fdlist", lambda: os.listdir(f"/proc/{q}/fd")),
                     ("environ", lambda: open(f"/proc/{q}/environ", "rb").read())):
        try:
            fn()
            r[what] = "open"
        except PermissionError:
            r[what] = "refused"
        except OSError as e:
            r[what] = f"error {e.errno}"
    res[q] = r
out["access"] = res
print(json.dumps(out))
"""
    r = th.pi("python3", "-c", probe, PI_HOME)
    out = json.loads(r.stdout)
    chain, sat, uid = out["chain"], out["satellite"], out["uid"]
    # the satellite is the pi user's own process, and non-dumpable: refused to that same user
    assert chain[0][2] == uid and chain[0][1].endswith(
        "-m switchboard satellite --home /home/pi/.switchboard --name fpga-pi"
    ), chain
    sessions = [(q, n, u) for q, n, u in chain[1:] if n.startswith("sshd")]
    assert sessions and any(
        n.startswith(("sshd: pi@notty", "sshd-session: pi@notty")) for _q, n, _u in sessions
    ), chain
    if half == "satellite":
        assert out["access"][str(sat)] == {"fd1": "refused", "fdlist": "refused", "environ": "refused"}, out
        return
    # a system sshd keeps a root-owned monitor above the user's session; a user-level one has none
    if all(u == uid for _q, _n, u in sessions):
        pytest.skip(
            "this sshd is user-level (the listener runs as the user): its session stays open to that user;"
            " the sshd half of G2 was measured live against a system sshd (DESIGN.md §27.16)"
        )
    for q, _n, _u in sessions:
        assert out["access"][str(q)] == {"fd1": "refused", "fdlist": "refused", "environ": "refused"}, out


def test_separate_pid_namespaces_no_desk_probe(th: TwoHost) -> None:
    """The remote's pids mean nothing on the desktop: the broker never probes them there,
    yet the remote member stays online through the liveness checks."""
    who = th.member("bench")
    assert who is not None and who["status"] != "offline"
    pid = next(e for e in th.bench.log if e.get("ev") == "joined")["pid"]
    rows = th.rpc("sys.status").get("remotes", [])
    assert rows and rows[0]["name"] == NAME
    # the same number is no process on the desktop, so a desktop probe of it (the bug this
    # catches) would find it dead and end the member within the ticks below
    r = th.desk("bash", "-c", f"test -e /proc/{pid} && tr '\\0' ' ' < /proc/{pid}/cmdline", check=False)
    if r.returncode == 0:
        assert "claude" not in r.stdout
        pytest.skip(
            f"pid {pid} also exists on the desk container ({r.stdout[:60]!r}): this run can't tell a desk"
            " probe of it from none"
        )
    time.sleep(6)  # three liveness ticks: a desktop probe of that pid would have ended it
    who = th.member("bench")
    assert who is not None and who["status"] != "offline" and who["host"] == NAME
    assert not [m for m in th.messages() if m["kind"] == "leave" and m["from"] == "bench"]
    # the satellite reported its own machine's facts: the desktop's broker has its own pid 1 world
    assert th.pi("bash", "-c", f"tr '\\0' ' ' < /proc/{pid}/cmdline").stdout.split()[1].endswith("/claude")
