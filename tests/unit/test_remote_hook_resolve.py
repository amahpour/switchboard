"""A remote hook resolves on its own host's chain, among its own host's members (DESIGN.md §27.5.5).

The chain is what the satellite sends (``facts.chain``: pids on that host and a
verdict each, no argv); the broker turns it into ProcInfo entries and an argv
reader of canonical argv, and runs the unchanged resolver. Remote pids here
deliberately collide with local rows' pids.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import time
from types import SimpleNamespace
from typing import Any

from conftest import InProcBroker

from switchboard.broker import proc
from switchboard.broker.agents import AgentService, cred_hash
from switchboard.broker.peer import HookCandidate, resolve_hook_participant
from switchboard.broker.remote import RemotePeer
from switchboard.models import Participant, session_key

PI = "fpga-pi"
A = (1000004242, 100.0)  # the remote agent (pid, start), as the satellite reports it
HOOK = (1000009000, 150.0)
_ids = itertools.count(880000)


class RConn:
    """A remote connection as the hook path sees it: a host, its link, this request's facts."""

    remote = True

    def __init__(self, chain: list[list[Any]] | None, host: str = PI, sat_test: bool = True):
        self.host = host
        self._facts = {"chain": [tuple(x) for x in chain]} if chain else {}
        self.link = SimpleNamespace(sat_test_mode=sat_test, name=host)
        self.id = next(_ids)
        self.mcp = None
        self.closed = False
        self.peer = RemotePeer(pid=None, uid=None, start=None, host=host)

    @property
    def facts(self) -> dict[str, Any]:
        return self._facts

    def send(self, obj: Any) -> None:
        pass


def mk(
    b: InProcBroker, harness: str, host: str, rest: str, name: str, agent: tuple[int, float] = A, **kw: Any
) -> Participant:
    def f() -> Participant:
        st = b.state.store
        room = st.get_room("#fpga") or st.create_room("#fpga", "alice", 60, 6)
        p = st.upsert_participant(
            harness,
            session_key(harness, host, rest),
            host=host,
            agent_pid=agent[0],
            agent_start=agent[1],
            mcp_pid=agent[0] + 1,
            mcp_start=agent[1] + 1,
            status="busy",
            **kw,
        )
        st.create_membership(room.id, p.id, name, cred_hash(secrets.token_urlsafe(8)))
        b.state.agents.refresh_index()
        return p

    return b.on_loop(f)


def hook(
    b: InProcBroker, conn: Any, harness: str, event: str = "PostToolUse", **params: Any
) -> dict[str, Any]:
    ev = {"harness": harness, "event": event, **params}
    fut = asyncio.run_coroutine_threadsafe(b.state.agents.hook_event(conn, ev), b.loop)
    return fut.result(10)


def seen(b: InProcBroker, p: Participant) -> bool:
    got = b.on_loop(b.state.store.get_participant, p.id)
    return got.hooks_seen_at is not None


def test_own_host_member_resolves(broker: InProcBroker) -> None:
    p = mk(broker, "claude", PI, f"{A[0]}@{A[1]:.2f}", "bench")
    chain = [[*HOOK, "-"], [A[0] + 7, 120.0, "-"], [*A, "claude"]]  # hook <- bash <- claude
    hook(broker, RConn(chain), "claude", sid="s-1")
    assert seen(broker, p)


def test_desktop_member_never_candidate_for_remote_hook(broker: InProcBroker) -> None:
    local = mk(broker, "claude", "", f"{A[0]}@{A[1]:.2f}", "vivado")  # the same pid numbers, this machine
    chain = [[*HOOK, "-"], [*A, "claude"]]
    hook(broker, RConn(chain), "claude")
    assert not seen(broker, local)
    # another remote host naming the same pids doesn't reach fpga-pi's row either
    rp = mk(broker, "claude", PI, f"{A[0]}@{A[1]:.2f}", "bench")
    hook(broker, RConn(chain, host="other-pi"), "claude")
    assert not seen(broker, rp) and not seen(broker, local)
    hook(broker, RConn(chain), "claude")
    assert seen(broker, rp) and not seen(broker, local)


def test_local_hook_never_reaches_remote_row(broker: InProcBroker) -> None:
    me = proc.info(os.getpid())
    assert me is not None
    # the remote row names this very process's pid and start: a local hook from here is ours, not its
    rp = mk(broker, "test", PI, "bench", "bench", agent=(me.pid, me.start))
    assert broker.call("hook.event", {"harness": "test", "event": "UserPromptSubmit", "t": time.time()}) == {
        "out": None
    }
    assert not seen(broker, rp)
    # the same row's hook through the link resolves
    hook(broker, RConn([[*HOOK, "-"], [me.pid, me.start, "-"]]), "test", "UserPromptSubmit")
    assert seen(broker, rp)


def test_unreadable_between_is_inert(broker: InProcBroker) -> None:
    p = mk(broker, "claude", PI, f"{A[0]}@{A[1]:.2f}", "bench")
    hook(broker, RConn([[*HOOK, "-"], [A[0] + 3, 130.0, "?"], [*A, "claude"]]), "claude")
    assert not seen(broker, p)  # can't tell what sits in between: fail closed, as locally
    hook(broker, RConn([[*HOOK, "-"], [A[0] + 3, 130.0, "-"], [*A, "claude"]]), "claude")
    assert seen(broker, p)


def test_nested_agent_is_inert(broker: InProcBroker) -> None:
    p = mk(broker, "claude", PI, f"{A[0]}@{A[1]:.2f}", "bench")
    # a `claude -p` started from the joined agent's Bash: its hooks aren't the outer session's
    hook(broker, RConn([[*HOOK, "-"], [A[0] + 5, 140.0, "claude"], [*A, "claude"]]), "claude")
    assert not seen(broker, p)
    # no chain at all (a satellite that couldn't walk it): inert, never a guess
    hook(broker, RConn(None), "claude")
    assert not seen(broker, p)
    # the test harness over a link needs a test-mode satellite
    tp = mk(broker, "test", PI, "t1", "t1", agent=(A[0] + 50, 1.0))
    hook(broker, RConn([[*HOOK, "-"], [A[0] + 50, 1.0, "-"]], sat_test=False), "test", "UserPromptSubmit")
    assert not seen(broker, tp)


def test_sid_keyed_compare_uses_host(broker: InProcBroker) -> None:
    tid = "019a0000-0000-7000-8000-00000000abcd"
    local = mk(broker, "codex", "", tid + "-local", "cx-local")  # another thread, the same agent pid here
    rp = mk(broker, "codex", PI, tid, "cx-pi")
    chain = [[*HOOK, "-"], [*A, "codex"]]
    hook(broker, RConn(chain), "codex", sid=tid + "-other")
    assert not seen(broker, rp)  # a sibling thread under the same app-server is inert
    hook(broker, RConn(chain), "codex", sid=tid)
    assert seen(broker, rp) and not seen(broker, local)
    # the resolver compares the key with the host: the same sid under host '' is not this row
    cand = HookCandidate(
        participant_id=rp.id,
        harness="codex",
        agent_pid=A[0],
        agent_start=A[1],
        session_key=session_key("codex", PI, tid),
    )
    procs, argv_fn = _procs(chain)
    assert resolve_hook_participant(procs, "codex", tid, [cand], argv_fn=argv_fn, host=PI) == cand
    assert resolve_hook_participant(procs, "codex", tid, [cand], argv_fn=argv_fn, host="") is None


def _procs(chain: list[list[Any]]) -> tuple[list[Any], Any]:
    got = AgentService._remote_chain(RConn(chain))
    assert got is not None
    _host, procs, argv_fn = got
    assert [p.pid for p in procs] == [c[0] for c in chain]
    assert [p.ppid for p in procs] == [c[0] for c in chain[1:]] + [0]
    return procs, argv_fn
