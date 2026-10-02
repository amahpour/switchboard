"""The dialer (DESIGN.md §31.7): the long-running process on a machine whose home dials its
broker (``switchboard remote join``). It keeps that machine's link up.

- **Start** (``main``, once): ``satellite.toml`` must say ``transport = "wss"``, and the
  satellite's own start checks pass in dialer mode (no ``SSH_CONNECTION``). Then ``harden()``
  (this process holds the machine key and the live link), the home's locks as a satellite
  takes them (``run/satellite.lock``, then ``run/broker.lock``: one dialer per home, and no
  broker runs from it meanwhile), and ``run/dialer.pid`` and ``run/dialer.state`` for
  ``switchboard stop`` and ``switchboard status``.
- **Each connection**: ``wss://<broker>/link``, over TLS that trusts the operating system's
  certificate store (``truststore``, so a TLS-inspecting proxy whose CA is installed there
  works on macOS and Windows too), with no Origin and no cookie. The signed handshake
  (``remote/linkkey.py``) checks the broker's key against the one pinned at ``remote join``;
  a pending machine waits there (the server's pings keep the connection open); once
  approved, one satellite session (``Satellite.run_session``) runs over a socketpair, one
  frame per WebSocket text message each way. The session binds the home's socket only after
  the broker's welcome and unlinks it when the link ends, so between links local processes
  find no socket, as on an ssh remote.
- **After a drop** it redials with backoff (1 s to 30 s; a minute after being replaced by
  another connection with this key). **It stops for good** on ``removed`` or ``unknown`` (the
  broker holds no such key) and on a broker key that isn't the pinned one: it says why in its
  log and its state file, and exits.

It opens the network connection and hands the satellite one end of a socketpair: the
satellite itself still has no network socket and this module no spawn site
(``tests/unit/test_satellite_static.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import logging.handlers
import os
import random
import secrets
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from switchboard import __version__
from switchboard.broker import proc
from switchboard.broker.auth import WebOrigin
from switchboard.paths import Paths, UnsafePathError, write_hook_copy
from switchboard.remote import linkkey, proto
from switchboard.remote import satellite as sat
from switchboard.remote.config import RemoteConfigError, SatelliteConf, read_satellite_conf

log = logging.getLogger("switchboard.dialer")

BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
BACKOFF_JITTER = 0.2
BACKOFF_RESET_UP_S = 60.0  # a link up this long starts the backoff over
REPLACED_WAIT_S = 60.0
CONNECT_TIMEOUT_S = 15.0
PING_S = 20.0
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FINAL = 3  # stopped for good: removed, unknown, or a changed broker key
STATE_FILE = "dialer.state"
PID_FILE = "dialer.pid"
FINAL_TEXT = {
    "removed": "the broker's owner removed this machine. To bring it back, make a new code in the web UI"
               " (Add a machine) and run `switchboard remote join` here again",
    "unknown": "the broker holds no key for this machine (it was removed, or the broker's data was reset). Make"
               " a new code in the web UI (Add a machine) and run `switchboard remote join` here again",
    "broker_key": "the broker's key is not the one pinned at `remote join`. If the broker was reinstalled, pair"
                  " this machine again (a new code, `switchboard remote join`); if it wasn't, something between"
                  " this machine and the broker is answering in its place",
}


class Final(Exception):
    """Stop for good: ``reason`` is a key of FINAL_TEXT."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.message = message or FINAL_TEXT.get(reason, reason)


class Down(Exception):
    """This connection ended; dial again later."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------- state files
def state_path(paths: Paths) -> Path:
    return paths.run_dir / STATE_FILE


def pid_path(paths: Paths) -> Path:
    return paths.run_dir / PID_FILE


def read_state(paths: Paths) -> dict[str, Any] | None:
    """What the dialer last said about itself (``switchboard status``), or None."""
    try:
        data = json.loads(state_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def running_pid(paths: Paths) -> int | None:
    """The pid of this home's dialer, if it runs (pid and start time from its pidfile, and an
    argv that is ``switchboard start``)."""
    try:
        pid_s, start_s = pid_path(paths).read_text(encoding="ascii").split()
        pid, start = int(pid_s), float(start_s)
    except (OSError, ValueError):
        return None
    proc.pin_home_btime(paths)
    if not proc.alive(pid, start):
        return None
    words = proc.argv(pid, start).split()
    if "start" not in words or not any(w == "switchboard" or w.endswith("/switchboard") for w in words):
        return None  # a pid that was reused since, by something else
    return pid


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


@dataclass
class DialerState:
    paths: Paths
    conf: SatelliteConf
    state: str = "starting"
    reason: str = ""
    since: float = field(default_factory=time.time)

    def set(self, state: str, reason: str = "", **extra: Any) -> None:
        if (state, reason) != (self.state, self.reason):
            self.state, self.reason, self.since = state, reason, time.time()
            log.info("dialer: %s%s", state, f" ({reason})" if reason else "")
        data = {"state": self.state, "reason": self.reason or None, "since": self.since, "pid": os.getpid(),
                "name": self.conf.name, "broker": self.conf.broker_url, "version": __version__, **extra}
        with contextlib.suppress(OSError):
            _write_atomic(state_path(self.paths), json.dumps(data))


# ------------------------------------------------------------------ the wire
def tls_context() -> ssl.SSLContext:
    """TLS that trusts the operating system's certificate store (truststore; pip does the same)."""
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


async def _recv(ws: Any, timeout: float | None) -> dict[str, Any]:
    """The next handshake frame; a ``refuse`` becomes Final or Down."""
    from websockets.exceptions import ConnectionClosed

    try:
        text = await (asyncio.wait_for(ws.recv(), timeout) if timeout is not None else ws.recv())
    except (asyncio.TimeoutError, TimeoutError):
        raise Down("timeout") from None
    except ConnectionClosed:
        raise Down("closed") from None
    if not isinstance(text, str) or len(text) > linkkey.HANDSHAKE_MAX:
        raise Down("protocol")
    try:
        obj = proto.loads(text)
    except ValueError:
        raise Down("protocol") from None
    if isinstance(obj, dict) and obj.get("t") == "refuse":
        try:
            why, message = linkkey.check_refuse(obj)
        except linkkey.HandshakeError:
            raise Down("protocol") from None
        if why in linkkey.FINAL_REFUSALS:
            raise Final(why)
        log.warning("dialer: the broker refused the connection: %s (%s)", why, message[:120])
        raise Down(why)
    if not isinstance(obj, dict):
        raise Down("protocol")
    return obj


async def handshake(ws: Any, conf: SatelliteConf, key: Any, host: str, on_pending: Any = None) -> None:
    """Prove this machine's key, check the broker's against the pinned one, then wait until the
    machine is approved (``on_pending`` is called once if it has to wait)."""
    nm = secrets.token_bytes(linkkey.NONCE_BYTES)
    await ws.send(json.dumps({"t": "auth", "v": linkkey.LINK_VERSION, "name": conf.name,
                              "key": linkkey.b64u(linkkey.pub_raw(key)), "nm": linkkey.b64u(nm)},
                             separators=(",", ":")))
    try:
        nb, bkey, sig = linkkey.check_challenge(await _recv(ws, linkkey.HANDSHAKE_TIMEOUT_S))
    except linkkey.HandshakeError:
        raise Down("protocol") from None
    pinned = linkkey.unb64u(conf.broker_key, 32)
    if not hmac.compare_digest(bkey, pinned):
        raise Final("broker_key")
    if not linkkey.verify(bkey, sig, "broker", host, nm, nb):
        raise Down("broker_proof")
    await ws.send(json.dumps({"t": "proof", "sig": linkkey.sign(key, "machine", host, nb, nm)},
                             separators=(",", ":")))
    waiting = False
    while True:
        f = await _recv(ws, None if waiting else linkkey.HANDSHAKE_TIMEOUT_S)
        if f == {"t": "approved"}:
            return
        if f == {"t": "pending"} and not waiting:
            waiting = True
            if on_pending is not None:
                on_pending()
            continue
        raise Down("protocol")


async def run_link(ws: Any, satellite: sat.Satellite) -> str:
    """One satellite session over the approved connection, through a socketpair: each line the
    satellite writes is one text message to the broker, and each message a line to it.
    Returns why the session ended (the broker's refuse reason, or the satellite's bye)."""
    from websockets.exceptions import ConnectionClosed

    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    s_reader, s_writer = await asyncio.open_unix_connection(sock=a, limit=proto.MAX_FRAME + 1)
    b_reader, b_writer = await asyncio.open_unix_connection(sock=b, limit=proto.MAX_FRAME + 2)

    async def to_broker() -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                line = await b_reader.readline()
                if not line:
                    return
                await ws.send(line.rstrip(b"\n").decode("utf-8", "replace"))

    async def to_satellite() -> None:
        with contextlib.suppress(ConnectionClosed):
            async for msg in ws:
                if not isinstance(msg, str) or "\n" in msg or len(msg) > proto.MAX_FRAME + 1:
                    return  # a frame no line can carry: the session ends
                b_writer.write(msg.encode("utf-8") + b"\n")
                await b_writer.drain()

    session = asyncio.ensure_future(satellite.run_session(s_reader, s_writer))
    up = asyncio.ensure_future(to_broker())
    down = asyncio.ensure_future(to_satellite())
    try:
        await asyncio.wait({session, up, down}, return_when=asyncio.FIRST_COMPLETED)
        if not session.done():
            # the broker went away: end of file for the satellite, which says bye and unlinks
            with contextlib.suppress(Exception):
                b_writer.close()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(session), 10.0)
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(up), 2.0)  # the satellite's bye, while the socket carries it
    finally:
        for t in (up, down, session):
            if not t.done():
                t.cancel()
        results = await asyncio.gather(session, up, down, return_exceptions=True)
        for w in (b_writer, s_writer):
            with contextlib.suppress(Exception):
                w.close()
    why = results[0]
    return why if isinstance(why, str) else "eof"


# ------------------------------------------------------------------ the loop
class Dialer:
    def __init__(self, paths: Paths, conf: SatelliteConf, key: Any, satellite: sat.Satellite, state: DialerState):
        self.paths = paths
        self.conf = conf
        self.key = key
        self.satellite = satellite
        self.state = state
        origin = WebOrigin.parse(conf.broker_url)
        self.host = origin.host
        self.uri = origin.ws + linkkey.LINK_PATH
        self.secure = origin.scheme == "https"
        self.stopping = asyncio.Event()
        self.ws: Any = None
        self.in_session = False

    def stop(self) -> None:
        """SIGTERM or SIGINT: end the session (bye), close the connection, dial no more."""
        self.stopping.set()
        if self.in_session:
            self.satellite.stop("shutdown")
        ws = self.ws
        if ws is not None:
            asyncio.get_running_loop().create_task(ws.close())

    def _connect(self) -> Any:
        from websockets.asyncio.client import connect

        kwargs: dict[str, Any] = dict(compression=None, max_size=proto.MAX_FRAME + 1024, open_timeout=CONNECT_TIMEOUT_S,
                                      ping_interval=PING_S, ping_timeout=PING_S, close_timeout=5,
                                      user_agent_header=f"switchboard/{__version__}")
        if self.secure:
            kwargs["ssl"] = tls_context()
        return connect(self.uri, **kwargs)

    async def once(self) -> tuple[str, float]:
        """One connection: (why it ended, how long the link was up)."""
        up_at: float | None = None
        async with self._connect() as ws:
            self.ws = ws
            try:
                await handshake(ws, self.conf, self.key, self.host,
                                on_pending=lambda: self.state.set("pending", "waiting for approval in the web UI"))
                if self.stopping.is_set():
                    return "shutdown", 0.0
                self.state.set("up")
                up_at = time.monotonic()
                self.in_session = True
                why = await run_link(ws, self.satellite)
            finally:
                self.in_session = False
                self.ws = None
        return why, time.monotonic() - (up_at or time.monotonic())

    async def run(self) -> int:
        from websockets.exceptions import InvalidHandshake, InvalidURI, WebSocketException

        failures = 0
        while not self.stopping.is_set():
            self.state.set("connecting")
            wait: float | None = None
            up_for = 0.0
            try:
                why, up_for = await self.once()
                if why in linkkey.FINAL_REFUSALS:
                    raise Final(why)
                if why == "replaced":
                    wait = REPLACED_WAIT_S
                    log.warning("dialer: another connection with this machine's key replaced this one")
                reason = why
            except Final as f:
                self.state.set("stopped", f.reason, message=f.message)
                log.error("dialer: stopped for good: %s", f.message)
                print(f"switchboard: the dialer stopped: {f.message}", file=sys.stderr, flush=True)
                return EXIT_FINAL
            except Down as d:
                reason = d.reason
            except (InvalidURI, InvalidHandshake) as e:
                reason = f"refused ({type(e).__name__})"
            except (OSError, WebSocketException, asyncio.TimeoutError, TimeoutError) as e:
                reason = type(e).__name__ if not isinstance(e, OSError) else (e.strerror or type(e).__name__)
            if self.stopping.is_set():
                break
            if up_for >= BACKOFF_RESET_UP_S:
                failures = 0
            failures += 1
            delay = wait or BACKOFF_S[min(failures, len(BACKOFF_S)) - 1] * random.uniform(1 - BACKOFF_JITTER,
                                                                                         1 + BACKOFF_JITTER)
            self.state.set("down", str(reason)[:80], retry_in_s=round(delay, 1))
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), delay)
        self.state.set("stopped", "shutdown")
        return EXIT_OK


# ------------------------------------------------------------------- start
def setup_logging(paths: Paths, *, stdout: bool = False) -> None:
    """``logs/dialer.log`` (0600, rotating), or stdout: ids and states only, never text."""
    handler: logging.Handler
    if stdout:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.RotatingFileHandler(paths.logs_dir / "dialer.log", maxBytes=2 * 1024 * 1024,
                                                       backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("switchboard").setLevel(logging.INFO)
    for name in ("websockets", "websockets.client"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(paths: Paths, *, test_mode: bool = False, log_stdout: bool = False) -> int:
    """``switchboard start --foreground`` on a home that dials its broker."""
    os.umask(0o077)
    try:
        conf = read_satellite_conf(paths)
    except FileNotFoundError:
        print("switchboard: this home dials no broker (no satellite.toml): run `switchboard remote join`",
              file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, RemoteConfigError) as e:
        print(f"switchboard: {e}", file=sys.stderr)
        return EXIT_REFUSED
    why = sat.start_refusal(paths, conf.name, test_mode=test_mode, environ=os.environ, dialer=True)
    if why:
        print(f"switchboard: {why}", file=sys.stderr)
        return EXIT_REFUSED
    shift, skew, link_proto, frame_log = 0, 0.0, proto.LINK_PROTO, None
    if test_mode:
        frame_log = os.environ.get("SWITCHBOARD_TEST_FRAME_LOG") or None
        try:
            shift = sat._env_int(os.environ, "SWITCHBOARD_TEST_PID_SHIFT", 0, 2**30)
            skew = sat._env_float(os.environ, "SWITCHBOARD_TEST_CLOCK_SKEW")
            link_proto = sat._env_int(os.environ, "SWITCHBOARD_TEST_LINK_PROTO", 0, 2**16) or proto.LINK_PROTO
        except ValueError as e:
            print(f"switchboard: {e}", file=sys.stderr)
            return EXIT_REFUSED
    # before anything else: no same-user process may attach to this one (it holds the key)
    harden_state = sat.harden()
    try:
        paths.ensure()
    except UnsafePathError as e:
        print(f"switchboard: unsafe path: {e}", file=sys.stderr)
        return EXIT_REFUSED
    setup_logging(paths, stdout=log_stdout)
    try:
        key = linkkey.load_key(paths.home / "link" / linkkey.MACHINE_KEY)
    except linkkey.KeyFileError as e:
        print(f"switchboard: this machine's link key: {e} (run `switchboard remote join` again)", file=sys.stderr)
        return EXIT_REFUSED
    clock_state = sat.pin_linux_clock(paths)
    from switchboard.config import ConfigError, load

    try:
        cfg = load(paths)
    except ConfigError as e:
        print(f"switchboard: config error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    me = proc.info(os.getpid())
    me_pair = (os.getpid(), me.start if me else time.time())
    locks = sat.take_locks(paths, me_pair)
    if isinstance(locks, str):
        what = ("a switchboard broker runs from this home" if locks == "local_broker"
                else "another dialer (or a satellite) runs from this home")
        print(f"switchboard: {what}", file=sys.stderr)
        return 1
    _write_atomic(pid_path(paths), f"{me_pair[0]} {me_pair[1]!r}\n")
    state = DialerState(paths, conf)
    log.info("dialer: %s for %s (test mode %s, harden %s, process clock %s, satellite %s)", conf.name,
             conf.broker_url, test_mode, harden_state, clock_state, __version__)
    write_hook_copy(paths)
    satellite = sat.Satellite(paths, conf.name, test_mode=test_mode, sessions_dir=cfg.claude.sessions_dir,
                              harden_state=harden_state, pid_shift=shift, clock_skew=skew, link_proto=link_proto,
                              desktop=conf.desktop, frame_log=frame_log, stdio="wss")

    async def run() -> int:
        dialer = Dialer(paths, conf, key, satellite, state)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, dialer.stop)
        return await dialer.run()

    try:
        return asyncio.run(run())
    finally:
        with contextlib.suppress(OSError):
            pid_path(paths).unlink()
        for fd in locks:
            with contextlib.suppress(OSError):
                os.close(fd)
        log.info("dialer: stopped")
