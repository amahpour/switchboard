"""Host views (DESIGN.md §27.5.6): the local view is this machine's kernel and
Claude registry; any other host is unknown (``None``) until its link reports on it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from switchboard.broker import proc
from switchboard.broker.hosts import HostViews, LocalView, RemoteView
from switchboard.models import Participant


def me() -> proc.ProcInfo:
    i = proc.info(os.getpid())
    assert i is not None
    return i


def participant(host: str, pid: int | None, start: float | None) -> Participant:
    return Participant(
        id=1, harness="claude", session_key="k", session_id=None, agent_pid=pid, agent_start=start,
        mcp_pid=pid, mcp_start=start, claude_socket=None, bind_state="bound", bind_nonce=None,
        thread_proof=False, status="idle", status_at=None, status_src=None, tier=None, tier_note=None,
        approval_mode="unknown", env_leak=False, away=None, boundary_seq=0, gen=None, gen_tainted=False,
        rearms_in_gen=0, last_loop_count=None, unconfirmed_followups=0, push_expiries=0, hooks_seen_at=None,
        last_say_at=None, created_at=0.0, last_seen=None, ended_at=None, host=host,
    )


def test_local_view_is_proc(tmp_path: Path) -> None:
    views = HostViews(str(tmp_path))
    v = views.view("")
    assert isinstance(v, LocalView) and v is views.local and v.host == ""
    i = me()
    assert v.alive(i.pid, i.start) is True == proc.alive(i.pid, i.start)
    assert v.alive(i.pid, i.start + 100.0) is False  # a recycled pid is not this process
    assert v.alive(None, None) is False
    chain = v.ancestry(i.pid, 3)
    assert chain is not None and [p.pid for p in chain] == [p.pid for p in proc.ancestry(i.pid, 3)]
    assert v.argv_many(chain) == proc.argv_many(chain)
    (tmp_path / f"{i.pid}.json").write_text(json.dumps({"pid": i.pid, "status": "idle"}))
    assert v.read_registry(i.pid) == {"pid": i.pid, "status": "idle"}
    assert v.read_registry(i.pid + 1) is None
    assert views.agent_alive(participant("", i.pid, i.start)) is True
    assert views.mcp_alive(participant("", i.pid, i.start + 5.0)) is False


def test_unknown_host_is_unknown(tmp_path: Path) -> None:
    views = HostViews(str(tmp_path))
    i = me()
    (tmp_path / f"{i.pid}.json").write_text(json.dumps({"pid": i.pid, "status": "idle"}))
    for host in ("fpga-pi", "Bad Host", "x@y"):
        v = views.view(host)
        assert isinstance(v, RemoteView) and v is not views.local
        # the local process table and registry are never consulted for another host,
        # even for a pid that is alive (and has a registry file) here
        assert v.alive(i.pid, i.start) is None
        assert v.ancestry(i.pid) is None and v.argv_many([i]) is None and v.read_registry(i.pid) is None
        assert views.alive(host, i.pid, i.start) is None
    assert views.agent_alive(participant("fpga-pi", i.pid, i.start)) is None
    assert views.remotes == {}  # an unknown host is not remembered


def test_configured_remote_is_unknown_until_its_link_reports(tmp_path: Path) -> None:
    views = HostViews(str(tmp_path))
    v = views.add_remote("fpga-pi")
    assert views.add_remote("fpga-pi") is v and views.view("fpga-pi") is v
    assert set(views.remotes) == {"fpga-pi"}
    i = me()
    assert v.alive(i.pid, i.start) is None
    views.remove_remote("fpga-pi")
    assert views.view("fpga-pi") is not v
    with pytest.raises(ValueError):
        views.add_remote("")  # the local host is not a remote
    with pytest.raises(ValueError):
        views.add_remote("Not-A-Host")


# ------------------------------------------------ M8c: the link feeds RemoteView
class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def now(self) -> float:
        return self.t


def fed_view() -> tuple[RemoteView, Clock]:
    clock = Clock()
    v = HostViews("/nonexistent", clock).add_remote("fpga-pi")
    v.link_up()
    return v, clock


def test_remote_view_true_false_none() -> None:
    v, _clock = fed_view()
    a, b, c = (1000004242, 10.5), (1000004243, 11.0), (1000009999, 12.0)
    assert v.alive(*a) is None  # not watched yet
    v.set_watch(1, {a, b})
    assert v.alive(*a) is None  # watched, but no alive frame has answered that watch
    assert v.on_alive(0, []) == set()  # an alive frame for an older watch vouches for nothing new
    assert v.alive(*a) is None
    assert v.on_alive(1, [b]) == {b}
    assert v.alive(*a) is True and v.alive(*b) is False
    assert v.alive(*c) is None  # never watched
    assert v.alive(a[0], a[1] + 5.0) is None  # the same pid with another start is another process
    assert v.alive(a[0], a[1] + 0.005) is True  # start times compare as proc.same_start does
    # dead is final: a later frame that doesn't list it (it left the watch) keeps it dead
    v.set_watch(2, {a})
    v.on_alive(2, [])
    assert v.alive(*b) is False and v.alive(*a) is True
    assert v.on_alive(2, [b]) == set()  # already known dead: not "newly" dead
    assert v.alive(None, None) is None and v.alive(a[0], None) is None
    # a pair joining a watch later counts only from the watch that named it
    v.set_watch(3, {a, c})
    v.on_alive(2, [])
    assert v.alive(*c) is None
    v.on_alive(3, [])
    assert v.alive(*c) is True


def test_remote_view_stale_after_3s() -> None:
    v, clock = fed_view()
    a = (1000004242, 10.5)
    v.set_watch(1, {a})
    v.on_alive(1, [])
    clock.t += RemoteView.FRESH_S
    assert v.alive(*a) is True
    clock.t += 0.01
    assert v.alive(*a) is None  # no alive frame for over 3 s: can't tell
    v.on_alive(1, [])
    assert v.alive(*a) is True


def test_remote_view_none_when_link_down() -> None:
    v, _clock = fed_view()
    a, b = (1000004242, 10.5), (1000004243, 11.0)
    v.set_watch(1, {a, b})
    v.on_alive(1, [b])
    v.link_down()
    assert v.alive(*a) is None  # nothing vouches for it while the link is down
    assert v.alive(*b) is False  # a death already reported stays known
    v.link_up()
    assert v.alive(*a) is None  # up again: only a fresh alive frame vouches
    v.on_alive(1, [])
    assert v.alive(*a) is True
