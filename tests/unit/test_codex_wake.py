"""Waking a remote Codex thread on its own machine (issue #63, ``mcp/codex_wake.py``),
against the fake app-server: the checks before the ``turn/start`` and what it sends."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes.fake_codex_daemon import FakeCodexDaemon

from switchboard.adapters import codex_rpc
from switchboard.mcp import codex_wake

TID = "019a0000-0000-7000-8000-0000000c0dea"
NONCE = "0123456789abcdef"
JOIN_TEXT = f"[switchboard] You joined #fpga as cx.\njoin yk:j{NONCE}"


@pytest.fixture
def daemon() -> Iterator[tuple[FakeCodexDaemon, str]]:
    d = Path(tempfile.mkdtemp(prefix="yk-cw-", dir="/tmp"))
    os.chmod(d, 0o700)
    sock = str(d / "app-server-control.sock")
    fake = FakeCodexDaemon(sock).start()
    try:
        yield fake, sock
    finally:
        fake.stop()
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tui(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Whether a Codex TUI is attached (lsof is exercised by the integration test)."""
    state = {"attached": True}
    monkeypatch.setattr(codex_wake, "tui_attached", lambda sock: state["attached"])
    return state


def wake(
    sock: str | None,
    proven: set[str] | None = None,
    *,
    text: str = "[switchboard] #fpga: hi",
    tid: str = TID,
    nonce: str = NONCE,
) -> Any:
    return asyncio.run(codex_wake.wake(sock, tid, nonce, text, 42, set() if proven is None else proven))


def refused(sock: str | None, **kw: Any) -> str:
    with pytest.raises(codex_wake.Refused) as e:
        wake(sock, **kw)
    return e.value.code


def test_an_idle_proven_thread_gets_exactly_a_turn_start(daemon: Any, tui: dict[str, bool]) -> None:
    fake, sock = daemon
    fake.add_thread(TID)
    fake.prove(TID, JOIN_TEXT)
    proven: set[str] = set()
    t = wake(sock, proven)
    assert isinstance(t, float) and proven == {TID}
    [call] = fake.calls("turn/start")
    assert set(call) == {"threadId", "input", "clientUserMessageId"}
    assert call["threadId"] == TID and call["clientUserMessageId"] == "yk-b42"
    assert call["input"] == [{"type": "text", "text": "[switchboard] #fpga: hi", "text_elements": []}]
    # the proof was read once, with turns; the next wake reads the status only
    reads = fake.calls("thread/read")
    assert reads == [{"threadId": TID, "includeTurns": True}]
    fake.end_turn(TID)  # the fake started a turn: back to idle
    fake.set_status(TID, "idle")
    wake(sock, proven)
    assert fake.calls("thread/read")[-1] == {"threadId": TID, "includeTurns": False}
    assert fake.methods() <= codex_rpc.ALLOWED_METHODS


def test_no_wake_while_busy_waiting_or_unloaded(daemon: Any, tui: dict[str, bool]) -> None:
    fake, sock = daemon
    fake.add_thread(TID)
    fake.prove(TID, JOIN_TEXT)
    proven = {TID}
    fake.set_status(TID, "active", ["waitingOnApproval"])
    assert refused(sock, proven=proven) == codex_wake.NOT_IDLE
    fake.set_status(TID, "active", ["waitingOnUserInput"])
    assert refused(sock, proven=proven) == codex_wake.NOT_IDLE
    fake.set_status(TID, "active")
    assert refused(sock, proven=proven) == codex_wake.NOT_IDLE
    fake.set_status(TID, "notLoaded")
    assert refused(sock, proven=proven) == codex_wake.NOT_LOADED
    assert fake.calls("turn/start") == []


def test_the_thread_proof_comes_first(daemon: Any, tui: dict[str, bool]) -> None:
    fake, sock = daemon
    fake.add_thread(TID)
    # no join result in the thread: a process that only knows the thread id
    assert refused(sock) == codex_wake.UNPROVEN
    # the nonce anywhere but switchboard's own join result proves nothing
    fake.add_item(TID, {"type": "agentMessage", "text": f"join yk:j{NONCE}"})
    assert refused(sock) == codex_wake.UNPROVEN
    fake.prove(TID, JOIN_TEXT.replace(NONCE, "f" * 16))  # another join's code
    assert refused(sock) == codex_wake.UNPROVEN
    assert fake.calls("turn/start") == []
    fake.prove(TID, JOIN_TEXT)
    wake(sock)
    assert len(fake.calls("turn/start")) == 1


def test_no_tui_or_no_daemon_means_no_wake(daemon: Any, tui: dict[str, bool], tmp_path: Path) -> None:
    fake, sock = daemon
    fake.add_thread(TID)
    fake.prove(TID, JOIN_TEXT)
    tui["attached"] = False
    assert refused(sock) == codex_wake.NO_TUI
    tui["attached"] = True
    assert refused(str(tmp_path / "nothing.sock")) == codex_wake.NO_DAEMON
    assert refused(None) == codex_wake.NO_DAEMON
    assert refused("relative/path.sock") == codex_wake.NO_DAEMON
    assert fake.calls("turn/start") == []


def test_the_tui_check_matches_the_configured_path_and_where_it_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lsof names a socket by the path it was bound with: on macOS a socket under /tmp
    shows as /tmp/..., though it resolves to /private/tmp/... (a CI failure)."""
    from switchboard.adapters import codex as cx
    from switchboard.broker import proc

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    bound = str(link / "app-server-control.sock")  # as the app-server bound it
    tui_pid = os.getpid() + 1
    out = (
        f"p{os.getpid() + 2}\nf5\nd0xaaa\nn{bound}\n"  # the app-server's end
        f"p{tui_pid}\nf7\nd0xbbb\nn->0xaaa\n"
    )  # the TUI's end
    monkeypatch.setattr(cx, "lsof_bin", lambda: "/usr/sbin/lsof")
    monkeypatch.setattr(cx, "_run_lsof", lambda _bin: out)
    monkeypatch.setattr(
        proc, "info", lambda pid: proc.ProcInfo(pid, 1, 100.0, os.getuid()) if pid == tui_pid else None
    )
    monkeypatch.setattr(proc, "argv_many", lambda infos: {i.pid: "codex" for i in infos})
    assert codex_wake.tui_attached(bound)
    assert codex_wake.tui_attached(str(real / "app-server-control.sock")) is False  # only the resolved path
    monkeypatch.setattr(proc, "argv_many", lambda infos: {i.pid: "codex app-server" for i in infos})
    assert codex_wake.tui_attached(bound) is False  # an app-server client is not a TUI


def test_an_rpc_error_is_a_refusal(daemon: Any, tui: dict[str, bool]) -> None:
    fake, sock = daemon
    fake.add_thread(TID)
    fake.prove(TID, JOIN_TEXT)
    fake.fail_next["turn/start"] = (-32000, "boom")
    assert refused(sock) == codex_wake.RPC_FAILED


def test_thread_contents_never_leave_memory(
    daemon: Any, tui: dict[str, bool], caplog: pytest.LogCaptureFixture
) -> None:
    from fakes.fake_codex_daemon import CANARY

    fake, sock = daemon
    fake.add_thread(TID)
    fake.prove(TID, JOIN_TEXT)
    with caplog.at_level("DEBUG"):
        wake(sock)
    assert CANARY not in caplog.text and CANARY not in json.dumps([r.getMessage() for r in caplog.records])
