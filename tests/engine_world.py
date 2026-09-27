"""A store + engine + FakeClock world for engine-level unit tests (no broker, no I/O)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from switchboard import db
from switchboard.adapters import build_adapters
from switchboard.config import Config
from switchboard.delivery.engine import Engine
from switchboard.delivery.sinks import SinkRegistry
from switchboard.models import Action, HookEvent, Membership, Message, Participant, ResolveSink
from switchboard.store import Store

KEY = b"k" * 32


class World:
    def __init__(self, tmp_path: Path, clock: Any, cfg: Config | None = None):
        self.clock = clock
        self.cfg = cfg or Config(human_name="alice")
        self.store = Store(db.open_db(tmp_path / "y.db"), clock)
        self.engine = Engine(self.store, clock, self.cfg, build_adapters(self.cfg), SinkRegistry(),
                             key=KEY, test_mode=True)
        d = self.cfg.delivery
        self.room = self.store.create_room("#build", "alice", d.budget_per_hour, d.hop_limit)
        self._pid = 50000
        self.actions: list[Action] = []

    # ------------------------------------------------------------ setup
    def agent(self, name: str, *, harness: str = "test", status: str = "busy", hooks: bool = False,
              ack: str = "next_call", room_id: int | None = None) -> tuple[Participant, Membership]:
        self._pid += 1
        p = self.store.upsert_participant(
            harness, f"{harness}:{name}", status=status, agent_pid=self._pid, agent_start=1.0,
            mcp_pid=self._pid + 100000, mcp_start=2.0, tier="mcp-only", session_id=f"sid-{name}",
        )
        if hooks:
            p = self.store.update_participant(p.id, hooks_seen_at=self.clock.now())
        if harness == "test":
            self.engine.ack_modes[p.id] = ack
        m = self.store.create_membership(room_id or self.room.id, p.id, name, "h-" + name)
        return p, m

    def p(self, p: Participant | int) -> Participant:
        pid = p if isinstance(p, int) else p.id
        got = self.store.get_participant(pid)
        assert got is not None
        return got

    def m(self, m: Membership | int) -> Membership:
        mid = m if isinstance(m, int) else m.id
        got = self.store.get_membership(mid)
        assert got is not None
        return got

    # ---------------------------------------------------------- traffic
    def human(self, text: str, mentions: tuple[str, ...] = ()) -> Message:
        msg = self.store.insert_message(self.room.id, sender_name="alice", sender_kind="human", via="web",
                                        text=text, mentions=mentions)
        self.actions += self.engine.on_message(msg.id)
        return msg

    def agent_says(self, m: Membership, text: str, mentions: tuple[str, ...] = ()) -> Message:
        p = self.p(m.participant_id)
        msg = self.store.insert_message(self.room.id, sender_name=m.screen_name, sender_kind="agent",
                                        via="mcp", text=text, sender_membership_id=m.id,
                                        sender_harness=p.harness, mentions=mentions)
        self.actions += self.engine.on_message(msg.id)
        return msg

    def hook(self, p: Participant, event: str, **kw: Any) -> Any:
        ev = HookEvent(harness=p.harness, event=event, t=kw.pop("t", self.clock.now()), **kw)
        out, acts = self.engine.claim_for_hook(self.p(p), ev)
        self.actions += acts
        return out

    # ---------------------------------------------------------- queries
    def states(self, m: Membership) -> dict[int, str]:
        return {d["message_id"]: d["state"] for d in self.store.deliveries(m.id)}

    def delivery(self, m: Membership, msg: Message) -> dict[str, Any]:
        for d in self.store.deliveries(m.id):
            if d["message_id"] == msg.id:
                return d
        raise AssertionError("no delivery")

    def resolved(self, sink_id: int | None = None) -> list[dict[str, Any]]:
        return [a.result for a in self.actions if isinstance(a, ResolveSink)
                and (sink_id is None or a.sink_id == sink_id)]

    def take(self) -> list[Action]:
        out, self.actions = self.actions, []
        return out
