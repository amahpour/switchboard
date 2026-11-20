"""Shared fixtures (DESIGN.md §12).

- ``restore_umask`` (autouse): every test starts and ends with the run's umask.
- ``fail_on_unraisable_exception`` (autouse): ``gc.collect(1)`` after every test, so a leaked
  object's ``__del__`` fails the test that leaked it (pyproject.toml's ``filterwarnings`` turns
  the ``PytestUnraisableExceptionWarning`` into an error), not a later test the collector happens
  to run in (#277).
- ``clean_env`` (autouse): strips agent-harness variables from os.environ and
  points HOME at a temp dir, so nothing here can reach the build session's
  Claude inbox or the user's real harness config. It also pins the login name
  ``Config()`` defaults the human to (``TEST_HUMAN``), so every machine sees the same.
- ``child_env()`` builds every child process env from an allowlist (with
  ``LOGNAME=TEST_HUMAN``, for a child broker's default human name).
- ``own_color_env()`` clears ``NO_COLOR``, ``FORCE_COLOR`` and ``CLICOLOR_FORCE`` and
  pins ``TERM`` for the colour tests, so a shell that disables colour does not (#650).
- ``tmp_home``: a short /tmp home with the ``.switchboard-test`` marker.
- ``no_homes_left_behind`` (session, autouse): fails the run, naming them, if any home
  ``make_tmp_home()`` made in this process is still there once every fixture is torn down (#919).
- ``sweep_stale_homes()``, run once per session from the controller (``pytest_sessionstart``):
  removes the marked homes a killed run left in /tmp that it can show nobody uses; none without /proc (#919).
- ``broker``: an in-process broker (uvicorn in a thread, AllowAllHumans, test mode).
- ``web``: an httpx client on http://switchboard.localhost:<port>, signed in.
- ``SubprocBroker``: ``switchboard start --foreground`` in a child process.
"""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

import switchboard.config
from switchboard.config import Config
from switchboard.paths import TEST_MARKER

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
    # the CLI's colour switches (issue #28): output is plain unless a test asks for colour.
    # TERM stays as the shell left it. The colour tests pin it themselves (own_color_env, #650):
    # pinning it here would colour every test that pretends stdout is a terminal.
    for k in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(k, raising=False)
    # `start`'s container settings (issue #34): a broker listens on loopback unless a test says so
    for k in ("SWITCHBOARD_PORT", "SWITCHBOARD_LISTEN", "SWITCHBOARD_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    # the meeting bot's address (#591), how many calls it follows (#607) and the model it transcribes with
    # (#617): a machine that has any set must not give a test a meeting server, or a limit or model the test
    # did not choose
    for k in (
        "SWITCHBOARD_JOINLY_URL",
        "SWITCHBOARD_MEETING_MAX_CALLS",
        "SWITCHBOARD_MEETING_STT_MODEL",
        "SWITCHBOARD_MEETING_TRANSCRIBER",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SWITCHBOARD_TEST", "1")
    # Only switchboard.config's view of the login name: getpass itself stays real.
    monkeypatch.setattr(switchboard.config, "getpass", types.SimpleNamespace(getuser=lambda: TEST_HUMAN))


# Each test phase's report ("setup", "call", "teardown"), kept on the item, so a fixture's
# teardown can tell whether its test failed (tests/e2e saves a trace and a screenshot then).
PHASE_REPORTS = pytest.StashKey[dict[str, pytest.TestReport]]()


def _e2e_parallel(config: pytest.Config) -> bool:
    # Only the exact opt-in expression means every selected test is an e2e test. A broader
    # expression may also select unit tests, which need ordinary test-level load balancing.
    return config.getoption("markexpr") == "e2e" and bool(config.getoption("numprocesses", None))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("markexpr") != "e2e" or not hasattr(config, "workerinput"):
        return
    # The controller supplies LoadGroupScheduling. A worker parses its own -n auto command
    # and xdist resets --dist to "no" there; enable xdist's group suffix before its collection
    # hook reads the marks. This hook's tryfirst ordering is required for that handoff.
    config.option.loadgroup = True
    for item in items:
        if item.get_closest_marker("e2e") is None:
            continue
        name = item.path.name
        group = f"{name}-{zlib.crc32(item.name.encode('utf-8')) % 3}" if name == "test_web_ui.py" else name
        item.add_marker(pytest.mark.xdist_group(group))


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_make_scheduler(config: pytest.Config, log: Any) -> Any:
    """Share the large e2e module across three worlds; keep small modules together.

    The default unit/integration suite keeps xdist's ordinary test-level load balancing.
    """
    if _e2e_parallel(config):
        from xdist.scheduler.loadgroup import LoadGroupScheduling

        return LoadGroupScheduling(config, log)
    return None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[pytest.TestReport]:
    rep = yield
    item.stash.setdefault(PHASE_REPORTS, {})[rep.when] = rep
    return rep


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    sanitize_env(monkeypatch, tmp_path_factory)
    yield


# The umask this run started with.
_START_UMASK = os.umask(0o022)
os.umask(_START_UMASK)


@pytest.fixture(autouse=True)
def restore_umask() -> Iterator[None]:
    """Every test starts with the run's umask and leaves it so. ``cli.main``, the daemon and the
    satellite set 0o077 for their own process; a test that ran one in-process used to leave it
    set for every later test in its worker, so a test that made a directory without a mode
    passed or failed by the order the tests ran in."""
    os.umask(_START_UMASK)
    yield
    os.umask(_START_UMASK)


@pytest.fixture(autouse=True)
def fail_on_unraisable_exception() -> Iterator[None]:
    """Collect garbage after every test (issue #277, found fixing #276).

    An object whose ``__del__`` raises (a leaked asyncio transport, say) reports a
    ``PytestUnraisableExceptionWarning`` the moment the garbage collector reclaims it, not when it
    leaked — and ``filterwarnings`` (pyproject.toml) now turns that warning into a test failure.
    Autouse and undeclared, so this fixture is set up before (and torn down after) every other
    function-scoped fixture a test requests, including ``broker``'s own teardown; the explicit
    collect here, rather than leaving it to whichever later test's allocations next cross the
    collector's threshold, attributes the failure to the test that actually leaked the object.

    ``gc.collect(1)``, not a full ``gc.collect()``: generation 2 holds every long-lived object
    the process has ever promoted (every import's classes and module globals), and a full
    collection re-scans all of it on every single test, measured at ~0.25s/test (about 13
    minutes over this suite) against ~1ms/test for generations 0 and 1 alone. An object this
    test leaked was allocated during it and has not yet survived a collection, so it is still in
    generation 0 (or, if the test's own code already triggered an automatic collection, freshly
    promoted to 1); either is well within what ``gc.collect(1)`` reaches."""
    yield
    gc.collect(1)


def own_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The colour tests decide colour. The shell that launched pytest does not (#650).

    ``colors.enabled`` still honours ``NO_COLOR``, ``FORCE_COLOR``, ``CLICOLOR_FORCE`` and
    ``TERM=dumb``. Some agent CLIs export ``NO_COLOR=1``, ``FORCE_COLOR=0`` and ``TERM=dumb``,
    and a test that read that shell saw a terminal as plain. Clearing the three switches and
    pinning ``TERM`` is the same idea as ``child_env``: the test owns its environment.
    """
    for name in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm")


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


def node_for_tests() -> tuple[str | None, pytest.MarkDecorator]:
    """The ``node`` binary for the web UI's node tests (tests/unit/test_web_app_behavior.py,
    test_web_markdown.py) and the ``pytestmark`` for their modules.

    Without node those tests are skipped, so a machine without it still runs the rest. CI sets
    ``SWITCHBOARD_REQUIRE_NODE=1`` (and installs node): there a missing node fails the
    collection instead, so the tests can never be skipped by accident."""
    node = shutil.which("node")
    if node is None and os.environ.get("SWITCHBOARD_REQUIRE_NODE") == "1":
        pytest.fail(
            "node is not on PATH, and SWITCHBOARD_REQUIRE_NODE=1 says the web UI's node tests "
            "must run: install node (22 in CI) or unset the variable",
            pytrace=False,
        )
    return node, pytest.mark.skipif(
        node is None, reason="node is not installed (SWITCHBOARD_REQUIRE_NODE=1 makes this an error)"
    )


TMP_HOME_ROOT = Path("/tmp")
TMP_HOME_PREFIX = "yk-"

# A home is stale once it is this old. The longest run that makes homes (the whole suite, an eval of
# 25 trials, a live run of every harness) takes well under an hour and CI's longest job is capped at
# 30 minutes, so 6 h keeps every home of a run going on alongside, a slow machine's included, and
# still clears a killed run's homes the same day (#919).
STALE_HOME_AGE_S = 6 * 3600

# Where the sweep reads the running processes (Linux's procfs); a test points it
# elsewhere. With no procfs the sweep can't show a home unused, so it removes nothing (#919).
PROC_ROOT = Path("/proc")


# Every home make_tmp_home() made in this process (an xdist worker is a process of its own), for
# no_homes_left_behind: only these are ever counted, so a home another run made never is (#919). Each
# keeps the test that was running when it was made (pytest's PYTEST_CURRENT_TEST), so a leak names it.
MADE_HOMES: dict[Path, str] = {}


def make_tmp_home() -> Path:
    # short on purpose: the broker's socket inside it must fit sun_path (paths.SOCK_MAX_BYTES)
    d = Path(tempfile.mkdtemp(prefix=TMP_HOME_PREFIX, dir=TMP_HOME_ROOT))
    # The marker names the process that made the home, so the sweep keeps a home whose run is still
    # going however long it has been quiet (#919); the broker only checks that the marker exists.
    (d / TEST_MARKER).write_text(f"{os.getpid()}\n")
    MADE_HOMES[d] = os.environ.get("PYTEST_CURRENT_TEST", "outside a test").rsplit(" (", 1)[0]
    return d


def homes_left_behind(made: dict[Path, str]) -> dict[Path, str]:
    """The homes of ``made`` that are still there, each with the test that made it (#919). A symlink
    or file in a home's place counts too: something the test made is still in /tmp."""
    return {d: test for d, test in made.items() if os.path.lexists(d)}


@pytest.fixture(scope="session", autouse=True)
def no_homes_left_behind() -> Iterator[None]:
    """Fail the run if a test or fixture left a home it made in /tmp (#919).

    Session-scoped and autouse in the root conftest, so it is set up before every other fixture and
    torn down after all of them, module- and session-scoped worlds included: a home still present
    then is one nothing will remove. An error in this teardown fails the run, under xdist too, and
    names each home. Only this process's homes are checked (``MADE_HOMES``)."""
    yield
    left = homes_left_behind(MADE_HOMES)
    if left:
        names = "\n  ".join(f"{d}  made by {test}" for d, test in left.items())
        pytest.fail(
            f"{len(left)} temp home(s) made by make_tmp_home() were not removed by the test or "
            f"fixture that made them (#919):\n  {names}",
            pytrace=False,
        )


def sweep_stale_homes(
    root: Path = TMP_HOME_ROOT,
    *,
    now: float | None = None,
    max_age_s: float = STALE_HOME_AGE_S,
    proc: Path | None = None,
) -> list[Path]:
    """Remove the test homes a killed run left behind, and return the ones removed (#919).

    ``tmp_home`` and the e2e worlds remove their home in a ``finally``, which a SIGKILL, the OOM
    killer, a timeout or a cancelled run skips. The sweep removes a home only when it can show that
    nobody uses it; anything it can't determine keeps the home. All of these must hold:

    1. it is a directory directly under ``root`` named ``yk-*``, not a symlink, owned by this user,
       carrying the ``.switchboard-test`` marker as a regular file, and last changed ``max_age_s`` ago
       or more (a young one may belong to a run going on alongside);
    2. the marker holds a valid pid (a positive decimal) and that process is gone
       (``ProcessLookupError``). An empty marker (an older checkout's), a malformed one, or any
       other answer from ``os.kill`` keeps it;
    3. no running process has anything inside it open: its cwd, its root or any open file
       (``/proc/<pid>/cwd``, ``root`` and every ``fd/*``), or a Unix socket bound inside it
       (``/proc/net/unix``). The marker's pid is the pytest process
       that made the home, but ``SubprocBroker`` starts a broker of its own that can outlive it, with
       its database and socket in the home; this check finds it, and any other child, without naming
       them. Entries the kernel won't show (another user's processes) are skipped; any other failure
       to read the process list leaves the scan unproven, and nothing is removed.

    Without procfs (macOS, Windows) check 3 can't be made, so the sweep fails closed and removes
    nothing. Errors are ignored, so it never fails a run, and only the homes that are gone are
    returned.
    """
    now = time.time() if now is None else now
    proc = PROC_ROOT if proc is None else proc
    uid = os.getuid()
    removed: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    candidates: list[str] = []
    for entry in entries:
        if not entry.name.startswith(TMP_HOME_PREFIX):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or now - st.st_mtime < max_age_s:
                continue
            marker = os.path.join(entry.path, TEST_MARKER)
            if not stat.S_ISREG(os.lstat(marker).st_mode):
                continue
        except OSError:
            continue
        owner = _marker_pid(marker)
        if owner is None or _process_alive(owner):
            continue
        candidates.append(entry.path)
    if not candidates:
        return removed
    in_use = _open_paths(proc)
    if in_use is None:
        return removed
    for home in candidates:
        if _used(home, in_use):
            continue
        shutil.rmtree(home, ignore_errors=True)
        # ignore_errors can leave some or all of it: only a home that is gone counts as removed
        if not os.path.lexists(home):
            removed.append(Path(home))
    return removed


def _open_paths(proc: Path) -> set[str] | None:
    """Every path a running process has as its cwd, its root or an open file, and every Unix socket
    path bound, read from procfs (#919); None when that can't be read at all (no procfs, no process
    list) or an entry fails in a way that isn't "gone" or "denied".

    A process that exits during the scan is skipped (its entries are gone, and so is its use). An
    entry the kernel won't show (``PermissionError``) is skipped: that is another user's process, whose
    home would be its own (the sweep only removes this user's), or a non-dumpable one, which no test
    run starts."""
    paths: set[str] = set()
    try:
        pids = [e.path for e in os.scandir(proc) if e.name.isdigit()]
    except OSError:
        return None
    if not pids:
        return None
    for pid in pids:
        links = [os.path.join(pid, "cwd"), os.path.join(pid, "root")]
        try:
            links += [os.path.join(pid, "fd", fd) for fd in os.listdir(os.path.join(pid, "fd"))]
        except (FileNotFoundError, PermissionError):
            pass
        except OSError:
            return None
        for link in links:
            try:
                paths.add(os.readlink(link))
            except (FileNotFoundError, PermissionError):
                continue
            except OSError:
                return None
    # A bound Unix socket (a broker's) shows in its fds only as ``socket:[inode]``: its path is here.
    try:
        with open(proc / "net" / "unix", encoding="utf-8", errors="replace") as f:
            next(f, None)
            for line in f:
                fields = line.split(None, 7)
                if len(fields) == 8:
                    paths.add(fields[7].rstrip("\n").lstrip("@"))
    except OSError:
        return None
    return paths


def _used(home: str, in_use: set[str]) -> bool:
    """Whether any open path is ``home`` or inside it, by its own path or its resolved one (a /tmp
    that is a symlink). A deleted file reads as ``<path> (deleted)`` and still matches."""
    prefixes = {home.rstrip("/"), os.path.realpath(home).rstrip("/")}
    return any(p == h or p.startswith(h + "/") or p == h + " (deleted)" for p in in_use for h in prefixes)


def _marker_pid(marker: str) -> int | None:
    """The pid a home's marker holds, or None for an empty, unreadable or malformed one (#919). Read
    without following a symlink and cut short: the marker is a claim, and a pid of 0 or less would
    name a process group to ``os.kill``, never one process."""
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            text = os.read(fd, 32).decode("ascii").strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError):
        return None
    if not text.isdigit() or len(text) > 10:
        return None
    pid = int(text)
    return pid if pid > 0 else None


def _process_alive(pid: int) -> bool:
    """Whether a process ``pid`` exists. Only ``ProcessLookupError`` says it is gone: a process of
    another user (``PermissionError``) is alive, and a pid reused since only keeps a home, the safe side."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def pytest_sessionstart(session: pytest.Session) -> None:
    # Once per session, from the controller: an xdist worker has ``workerinput`` and skips it, so N
    # workers do not race each other through /tmp.
    if not hasattr(session.config, "workerinput"):
        sweep_stale_homes()


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
        web_origin: Any = None,
    ):
        from switchboard.broker.peer import AllowAllHumans
        from switchboard.paths import Paths

        self.home = Path(home)
        self.paths = Paths.from_home(home)
        self.cfg = cfg or Config(human_name=TEST_HUMAN)
        self.policy = policy if policy is not None else AllowAllHumans()
        self.test_mode = test_mode
        self.clock = clock
        # a broker.auth.WebOrigin: a public URL (DESIGN.md §30); or a callable of the bound port,
        # for a public URL that must name it (http://sb.localhost:<port>, the e2e passkey tests)
        self.web_origin = web_origin
        self.port = 0
        self.app: Any = None
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None

    # How long start() waits for the lifespan: it returns the moment the server is up, so this is a ceiling
    # for a loaded runner, not a wait. It was 10 s, and a CI run (#832) timed out on five slow starts.
    START_TIMEOUT_S = 60.0

    def start(self) -> "InProcBroker":
        import uvicorn

        from switchboard.broker.app import create_app
        from switchboard.broker.daemon import loopback_listener
        from switchboard.broker.restarts import BrokerServer

        tcp = loopback_listener(self.port)  # the broker's own socket; a restart keeps its port (and Origin)
        self.port = tcp.getsockname()[1]
        self.tcp = tcp
        origin = self.web_origin(self.port) if callable(self.web_origin) else self.web_origin
        self.app = create_app(
            self.paths,
            self.cfg,
            self.policy,
            self.test_mode,
            port=self.port,
            clock=self.clock,
            web_origin=origin,
        )
        self.server = BrokerServer(
            uvicorn.Config(
                self.app,
                access_log=False,
                log_level="warning",
                log_config=None,
                lifespan="on",
                ws_ping_interval=None,
            ),
            self.app.state.broker,
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
                # sse_starlette starts a loop-level shutdown watcher for MCP responses.
                # Uvicorn does not own that task, so finish it before closing this test loop.
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.close()

        self.thread = threading.Thread(target=run, name="yk-broker", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + self.START_TIMEOUT_S
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError(f"in-process broker failed to start: {self.error!r}")
            if time.monotonic() > deadline:
                # Still starting, not dead. Stop it and wait for the thread before saying so: the caller's
                # cleanup removes the home, and a start that went on under a removed home failed with a
                # FileNotFoundError that hid the real cause, a start too slow for its deadline (CI, #832).
                self.server.should_exit = True
                self.thread.join(max(self.START_TIMEOUT_S, 10.0))
                raise RuntimeError(
                    f"in-process broker did not start within {self.START_TIMEOUT_S:g} s: {self.error!r}"
                )
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

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        from switchboard.mcp.client import call_sync

        return call_sync(self.paths.sock, method, params or {}, timeout)

    def on_loop(self, fn: Any, *args: Any, timeout: float = 10) -> Any:
        """Run a plain callable on the broker's event loop thread and return its result. ``timeout`` is how
        long to wait for it: a test that plants a great deal of data on the loop gives itself more."""
        assert self.loop is not None

        async def wrapper() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(wrapper(), self.loop).result(timeout)

    def login_url(self) -> str:
        return self.call("human.login_link")["url"]

    def web_client(self) -> httpx.Client:
        # An idle connection is dropped after 1 s, well before uvicorn closes it (5 s,
        # timeout_keep_alive): reusing one the server was closing at that moment got
        # "Connection reset by peer" on a POST, which httpx doesn't retry (seen on a slow runner).
        c = httpx.Client(
            base_url=self.base,
            timeout=10.0,
            follow_redirects=False,
            limits=httpx.Limits(keepalive_expiry=1.0),
        )
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


def ws_connect(
    broker: InProcBroker, cookie: str | None, *, origin: str | None = "default", host: str | None = None
) -> Any:
    """Open the UI WebSocket.

    ``host`` replaces switchboard.localhost:<port> in the URL (and Host header).
    """
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

    # How long start() waits for the socket to answer. It returns the moment ping succeeds, so this is a
    # ceiling for a loaded machine, not a wait. It was 15 s, and a loaded suite (#857) gave up while the
    # child was still starting: once after the ready line, and once before that line was printed. The ready
    # line is printed before the lifespan creates the socket, so the line is not the event. The in-process
    # fixture's ceiling is the same 60 s (#832).
    START_TIMEOUT_S = 60.0

    def __init__(self, home: Path, *, trust: bool = True, test_mode: bool = True, args: Sequence[str] = ()):
        from switchboard.paths import Paths

        self.home = Path(home)
        self.paths = Paths.from_home(home)
        self.trust = trust
        self.test_mode = test_mode
        self.args = list(args)  # more `start` flags (--public-url, --log-stdout: issue #34)
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.pid = 0
        self.env = child_env()

    def start(self) -> "SubprocBroker":
        from switchboard.mcp.client import ping

        cmd = [
            sys.executable,
            "-m",
            "switchboard",
            "start",
            "--foreground",
            "--home",
            str(self.home),
            "--port",
            "0",
            *self.args,
        ]
        if self.test_mode:
            cmd.append("--test-mode")
            if self.trust:
                cmd.append("--test-trust-uds")
        self.out = open(self.home / "subproc.out", "ab")
        self.proc = subprocess.Popen(
            cmd, env=self.env, stdin=subprocess.DEVNULL, stdout=self.out, stderr=self.out
        )
        deadline = time.monotonic() + self.START_TIMEOUT_S
        while time.monotonic() < deadline:
            info = ping(self.paths.sock, timeout=1.0)
            if info:
                self.port = info["port"]
                self.pid = info["pid"]
                return self
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        # A child that dies at once is not the loaded-machine timeout #857 raised the ceiling for.
        # Reading the exit before kill() keeps a signalled stop from looking like that crash.
        code = self.proc.poll()
        self.kill()
        output = (self.home / "subproc.out").read_text()
        if code is not None:
            raise RuntimeError(f"subprocess broker exited {code} before the socket answered: {output}")
        raise RuntimeError(f"subprocess broker did not start within {self.START_TIMEOUT_S:g} s: {output}")

    def cli(
        self, *args: str, input: str | None = None, timeout: float = 20.0, under: Sequence[str] = ()
    ) -> subprocess.CompletedProcess[str]:
        # A new session: the child never has a controlling terminal, so TTY-gated
        # verbs (``login``) behave the same in a real terminal, under CI and under an agent.
        return subprocess.run(
            [*under, sys.executable, "-m", "switchboard", "--home", str(self.home), *args],
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


def stand_in_agent(directory: Path) -> list[str]:
    """An argv prefix that runs a command as the child of a stand-in agent harness, so the production
    peer check finds an agent in that command's ancestry whoever started pytest.

    An agent-started pytest may be orphaned and lose the agent from its own ancestry (#437, #458).
    Putting the agent in the command's own chain doesn't change with the runner. The stand-in is
    a script named ``claude`` that runs its arguments as a child, so its argv
    (``/bin/sh <directory>/claude <command>``) matches the harness patterns in ``broker/peer.py``."""
    exe = directory / "claude"
    exe.write_text('#!/bin/sh\n"$@"\n')
    exe.chmod(0o755)
    return [str(exe)]


def _default_sigint() -> None:  # pragma: no cover - runs in the child
    signal.signal(signal.SIGINT, signal.SIG_DFL)
