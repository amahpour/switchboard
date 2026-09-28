"""create_app(): the broker's FastAPI app; its lifespan wires everything (DESIGN.md §3).

The same event loop serves HTTP/WebSocket (TCP, 127.0.0.1 only) and the
JSON-lines RPC server (0600 Unix socket). The broker is the only SQLite writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from switchboard import db
from switchboard.adapters import build_adapters
from switchboard.broker import web
from switchboard.broker.agents import AgentService
from switchboard.broker.auth import UI_HOST, HostOriginGuard, LoginTokens, SecurityHeaders, Sessions
from switchboard.broker.hosts import HostViews
from switchboard.broker.hub import Hub, WsSubscriber
from switchboard.broker.peer import AllowAllHumans, PeerPolicy, ProcessPeerPolicy
from switchboard.broker.rpc import RpcServer
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.delivery.engine import Engine
from switchboard.delivery.runner import Runner
from switchboard.delivery.sinks import SinkRegistry
from switchboard.paths import Paths, check_hook_copies, write_hook_copy
from switchboard.store import Store

log = logging.getLogger("switchboard.broker")

MAINTENANCE_S = 60.0


@dataclass
class BrokerState:
    paths: Paths
    cfg: Config
    port: int
    peer_policy: PeerPolicy
    test_mode: bool
    clock: Clock
    info: BrokerInfo
    login_tokens: LoginTokens
    # every probe about a participant goes through its host's view (DESIGN.md §27.5.6)
    hosts: HostViews = None  # type: ignore[assignment]
    store: Store = None  # type: ignore[assignment]
    sessions: Sessions = None  # type: ignore[assignment]
    hub: Hub = None  # type: ignore[assignment]
    service: RoomService = None  # type: ignore[assignment]
    rpc: RpcServer = None  # type: ignore[assignment]
    engine: Engine = None  # type: ignore[assignment]
    runner: Runner = None  # type: ignore[assignment]
    agents: AgentService = None  # type: ignore[assignment]
    shutdown_cb: Callable[[], None] | None = None
    recovery: dict[str, int] = field(default_factory=dict)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{UI_HOST}:{self.port}"

    @property
    def origin(self) -> str:
        return self.base_url

    def request_shutdown(self) -> None:
        if self.shutdown_cb is not None:
            self.shutdown_cb()
        else:  # pragma: no cover
            log.warning("shutdown requested but no shutdown callback is set")


def default_peer_policy(cfg: Config, test_trust_uds: bool = False) -> PeerPolicy:
    """The broker's UDS peer policy: the production ``ProcessPeerPolicy`` with
    ``[security] allow_ssh_cli`` from the config (DESIGN.md §27.5.7), or
    ``AllowAllHumans`` under ``--test-trust-uds``."""
    if test_trust_uds:
        return AllowAllHumans()
    return ProcessPeerPolicy(allow_ssh_cli=cfg.security.allow_ssh_cli)


def create_app(
    paths: Paths,
    cfg: Config,
    peer_policy: PeerPolicy | None = None,
    test_mode: bool = False,
    *,
    port: int | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the broker app. ``port`` is the real bound TCP port (default cfg.port)."""
    clock = clock or SystemClock()
    port = cfg.port if port is None else port
    state = BrokerState(
        paths=paths,
        cfg=cfg,
        port=port,
        peer_policy=peer_policy or default_peer_policy(cfg),
        test_mode=test_mode,
        clock=clock,
        info=BrokerInfo(port=port, test_mode=test_mode, home=str(paths.home), started_at=clock.now()),
        login_tokens=LoginTokens(clock),
        hosts=HostViews(cfg.claude.sessions_dir),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        os.umask(0o077)
        paths.ensure()
        con = db.open_db(paths.db)  # a version-1 database is migrated here, after a backup (§27.6)
        state.store = Store(con, clock)
        # local rows only: a remote row's pids are pids on its own host (§27.5.6)
        state.recovery = state.store.recover_on_start(state.hosts.local.alive)
        if any(state.recovery.values()):
            log.warning("restart recovery: %s", state.recovery)
        write_hook_copy(paths)
        _refresh_hook_state(state)
        state.sessions = Sessions(state.store)
        state.hub = Hub()
        state.service = RoomService(state.store, state.hub, cfg, state.info, clock)
        state.engine = Engine(state.store, clock, cfg, build_adapters(cfg), SinkRegistry(),
                              test_mode=test_mode)
        state.runner = Runner(state)
        state.agents = AgentService(state)
        state.service.delivery = state.agents
        state.rpc = RpcServer(paths.sock, state)
        try:
            await state.rpc.start()
        except BaseException:
            con.close()
            raise
        if test_mode:
            _write_test_token(state)
        state.tasks.append(asyncio.create_task(_maintenance(state)))
        state.tasks.append(asyncio.create_task(state.runner.run()))
        state.tasks.append(asyncio.create_task(state.agents.liveness_loop()))
        for adapter in state.engine.adapters.values():
            await adapter.start(state.runner)
        log.info("broker up: pid %d port %d test_mode %s", os.getpid(), port, test_mode)
        try:
            yield
        finally:
            for t in state.tasks:
                t.cancel()
            for t in state.tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await state.rpc.stop()
            await state.runner.stop()
            for adapter in state.engine.adapters.values():
                with contextlib.suppress(Exception):
                    await adapter.stop()
            state.hub.close_all()
            await asyncio.sleep(0)  # let WS senders see their close sentinel
            with contextlib.suppress(Exception):
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            with contextlib.suppress(FileNotFoundError):
                paths.test_login_token.unlink()
            log.info("broker stopped")

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_oauth2_redirect_url=None,
        lifespan=lifespan,
    )
    app.state.broker = state
    web.install(app, state)
    # Added last = outermost: SecurityHeaders wraps the guard's own 421/403 replies.
    app.add_middleware(HostOriginGuard, port=port)
    app.add_middleware(SecurityHeaders, port=port)
    return app


def _write_test_token(state: BrokerState) -> None:
    tok = state.login_tokens.mint()
    p = state.paths.test_login_token
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.replace(tmp, p)


def _refresh_hook_state(state: BrokerState) -> None:
    bad = check_hook_copies(state.paths)
    if bad:
        state.info.hook_state = "MISMATCH: " + ", ".join(bad)
    else:
        n = len(list(state.paths.hooks_dir.glob("switchboard_hook-*.py")))
        state.info.hook_state = f"ok ({n} cop{'y' if n == 1 else 'ies'})"
    return None


async def _maintenance(state: BrokerState) -> None:
    """Every 60 s: WAL checkpoint, hook hash check, session purge, stale WebSockets."""
    was_bad = state.info.hook_state.startswith("MISMATCH")
    while True:
        await asyncio.sleep(MAINTENANCE_S)
        try:
            db.checkpoint(state.store.con)
            _refresh_hook_state(state)
            bad = state.info.hook_state.startswith("MISMATCH")
            if bad and not was_bad:
                state.store.add_event("hook_hash", data={"state": "mismatch"})
                state.hub.notice(None, "warn", "switchboard hook copy changed on disk: " + state.info.hook_state)
            was_bad = bad
            state.store.web_session_purge()
            state.login_tokens.purge()
            for sub in list(state.hub.subs):
                if isinstance(sub, WsSubscriber) and sub.sid_hash and not state.store.web_session_valid(sub.sid_hash):
                    state.hub.remove(sub)
        except Exception:
            log.exception("maintenance failed")
