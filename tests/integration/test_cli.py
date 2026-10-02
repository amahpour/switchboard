"""The CLI against a real broker subprocess (DESIGN.md §3, §12.2)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from conftest import SubprocBroker, child_env, human_cli_denial_word
from switchboard import __version__, build_info
from switchboard.broker import proc
from switchboard.mcp.client import ping
from switchboard.paths import Paths

RELAY = Path(__file__).resolve().parents[1] / "fakes" / "fake_relay.py"


def test_status_names_the_running_brokers_commit(subproc_broker: SubprocBroker) -> None:
    """Status gets the running broker's revision over RPC, not the CLI process's guess."""
    want = build_info.commit()
    assert want is not None
    data = json.loads(subproc_broker.cli("status", "--json").stdout)
    assert data["commit"] == want
    text = subproc_broker.cli("status").stdout
    assert text.startswith(f"switchboard {__version__} ({want[:7]}) running:")


def test_say_tail_who_cmd_stop(subproc_broker: SubprocBroker) -> None:
    b = subproc_broker
    r = b.cli("create", "#build")
    assert r.returncode == 0 and "created #build" in r.stdout, r.stderr
    assert b.cli("say", "#build", "hello", "from", "the", "cli").returncode == 0
    assert b.cli("say", "build", "/pause").returncode == 0  # literal, not a command
    r = b.cli("say", "#build", "-", input="from stdin\nsecond line\n")
    assert r.returncode == 0, r.stderr

    r = b.cli("tail", "#build", "--no-follow")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].endswith("-!- #build created by alice")
    assert lines[1].endswith("<alice> (via cli) hello from the cli")
    assert lines[2].endswith("<alice> (via cli) /pause")
    assert lines[3].endswith("<alice> (via cli) from stdin") and lines[4] == " " * 11 + "second line"
    assert lines[1].startswith("[") and lines[1][9] == "]"

    r = b.cli("tail", "#build", "--no-follow", "--json")
    msgs = [json.loads(x) for x in r.stdout.splitlines()]
    assert [m["text"] for m in msgs][1:3] == ["hello from the cli", "/pause"]
    r = b.cli("tail", "#build", "--no-follow", "--after", str(msgs[1]["id"]), "--json")
    assert [json.loads(x)["text"] for x in r.stdout.splitlines()][0] == "/pause"

    r = b.cli("who", "#build")
    assert r.returncode == 0 and "alice (you, human) + 0 agent(s)" in r.stdout
    assert json.loads(b.cli("who", "#build", "--json").stdout)["members"] == []

    r = b.cli("cmd", "#build", "/pause")
    assert r.returncode == 0 and "paused" in r.stdout
    r = b.cli("cmd", "#build", "budget", "7")  # leading slash optional
    assert r.returncode == 0 and "budget set to 7" in r.stdout
    r = b.cli("cmd", "#build", "/resume")  # test trust grants 'human'
    assert r.returncode == 0 and "resumed" in r.stdout
    r = b.cli("cmd", "#build", "/kick", "nobody")
    assert r.returncode == 1 and "no such member" in r.stderr
    r = b.cli("cmd", "#build", "/bogus")
    assert r.returncode == 1 and "unknown command" in r.stderr

    r = b.cli("status")
    assert r.returncode == 0 and "TEST MODE" in r.stdout and "#build: active" in r.stdout
    st = json.loads(b.cli("status", "--json").stdout)
    assert st["pid"] == b.pid and st["test_mode"] is True and st["hooks"].startswith("ok")
    assert json.loads(b.cli("rooms", "--json").stdout)[0]["name"] == "#build"
    r = b.cli("login")
    assert r.returncode == 0 and f"http://switchboard.localhost:{b.port}/login?t=" in r.stdout

    r = b.cli("stop")
    assert r.returncode == 0 and "stopped" in r.stdout, r.stderr
    assert b.proc is not None
    b.proc.wait(10)
    assert ping(b.paths.sock) is None
    assert not b.paths.sock.exists() and not b.paths.pidfile.exists()
    r = b.cli("say", "#build", "anyone?")
    assert r.returncode == 3 and "not running" in r.stderr


def test_tail_follows_live_messages(subproc_broker: SubprocBroker) -> None:
    b = subproc_broker
    b.cli("create", "#build")
    tail = b.cli_popen("tail", "#build", "-n", "1")
    try:
        assert tail.stdout is not None
        # the backlog line proves the subscription exists (both happen in one RPC)
        assert tail.stdout.readline().rstrip("\n").endswith("-!- #build created by alice")
        assert b.cli("say", "#build", "live one").returncode == 0
        assert b.cli("say", "#build", "live two").returncode == 0
        got = [tail.stdout.readline().rstrip("\n") for _ in range(2)]
        assert got[0].endswith("<alice> (via cli) live one") and got[1].endswith("<alice> (via cli) live two")
    finally:
        tail.send_signal(signal.SIGINT)
        try:
            tail.wait(5)
        except subprocess.TimeoutExpired:
            tail.kill()
            tail.wait(5)
            raise
    assert tail.returncode in (0, 130), tail.stderr.read() if tail.stderr else ""


def test_without_test_trust_raising_commands_are_forbidden(tmp_home: Path) -> None:
    """No --test-trust-uds: the real peer check applies. /resume needs the web session.

    The room exists (created through a web session), so every refusal below is
    the role check itself, not a missing room. The result is the same whether
    pytest runs in a real terminal, under CI, or inside an agent harness.
    """
    import httpx

    b = SubprocBroker(tmp_home, trust=False).start()
    try:
        tok = b.paths.test_login_token.read_text().strip()
        with httpx.Client(base_url=b.base, timeout=10) as web:
            assert web.get(f"/login?t={tok}").status_code == 303
            h = {"Origin": b.base, "X-Switchboard": "1"}
            assert web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 200
            assert web.post("/api/rooms/build/command", json={"text": "/pause"}, headers=h).status_code == 200
        word = human_cli_denial_word()
        denied = word is not None

        # Raising verbs: refused either way. Outside an agent the reason is the
        # missing web session; under an agent (or an ssh login), human_cli itself is refused first.
        why = word or "web session"
        for args in (("cmd", "#build", "/resume"), ("cmd", "#build", "/budget", "500")):
            r = b.cli(*args)
            assert r.returncode == 1 and "forbidden" in r.stderr and why in r.stderr, r.stderr
        r = b.cli("create", "#other")
        assert r.returncode == 1 and "forbidden" in r.stderr and "web session" in r.stderr, r.stderr
        r = b.cli("login")  # the CLI child has no controlling terminal
        assert r.returncode == 1 and "forbidden" in r.stderr, r.stderr
        if denied:
            # this test process runs under an agent harness (or an ssh login): human_cli is refused too
            r = b.cli("cmd", "#build", "/budget", "1")
            assert r.returncode == 1 and "forbidden" in r.stderr and why in r.stderr
            r = b.cli("say", "#build", "hi")
            assert r.returncode == 1 and "forbidden" in r.stderr and why in r.stderr
        else:
            r = b.cli("cmd", "#build", "/budget", "1")  # lowering: allowed at human_cli
            assert r.returncode == 0 and "budget set to 1" in r.stdout, r.stderr
            r = b.cli("say", "#build", "hi")
            assert r.returncode == 0, r.stderr
        assert b.cli("status").returncode == 0  # anon
    finally:
        b.kill()


def test_cli_through_a_relay_named_ssh_is_forbidden(tmp_home: Path) -> None:
    """DESIGN.md §27.5.7: through a hand-made forward of broker.sock (``ssh -R``,
    ``ssh -L``, socat) the broker's kernel peer is the relay, so the relay rule
    refuses every human verb, whoever sits behind it. Production peer policy
    (no --test-trust-uds); the relay is a stdlib script copied to a path ending in /ssh."""
    import httpx

    b = SubprocBroker(tmp_home, trust=False).start()
    d = Path(tempfile.mkdtemp(prefix="yk-rl-", dir="/tmp"))
    relay = None
    try:
        tok = b.paths.test_login_token.read_text().strip()
        with httpx.Client(base_url=b.base, timeout=10) as web:
            assert web.get(f"/login?t={tok}").status_code == 303
            h = {"Origin": b.base, "X-Switchboard": "1"}
            assert web.post("/api/rooms", json={"name": "#build"}, headers=h).status_code == 200
        exe = d / "ssh"
        shutil.copy(RELAY, exe)
        # the CLI side: a home whose socket path is the relay's listening socket
        far = d / "far"
        (far / "run").mkdir(parents=True, mode=0o700)
        os.chmod(far, 0o700)
        listen = Paths.from_home(far).sock
        relay = subprocess.Popen([sys.executable, str(exe), str(listen), str(b.paths.sock)], env=child_env(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        assert relay.stdout is not None and relay.stdout.readline().strip() == "ready"

        def via_relay(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-m", "switchboard", "--home", str(far), *args], env=b.env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
                                  start_new_session=True)

        r = via_relay("status")  # anon: the relay itself works
        assert r.returncode == 0 and "running" in r.stdout, r.stderr
        for args in (("say", "#build", "through the relay"), ("cmd", "#build", "/budget", "1"),
                     ("cmd", "#build", "/pause"), ("login",)):
            r = via_relay(*args)
            assert r.returncode == 1 and "forbidden" in r.stderr, (args, r.stderr)
            # the relay reason, not the agent one: this holds under an agent harness too
            assert "arrived through ssh" in r.stderr and "(the process on the broker socket is ssh)" in r.stderr, r.stderr
            assert "allow_ssh_cli" not in r.stderr, r.stderr  # no setting relaxes it
        r = b.cli("tail", "#build", "--no-follow")
        assert "through the relay" not in r.stdout and "paused" not in r.stdout, r.stdout
        # straight to broker.sock the same verb depends only on who runs pytest (the control case)
        word = human_cli_denial_word()
        r = b.cli("say", "#build", "direct")
        if word is None:
            assert r.returncode == 0, r.stderr
        else:
            assert r.returncode == 1 and word in r.stderr and "the process on the broker socket" not in r.stderr
    finally:
        if relay is not None:
            relay.kill()
            relay.wait(5)
            if relay.stdout is not None:
                relay.stdout.close()
        b.kill()
        shutil.rmtree(d, ignore_errors=True)


def test_daemonized_start_and_stop(tmp_home: Path) -> None:
    env = child_env()
    base = [sys.executable, "-m", "switchboard", "--home", str(tmp_home)]
    r = subprocess.run(base + ["start", "--port", "0", "--test-mode", "--test-trust-uds"],
                       env=env, capture_output=True, text=True, timeout=30)
    pid = None
    try:
        assert r.returncode == 0, r.stderr
        assert "switchboard is running" in r.stdout and "TEST MODE" in r.stdout
        assert "/login?t=" in r.stdout
        from switchboard.paths import Paths

        p = Paths.from_home(tmp_home)
        info = ping(p.sock)
        assert info is not None
        pid = info["pid"]
        pf = p.pidfile.read_text().split()
        assert int(pf[0]) == pid
        me = proc.info(pid)
        assert me is not None and me.ppid != os.getpid()
        if sys.platform == "darwin":
            assert me.ppid == 1  # daemonized: reparented to launchd
        # starting again reports the running broker
        r2 = subprocess.run(base + ["start", "--test-mode"], env=env, capture_output=True, text=True, timeout=30)
        assert r2.returncode == 0 and "already running" in r2.stdout
        r3 = subprocess.run(base + ["stop"], env=env, capture_output=True, text=True, timeout=30)
        assert r3.returncode == 0 and "stopped" in r3.stdout
        deadline = time.time() + 5
        while time.time() < deadline and proc.alive(pid, me.start):
            time.sleep(0.05)
        assert not proc.alive(pid, me.start)
        pid = None
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGTERM)


def test_test_mode_guards(tmp_path: Path, tmp_home: Path) -> None:
    base = [sys.executable, "-m", "switchboard"]
    # no SWITCHBOARD_TEST
    env = child_env()
    env.pop("SWITCHBOARD_TEST")
    r = subprocess.run(base + ["--home", str(tmp_home), "start", "--foreground", "--test-mode"],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "SWITCHBOARD_TEST=1" in r.stderr
    # no marker file
    unmarked = Path("/tmp") / f"yk-nomark-{os.getpid()}"
    unmarked.mkdir(mode=0o700)
    try:
        r = subprocess.run(base + ["--home", str(unmarked), "start", "--foreground", "--test-mode"],
                           env=child_env(), capture_output=True, text=True, timeout=30)
        assert r.returncode == 2 and "marker" in r.stderr
    finally:
        unmarked.rmdir()
    # --home missing (SWITCHBOARD_HOME alone is not enough)
    r = subprocess.run(base + ["start", "--foreground", "--test-mode"],
                       env=child_env(SWITCHBOARD_HOME=str(tmp_home)), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "--home" in r.stderr
    # trust without test mode
    r = subprocess.run(base + ["--home", str(tmp_home), "start", "--foreground", "--test-trust-uds"],
                       env=child_env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_second_foreground_broker_is_refused(subproc_broker: SubprocBroker) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "switchboard", "--home", str(subproc_broker.home), "start", "--foreground",
         "--port", "0", "--test-mode"],
        env=child_env(), capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 1 and "already runs" in r.stderr
    assert ping(subproc_broker.paths.sock) is not None


def test_cli_without_broker(tmp_home: Path) -> None:
    r = subprocess.run([sys.executable, "-m", "switchboard", "--home", str(tmp_home), "who", "#build"],
                       env=child_env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 3 and "not running" in r.stderr
    r = subprocess.run([sys.executable, "-m", "switchboard", "--home", str(tmp_home), "stop"],
                       env=child_env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "not running" in r.stdout
    r = subprocess.run([sys.executable, "-m", "switchboard"], env=child_env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "COMMAND" in r.stdout


@pytest.mark.parametrize("args", [["say", "#build"], ["tail"], ["cmd", "#build"]])
def test_cli_usage_errors(tmp_home: Path, args: list[str]) -> None:
    r = subprocess.run([sys.executable, "-m", "switchboard", "--home", str(tmp_home), *args],
                       env=child_env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_tail_after_pages_past_one_batch(subproc_broker: SubprocBroker) -> None:
    b = subproc_broker
    b.cli("create", "#build")
    from switchboard.mcp.client import call_sync

    for i in range(1105):
        call_sync(b.paths.sock, "human.say", {"room": "#build", "text": f"x{i}"})
    r = b.cli("tail", "#build", "--after", "0", "--no-follow", "--json")
    texts = [json.loads(x)["text"] for x in r.stdout.splitlines()]
    assert texts[0] == "#build created by alice" and texts[-1] == "x1104" and len(texts) == 1106
