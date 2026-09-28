"""Shared fixtures (DESIGN.md §12).

- ``clean_env`` (autouse): strips agent-harness variables from os.environ and
  points HOME at a temp dir, so nothing here can reach the build session's
  Claude inbox or the user's real harness config. It also pins the login name
  ``Config()`` defaults the human to (``TEST_HUMAN``), so every machine sees the same.
- ``child_env()`` builds every child process env from an allowlist (with
  ``LOGNAME=TEST_HUMAN``, for a child broker's default human name).
- ``tmp_home``: a short /tmp home with the ``.switchboard-test`` marker.
- ``broker``: an in-process broker (uvicorn in a thread, AllowAllHumans, test mode).
- ``web``: an httpx client on http://switchboard.localhost:<port>, signed in.
- ``SubprocBroker``: ``switchboard start --foreground`` in a child process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import switchboard.config
from switchboard.config import Config

_STRIP_PREFIXES = ("CLAUDE", "CODEX_", "CURSOR_", "DEVIN_", "CHISEL_", "AI_AGENT")
_REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
_REAL_LANG = os.environ.get("LANG", "en_US.UTF-8")
# The human's screen name in every test. Config() defaults it to the login name,
# so tests pin that login name (never the real one: it differs per machine).
TEST_HUMAN = "alice"


def _localhost_names_resolve() -> bool:
    try:
        infos = socket.getaddrinfo("switchboard.localhost", 80, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return any(ai[4][0] == "127.0.0.1" for ai in infos)


def _loopback_for_dot_localhost() -> None:
    """Resolve ``*.localhost`` to 127.0.0.1 in this test process.

    macOS (and Linux with systemd-resolved) already does; plain glibc in a
    container or CI runner sends it to DNS and fails. Browsers and curl map
    ``*.localhost`` to loopback themselves (RFC 6761 §6.3), and the product never
    resolves the name, so only the test clients (httpx, websockets) need this.
    """
    real = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower().rstrip(".").endswith(".localhost"):
            host = "127.0.0.1"
        return real(host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo


if not _localhost_names_resolve():
    _loopback_for_dot_localhost()


def sanitize_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """What ``clean_env`` does, for a fixture of a wider scope (it runs before the autouse one)."""
    for k in list(os.environ):
        if k.startswith(_STRIP_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("SWITCHBOARD_HOME", raising=False)
    monkeypatch.setenv("SWITCHBOARD_TEST", "1")
    # Only switchboard.config's view of the login name: getpass itself stays real.
    monkeypatch.setattr(switchboard.config, "getpass", types.SimpleNamespace(getuser=lambda: TEST_HUMAN))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    sanitize_env(monkeypatch, tmp_path_factory)
    yield


def child_env(home: str | os.PathLike | None = None, **extra: str) -> dict[str, str]:
    """Allowlisted env for child processes: PATH, HOME (temp), LANG, TMPDIR, SWITCHBOARD_TEST=1,
    and LOGNAME=TEST_HUMAN (getpass reads it first, so a child broker's human is TEST_HUMAN)."""
    env = {
        "PATH": _REAL_PATH,
        "HOME": str(home) if home else os.environ.get("HOME", "/tmp"),
        "LANG": _REAL_LANG,
        "TMPDIR": tempfile.gettempdir(),
        "SWITCHBOARD_TEST": "1",
        "LOGNAME": TEST_HUMAN,
    }
    # Under ``pytest --cov`` only: coverage's own subprocess setting ([tool.coverage.run]
    # patch), so the Python children (broker, ``switchboard mcp``, CLI runs) are measured too.
    if "COVERAGE_PROCESS_CONFIG" in os.environ:
        env["COVERAGE_PROCESS_CONFIG"] = os.environ["COVERAGE_PROCESS_CONFIG"]
    env.update(extra)
    return env


def make_tmp_home() -> Path:
    d = Path(tempfile.mkdtemp(prefix="yk-", dir="/tmp"))
    (d / ".switchboard-test").touch()
    return d


@pytest.fixture
def tmp_home() -> Iterator[Path]:
    d = make_tmp_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeClock:
    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cfg_fast() -> Config:
    return Config(human_name=TEST_HUMAN).with_delivery(quiet_s=0.0, max_hold_s=0.0)


# --------------------------------------------------------------- in-process
class InProcBroker:
    """The real app on a real TCP port and UDS, in a background thread."""

    def __init__(
        self,
        home: Path,
        cfg: Config | None = None,
        *,
        policy: Any = None,
        test_mode: bool = True,
        clock: Any = None,
    ):
        from switchboard.broker.peer import AllowAllHumans
        from switchboard.paths import Paths

        self.home = Path(home)
        self.paths = Paths.from_home(home)
        # Never let a test broker look at the user's real Codex daemon or Claude registry.
        base = cfg or Config(human_name=TEST_HUMAN)
        self.cfg = base.replace(
            codex=dataclasses.replace(base.codex, control_socket=str(self.home / "cx.sock"), bin="/usr/bin/false"),
            claude=dataclasses.replace(base.claude, sessions_dir=str(self.home / "claude-sessions")),
        )
        self.policy = policy if policy is not None else AllowAllHumans()
        self.test_mode = test_mode
        self.clock = clock
        self.port = 0
        self.app: Any = None
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None

    def start(self) -> "InProcBroker":
        import uvicorn

        from switchboard.broker.app import create_app
        from switchboard.broker.daemon import loopback_listener

        tcp = loopback_listener(self.port)  # the broker's own socket; a restart keeps its port (and Origin)
        self.port = tcp.getsockname()[1]
        self.tcp = tcp
        self.app = create_app(
            self.paths, self.cfg, self.policy, self.test_mode, port=self.port, clock=self.clock
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                access_log=False,
                log_level="warning",
                log_config=None,
                lifespan="on",
                ws_ping_interval=None,
            )
        )
        self.app.state.broker.shutdown_cb = lambda: setattr(self.server, "should_exit", True)

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.server.serve(sockets=[tcp]))
            except BaseException as e:  # pragma: no cover
                self.error = e
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=run, name="yk-broker", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"in-process broker failed to start: {self.error!r}")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(10)
        try:
            self.tcp.close()
        except Exception:
            pass

    def restart(self) -> "InProcBroker":
        self.stop()
        return self.start()

    @property
    def state(self) -> Any:
        return self.app.state.broker

    @property
    def base(self) -> str:
        return f"http://switchboard.localhost:{self.port}"

    @property
    def origin(self) -> str:
        return self.base

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        from switchboard.mcp.client import call_sync

        return call_sync(self.paths.sock, method, params or {}, timeout)

    def on_loop(self, fn: Any, *args: Any) -> Any:
        """Run a plain callable on the broker's event loop thread and return its result."""
        assert self.loop is not None

        async def wrapper() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(wrapper(), self.loop).result(10)

    def login_url(self) -> str:
        return self.call("human.login_link")["url"]

    def web_client(self) -> httpx.Client:
        c = httpx.Client(base_url=self.base, timeout=10.0, follow_redirects=False)
        r = c.get(self.login_url().removeprefix(self.base))
        assert r.status_code == 303, r.text
        return c

    def write_headers(self) -> dict[str, str]:
        return {"Origin": self.origin, "X-Switchboard": "1", "Content-Type": "application/json"}


@pytest.fixture
def broker(tmp_home: Path) -> Iterator[InProcBroker]:
    b = InProcBroker(tmp_home).start()
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture
def web(broker: InProcBroker) -> Iterator[httpx.Client]:
    c = broker.web_client()
    try:
        yield c
    finally:
        c.close()


def cookie_of(client: httpx.Client) -> str:
    for c in client.cookies.jar:
        if c.name == "switchboard_session":
            return c.value
    raise AssertionError("no switchboard_session cookie")


def ws_connect(broker: InProcBroker, cookie: str | None, *, origin: str | None = "default", host: str | None = None) -> Any:
    """Open the UI WebSocket. ``host`` replaces switchboard.localhost:<port> in the URL (and so the Host header)."""
    from websockets.sync.client import connect

    headers = {}
    if cookie is not None:
        headers["Cookie"] = f"switchboard_session={cookie}"
    o = broker.origin if origin == "default" else origin
    return connect(
        f"ws://{host or f'switchboard.localhost:{broker.port}'}/ws",
        origin=o,
        additional_headers=headers,
        open_timeout=5,
        close_timeout=2,
        legacy=True,  # connect now; tests close explicitly
    )


# --------------------------------------------------------------- subprocess
class SubprocBroker:
    """``python -m switchboard start --foreground --test-mode`` in a child process."""

    def __init__(self, home: Path, *, trust: bool = True, test_mode: bool = True):
        from switchboard.paths import Paths

        self.home = Path(home)
        self.paths = Paths.from_home(home)
        self.trust = trust
        self.test_mode = test_mode
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.pid = 0
        self.env = child_env()

    def start(self) -> "SubprocBroker":
        from switchboard.mcp.client import ping

        cmd = [sys.executable, "-m", "switchboard", "start", "--foreground", "--home", str(self.home), "--port", "0"]
        if self.test_mode:
            cmd.append("--test-mode")
            if self.trust:
                cmd.append("--test-trust-uds")
        self.out = open(self.home / "subproc.out", "ab")
        self.proc = subprocess.Popen(
            cmd, env=self.env, stdin=subprocess.DEVNULL, stdout=self.out, stderr=self.out
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = ping(self.paths.sock, timeout=1.0)
            if info:
                self.port = info["port"]
                self.pid = info["pid"]
                return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        self.kill()
        raise RuntimeError("subprocess broker did not start: " + (self.home / "subproc.out").read_text())

    def cli(self, *args: str, input: str | None = None, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        # A new session: the child never has a controlling terminal, so TTY-gated
        # verbs (``login``) behave the same in a real terminal, under CI and under an agent.
        return subprocess.run(
            [sys.executable, "-m", "switchboard", "--home", str(self.home), *args],
            env=self.env,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
        )

    def cli_popen(self, *args: str) -> subprocess.Popen[str]:
        # SIGINT back to the default, so Ctrl-C (SIGINT) works in the child even
        # when pytest runs as a background job that ignores it.
        return subprocess.Popen(
            [sys.executable, "-m", "switchboard", "--home", str(self.home), *args],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_default_sigint,
        )

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        try:
            self.out.close()
        except Exception:
            pass

    @property
    def base(self) -> str:
        return f"http://switchboard.localhost:{self.port}"


@pytest.fixture
def subproc_broker(tmp_home: Path) -> Iterator[SubprocBroker]:
    b = SubprocBroker(tmp_home).start()
    try:
        yield b
    finally:
        b.kill()


def human_cli_denied_here() -> bool:
    """True when the production peer check would refuse ``human_cli`` to this
    pytest process (and so to the CLI children it starts): it runs under an
    agent harness such as Claude Code or an ssh login, or its chain can't be
    fully verified. Uses the exact policy the broker uses, so the two never disagree."""
    return human_cli_denial_word() is not None


def human_cli_denial_word() -> str | None:
    """None when the production peer check would grant ``human_cli`` to this pytest
    process (and so to the CLI children it starts); else a word the refusal
    message contains: "ssh" when pytest runs under an ssh login or relay
    (DESIGN.md §27.5.7), "agent" when it runs under an agent harness such as
    Claude Code or its chain can't be fully verified."""
    from switchboard.broker import proc
    from switchboard.broker.peer import Peer, ProcessPeerPolicy

    me = proc.info(os.getpid())
    peer = Peer(pid=os.getpid(), uid=os.getuid(), start=me.start if me else None)
    pol = ProcessPeerPolicy()
    if pol.human_cli_allowed(peer):
        return None
    return "ssh" if pol.refusal(peer) else "agent"


def has_controlling_tty() -> bool:
    """True when this pytest process has a controlling terminal (run from a real shell)."""
    from switchboard.broker import proc

    return proc.tty(os.getpid()) is not None


def _default_sigint() -> None:  # pragma: no cover - runs in the child
    signal.signal(signal.SIGINT, signal.SIG_DFL)
