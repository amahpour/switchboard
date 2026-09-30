"""Machines that dial in (DESIGN.md §31.7): pairing codes, ``POST /link/pair``, the ``/link``
WebSocket with its signed handshake, approval, one live link per key, and Remove.

- **A machine** is a ``link_machines`` row (``Store.machine*``): pending from its pairing until
  the owner approves it, approved until Remove, which forgets its key.
- **Pairing codes** (``PairingCodes``): 12 Crockford base32 characters, only their hash kept,
  single use, 10 minutes, one live code per name, made by the owner's web session after a
  fresh passkey check. A used code is remembered until it would have expired, so a second
  machine trying it hears "already used", not "invalid".
- **The handshake** (``remote/linkkey.py``): the broker refuses a key it doesn't hold for that
  name, signs its own challenge, and checks the machine's proof. A pending machine then waits
  in the handshake (``{"t":"pending"}``, the WebSocket kept open by pings) until Approve sends
  ``{"t":"approved"}``, or Remove refuses it. No session exists for a pending machine, so its
  satellite binds no socket and its agents can't reach anything.
- **An approved machine's link** is a ``MachineLink``: M8's ``RemoteLink`` in passive mode,
  which never dials. Each approved connection runs one attempt on it, the WebSocket bridged to
  a socketpair whose other end the link reads and writes as it does an ssh child's stdio: the
  frames, ``REMOTE_METHODS``, the watch, the pings and the notices are M8's, unchanged. A second
  connection with the same key replaces the first (``refuse replaced``, a warning in the rooms,
  so a copied key in use shows); Remove refuses the live one (``refuse removed``) and the dialer
  stops for good.
- **Last seen** is kept in memory at every frame, and saved every minute and at each disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from switchboard import __version__
from switchboard.broker.remote import PING_MISSES, PING_S, Attempt, LinkClosed, RemoteLink
from switchboard.broker.service import ServiceError
from switchboard.envelope import clean
from switchboard.models import HARNESSES, MachineRow, valid_host
from switchboard.remote import linkkey, proto
from switchboard.remote.config import ANY_ROOM, REMOTE_HARNESSES, RemoteEntry
from switchboard.remote.describe import BLOCK_HINTS, fmt_ms
from switchboard.store import Conflict

if TYPE_CHECKING:  # pragma: no cover
    from starlette.websockets import WebSocket

    from switchboard.broker.app import BrokerState
    from switchboard.broker.remote import RemoteManager

log = logging.getLogger("switchboard.machines")

PAIR_TTL_S = 600.0  # a pairing code lives 10 minutes
SEEN_SAVE_S = 60.0
REPLACED_NOTICE_S = 600.0  # a replaced link warns the rooms at most this often
FAIL_EVENT_S = 60.0
EVENT_S = 0.2  # the web UI's `machines` event is debounced, as `remotes` is
FACT_LIMITS = {"hostname": 64, "os": 80, "arch": 32, "version": 40}
INSTALL_URL = "git+https://github.com/amahpour/switchboard"
REFUSE_TEXT = {
    "removed": "the owner removed this machine: pair it again with a new code to bring it back",
    "unknown": "this broker holds no such key for that name (the machine was removed, or never paired):"
               " pair it again with a new code",
    "replaced": "another connection with this machine's key took over",
    "shutdown": "the broker is stopping",
    "protocol": "the handshake went wrong",
    "proof": "the machine's proof doesn't verify",
}
# a blocked link of a machine: what to tell the owner (M8's hints are about ssh)
MACHINE_HINTS = {
    "proto": "the machine runs another switchboard version: install the broker's version there",
    "name": "the machine's satellite.toml names another machine: run `switchboard remote join` there again",
    "test_mode": "the machine runs in test mode and this broker doesn't",
}


def clean_facts(raw: Any) -> dict[str, Any]:
    """What a machine says about itself (its host name, OS, arch, version, the harnesses it
    found): claims, kept only as short cleaned text and known harness names."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, n in FACT_LIMITS.items():
        v = raw.get(k)
        if isinstance(v, str):
            s = " ".join(clean(v).split())[:n]
            if s:
                out[k] = s
    hs = raw.get("harnesses")
    if isinstance(hs, list):
        out["harnesses"] = sorted({h for h in hs if isinstance(h, str) and h in HARNESSES and h != "test"})
    return out


# ------------------------------------------------------------------- codes
@dataclass
class _Code:
    name: str
    expires: float
    used_fp: str | None = None


class PairingCodes:
    """Pairing codes, in memory, by the hash of their canonical form."""

    def __init__(self, clock: Any, ttl_s: float = PAIR_TTL_S):
        self.clock = clock
        self.ttl_s = ttl_s
        self._codes: dict[bytes, _Code] = {}

    def mint(self, name: str) -> str:
        """A fresh code for ``name``; an unused code made for it before dies."""
        self.purge()
        for h in [h for h, c in self._codes.items() if c.name == name and c.used_fp is None]:
            del self._codes[h]
        code = linkkey.make_code()
        canon = linkkey.normalize_code(code)
        assert canon is not None
        self._codes[linkkey.code_hash(canon)] = _Code(name, self.clock.now() + self.ttl_s)
        return code

    def use(self, text: Any, fp: str) -> tuple[str, str | None]:
        """("ok", name) spends a live code for the key ``fp``; ("used", name) for a code that
        was spent already; ("bad", None) for anything else."""
        self.purge()
        canon = linkkey.normalize_code(text)
        c = self._codes.get(linkkey.code_hash(canon)) if canon is not None else None
        if c is None:
            return "bad", None
        if c.used_fp is not None:
            return "used", c.name
        c.used_fp = fp
        return "ok", c.name

    def live(self, name: str) -> _Code | None:
        self.purge()
        return next((c for c in self._codes.values() if c.name == name and c.used_fp is None), None)

    def unused(self) -> list[dict[str, Any]]:
        """The live codes nobody used yet, as the web UI lists them: a name and the seconds left."""
        self.purge()
        now = self.clock.now()
        return sorted(({"name": c.name, "expires_in_s": round(c.expires - now, 1)}
                       for c in self._codes.values() if c.used_fp is None), key=lambda d: d["name"])

    def cancel(self, name: str) -> bool:
        """The owner gave up on a code: an unused one for ``name`` dies (a used one stays
        remembered, so a second machine trying it still hears "used")."""
        hs = [h for h, c in self._codes.items() if c.name == name and c.used_fp is None]
        for h in hs:
            del self._codes[h]
        return bool(hs)

    def drop(self, name: str) -> None:
        for h in [h for h, c in self._codes.items() if c.name == name]:
            del self._codes[h]

    def purge(self) -> None:
        now = self.clock.now()
        for h in [h for h, c in self._codes.items() if c.expires <= now]:
            del self._codes[h]

    def __len__(self) -> int:
        return len(self._codes)


# -------------------------------------------------------------------- link
class MachineLink(RemoteLink):
    """An approved machine's end of its link: M8's ``RemoteLink`` in passive mode. It never
    dials; ``serve`` runs one attempt over the reader and writer of each approved connection."""

    def __init__(self, machines: "MachineManager", row: MachineRow):
        entry = RemoteEntry(name=row.name, host="", user="", rooms=(ANY_ROOM,), harnesses=REMOTE_HARNESSES,
                            transport="wss")
        self.machines = machines
        self.key_fp = row.key_fp
        self.gone = False  # removed: its connection is refused, and no "link down" line follows
        self._next: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._lock = asyncio.Lock()
        self._replaced_noted = float("-inf")
        super().__init__(machines.remotes, entry, "")
        self.state, self.reason = "down", "waiting"

    # the approval is the consent: nothing about a config to check before an attempt
    def start(self) -> None:
        return None

    def files_changed(self) -> bool:
        return False

    def may_dial(self) -> bool:
        return not self.gone

    def consent_state(self) -> tuple[str, str]:
        return "down", "waiting"

    async def _spawn(self, a: Attempt) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._next is None:
            raise LinkClosed("down", "no_connection")
        reader, writer = self._next
        self._next = None
        a.writer = writer
        return reader, writer

    def _set(self, state: str, reason: str = "") -> None:
        changed = (state, reason) != (self.state, self.reason)
        super()._set(state, reason)
        if changed:
            self.machines.changed()

    async def _on_frame(self, a: Attempt, f: dict[str, Any]) -> None:
        if a is self.attempt:
            self.machines.seen(self.name)
        await super()._on_frame(a, f)

    def _blocked_text(self, reason: str, *, detail: bool = True) -> str:
        hint = MACHINE_HINTS.get(reason) or str(BLOCK_HINTS.get(reason, reason)).replace("<name>", self.name)
        return f"{self.name}: link refused ({reason}): {hint}"

    def _closed(self, e: LinkClosed) -> float:
        """A machine redials by itself, so nothing is blocked here: a failure that repeats is
        told once until the link is next up. A replaced connection says nothing: the one that
        replaced it warns."""
        if e.state == "blocked":
            e = LinkClosed("down", e.reason, e.notice or self._blocked_text(e.reason), "warn", e.detail)
        if e.reason == "replaced" and not e.notice:
            self.end_detail = e.detail
            self._set(e.state, e.reason)
            self._wake_waiters()
            return self.last_up_for
        return super()._closed(e)

    async def _announce(self, a: Attempt) -> None:
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(a.first_pong.wait(), PING_S * PING_MISSES)
        if a is not self.attempt or self.state != "up":
            return
        row = self.st.store.machine(self.name)
        via = (row.approved_via if row is not None else None) or "?"
        approved = row.approved_at if row is not None else None
        when = time.strftime("%Y-%m-%d", time.localtime(approved)) if approved else "?"
        rtt = f"{fmt_ms(self.rtt_ms)} ms" if self.rtt_ms is not None else "?"
        self.notice(f"{self.name}: link up (approved via {via} by {self.st.cfg.human_name} on {when};"
                    f" satellite {self.sat_version}, rtt {rtt})")
        self.machines.changed()
        self._wake_waiters()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> LinkClosed:
        """One approved connection: it replaces a live one, then runs as an attempt until either
        end closes. Returns how it ended."""
        old = self.attempt
        if old is not None:
            self._end_attempt_with(old, "replaced")
            now = self.now()
            if now - self._replaced_noted >= REPLACED_NOTICE_S:
                self._replaced_noted = now
                self.notice(f"{self.name}: a second connection with this machine's key replaced the first. If you"
                            " didn't start one, remove the machine in the web UI: its key may have been copied",
                            "warn")
            log.warning("machine %s: a second connection with its key replaced the first", self.name)
        async with self._lock:
            if self.gone:
                writer.close()
                return LinkClosed("down", "removed")
            self._next = (reader, writer)
            self._set("connecting")
            e: LinkClosed | None = None
            try:
                await self._run_attempt()
            except LinkClosed as x:
                e = x
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("machine %s: link failed", self.name)
                e = LinkClosed("down", "error")
            finally:
                self._next = None
                self.machines.save_seen(self.name)
            e = e or LinkClosed("down", "eof")
            if not self.gone:
                self._closed(e)
            return e

    def _end_attempt_with(self, a: Attempt, why: str) -> None:
        """End attempt ``a`` from outside: a refuse frame first (so the dialer knows why), then its
        end of the socketpair is closed, which its frame loop reads as ``why``."""
        self.send_frame(a, proto.refuse(why, REFUSE_TEXT.get(why, why)))
        a.abort = LinkClosed("down", why)
        if a.writer is not None:
            with contextlib.suppress(Exception):
                a.writer.close()

    async def end(self, why: str) -> None:
        """End the live connection, if any, and wait for its attempt to finish."""
        a = self.attempt
        if a is not None:
            self._end_attempt_with(a, why)
        try:
            await asyncio.wait_for(self._lock.acquire(), 10.0)
        except (asyncio.TimeoutError, TimeoutError):
            return
        self._lock.release()

    async def stop(self, state: str | None = None, reason: str = "") -> None:
        await self.end("shutdown")
        if state is not None:
            self._set(state, reason)
        self._wake_waiters()

    def dest(self) -> str:
        return "dials in over wss"

    def host_keys(self) -> list[str]:
        return [self.key_fp] if self.key_fp else []

    def hint(self) -> str | None:
        if self.state in ("blocked", "down") and self.reason in MACHINE_HINTS:
            return MACHINE_HINTS[self.reason]  # refused at each dial until it's fixed on the machine
        if self.state == "down" and self.reason == "waiting":
            return "waiting for it to dial in: its dialer runs `switchboard start` there"
        if self.state == "down":
            return "it redials by itself; if it doesn't come back, check `switchboard status` on that machine"
        return None


# ----------------------------------------------------------------- manager
@dataclass
class _Waiter:
    """A pending machine's connection, waiting in the handshake for the owner."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: str = ""

    def decide(self, outcome: str) -> None:
        if not self.event.is_set():
            self.outcome = outcome
            self.event.set()


class _End(Exception):
    """End a /link connection: ``frame`` (a refuse) is sent first if given; ``code`` is for the log."""

    def __init__(self, frame: dict[str, Any] | None, code: str):
        super().__init__(code)
        self.frame = frame
        self.code = code


class MachineManager:
    """Every machine that dials in, for a hosted broker with passkeys (§31.7)."""

    def __init__(self, state: "BrokerState"):
        self.state = state
        self.codes = PairingCodes(state.clock)
        self.key = linkkey.load_or_make_key(state.paths.home / "link" / linkkey.BROKER_KEY)
        self.bkey = linkkey.pub_raw(self.key)
        self.host = state.web_origin.host
        self.waiting: dict[str, _Waiter] = {}
        self._seen: dict[str, float] = {}
        self._saved: dict[str, float] = {}
        self._fails: dict[str, list[float]] = {}
        self._event: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self.running = False

    @property
    def remotes(self) -> "RemoteManager":
        assert self.state.remotes is not None
        return self.state.remotes

    @property
    def fingerprint(self) -> str:
        return linkkey.fingerprint(self.bkey)

    @property
    def link_url(self) -> str:
        return self.state.web_origin.ws + linkkey.LINK_PATH

    def now(self) -> float:
        return self.state.clock.now()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        for row in self.state.store.machines():
            if row.approved:
                self._link(row)
        self.running = True
        self._task = asyncio.get_running_loop().create_task(self._save_loop())
        log.info("machines: %d approved, broker key %s", len(self.remotes.machines), self.fingerprint)

    async def stop(self) -> None:
        self.running = False
        if self._event is not None:
            self._event.cancel()
            self._event = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for w in list(self.waiting.values()):
            w.decide("shutdown")
        await asyncio.gather(*(link.stop() for link in list(self.remotes.machines.values())),
                             return_exceptions=True)
        self.save_all()

    def _link(self, row: MachineRow) -> MachineLink:
        link = self.remotes.machines.get(row.name)
        if not isinstance(link, MachineLink):
            link = MachineLink(self, row)
            self.remotes.machines[row.name] = link
        return link

    # ------------------------------------------------------------ last seen
    def seen(self, name: str) -> None:
        self._seen[name] = self.now()

    def save_seen(self, name: str) -> None:
        t = self._seen.get(name)
        if t is not None and self._saved.get(name) != t:
            self._saved[name] = t
            with contextlib.suppress(Exception):
                self.state.store.machine_seen(name, t)

    def save_all(self) -> None:
        for name in list(self._seen):
            self.save_seen(name)

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(SEEN_SAVE_S)
            try:
                self.save_all()
                self.codes.purge()
            except Exception:
                log.exception("machines: saving last seen failed")

    # --------------------------------------------------------------- the UI
    def info(self, row: MachineRow) -> dict[str, Any]:
        """One machine, as the web UI shows it: the link's state for an approved one."""
        link = self.remotes.machines.get(row.name)
        out: dict[str, Any] = {
            "name": row.name,
            "key_fp": row.key_fp,
            "facts": row.facts,
            "created_at": row.created_at,
            "approved": row.approved,
            "approved_at": row.approved_at,
            "approved_via": row.approved_via,
            "last_seen": self._seen.get(row.name) or row.last_seen_at,
            "dialed_in": row.name in self.waiting or (link is not None and link.attempt is not None),
            "transport": "wss",
        }
        if row.pending or link is None:
            out.update(state="pending", reason="waiting_approval" if row.name in self.waiting else "not_dialed_in",
                       members=[], rtt_ms=None, version=None, hint=None)
        else:
            up = link.state == "up"
            out.update(state=link.state, reason=link.reason or None, since=link.since,
                       rtt_ms=link.rtt_ms if up else None, version=link.sat_version, skew_s=link.skew_s,
                       hooks=link.hooks, harden=link.harden, test_mode=link.sat_test_mode,
                       members=link.members(), max_members=link.entry.max_members, hint=link.hint(),
                       detail=link.end_detail)
        return out

    def summary(self) -> list[dict[str, Any]]:
        return [self.info(row) for row in self.state.store.machines()]

    def changed(self) -> None:
        """The web UI's ``machines`` event (every machine, as ``GET /api/machines`` lists them),
        debounced to one per ``EVENT_S``."""
        if self._event is not None or not self.running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop
            self._publish()
            return
        self._event = loop.call_later(EVENT_S, self._publish)

    def _publish(self) -> None:
        self._event = None
        hub = self.state.hub
        if hub is None:
            return
        try:
            hub.publish("machines", None, {"machines": self.summary(), "codes": self.codes.unused()})
        except Exception:
            log.exception("machines event failed")

    def _failed(self, what: str) -> None:
        """A failed pairing or link attempt needs no session: counted, and one event (and log
        line) a minute at most, as bad login tokens are."""
        f = self._fails.setdefault(what, [0.0, float("-inf")])
        f[0] += 1
        now = self.now()
        if now - f[1] >= FAIL_EVENT_S:
            self.state.store.add_event("machine", data={"what": what, "count": int(f[0])})
            log.warning("machines: %s (%d since the last note)", what.replace("_", " "), int(f[0]))
            f[0], f[1] = 0.0, now

    # -------------------------------------------------------- owner actions
    def mint(self, name: Any) -> dict[str, Any]:
        """A pairing code for a machine named ``name`` (the caller checked the fresh passkey check)."""
        if not isinstance(name, str) or not valid_host(name):
            raise ServiceError("bad_request", "a machine's name looks like work-laptop (a-z first, then a-z, 0-9,"
                                              " '-'; at most 24)")
        if name in self.remotes.links:
            raise ServiceError("conflict", f"{name} is a remote this broker dials over ssh (remotes.toml): pick"
                                           " another name")
        row = self.state.store.machine(name)
        if row is not None and not row.removed:
            what = "waiting for your approval" if row.pending else "approved"
            raise ServiceError("conflict", f"{name} is paired already ({what}): remove it first")
        code = self.codes.mint(name)
        origin = self.state.web_origin.origin
        self.state.store.add_event("machine", data={"what": "code", "name": name})
        log.info("machines: a pairing code for %s", name)
        self.changed()
        return {"name": name, "code": code, "expires_in_s": self.codes.ttl_s, "broker": origin,
                "broker_fingerprint": self.fingerprint,
                "install": f"uv tool install {INSTALL_URL}@v{__version__}",
                "join": f"switchboard remote join {origin} {code}"}

    def cancel(self, name: str) -> dict[str, Any]:
        """Forget the unused pairing code for ``name`` (the web UI's Cancel)."""
        dropped = self.codes.cancel(name)
        if dropped:
            self.state.store.add_event("machine", data={"what": "code_cancelled", "name": name})
            self.changed()
        return {"name": name, "cancelled": dropped}

    def approve(self, name: str, via: str = "web") -> dict[str, Any]:
        row = self.state.store.machine(name)
        if row is None or row.removed:
            raise ServiceError("not_found", f"no machine named {name[:40]}")
        if row.pending:
            self.state.store.machine_approve(name, via)
            self.state.store.add_event("machine", data={"what": "approved", "name": name, "via": via})
            log.warning("machine %s approved via %s (key %s)", name, via, row.key_fp)
            self.state.hub.notice(None, "info", f"{name} approved by {self.state.cfg.human_name} (key {row.key_fp})")
            row = self.state.store.machine(name)
            assert row is not None
            self._link(row)
            w = self.waiting.get(name)
            if w is not None:
                w.decide("approved")
            self.changed()
        return self.info(row)

    async def remove(self, name: str, via: str = "web") -> dict[str, Any]:
        """Remove (or reject) a machine: its key is forgotten, its live connection refused (its
        dialer stops for good), and its members ended at once."""
        row = self.state.store.machine(name)
        if row is None or row.removed:
            raise ServiceError("not_found", f"no machine named {name[:40]}")
        self.state.store.machine_remove(name)
        self.codes.drop(name)
        w = self.waiting.pop(name, None)
        if w is not None:
            w.decide("removed")
        link = self.remotes.machines.pop(name, None)
        if isinstance(link, MachineLink):
            link.gone = True
            await link.end("removed")
        self.state.hosts.remove_remote(name)
        ended = self.remotes.end_members(name, "removed")
        self._seen.pop(name, None)
        self.state.store.add_event("machine", data={"what": "removed", "name": name, "via": via, "ended": ended})
        log.warning("machine %s removed via %s", name, via)
        what = "rejected" if row.pending else "removed"
        tail = f"; {ended} member(s) ended" if ended else ""
        self.state.hub.notice(None, "warn", f"{name} {what} by {self.state.cfg.human_name}: its key is forgotten{tail}")
        self.changed()
        return {"name": name, "ended": ended}

    # ------------------------------------------------------ the machine's side
    def pair(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /link/pair``: a code, the machine's public key and its facts. The machine is
        saved as pending, with that key; the answer pins the broker's key on the machine."""
        try:
            key = linkkey.unb64u(body.get("key"), 32)
        except ValueError:
            self._failed("bad_pairing")
            return 400, {"error": "bad_request", "message": "key must be an Ed25519 public key (base64url)"}
        fp = linkkey.fingerprint(key)
        outcome, name = self.codes.use(body.get("code"), fp)
        if outcome == "bad" or name is None:
            self._failed("bad_code")
            return 403, {"error": "bad_code", "message": "this pairing code is invalid or expired: make a new one in"
                                                          " the web UI (Add a machine)"}
        if outcome == "used":
            self._failed("used_code")
            self.state.hub.notice(None, "warn", f"a second machine tried {name}'s pairing code. If your machine says"
                                                " the code was already used, reject the pending machine and make a"
                                                " new code")
            return 409, {"error": "used", "message": "This code was already used by another machine. Don't approve the"
                                                      " pending machine: remove it in the web UI and make a new code."}
        try:
            self.state.store.machine_pair(name, key, fp, clean_facts(body.get("facts")))
        except Conflict:
            return 409, {"error": "conflict", "message": f"{name} is paired already: remove it in the web UI first"}
        self.state.store.add_event("machine", data={"what": "paired", "name": name, "key": fp})
        log.warning("machine %s paired (key %s), waiting for approval", name, fp)
        self.state.hub.notice(None, "warn", f"{name} paired, with the key {fp}. Before you approve it, check that"
                                            " this is the key remote join printed on your machine")
        self.changed()
        return 200, {"name": name, "fingerprint": fp, "broker_key": linkkey.b64u(self.bkey),
                     "broker_fingerprint": self.fingerprint, "link_url": self.link_url}

    async def serve(self, ws: "WebSocket") -> None:
        """``GET /link`` (the WebSocket), after its Host and no-Origin checks: the handshake, a
        wait for approval, then the link, bridged to a ``MachineLink``."""
        await ws.accept()
        try:
            row = await self._handshake(ws)
            name = row.name
            self.seen(name)
            row = self.state.store.machine(name) or row
            if row.removed:
                raise _End(linkkey.refuse("removed", REFUSE_TEXT["removed"]), "removed")
            if row.pending and not await self._wait_approval(ws, name):
                return
            row = self.state.store.machine(name)
            if row is None or not row.approved:
                raise _End(linkkey.refuse("removed", REFUSE_TEXT["removed"]), "removed")
            await ws.send_text('{"t":"approved"}')
            await self._relay(ws, self._link(row))
        except _End as e:
            if e.code not in ("gone", "timeout"):
                log.info("machines: a /link connection ended: %s", e.code)
            await self._close(ws, e.frame)
        except Exception:
            log.exception("machines: a /link connection failed")
            await self._close(ws, None)

    async def _close(self, ws: "WebSocket", frame: dict[str, Any] | None) -> None:
        with contextlib.suppress(Exception):
            if frame is not None:
                await ws.send_text(json.dumps(frame, separators=(",", ":")))
        with contextlib.suppress(Exception):
            await ws.close(code=1000 if frame is None else 1008)

    async def _recv(self, ws: "WebSocket", timeout: float | None) -> dict[str, Any]:
        try:
            msg = await (asyncio.wait_for(ws.receive(), timeout) if timeout is not None else ws.receive())
        except (asyncio.TimeoutError, TimeoutError):
            raise _End(linkkey.refuse("protocol", "no answer within 5 s"), "timeout") from None
        if msg.get("type") == "websocket.disconnect":
            raise _End(None, "gone")
        text = msg.get("text")
        if not isinstance(text, str) or len(text) > linkkey.HANDSHAKE_MAX:
            raise _End(linkkey.refuse("protocol", "a handshake frame is text of at most 4 KiB"), "oversize")
        try:
            obj = proto.loads(text)
        except ValueError:
            raise _End(linkkey.refuse("protocol", "not JSON"), "bad_json") from None
        if not isinstance(obj, dict):
            raise _End(linkkey.refuse("protocol", "not a frame"), "bad_frame")
        return obj

    async def _handshake(self, ws: "WebSocket") -> MachineRow:
        try:
            name, key, nm = linkkey.check_auth(await self._recv(ws, linkkey.HANDSHAKE_TIMEOUT_S))
        except linkkey.HandshakeError as e:
            raise _End(linkkey.refuse("protocol", f"a bad auth frame ({e.code})"), e.code) from None
        row = self.state.store.machine(name)
        if row is None or row.removed or not hmac.compare_digest(row.key, key):
            self._failed("unknown_key")
            raise _End(linkkey.refuse("unknown", REFUSE_TEXT["unknown"]), "unknown")
        nb = secrets.token_bytes(linkkey.NONCE_BYTES)
        sig = linkkey.sign(self.key, "broker", self.host, nm, nb)
        await ws.send_text(json.dumps({"t": "challenge", "nb": linkkey.b64u(nb), "bkey": linkkey.b64u(self.bkey),
                                       "sig": sig}, separators=(",", ":")))
        try:
            proof = linkkey.check_proof(await self._recv(ws, linkkey.HANDSHAKE_TIMEOUT_S))
        except linkkey.HandshakeError as e:
            raise _End(linkkey.refuse("protocol", f"a bad proof frame ({e.code})"), e.code) from None
        if not linkkey.verify(row.key, proof, "machine", self.host, nb, nm):
            self._failed("bad_proof")
            raise _End(linkkey.refuse("proof", REFUSE_TEXT["proof"]), "proof")
        return row

    async def _wait_approval(self, ws: "WebSocket", name: str) -> bool:
        """A pending machine waits here, its WebSocket open (the server's pings keep it so), until
        the owner approves it (True) or removes it, or it goes away (False)."""
        old = self.waiting.get(name)
        if old is not None:
            old.decide("replaced")
        w = _Waiter()
        self.waiting[name] = w
        self.changed()
        recv: asyncio.Future[Any] | None = None
        decided: asyncio.Future[Any] | None = None
        try:
            await ws.send_text('{"t":"pending"}')
            recv = asyncio.ensure_future(ws.receive())
            decided = asyncio.ensure_future(w.event.wait())
            done, _ = await asyncio.wait({recv, decided}, return_when=asyncio.FIRST_COMPLETED)
            if decided in done:
                if w.outcome == "approved":
                    return True
                await self._close(ws, linkkey.refuse(w.outcome, REFUSE_TEXT.get(w.outcome, w.outcome)))
                return False
            msg = recv.result()
            if msg.get("type") != "websocket.disconnect":
                await self._close(ws, linkkey.refuse("protocol", "nothing is sent while waiting for approval"))
            return False
        finally:
            for fut in (recv, decided):
                if fut is not None and not fut.done():
                    fut.cancel()
            if self.waiting.get(name) is w:
                del self.waiting[name]
            self.seen(name)
            self.save_seen(name)
            self.changed()

    async def _relay(self, ws: "WebSocket", link: MachineLink) -> None:
        """The approved link: the WebSocket's text messages to and from one end of a socketpair,
        one frame each way per message; the link reads and writes the other end."""
        mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            reader, writer = await asyncio.open_unix_connection(sock=mine, limit=proto.MAX_FRAME + 1)
            p_reader, p_writer = await asyncio.open_unix_connection(sock=theirs, limit=proto.MAX_FRAME + 2)
        except OSError:
            mine.close()
            theirs.close()
            raise _End(None, "socketpair") from None
        serving = asyncio.ensure_future(link.serve(reader, writer))
        up = asyncio.ensure_future(self._machine_to_link(ws, p_writer))
        down = asyncio.ensure_future(self._link_to_machine(p_reader, ws))
        try:
            await asyncio.wait({serving, up, down}, return_when=asyncio.FIRST_COMPLETED)
            if not serving.done():
                # the machine went away: end of file for the link, as when an ssh child exits
                with contextlib.suppress(Exception):
                    p_writer.close()
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(serving), 10.0)
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(down), 1.0)  # the link's last frames (a refuse)
        finally:
            for t in (up, down, serving):
                if not t.done():
                    t.cancel()
            await asyncio.gather(up, down, serving, return_exceptions=True)
            for w in (p_writer, writer):
                with contextlib.suppress(Exception):
                    w.close()
            self.seen(link.name)
            self.save_seen(link.name)
            with contextlib.suppress(Exception):
                await ws.close(code=1000)

    async def _machine_to_link(self, ws: "WebSocket", w: asyncio.StreamWriter) -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            if not isinstance(text, str) or "\n" in text or len(text) > proto.MAX_FRAME + 1:
                return  # a frame no line can carry: the link ends, as with a malformed frame
            w.write(text.encode("utf-8") + b"\n")
            await w.drain()

    async def _link_to_machine(self, r: asyncio.StreamReader, ws: "WebSocket") -> None:
        while True:
            line = await r.readline()
            if not line:
                return
            await ws.send_text(line.rstrip(b"\n").decode("utf-8", "replace"))
