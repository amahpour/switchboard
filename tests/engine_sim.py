"""A seeded random simulation of the five harness kinds around the real delivery
engine, for the M6 property test (DESIGN.md §13): random interleavings of status,
message, sink and command events, then quiescence, then the invariant.

Everything runs on a FakeClock against a real Store and Engine with the real
adapters (no broker, no I/O, no asyncio). The simulated harnesses act the way
the broker would call the engine: hooks through ``claim_for_hook``, ``wait()``
through ``open_wait``, pushes through a stand-in for the runner, the Claude
registry poller and the Codex status link by hand.

Invariant (DESIGN.md §13 M6): after quiescence, no delivery of an active
membership is ``offered``, or ``pending`` and wake-eligible (not a notified,
pull-only stub). Quiescence: the clock is past ``max_hold_s`` plus the largest
TTL (the offer backstop), the rooms are unpaused, the budget covers what is
pending, and every member is idle with a push path or an open sink.

Safety rules checked all along (not only at the end): the pause rules after
every random event; at every batch, nothing but a pull for a session on an
approval prompt (or offline), and no wake for an @mention the watchdog has
escalated; after every event and at the end, at most ``watchdog_max``
reminders per delivery, and an escalated @mention never wake-eligible again.

pass() follows the read-first rule (DESIGN.md §24): refused while the member has
a peer message it saw only as a stub. A random pass that is refused does
nothing; a cooperative agent (quiescence) reads, then passes, and the simulator
asserts that the read() lifted the rule unless read() hit its limit.

Remote members (DESIGN.md §27, M8c): some members of the hook- and wait()-reached
kinds (test, devin, cursor) live on a remote host, ``pi``. A ``link_drop`` takes
every member of that host offline at once, as the broker does when a link goes
down (their connections close: open waits and parks end, pull answers expire,
status offline; nobody is ended), and while it is down they can do nothing. A
``link_up`` brings them back as a reconnecting MCP server does (``starting``).
Quiescence starts with the link up.

Remote Claude members (M8d) are reached by their inbox over the link: their registry
reaches the adapter as the link's relayed view (``ClaudeAdapter.relay``, every watched
remote Claude at once, only while the link is up; the views are forgotten when it
drops, and the MCP servers attach again when it is back), and a frame to one passes
the satellite's last-mile check: pushed when the session is no longer what the frame
was routed for (``idle`` for a wake, ``busy`` for a priority batch), it is dropped
after a fresh relayed view and re-routed, uncounted (``stale_status``).
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import FakeClock
from engine_world import World

from switchboard import envelope
from switchboard.adapters.claude import ClaudeAdapter, registry_transition
from switchboard.adapters.codex import Clients, CodexAdapter
from switchboard.adapters.devin import WAIT_TOOL
from switchboard.broker.hosts import Relayed
from switchboard.config import Config
from switchboard.models import WATCHDOG_DONE, Action, HookEvent, HookOut, Notice, Push, Room, session_key

KINDS = ("test", "devin", "claude", "cursor", "codex")
SINGLE_ROOM = ("test", "devin")  # wait()-based: one open wait() per session, so one room
# kinds that may live on the remote host: those reached by hooks and wait() (M8c), and Claude
# with its inbox over the link (M8d); remote Codex is pull-only and not simulated here
REMOTE_KINDS = ("test", "devin", "cursor", "claude")
PI = "pi"
BIG_BUDGET = 100_000


class FakeConn:
    closed = False

    def __init__(self) -> None:
        self.pushes: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, data: dict[str, Any]) -> None:
        self.pushes.append((kind, data))


@dataclass
class Mem:
    kind: str
    name: str
    pid: int
    mids: list[int]
    conn: int
    tid: str = ""
    wid: str | None = None  # last wait() id
    sink: int | None = None  # last wait() sink
    sink_used: bool = False  # its result was acted on
    tuid: str = ""  # Devin: the wait() call's tool_use_id
    park: int | None = None  # Cursor: the stop hook's park
    park_used: bool = False
    gen: int = 0
    loop: int = 0  # Cursor loop_count
    reg: str = "idle"  # Claude registry status
    view: str = "idle"  # Codex thread status
    mode: str = "prompting"
    attached: bool = True
    iconn: FakeConn = field(default_factory=FakeConn)
    shown: list[tuple[int, str]] = field(default_factory=list)  # tokens in tool results, not yet in a hook
    acks: list[tuple[int, str]] = field(default_factory=list)  # hook outputs not acked yet
    cont: tuple[int, str] | None = None  # Devin: a printed Stop block awaiting its next hook
    n: int = 0
    lazy: bool = False  # in quiescence: takes every delivery but never answers (the watchdog's case)
    host: str = ""  # '' on the broker's machine, PI on the remote host
    to_read: set[int] = field(default_factory=set)  # lazy: memberships with an escalated @mention to read()


class _Runner:
    """What the Claude adapter's relay path reads of the broker's runner."""

    def __init__(self, sim: "Sim") -> None:
        self.state = type("S", (), {"store": sim.store, "engine": sim.engine, "clock": sim.clock})()
        self.execute = sim.run


class Sim:
    def __init__(self, tmp_path: Path, seed: int):
        self.seed = seed
        r = self.rng = random.Random(seed)
        cfg = Config(human_name="alice").with_delivery(
            quiet_s=r.choice([0.0, 3.0]),
            max_hold_s=r.choice([0.0, 20.0, 60.0]),
            batch_max_msgs=r.choice([2, 5, 20]),
            budget_per_hour=r.choice([4, 60]),
            hop_limit=r.choice([0, 3, 6]),
            watchdog_s=r.choice([30.0, 120.0]),
            watchdog_max=r.choice([0, 1, 2]),
        )
        self.clock = FakeClock()
        self.w = World(tmp_path, self.clock, cfg)
        self.engine = self.w.engine
        self.store = self.w.store
        self.rooms: list[Room] = [self.w.room]
        if r.random() < 0.35:
            d = cfg.delivery
            self.rooms.append(self.store.create_room("#side", "alice", d.budget_per_hour, d.hop_limit))
        self.claude = self.engine.adapters["claude"]
        self.codex = self.engine.adapters["codex"]
        assert isinstance(self.claude, ClaudeAdapter) and isinstance(self.codex, CodexAdapter)
        self.claude.clock = self.clock
        self.claude.runner = _Runner(self)
        self.codex.clock = self.clock
        self.codex.link_state = "up"
        self.inflight: dict[int, Push] = {}
        self.notices: list[Notice] = []
        self.trace: list[str] = []
        self.kicked = False
        self.just_paused: set[int] = set()
        # room id -> the last batch id when it was paused (by /pause or the loop guard)
        self.paused_after: dict[int, int] = {}
        set_paused = self.store.set_paused

        def track(room_id: int, paused: bool, reason: str | None = None) -> Room:
            if paused and not self.store.room_by_id(room_id).paused:
                self.paused_after[room_id] = self._last_batch()
            return set_paused(room_id, paused, reason)

        self.store.set_paused = track  # type: ignore[method-assign]
        # safety rules checked at every batch and watchdog escalation (see the module doc)
        self.bad: list[str] = []
        self.escalated: set[tuple[int, int]] = set()  # (membership_id, message_id)
        create_batch, watchdog_done = self.store.create_batch, self.store.watchdog_done

        def checked_batch(membership_id: int, **kw: Any) -> Any:
            if kw.get("kind") != "pull":
                self._check_batch(membership_id, kw)
            return create_batch(membership_id, **kw)

        def escalate(membership_id: int, message_ids: Any, *, keep_count: bool) -> int:
            ids = list(message_ids)
            if keep_count:  # the escalation to the human
                self.escalated.update((membership_id, i) for i in ids)
                for mem in self.members:  # a lazy agent will read() it, and still not answer
                    if mem.lazy and membership_id in mem.mids:
                        mem.to_read.add(membership_id)
            return watchdog_done(membership_id, ids, keep_count=keep_count)

        self.store.create_batch = checked_batch  # type: ignore[method-assign]
        self.store.watchdog_done = escalate  # type: ignore[method-assign]
        self.members: list[Mem] = []
        self.link_up = True  # the remote host's link
        kinds = [r.choice(KINDS) for _ in range(r.randint(2, 5))]
        for i, kind in enumerate(kinds):
            host = PI if kind in REMOTE_KINDS and r.random() < 0.4 else ""
            self._add(kind, f"{kind[:3]}{i}", i, host)

    # ------------------------------------------------------------- set-up
    def _add(self, kind: str, name: str, i: int, host: str = "") -> None:
        r = self.rng
        status = {"test": "busy", "devin": "busy", "claude": "idle", "cursor": "busy", "codex": "idle"}[kind]
        p, m = self.w.agent(name, harness=kind, status=status, hooks=kind != "test", host=host)
        mids = [m.id]
        if len(self.rooms) > 1 and kind not in SINGLE_ROOM and r.random() < 0.7:
            mids.append(self.store.create_membership(self.rooms[1].id, p.id, name, f"h2-{name}").id)
        mem = Mem(kind=kind, name=name, pid=p.id, mids=mids, conn=1000 + i, lazy=r.random() < 0.3, host=host)
        if kind == "claude":
            mem.mode = r.choice(["prompting", "bypass"])
            self.store.update_participant(p.id, claude_socket=f"/tmp/yk-sim-{i}.sock", approval_mode=mem.mode)
            self.claude.attach(p.mcp_pid, p.mcp_start, mem.iconn, host=host)
        elif kind == "cursor":
            self.store.update_participant(
                p.id, session_key=session_key("cursor", host, f"conv-{name}"), bind_state="bound"
            )
        elif kind == "codex":
            mem.tid = f"019a0000-0000-7000-8000-{i:012d}"
            self.store.update_participant(
                p.id, session_key=f"codex:{mem.tid}", thread_proof=1, approval_mode="prompting"
            )
            self.codex.loaded.add(mem.tid)
            self.codex.view[mem.tid] = ("idle", self.clock.now())
        self.members.append(mem)
        self.refresh()
        if kind == "cursor":
            self.hook(mem, "stop", status="completed", loop_count=0, max_wait_s=630.0)

    # ------------------------------------------------------------ plumbing
    def run(self, acts: list[Action]) -> None:
        for a in acts:
            if isinstance(a, Push):
                self.inflight[a.batch_id] = a
            elif isinstance(a, Notice):
                self.notices.append(a)

    def p(self, mem: Mem) -> Any:
        return self.store.get_participant(mem.pid)

    def active(self) -> list[Mem]:
        return [m for m in self.members if m.mids]

    def reachable(self) -> list[Mem]:
        """Active members that can act now: a remote host's members can't while its link is down."""
        return [m for m in self.active() if self.link_up or not m.host]

    # ------------------------------------------------------------- the link
    def link_drop(self) -> None:
        """The remote host's link goes down: what AgentService.conn_closed does for each of
        its connections (waits and parks end, pull answers expire, offline), for all at once."""
        self.link_up = False
        for mem in self.active():
            if not mem.host:
                continue
            self.run(self.engine.close_conn_sinks(mem.conn))
            p = self.p(mem)
            acts = self.engine.expire_pull_batches(p.id, "disconnect")
            acts += self.engine.set_status(p, "offline", "mcp:bye")
            self.run(acts)
            mem.sink = mem.park = None
            mem.acks.clear()
            mem.cont = None
            mem.shown.clear()
            if mem.kind == "claude":  # its inbox channel was that connection
                self.claude.detach(mem.iconn)
                mem.iconn = FakeConn()
                mem.attached = False
        self.claude.forget_host(PI)  # the relayed views die with the link

    def link_back(self) -> None:
        """Up again: each MCP server reconnects and says hello (AgentService.hello); a
        Claude's attaches its inbox again; the first watch brings a relayed view."""
        self.link_up = True
        for mem in self.active():
            if mem.host and self.p(mem).status == "offline":
                self.run(self.engine.set_status(self.p(mem), "starting", "mcp:hello"))
            if mem.host and mem.kind == "claude" and not mem.attached:
                p = self.p(mem)
                self.claude.attach(p.mcp_pid, p.mcp_start, mem.iconn, host=PI)
                mem.attached = True
                self.run(self.engine.evaluate_participant(mem.pid))
        self.relay()

    def room_of(self, mid: int) -> Room:
        m = self.store.get_membership(mid)
        room = self.store.room_by_id(m.room_id)
        assert room is not None
        return room

    def token(self, bid: int) -> tuple[int, str]:
        b = self.store.get_batch(bid)
        t = envelope.TOKEN_RE.fullmatch(self.engine.token(b))
        assert t is not None
        return int(t.group(1)), t.group(2)

    def hook(self, mem: Mem, event: str, **kw: Any) -> HookOut | None:
        if not mem.mids:
            return None
        p = self.p(mem)
        if mem.kind == "codex":
            kw.setdefault("sid", mem.tid)
        ev = HookEvent(harness=mem.kind, event=event, t=self.clock.now(), **kw)
        out, acts = self.engine.claim_for_hook(p, ev)
        self.run(acts)
        if out is None:
            return None
        if out.kind == "context" and out.batch_id is not None and out.ack:
            mem.acks.append((out.batch_id, out.ack))
        elif out.kind == "continue" and out.batch_id is not None and out.ack:
            mem.cont = (out.batch_id, out.ack)
        elif out.kind == "park" and out.sink_id is not None:
            mem.park, mem.park_used = out.sink_id, False
            s = self.engine.sinks.get(out.sink_id)
            if s is not None and s.open:
                s.conn_id = mem.conn  # as the broker's hook RPC does
        return out

    def ack_all(self, mem: Mem) -> bool:
        did = bool(mem.acks)
        for bid, ack in mem.acks:
            self.run(self.engine.on_hook_ack(bid, ack))
        mem.acks.clear()
        return did

    def before_call(self, mem: Mem) -> None:
        self.run(self.engine.before_call(self.p(mem)))
        # an agent call confirms the session's acked stop continuations (§24): none is left
        # offered, so a stub it carried is pending for this call's read()/pass()
        left = [
            bid
            for bid, c in self.engine.continues.items()
            if c.participant_id == mem.pid and c.acked_at is not None
        ]
        assert not left, f"seed {self.seed}: {mem.name}'s call left acked continuations {left} offered"

    def show_tokens(self, mem: Mem) -> tuple[tuple[int, str], ...]:
        toks = tuple(mem.shown)
        mem.shown.clear()
        return toks

    # ---------------------------------------------------- adapters' loops
    def claude_poll(self, mem: Mem) -> None:
        """What ClaudeAdapter.poll_once does for this session's registry file (a remote
        session's: the next relayed view)."""
        if mem.host:
            self.relay()
            return
        p = self.p(mem)
        if p is None or not p.active or not mem.mids:
            return
        view, changed = self.claude.observe(
            p.agent_pid, {"pid": p.agent_pid, "status": mem.reg}, self.clock.now()
        )
        tr = registry_transition(p.status, p.hooks_seen_at, view, self.clock.now())
        if tr is not None:
            self.run(self.engine.set_status(p, tr[0], "claude:registry", bump=tr[1]))
        elif changed:
            self.run(self.engine.evaluate_participant(p.id))

    def relay(self) -> None:
        """A ``reg`` frame of the remote host's link: every watched remote Claude's registry,
        read on that host now (``ClaudeAdapter.relay``). Nothing while the link is down."""
        if not self.link_up:
            return
        now = self.clock.now()
        got: dict[tuple[int, float], Relayed] = {}
        for m in self.active():
            if m.host and m.kind == "claude":
                p = self.p(m)
                got[(p.agent_pid, p.agent_start)] = Relayed(status=m.reg, since=None, read_at=now)
        self.run(self.claude.relay(PI, got, now))

    def codex_view(self, mem: Mem, st: str) -> None:
        """A thread/status/changed notification (CodexAdapter._set_view + _apply_status)."""
        mem.view = st
        self.codex.view[mem.tid] = (st, self.clock.now())
        p = self.p(mem)
        if p is None or not p.active or not mem.mids:
            return
        if p.status == st:
            self.run(self.engine.evaluate_participant(p.id))
            return
        bump = st == "idle" and p.status in ("busy", "waiting-approval")
        self.run(self.engine.set_status(p, st, "codex:status", bump=bump))

    def refresh(self) -> None:
        now = self.clock.now()
        n = sum(1 for m in self.members if m.kind == "codex")
        self.codex.loaded_at = now
        self.codex.clients = Clients(True, now, max(1, n), None)
        for mem in self.active():
            if mem.kind == "claude":
                self.claude_poll(mem)

    def advance(self, dt: float) -> None:
        self.clock.advance(dt)
        self.refresh()
        self.run(self.engine.tick())

    # ----------------------------------------------------------- the runner
    def deliver(self, bid: int, outcome: str) -> bool:
        """The runner's _push, with the transport's outcome chosen here."""
        push = self.inflight.pop(bid, None)
        if push is None:
            return False
        b = self.store.get_batch(bid)
        if b is None or b.state != "offered" or b.posted_at is not None:
            return False  # cancelled (/pause) or settled before the transport got it
        mem = next((m for m in self.members if m.pid == push.participant_id), None)
        if mem is None or not mem.mids:
            return False
        self.store.mark_posted(bid)
        p = self.p(mem)
        adapter = self.engine.adapter(p)
        if outcome == "fail":
            adapter._failed(p)
            self.run(self.engine.on_expire(bid, "send_error"))
            return True
        if outcome == "lost" and push.path == "inbox":
            return True  # posted, never lands: the idle expiry takes it back
        if outcome == "lost":  # a Codex RPC either answers or fails
            adapter._failed(p)
            self.run(self.engine.on_expire(bid, "send_error"))
            return True
        tok = self.token(bid)
        if push.path == "inbox":
            want = "busy" if b.kind == "priority" else "idle"
            if mem.host and mem.attached and mem.reg != want:
                # the satellite's last-mile check: a fresh relayed view, then stale_status
                self.trace.append(f"stale:{bid}")
                self.relay()
                adapter._rerouted(p)
                self.run(self.engine.on_expire(bid, "reroute", count_failure=False))
                return True
            if not mem.attached or mem.reg == "waiting":
                return True  # never lands: the idle expiry takes it back
            if mem.reg == "idle":
                mem.gen += 1
                mem.reg = "busy"
                self.claude_poll(mem)
            self.hook(
                mem, "UserPromptSubmit", gen=f"c{mem.gen}", tokens=(tok,), permission_mode=self._pm(mem)
            )
            return True
        if push.path in ("turn_start", "steer"):
            want = "idle" if push.path == "turn_start" else "busy"
            if mem.view != want:  # the fresh status check before the RPC: a re-route
                self.codex._rerouted(p)
                self.run(self.engine.on_expire(bid, "reroute", count_failure=False))
                return True
            self.codex.reroutes.pop(p.id, None)
            self.codex.backoff.pop(p.id, None)
            if push.path == "turn_start":
                self.run(self.engine.on_confirm(bid, "rpc:turn/start"))
                mem.gen += 1
                self.codex_view(mem, "busy")
            self.hook(mem, "UserPromptSubmit", gen=f"x{mem.gen}", tokens=(tok,))
            return True
        raise AssertionError(f"unexpected push path {push.path}")

    @staticmethod
    def _pm(mem: Mem) -> str:
        return "bypassPermissions" if mem.mode == "bypass" else "default"

    # ------------------------------------------------------ shared actions
    def human(self, room: Room | None = None) -> None:
        r = self.rng
        room = room or r.choice(self.rooms)
        names = self.store.active_names(room.id)
        mentions = tuple(r.sample(names, k=min(len(names), r.choice([0, 0, 1, 2]))))
        text = " ".join(f"@{n}" for n in mentions) + f" human {r.randint(0, 999)}"
        msg = self.store.insert_message(
            room.id,
            sender_name="alice",
            sender_kind="human",
            via=r.choice(["web", "cli"]),
            text=text,
            mentions=mentions,
        )
        self.run(self.engine.on_message(msg.id))

    def say(self, mem: Mem) -> None:
        """AgentService.say: the rate limit, handled, post, then the unread as a say() pull."""
        r = self.rng
        mid = r.choice(mem.mids)
        self.before_call(mem)
        p, m = self.p(mem), self.store.get_membership(mid)
        room = self.room_of(mid)
        recent = self.store.history(room.id, limit=8)
        chats = [x for x in recent if x.kind == "chat"]
        target = r.choice(chats) if chats and r.random() < 0.5 else None
        if self.engine.check_say(p, m, target) is None:
            self.store.mark_handled(m.id)
            self.store.update_participant(p.id, last_say_at=self.clock.now())
            names = [n for n in self.store.active_names(room.id) if n != m.screen_name]
            mentions = tuple(r.sample(names, k=min(len(names), r.choice([0, 0, 1]))))
            msg = self.store.insert_message(
                room.id,
                sender_name=m.screen_name,
                sender_kind="agent",
                via="mcp",
                text=" ".join(f"@{n}" for n in mentions) + f" agent {r.randint(0, 999)}",
                sender_membership_id=m.id,
                sender_harness=p.harness,
                reply_to=target.id if target else None,
                mentions=mentions,
            )
            self.run(self.engine.on_message(msg.id))
            _t, bid, _c, _more, acts = self.engine.pull(self.p(mem), m, "say", 50, before_id=msg.id)
        else:
            _t, bid, _c, _more, acts = self.engine.pull(p, m, "say", 50)
        self.run(acts)
        if bid is not None:
            mem.shown.append(self.token(bid))

    def pass_(self, mem: Mem, mid: int | None = None, *, read_first: bool = False) -> None:
        """AgentService.pass_: refused, handling nothing, while the member has a peer
        message it saw only as a stub (the read-first rule, DESIGN.md §24). With
        ``read_first`` the agent does what the refusal says: read(), then pass()."""
        self.before_call(mem)
        mid = mid if mid is not None else self.rng.choice(mem.mids)
        if self.engine.check_pass(self.p(mem), self.store.get_membership(mid)):
            self.trace.append(f"pass_refused:{mem.name}")
            if not read_first:
                return
            more = self.read_now(mem, mid)
            self.before_call(mem)
            if self.engine.check_pass(self.p(mem), self.store.get_membership(mid)):
                # read() lifts the rule for everything it shows: only its limit leaves some
                assert more, f"seed {self.seed}: read() did not lift the read-first rule for {mem.name}"
                return  # more than one read() holds: it passes on a later round
        m = self.store.get_membership(mid)
        n = self.store.mark_handled(mid)
        self.store.add_event(
            "pass", room_id=m.room_id, membership_id=mid, participant_id=mem.pid, data={"handled": n}
        )
        self.run(self.engine.evaluate(mid))

    def read_now(self, mem: Mem, mid: int) -> bool:
        """A read() whose answer lands before the agent's next call: the harness's
        PostToolUse for it (or, for the test agent, its next call) confirms it.
        Returns ``more`` (read() hit its limit)."""
        _t, bid, _c, more, acts = self.engine.pull(self.p(mem), self.store.get_membership(mid), "read", 50)
        self.run(acts)
        if bid is None:
            return more
        tok = self.token(bid)
        if mem.kind == "test":
            self.before_call(mem)
        elif mem.kind == "devin":
            mem.n += 1
            tu, g = f"r{mem.n}", f"d{mem.gen}"
            self.hook(mem, "PreToolUse", tool="read", tool_use_id=tu, gen=g)
            self.hook(mem, "PostToolUse", tool="read", tool_use_id=tu, ok=True, tokens=(tok,), gen=g)
        elif mem.kind == "claude":
            self.hook(mem, "PostToolUse", ok=True, tokens=(tok,), permission_mode=self._pm(mem))
        elif mem.kind == "codex":
            self.hook(mem, "PostToolUse", tool="mcp__switchboard__read", ok=True, tokens=(tok,))
        else:
            self.hook(mem, "postToolUse", ok=True, tokens=(tok,))
        return more

    def read(self, mem: Mem, mid: int | None = None) -> None:
        self.before_call(mem)
        mid = mid if mid is not None else self.rng.choice(mem.mids)
        _t, bid, _c, _more, acts = self.engine.pull(
            self.p(mem), self.store.get_membership(mid), "read", self.rng.randint(1, 20)
        )
        self.run(acts)
        if bid is not None:
            mem.shown.append(self.token(bid))

    def open_wait(self, mem: Mem, secs: float) -> None:
        mid = mem.mids[0]
        mem.n += 1
        mem.wid = f"{mem.name}-w{mem.n}"
        if mem.kind == "devin":
            mem.tuid = f"call_{mem.name}_{mem.n}"
            self.hook(mem, "PreToolUse", tool=WAIT_TOOL, tool_use_id=mem.tuid, gen=f"d{mem.gen}")
        self.before_call(mem)
        sink, acts = self.engine.open_wait(
            self.p(mem), self.store.get_membership(mid), mem.wid, secs, conn_id=mem.conn
        )
        mem.sink, mem.sink_used = sink.id, False
        self.run(acts)

    def sink_result(self, mem: Mem) -> dict[str, Any] | None:
        s = self.engine.sinks.get(mem.sink) if mem.sink is not None else None
        return None if s is None or s.open else (s.result or {})

    def use_wait_result(self, mem: Mem, *, ok: bool = True) -> bool:
        """The agent receives its wait() result (the tool's PostToolUse carries the token)."""
        res = self.sink_result(mem)
        if res is None or mem.sink_used:
            return False
        mem.sink_used = True
        bid = res.get("batch_id")
        toks = (self.token(bid),) if isinstance(bid, int) else ()
        if mem.kind == "test":
            self.before_call(mem)  # --ack next_call
        elif mem.kind == "devin":
            self.hook(
                mem,
                "PostToolUse",
                tool=WAIT_TOOL,
                tool_use_id=mem.tuid,
                ok=ok,
                tokens=toks,
                gen=f"d{mem.gen}",
            )
        else:
            self.hook(
                mem,
                "PostToolUse",
                tool="mcp__switchboard__wait",
                ok=ok,
                tokens=toks,
                permission_mode=self._pm(mem),
            )
        return True

    # ------------------------------------------------------ safety checks
    def _check_batch(self, membership_id: int, kw: dict[str, Any]) -> None:
        """Before a non-pull batch is made: never for a session on an approval prompt
        (or offline), and never waking an @mention the watchdog escalated."""
        m = self.store.get_membership(membership_id)
        p = self.store.get_participant(m.participant_id)
        where = f"seed {self.seed}: a {kw.get('path')} {kw.get('kind')} batch for {m.screen_name}"
        if p.status in ("waiting-approval", "offline"):
            self.bad.append(f"{where} while it is {p.status}: {self.trace[-5:]}")
        rows = {d["message_id"]: d for d in self.store.deliveries(membership_id)}
        for message_id, _inline in kw.get("items", ()):
            d = rows.get(message_id)
            if (membership_id, message_id) in self.escalated and d is not None and d["notified_at"] is None:
                self.bad.append(f"{where} wakes it for escalated @mention {message_id}: {self.trace[-5:]}")

    def check_watchdog(self) -> list[str]:
        """At most watchdog_max reminders per delivery; escalated @mentions never
        wake-eligible again."""
        wmax = max(0, min(self.w.cfg.delivery.watchdog_max, WATCHDOG_DONE - 1))
        out = []
        for d in self.store.con.execute(
            "SELECT membership_id, message_id, state, notified_at, reminders"
            " FROM deliveries WHERE reminders>0"
        ):
            if d["reminders"] % WATCHDOG_DONE > wmax:
                out.append(
                    f"seed {self.seed}: {d['membership_id']}/{d['message_id']} had"
                    f" {d['reminders'] % WATCHDOG_DONE} reminders (max {wmax})"
                )
        for mid, message_id in self.escalated:
            st = self.store.con.execute(
                "SELECT state, notified_at FROM deliveries WHERE membership_id=? AND message_id=?",
                (mid, message_id),
            ).fetchone()
            if st is not None and st["state"] == "pending" and st["notified_at"] is None:
                out.append(f"seed {self.seed}: escalated @mention {mid}/{message_id} is wake-eligible again")
        return out

    # ---------------------------------------------------- the random phase
    def step(self) -> None:
        """One random event, then the safety checks."""
        before = self._last_batch()
        self._step()
        self.check_pause(before)
        bad = self.bad + self.check_watchdog()
        assert not bad, "\n".join(bad[:10])

    def _last_batch(self) -> int:
        return int(self.store.con.execute("SELECT COALESCE(MAX(id), 0) FROM batches").fetchone()[0])

    def check_pause(self, before: int) -> None:
        """/pause stops every wake at once: in a paused room nothing but an explicit
        pull (read/say) is offered, no wait() or unposted push stays pending from
        before the pause, and a Cursor park lives only while a room it serves is live."""
        rows = self.store.con.execute(
            "SELECT b.id, b.kind, b.path, m.room_id FROM batches b JOIN memberships m ON m.id=b.membership_id"
            " WHERE b.id>?",
            (before,),
        ).fetchall()
        for b in rows:
            room = self.store.room_by_id(b["room_id"])
            after = room.paused and b["id"] > self.paused_after.get(room.id, 0)
            assert not (after and b["kind"] != "pull"), (
                f"seed {self.seed}: a {b['path']} batch was offered in paused {room.name}: {self.trace[-5:]}"
            )
        for park in self.engine.sinks.parks():
            serves = self.store.participant_memberships(park.participant_id)
            assert any(not self.room_of(x.id).paused for x in serves), (
                f"seed {self.seed}: a Cursor stop is parked while every room it serves is"
                f" paused: {self.trace[-5:]}"
            )
        for room in self.rooms:
            r = self.store.room_by_id(room.id)
            if not r.paused or room.id not in self.just_paused:
                continue
            assert not self.engine.sinks.for_room(room.id), f"seed {self.seed}: a wait() outlived /pause"
            for m in self.store.room_memberships(room.id):
                stale = [b for b in self.store.offered_batches(m.id) if b.posted_at is None]
                assert not stale, f"seed {self.seed}: an unposted offer outlived /pause"
        self.just_paused.clear()

    def _step(self) -> None:
        r = self.rng
        kinds = [
            "human",
            "say",
            "pass",
            "read",
            "tick",
            "tick",
            "big_tick",
            "cmd",
            "push",
            "harness",
            "harness",
            "harness",
        ]
        weights = [10, 8, 4, 2, 10, 4, 1, 5, 8, 12, 12, 12]
        kinds = kinds + ["link"]
        weights = weights + [2 if any(m.host for m in self.members) else 0]
        what = r.choices(kinds, weights)[0]
        act = self.reachable()
        if what == "link":
            self.trace.append("link_drop" if self.link_up else "link_up")
            if self.link_up:
                self.link_drop()
            else:
                self.link_back()
        elif what == "human":
            self.human()
        elif what in ("say", "pass", "read") and act:
            mem = r.choice(act)
            if what == "read" and r.random() < 0.5:  # an agent looks at an escalated @mention
                mem = next((x for x in act if any(k[0] in x.mids for k in self.escalated)), mem)
            self.trace.append(f"{what}:{mem.name}")
            getattr(self, "pass_" if what == "pass" else what)(mem)
        elif what == "tick":
            self.advance(r.choice([0.1, 0.5, 1.0, 2.0, 3.0, 5.0, 11.0]))
        elif what == "big_tick":
            self.advance(r.choice([30.0, 70.0, 130.0, 200.0, 700.0]))
        elif what == "cmd":
            self.command()
        elif what == "push" and self.inflight:
            bid = r.choice(sorted(self.inflight))
            if not self.link_up and any(
                m.host and m.pid == self.inflight[bid].participant_id for m in self.members
            ):
                return  # no push reaches a remote member while its link is down
            self.trace.append(f"push:{bid}")
            self.deliver(bid, r.choices(["ok", "lost", "fail"], [7, 2, 1])[0])
        elif what == "harness" and act:
            mem = r.choice(act)
            getattr(self, f"ev_{mem.kind}")(mem)

    def command(self) -> None:
        r = self.rng
        room = r.choice(self.rooms)
        what = r.choices(["pause", "resume", "budget", "hold", "release", "kick"], [3, 4, 3, 1, 2, 0.2])[0]
        self.trace.append(f"cmd:{what}:{room.name}")
        if what == "pause":
            self.store.set_paused(room.id, True, "paused by alice")
            self.run(self.engine.on_command(room.id, "pause"))
            self.just_paused.add(room.id)
        elif what == "resume":
            self.store.set_paused(room.id, False)
            self.run(self.engine.on_command(room.id, "resume"))
        elif what == "budget":
            self.store.set_budget(room.id, r.choice([0, 1, 2, 5, 60]))
            self.run(self.engine.on_command(room.id, "budget"))
        elif what in ("hold", "release"):
            ms = self.store.room_memberships(room.id)
            if ms:
                m = r.choice(ms)
                self.store.set_held(m.id, what == "hold")
                self.run(self.engine.on_command(room.id, what, m.id))
        elif what == "kick" and not self.kicked and len(self.active()) > 2:
            ms = self.store.room_memberships(room.id)
            if ms:
                self.kicked = True
                m = r.choice(ms)
                self.store.end_membership(m.id, "kick", kicked=True)
                self.run(self.engine.on_membership_ended(m.id, "kick"))
                for mem in self.members:
                    if m.id in mem.mids:
                        mem.mids.remove(m.id)

    def ev_test(self, mem: Mem) -> None:
        r = self.rng
        what = r.choice(["wait", "wait", "use", "unwait", "drop"])
        self.trace.append(f"test:{what}:{mem.name}")
        if what == "wait":
            self.open_wait(mem, r.choice([5.0, 30.0, 600.0]))
        elif what == "use":
            self.use_wait_result(mem)
        elif what == "unwait" and mem.wid:
            self.run(self.engine.unwait(self.p(mem), mem.wid))
        elif what == "drop":
            self.run(self.engine.close_conn_sinks(mem.conn))

    def ev_devin(self, mem: Mem) -> None:
        r = self.rng
        what = r.choice(["wait", "wait", "use", "tool", "prompt", "stop", "stop", "ack", "subagent"])
        self.trace.append(f"devin:{what}:{mem.name}")
        g = f"d{mem.gen}"
        if what == "wait":
            self.open_wait(mem, r.choice([30.0, 600.0]))
        elif what == "use":
            self.use_wait_result(mem, ok=r.random() < 0.85)
        elif what == "tool":
            mem.n += 1
            tu = f"t{mem.n}"
            self.hook(mem, "PreToolUse", tool="read", tool_use_id=tu, gen=g)
            self.hook(
                mem, "PostToolUse", tool="read", tool_use_id=tu, ok=True, tokens=self.show_tokens(mem), gen=g
            )
        elif what == "prompt":
            mem.gen += 1
            self.hook(mem, "UserPromptSubmit", gen=f"d{mem.gen}")
        elif what == "stop":
            self.hook(mem, "Stop", gen=g)
            if mem.cont is not None and r.random() < 0.85:
                self.run(self.engine.on_hook_ack(*mem.cont))
        elif what == "ack":
            self.ack_all(mem)
        elif what == "subagent" and r.random() < 0.3:
            self.hook(mem, "PreToolUse", tool="run_subagent", tool_use_id="sub", subagent_bg=True, gen=g)

    def ev_claude(self, mem: Mem) -> None:
        r = self.rng
        what = r.choice(
            [
                "prompt",
                "tool",
                "tool",
                "toolfail",
                "stop",
                "esc",
                "approval",
                "answer",
                "ack",
                "attach",
                "wait",
                "use",
                "offline",
                "clear",
            ]
        )
        self.trace.append(f"claude:{what}:{mem.name}")
        pm = self._pm(mem)
        if what == "prompt":
            mem.gen += 1
            mem.reg = "busy"
            self.hook(mem, "UserPromptSubmit", gen=f"c{mem.gen}", permission_mode=pm)
            self.claude_poll(mem)
        elif what == "tool" and mem.reg == "busy":
            self.hook(mem, "PostToolUse", ok=True, tokens=self.show_tokens(mem), permission_mode=pm)
        elif what == "toolfail" and mem.reg == "busy":
            self.hook(mem, "PostToolUseFailure", ok=False, permission_mode=pm)
        elif what == "stop" and mem.reg == "busy":
            self.hook(mem, "Stop", permission_mode=pm)
            mem.reg = "idle"
            self.claude_poll(mem)
        elif what == "esc" and mem.reg == "busy":
            mem.reg = "idle"  # no Stop hook: the registry says it
            self.claude_poll(mem)
        elif what == "approval" and mem.reg == "busy":
            mem.reg = "waiting"
            self.claude_poll(mem)
        elif what == "answer" and mem.reg == "waiting":
            mem.reg = r.choice(["busy", "idle"])
            self.claude_poll(mem)
        elif what == "ack":
            self.ack_all(mem)
        elif what == "attach":
            p = self.p(mem)
            if mem.attached:
                self.claude.detach(mem.iconn)
                mem.iconn = FakeConn()
            else:
                self.claude.attach(p.mcp_pid, p.mcp_start, mem.iconn, host=mem.host)
            mem.attached = not mem.attached
            self.run(self.engine.evaluate_participant(mem.pid))
        elif what == "wait" and mem.reg == "busy":  # a tool call inside a turn
            self.open_wait(mem, r.choice([30.0, 110.0]))
        elif what == "use":
            self.use_wait_result(mem)
        elif what == "offline" and r.random() < 0.3:
            self.hook(mem, "SessionEnd", reason="prompt_input_exit")
            mem.reg = "idle"
        elif what == "clear":
            self.hook(mem, "SessionEnd", reason="clear")
            self.hook(mem, "SessionStart", source="clear")

    def ev_cursor(self, mem: Mem) -> None:
        r = self.rng
        what = r.choice(["prompt", "tool", "stop", "stop", "followup", "followup", "hookdie", "ack"])
        self.trace.append(f"cursor:{what}:{mem.name}")
        if what == "prompt":
            mem.gen += 1
            mem.loop = 0
            self.hook(mem, "beforeSubmitPrompt", gen=f"u{mem.gen}")
        elif what == "tool":
            self.hook(mem, "postToolUse", ok=r.random() < 0.9, tokens=self.show_tokens(mem))
        elif what == "stop":
            st = r.choices(["completed", "aborted", "error"], [6, 1, 1])[0]
            self.hook(mem, "stop", status=st, loop_count=mem.loop, max_wait_s=630.0)
        elif what == "followup":
            self.run_followup(mem, ack=r.random() < 0.85)
        elif what == "hookdie":
            self.run(self.engine.close_conn_sinks(mem.conn))
        elif what == "ack":
            self.ack_all(mem)

    def run_followup(self, mem: Mem, *, ack: bool) -> bool:
        """A parked stop hook was answered: it prints the follow-up and acks, and
        Cursor runs it as the next turn (loop_count + 1)."""
        s = self.engine.sinks.get(mem.park) if mem.park is not None else None
        if s is None or s.open or mem.park_used:
            return False
        mem.park_used = True
        res = s.result or {}
        if res.get("status") != "messages":
            return False
        if not ack:
            return True  # the hook died before acking: Cursor never runs it
        self.run(self.engine.on_hook_ack(res["batch_id"], res["ack"]))
        mem.loop += 1
        self.hook(mem, "postToolUse", ok=True)
        return True

    def ev_codex(self, mem: Mem) -> None:
        r = self.rng
        what = r.choice(["prompt", "tool", "tool", "stop", "approval", "answer", "ack"])
        self.trace.append(f"codex:{what}:{mem.name}")
        if what == "prompt" and mem.view == "idle":
            mem.gen += 1
            self.hook(mem, "UserPromptSubmit", gen=f"x{mem.gen}")
            self.codex_view(mem, "busy")
        elif what == "tool" and mem.view == "busy":
            self.hook(mem, "PostToolUse", tool="Bash", ok=True, tokens=self.show_tokens(mem))
        elif what == "stop" and mem.view == "busy":
            self.hook(mem, "Stop", gen=f"x{mem.gen}")
            self.codex_view(mem, "idle")
        elif what == "approval" and mem.view == "busy":
            self.codex_view(mem, "waiting-approval")
        elif what == "answer" and mem.view == "waiting-approval":
            if r.random() < 0.5:
                self.codex_view(mem, "busy")
            else:
                self.hook(mem, "Interrupt")
                self.codex_view(mem, "idle")
        elif what == "ack":
            self.ack_all(mem)

    # ------------------------------------------------------------ quiescence
    def quiesce(self) -> None:
        """Lift every cooldown, then let every agent cooperate (confirm what it is
        offered, answer with pass(), end its turn and listen again) while the
        clock runs past max_hold_s plus the largest TTL."""
        if not self.link_up:
            self.trace.append("link_up")
            self.link_back()
        for room in self.rooms:
            if self.store.room_by_id(room.id).paused:
                self.store.set_paused(room.id, False)
                self.run(self.engine.on_command(room.id, "resume"))
            for m in self.store.room_memberships(room.id):
                if m.held:
                    self.store.set_held(m.id, False)
                    self.run(self.engine.on_command(room.id, "release", m.id))
        d = self.w.cfg.delivery
        t_end = self.clock.now() + d.max_hold_s + d.offer_backstop_s + 5.0
        rounds = 0
        while self.clock.now() < t_end:
            rounds += 1
            assert rounds < 20000, "quiescence loop runaway"
            self.advance(self._dt(self.settle_round()))
        for _ in range(60):  # a few more cooperative rounds past the end
            acted = self.settle_round()
            self.advance(0.5 if acted else 5.0)
            if not acted and not self.violations():
                break

    def _dt(self, acted: bool) -> float:
        """Small steps while agents act; bigger ones while only timers can move things."""
        if acted:
            return 0.5
        return 5.0 if self.store.offered_batches() else 30.0

    def settle_round(self) -> bool:
        for room in self.rooms:  # "the budget is at least the number of pending items"
            if self.store.room_by_id(room.id).budget_remaining < BIG_BUDGET // 2:
                self.store.set_budget(room.id, BIG_BUDGET)
                self.run(self.engine.on_command(room.id, "budget"))
        busy = False
        for bid in sorted(self.inflight):
            busy |= self.deliver(bid, "ok")
        for mem in self.active():
            for mid in sorted(mem.to_read & set(mem.mids)):
                self.read(mem, mid)  # it reads the escalated @mention; its turn then ends unanswered
                if mem.kind == "test":
                    self.before_call(mem)  # its next call confirms the read (--ack next_call)
                busy = True
            mem.to_read.clear()
            busy |= getattr(self, f"settle_{mem.kind}")(mem)
            busy |= self.ack_all(mem)
            if not mem.lazy and any(self.store.in_context_items(mid) for mid in mem.mids):
                for mid in mem.mids:
                    self.pass_(mem, mid, read_first=True)
                busy = True
        return busy

    def answer(self, mem: Mem) -> None:
        """End of a turn: a cooperative agent passes (answers) first, reading any stub
        it was only told about when pass() says so; a lazy one doesn't."""
        if not mem.lazy:
            for mid in mem.mids:
                self.pass_(mem, mid, read_first=True)

    def settle_test(self, mem: Mem) -> bool:
        busy = self.use_wait_result(mem)
        s = self.engine.sinks.get(mem.sink) if mem.sink is not None else None
        if s is None or not s.open:
            self.open_wait(mem, 3600.0)
            busy = True
        return busy

    def settle_devin(self, mem: Mem) -> bool:
        busy = False
        if mem.shown:  # a read() in this turn: its PostToolUse confirms it
            mem.n += 1
            tu, g = f"t{mem.n}", f"d{mem.gen}"
            self.hook(mem, "PreToolUse", tool="read", tool_use_id=tu, gen=g)
            self.hook(
                mem, "PostToolUse", tool="read", tool_use_id=tu, ok=True, tokens=self.show_tokens(mem), gen=g
            )
            busy = True
        if self.p(mem).gen_tainted:
            mem.gen += 1
            self.hook(mem, "UserPromptSubmit", gen=f"d{mem.gen}")  # the human's next prompt
            busy = True
        if mem.cont is not None:
            self.run(self.engine.on_hook_ack(*mem.cont))
            mem.cont = None
            mem.n += 1
            self.hook(mem, "PreToolUse", tool="read", tool_use_id=f"t{mem.n}", gen=f"d{mem.gen}")
            busy = True
        busy |= self.use_wait_result(mem)
        s = self.engine.sinks.get(mem.sink) if mem.sink is not None else None
        if s is None or not s.open:
            self.open_wait(mem, 3600.0)
            busy = True
        return busy

    def settle_claude(self, mem: Mem) -> bool:
        busy = False
        p = self.p(mem)
        if not mem.attached:
            self.claude.attach(p.mcp_pid, p.mcp_start, mem.iconn, host=mem.host)
            mem.attached = True
            busy = True
        if p.status == "offline":
            self.hook(mem, "SessionStart", source="resume")
            busy = True
        if mem.reg == "waiting":
            mem.reg = "idle"  # the human declines the prompt
            self.claude_poll(mem)
            busy = True
        busy |= self.use_wait_result(mem)
        p = self.p(mem)
        if mem.reg == "busy" or p.status == "busy":
            if mem.shown:
                self.hook(
                    mem, "PostToolUse", ok=True, tokens=self.show_tokens(mem), permission_mode=self._pm(mem)
                )
            self.ack_all(mem)
            self.answer(mem)
            self.hook(mem, "Stop", permission_mode=self._pm(mem))
            mem.reg = "idle"
            self.claude_poll(mem)
            busy = True
        return busy

    def settle_cursor(self, mem: Mem) -> bool:
        busy = self.run_followup(mem, ack=True)
        s = self.engine.sinks.get(mem.park) if mem.park is not None else None
        if s is not None and s.open:
            return busy
        p = self.p(mem)
        if p.status != "busy":
            mem.gen += 1
            mem.loop = 0
            self.hook(mem, "beforeSubmitPrompt", gen=f"u{mem.gen}")  # the human pokes it
        if mem.shown:
            self.hook(mem, "postToolUse", ok=True, tokens=self.show_tokens(mem))
        self.ack_all(mem)
        self.answer(mem)
        self.hook(mem, "stop", status="completed", loop_count=mem.loop, max_wait_s=630.0)
        return True

    def settle_codex(self, mem: Mem) -> bool:
        busy = False
        if mem.view == "waiting-approval":
            self.hook(mem, "Interrupt")  # the human declines
            self.codex_view(mem, "idle")
            busy = True
        p = self.p(mem)
        if mem.view == "busy" or p.status == "busy":
            if mem.view != "busy":
                self.codex_view(mem, "busy")
            if mem.shown:
                self.hook(mem, "PostToolUse", tool="Bash", ok=True, tokens=self.show_tokens(mem))
            self.ack_all(mem)
            self.answer(mem)
            self.hook(mem, "Stop", gen=f"x{mem.gen}")
            self.codex_view(mem, "idle")
            busy = True
        return busy

    # ------------------------------------------------------------ invariant
    def violations(self) -> list[str]:
        """Deliveries of active memberships that are offered, or pending and wake-eligible;
        plus any safety rule broken on the way (quiescence included)."""
        out = self.bad + self.check_watchdog()
        for mem in self.active():
            for mid in mem.mids:
                for d in self.store.deliveries(mid):
                    if d["state"] == "offered" or (d["state"] == "pending" and d["notified_at"] is None):
                        out.append(
                            f"{mem.name}/{mid} msg {d['message_id']}: {d['state']}"
                            f" (prio {d['prio']}, attempts {d['attempts']}, reminders {d['reminders']})"
                        )
        return out

    def describe(self) -> str:
        lines = [f"seed {self.seed}: cfg {dataclasses.asdict(self.w.cfg.delivery)}"]
        for mem in self.members:
            p = self.p(mem)
            lines.append(
                f"  {mem.name} ({mem.kind}) status={p.status} tier={p.tier} reg={mem.reg} view={mem.view}"
                f" mids={mem.mids} parked={[self.engine.parked_reason(x) for x in mem.mids]}"
            )
        for room in self.rooms:
            r = self.store.room_by_id(room.id)
            lines.append(f"  {r.name} paused={r.paused} budget={r.budget_remaining} hops={r.hop_count}")
        lines.append("  last events: " + " ".join(self.trace[-25:]))
        return "\n".join(lines)
