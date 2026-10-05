"""create_app(): the broker's FastAPI app; its lifespan wires everything (DESIGN.md §3).

The same event loop serves HTTP/WebSocket (TCP, 127.0.0.1 only) and the
JSON-lines RPC server (0600 Unix socket). The broker is the only SQLite writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from switchboard import db
from switchboard.adapters import build_adapters
from switchboard.broker import oidc, web
from switchboard.broker.agents import AgentService
from switchboard.broker.auth import HostOriginGuard, LoginTokens, SecurityHeaders, Sessions, WebOrigin
from switchboard.broker.hosts import HostViews
from switchboard.broker.hub import Hub, WsSubscriber
from switchboard.broker.passkeys import (
    CLAIM_TTL_S,
    FRESH_CHECK_S,
    ClaimTokens,
    Sealer,
    UsedChallenges,
    WebAuthn,
    passkeys_unavailable,
)
from switchboard.broker.passwords import SignInLimiter
from switchboard.broker.peer import AllowAllHumans, PeerPolicy, ProcessPeerPolicy
from switchboard.broker.remote import RemoteManager
from switchboard.broker.reviews import Boards
from switchboard.broker.rpc import RpcServer
from switchboard.broker.service import BrokerInfo, RoomService
from switchboard.clock import Clock, SystemClock
from switchboard.config import Config
from switchboard.delivery.engine import Engine
from switchboard.delivery.runner import Runner
from switchboard.delivery.sinks import SinkRegistry
from switchboard.paths import Paths, hook_state_text, write_hook_copy
from switchboard.store import Store

log = logging.getLogger("switchboard.broker")

MAINTENANCE_S = 60.0
RESET_OWNER_ENV = "SWITCHBOARD_RESET_OWNER"


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
    # where browsers reach the web UI (DESIGN.md §30): switchboard.localhost:<port>, or --public-url
    web_origin: WebOrigin = None  # type: ignore[assignment]
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
    boards: Boards = None  # type: ignore[assignment]
    # the remote hosts' links (DESIGN.md §27.4); None until the RPC server listens
    remotes: RemoteManager | None = None
    # the machines that dial in (DESIGN.md §31.7): a hosted broker with passkeys only
    machines: Any = None
    shutdown_cb: Callable[[], None] | None = None
    recovery: dict[str, int] = field(default_factory=dict)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    # the owner of a hosted broker (DESIGN.md §31): passkeys work behind a public URL that is a
    # secure context with a DNS name (else None); the claim link lives while there is no owner
    webauthn: WebAuthn | None = None
    oidc: Any = None  # broker.oidc.OidcClient: Sign in with Google, when configured (§38)
    claim: ClaimTokens | None = None
    claim_out: Callable[[str], None] = field(default_factory=lambda: lambda line: print(line, flush=True))
    sealer: Sealer = None  # type: ignore[assignment]
    used_challenges: UsedChallenges = None  # type: ignore[assignment]
    # per web session (its id hash): when it last passed a passkey or password check, and
    # until when the claim's own session may add its backup passkey without one (§31.4)
    passkey_checks: dict[str, float] = field(default_factory=dict)
    claim_grace: dict[str, float] = field(default_factory=dict)
    # failed password sign-ins (§32.4)
    signin: SignInLimiter = None  # type: ignore[assignment]

    @property
    def hosted(self) -> bool:
        """Behind a public URL (§30): people sign in with a password or a passkey (§32)."""
        return self.web_origin is not None and self.web_origin.public

    def unclaimed(self) -> bool:
        """No owner yet: nobody claimed this broker and it has no passkey."""
        return self.store.owner_handle() is None and self.store.passkey_count() == 0

    def fresh_check(self, sid_hash: str) -> bool:
        """The session passed a passkey or password check in the last FRESH_CHECK_S, or it is
        the claim's own session within its grace: what adding a passkey or pairing or approving
        a machine needs (§31.6), so a stolen session can't make itself permanent."""
        now = self.clock.now()
        return (
            now - self.passkey_checks.get(sid_hash, float("-inf")) <= FRESH_CHECK_S
            or self.claim_grace.get(sid_hash, 0.0) > now
        )

    def claimed(self) -> None:
        """The claim succeeded: no claim link from now on."""
        self.claim = None
        with contextlib.suppress(FileNotFoundError):
            self.paths.test_claim_link.unlink()

    @property
    def base_url(self) -> str:
        return self.web_origin.origin

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
    web_origin: WebOrigin | None = None,
    reset_owner: str | None = None,
) -> FastAPI:
    """Build the broker app. ``port`` is the real bound TCP port (default cfg.port);
    ``web_origin`` is where browsers reach it (default ``http://switchboard.localhost:<port>``);
    ``reset_owner`` is ``SWITCHBOARD_RESET_OWNER`` (§31.5; read from the environment when
    not given)."""
    clock = clock or SystemClock()
    if reset_owner is None:
        reset_owner = os.environ.get(RESET_OWNER_ENV) or None
    port = cfg.port if port is None else port
    web_origin = web_origin or WebOrigin.local(port)
    state = BrokerState(
        paths=paths,
        cfg=cfg,
        port=port,
        peer_policy=peer_policy or default_peer_policy(cfg),
        test_mode=test_mode,
        clock=clock,
        info=BrokerInfo(
            port=port,
            test_mode=test_mode,
            home=str(paths.home),
            started_at=clock.now(),
            url=web_origin.origin + "/",
        ),
        login_tokens=LoginTokens(clock),
        web_origin=web_origin,
        hosts=HostViews(cfg.claude.sessions_dir, clock),
        sealer=Sealer(clock),
        used_challenges=UsedChallenges(clock),
        signin=SignInLimiter(clock),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        os.umask(0o077)
        paths.ensure()
        from switchboard.broker import proc

        proc.pin_home_btime(paths)  # recovery compares old process starts before ending any session
        if cfg.review.agentsview:
            # a 0.2.0 key: /catchup never looks for agentsview (DESIGN.md §26); once per start
            log.warning(
                "config.toml: [review] agentsview is ignored since /catchup replaced /review;"
                " you can remove it"
            )
        con = db.open_db(paths.db)  # a version-1 database is migrated here, after a backup (§27.6)
        state.store = Store(con, clock)
        # local rows only: a remote row's pids are pids on its own host (§27.5.6)
        state.recovery = state.store.recover_on_start(state.hosts.local.alive)
        if any(state.recovery.values()):
            log.warning("restart recovery: %s", state.recovery)
        write_hook_copy(paths)
        _refresh_hook_state(state)
        state.sessions = Sessions(state.store)
        _owner_start(state, reset_owner)
        state.hub = Hub()
        state.service = RoomService(state.store, state.hub, cfg, state.info, clock)
        state.engine = Engine(
            state.store, clock, cfg, build_adapters(cfg), SinkRegistry(), test_mode=test_mode
        )
        state.runner = Runner(state)
        state.agents = AgentService(state)
        state.boards = Boards(state)
        state.service.delivery = state.agents
        state.rpc = RpcServer(paths.sock, state)
        try:
            await state.rpc.start()
        except BaseException:
            con.close()
            raise
        if test_mode:
            _write_test_token(state)
        if state.claim is not None:
            _announce_claim(state)
            state.tasks.append(asyncio.create_task(_claim_loop(state)))
        state.tasks.append(asyncio.create_task(_maintenance(state)))
        state.tasks.append(asyncio.create_task(state.runner.run()))
        state.tasks.append(asyncio.create_task(state.agents.liveness_loop()))
        for adapter in state.engine.adapters.values():
            await adapter.start(state.runner)
        # after the RPC server: a link's requests are dispatched through it (§27.3)
        state.remotes = RemoteManager(state)
        await state.remotes.start()
        state.service.remotes = state.remotes
        if state.hosted and state.web_origin.secure_context():
            from switchboard.broker.machines import MachineManager

            state.machines = MachineManager(state)
            await state.machines.start()
            state.service.machines = state.machines
        log.info("broker up: pid %d port %d test_mode %s", os.getpid(), port, test_mode)
        try:
            yield
        finally:
            if state.machines is not None:
                with contextlib.suppress(Exception):
                    await state.machines.stop()
            if state.remotes is not None:
                # every link's child is killed and reaped; remote members go offline
                with contextlib.suppress(Exception):
                    await state.remotes.stop()
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
                db.checkpoint(con, "TRUNCATE")
            con.close()
            for f in (paths.test_login_token, paths.test_claim_link):
                with contextlib.suppress(FileNotFoundError):
                    f.unlink()
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
    app.add_middleware(HostOriginGuard, origin=web_origin)
    app.add_middleware(SecurityHeaders, origin=web_origin)
    return app


def _write_test_file(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, p)


def _write_test_token(state: BrokerState) -> None:
    _write_test_file(state.paths.test_login_token, state.login_tokens.mint())


# ---------------------------------------------------------- the owner (§31)
def _owner_start(state: BrokerState, reset_owner: str | None) -> None:
    """At start: passkeys are on behind a public URL where they can work; the owner reset
    (``SWITCHBOARD_RESET_OWNER``) is applied once per value; and while there is no owner, a
    claim link is made (printed by the lifespan, then a fresh one every hour)."""
    origin = state.web_origin
    if origin.public:
        why = passkeys_unavailable(origin)
        if why:
            log.warning(
                "passkeys are off: %s. Sign in with `switchboard login` in the container's shell", why
            )
        else:
            state.webauthn = WebAuthn(origin)
        try:
            cfg = oidc.from_env()
        except ValueError as e:
            log.warning("Sign in with Google is off: %s", e)
            cfg = None
        if cfg is not None:
            state.oidc = oidc.OidcClient(cfg)
            log.info("sign-in with %s is on (client %s)", cfg.provider, cfg.client_id[:12])
    if reset_owner:
        applied = hashlib.sha256(reset_owner.encode("utf-8")).hexdigest()
        if state.store.reset_owner_applied() != applied:
            got = state.store.reset_owner(applied)
            log.warning(
                "owner reset (%s): %d passkey(s) and %d web session(s) deleted, %d paired machine(s)"
                " back to pending; this can't be undone",
                RESET_OWNER_ENV,
                got["passkeys"],
                got["sessions"],
                got["machines_pending"],
            )
            state.store.add_event("login", data={"what": "owner_reset", **got})
    if origin.public and state.unclaimed():
        # passwords work wherever the broker is hosted; passkeys only where WebAuthn can (§32.4)
        state.claim = ClaimTokens(state.clock)


def _announce_claim(state: BrokerState) -> None:
    """One line on stdout (the container's log, which a deploy tool shows): the admin's one-time
    password (§32.4), and the same as a link, the password after ``#`` so no server ever
    receives it in a URL. Never through the log (``broker.log`` keeps no secrets); in test
    mode the link also goes to ``run/test-claim-link``, for the suite."""
    claim = state.claim
    if claim is None:
        return
    token = claim.mint()
    origin = state.web_origin.origin
    url = f"{origin}/setup#t={token}"
    minutes = int(claim.ttl_s // 60)
    state.claim_out(
        f"switchboard isn't set up yet. Sign in at {origin} as admin with the one-time password {token}"
        f" (it works once, for {minutes} min), then choose your own password or passkey."
        f" Or open {url}"
    )
    if state.test_mode:
        _write_test_file(state.paths.test_claim_link, url)
    log.warning(
        "no admin yet: a one-time password is on stdout (it works once, for %d min; a new one each time"
        " it expires, until someone signs in with it)",
        minutes,
    )


async def _claim_loop(state: BrokerState) -> None:
    """A fresh claim link every ``CLAIM_TTL_S`` while the broker stays unclaimed: a read-only
    role in a deploy tool can read logs but usually can't restart pods (§31.3)."""
    while True:
        await asyncio.sleep(CLAIM_TTL_S)
        if state.claim is None:
            return
        _announce_claim(state)


def _refresh_hook_state(state: BrokerState) -> None:
    state.info.hook_state = hook_state_text(state.paths)


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
                state.hub.notice(
                    None, "warn", "switchboard hook copy changed on disk: " + state.info.hook_state
                )
            was_bad = bad
            state.store.web_session_purge()
            state.login_tokens.purge()
            state.used_challenges.purge()
            now = state.clock.now()
            for k in [k for k, t in state.passkey_checks.items() if now - t > FRESH_CHECK_S]:
                del state.passkey_checks[k]
            for k in [k for k, t in state.claim_grace.items() if t <= now]:
                del state.claim_grace[k]
            for sub in list(state.hub.subs):
                if (
                    isinstance(sub, WsSubscriber)
                    and sub.sid_hash
                    and not state.store.web_session_valid(sub.sid_hash)
                ):
                    state.hub.remove(sub)
        except Exception:
            log.exception("maintenance failed")
