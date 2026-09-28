"""The stdlib hook script, in-process (DESIGN.md §7): argument parsing, the early
exits, the socket exchange with the broker (timeouts, a vanished agent, a
closed or garbled reply) and ``main``'s hard guard. The harness-shaped runs
through the real ``/bin/sh`` guard are in test_hook_script.py; these call the
functions directly, with one end of a socketpair standing in for the broker."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from switchboard.hook import switchboard_hook as hk

CTX = {"out": {"kind": "context", "text": "[switchboard] hello"}, "batch_id": 3, "ack": "a" * 32}
ARGV = ["--home", "/opt/yk/home", "--harness", "claude", "--event", "PostToolUse"]
POST = {"hook_event_name": "PostToolUse", "session_id": "s-1", "tool_name": "Bash"}
Wire = Callable[..., tuple[socket.socket, socket.socket]]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wire]:
    """``wire(reply=None)``: (hook end, broker end) of a socketpair; the hook's
    ``_connect`` returns the hook end. ``reply`` is queued on the broker end
    before the hook asks. Both ends are closed after the test."""
    made: list[socket.socket] = []

    def make(reply: bytes | None = None) -> tuple[socket.socket, socket.socket]:
        a, b = socket.socketpair()
        made.extend((a, b))
        monkeypatch.setattr(hk, "_connect", lambda path: a)
        if reply is not None:
            b.sendall(reply)
        return a, b

    yield make
    for s in made:
        s.close()


def received(b: socket.socket) -> list[dict[str, Any]]:
    """Every line the hook sent, read until it closes its end (a 2 s timeout
    fails the test if it never does)."""
    b.settimeout(2)
    buf = b""
    while True:
        try:
            chunk = b.recv(65536)
        except ConnectionResetError:
            # Linux reports a close with our reply still unread as a reset, after
            # handing over everything the hook sent: that is the close too
            break
        if not chunk:
            break
        buf += chunk
    b.close()
    return [json.loads(line) for line in buf.splitlines()]


def line(obj: Any) -> bytes:
    return (json.dumps(obj) + "\n").encode()


# ---------------------------------------------------------------- arguments
def test_parse_args_skips_unknown_and_dangling_flags() -> None:
    got = hk.parse_args(["--verbose", "--home", "/h", "stray", "--harness", "cursor", "--event", "stop",
                         "--max-wait", "70", "--event"])
    assert got == {"home": "/h", "harness": "cursor", "event": "stop", "max_wait": 70.0}


def test_parse_args_max_wait_is_a_non_negative_float_or_nothing() -> None:
    assert hk.parse_args(["--max-wait", "2.5"])["max_wait"] == 2.5
    assert hk.parse_args(["--max-wait", "-5"])["max_wait"] == 0.0
    assert hk.parse_args(["--max-wait", "soon"])["max_wait"] is None
    assert hk.parse_args([])["max_wait"] is None


# ---------------------------------------------------------------- allowlist
def test_as_text_of_every_value_shape() -> None:
    assert hk._as_text(None) == ""
    assert hk._as_text(12) == "12" and hk._as_text(True) == "True"
    assert hk._as_text({"a": [1]}) == '{"a": [1]}'
    loop: list[Any] = []
    loop.append(loop)
    assert hk._as_text(loop) == ""  # ValueError (circular): nothing to scan
    assert hk._as_text({"a": object()}) == ""  # TypeError: nothing to scan
    assert len(hk._as_text("x" * (hk.FIELD_SCAN_MAX + 10))) == hk.FIELD_SCAN_MAX


def test_a_top_level_success_flag_decides_ok() -> None:
    assert hk.build_params({"success": False}, "cursor", "postToolUse", 1.0, 1.0)["ok"] is False
    # the explicit flag wins over the event name
    assert hk.build_params({"success": True}, "claude", "PostToolUseFailure", 1.0, 1.0)["ok"] is True
    # a non-bool one is ignored: the event name decides
    assert hk.build_params({"success": "no"}, "claude", "PostToolUse", 1.0, 1.0)["ok"] is True
    assert hk.build_params({"success": "no"}, "claude", "PostToolUseFailure", 1.0, 1.0)["ok"] is False
    assert "ok" not in hk.build_params({"success": None}, "claude", "Stop", 1.0, 1.0)


# ------------------------------------------------------------ early exits
class _BrokenStdin:
    def read(self) -> str:
        raise OSError("stdin closed")


@pytest.mark.parametrize("argv,stdin,env", [
    (ARGV, _BrokenStdin(), {}),
    (ARGV, io.StringIO("not json"), {}),
    (ARGV, io.StringIO("[1, 2]"), {}),
    (ARGV, io.StringIO("null"), {}),
    (["--home", "/h", "--harness", "gemini", "--event", "PostToolUse"], io.StringIO(json.dumps(POST)), {}),
    (["--home", "/h", "--harness", "claude"], io.StringIO(json.dumps(POST)), {}),
    (ARGV, io.StringIO(json.dumps({**POST, "cursor_version": "1.0"})), {}),  # Cursor imported it
    (ARGV, io.StringIO(json.dumps(POST)), {"CHISEL_SESSION_DB": "/ws/x.db"}),  # Devin imported it
    (ARGV, io.StringIO(json.dumps({**POST, "hook_event_name": "Stop"})), {}),  # another event's payload
    (["--home", "/h", "--harness", "claude", "--event", "PreToolUse"],
     io.StringIO(json.dumps({**POST, "hook_event_name": "PreToolUse"})), {}),  # not registered for claude
    (["--harness", "claude", "--event", "PostToolUse"], io.StringIO(json.dumps(POST)), {}),  # no --home
], ids=["stdin-error", "not-json", "a-list", "null", "unknown-harness", "no-event", "cursor-payload",
        "devin-env", "event-mismatch", "unhandled-event", "no-home"])
def test_early_exits_never_ask_the_broker_or_print(monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: Any,
                                                   env: dict[str, str]) -> None:
    asked: list[Any] = []
    monkeypatch.setattr(hk, "ask_broker", lambda *a, **k: asked.append(a) or (CTX, None))
    out = io.StringIO()
    assert hk.run(argv, stdin, out, env) is None
    assert out.getvalue() == "" and asked == []


def test_bytes_stdin_is_read_through_its_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hk, "ask_broker", lambda *a, **k: (CTX, None))
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(POST).encode()), encoding="utf-8")
    out = io.StringIO()
    text = hk.run(ARGV, stdin, out, {})
    assert text == out.getvalue()
    assert json.loads(text) == {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                       "additionalContext": "[switchboard] hello"}}


# ------------------------------------------------------------- the exchange
def test_a_reply_with_nothing_to_print_closes_the_socket_without_an_ack(wire: Wire) -> None:
    _, b = wire(line({"id": 1, "result": {"out": None, "batch_id": 1, "ack": "a" * 32}}))
    out = io.StringIO()
    assert hk.run(ARGV, io.StringIO(json.dumps(POST)), out, {}) is None
    assert out.getvalue() == ""
    sent = received(b)  # returns only because the hook closed its end
    assert [m["method"] for m in sent] == ["hook.event"]
    assert sent[0]["params"]["sid"] == "s-1" and sent[0]["params"]["max_wait_s"] == 1.0


def test_a_printed_batch_is_acked_on_the_same_socket(wire: Wire) -> None:
    _, b = wire(line({"id": 1, "result": CTX}))
    out = io.StringIO()
    text = hk.run(ARGV, io.StringIO(json.dumps(POST)), out, {})
    assert text == out.getvalue() and "[switchboard] hello" in text
    sent = received(b)
    assert [m["method"] for m in sent] == ["hook.event", "hook.ack"]
    assert sent[1]["params"] == {"batch_id": 3, "ack": "a" * 32}


def test_a_failed_print_is_never_acked(wire: Wire) -> None:
    class ClosedStdout:
        def write(self, s: str) -> int:
            raise BrokenPipeError("the harness went away")

        def flush(self) -> None:
            pass

    a, b = wire(line({"id": 1, "result": CTX}))
    assert hk.run(ARGV, io.StringIO(json.dumps(POST)), ClosedStdout(), {}) is None
    a.close()
    assert [m["method"] for m in received(b)] == ["hook.event"]


def test_no_reply_within_max_wait_gives_up_and_closes(wire: Wire) -> None:
    a, b = wire()
    assert hk.ask_broker("unused", {"event": "Stop"}, 0.05) == (None, None)
    assert a.fileno() == -1  # closed by the hook
    assert [m["params"] for m in received(b)] == [{"event": "Stop"}]


def test_zero_max_wait_sends_and_gives_up_at_once(wire: Wire) -> None:
    a, b = wire(line({"id": 1, "result": CTX}))
    assert hk.ask_broker("unused", {"event": "Stop"}, 0) == (None, None)
    assert a.fileno() == -1 and len(received(b)) == 1


def test_a_watched_agent_that_is_alive_keeps_the_wait_going(wire: Wire) -> None:
    a, b = wire(line({"id": 1, "result": CTX}))
    result, s = hk.ask_broker("unused", {}, 1.0, watch_pid=os.getpid())
    assert result == CTX and s is a
    a.close()
    # alive but silent: waits out max_wait
    a, b = wire()
    assert hk.ask_broker("unused", {}, 0.05, watch_pid=os.getpid()) == (None, None)
    assert a.fileno() == -1


def test_a_watched_agent_that_is_gone_ends_the_wait(wire: Wire) -> None:
    """A parked Cursor stop hook stops waiting once its agent has exited, even
    with a reply already waiting (nobody would read what it prints)."""
    p = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    p.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(p.pid, 0)
    a, b = wire(line({"id": 1, "result": CTX}))
    assert hk.ask_broker("unused", {}, 5.0, watch_pid=p.pid) == (None, None)
    assert a.fileno() == -1


def test_the_broker_closing_its_end_is_no_reply(wire: Wire) -> None:
    a, b = wire()
    b.shutdown(socket.SHUT_WR)  # EOF for the hook; the request still goes through
    assert hk.ask_broker("unused", {"event": "Stop"}, 1.0) == (None, None)
    assert a.fileno() == -1 and len(received(b)) == 1


@pytest.mark.parametrize("reply", [b"not json\n", b'{"id": 1, "result": \n'])
def test_a_garbled_reply_is_no_reply(wire: Wire, reply: bytes) -> None:
    a, b = wire(reply)
    assert hk.ask_broker("unused", {}, 1.0) == (None, None)
    assert a.fileno() == -1


def test_a_reply_that_is_not_an_object_has_no_result(wire: Wire) -> None:
    a, b = wire(b"[1, 2]\n")
    result, s = hk.ask_broker("unused", {}, 1.0)
    assert result is None and s is a  # kept open: run() closes it
    a.close()


def test_a_socket_that_fails_even_to_close_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Broken:
        def sendall(self, data: bytes) -> None:
            calls.append("sendall")
            raise BrokenPipeError("broker gone")

        def close(self) -> None:
            calls.append("close")
            raise OSError("bad fd")

    monkeypatch.setattr(hk, "_connect", lambda path: Broken())
    assert hk.ask_broker("unused", {}, 1.0) == (None, None)
    assert calls == ["sendall", "close"]


def test_send_ack_on_a_dead_socket_is_quiet() -> None:
    a, b = socket.socketpair()
    try:
        hk.send_ack(a, 7, "f" * 32)
        a.close()
        assert received(b) == [{"id": 2, "method": "hook.ack", "params": {"batch_id": 7, "ack": "f" * 32}}]
        hk.send_ack(a, 8, "f" * 32)  # closed: settimeout raises inside, nothing escapes
    finally:
        a.close()


# ----------------------------------------------------------- cursor park
def test_a_long_cursor_stop_watches_its_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(hk, "_agent_gpid", lambda: 4242)
    monkeypatch.setattr(hk, "ask_broker", lambda path, params, mw, watch=None: calls.append((mw, watch)) or
                        (None, None))
    payload = json.dumps({"hook_event_name": "stop", "conversation_id": "c-1", "cursor_version": "1.0",
                          "status": "completed"})
    base = ["--home", "/opt/yk/home", "--harness", "cursor", "--event", "stop"]
    hk.run(base + ["--max-wait", "70"], io.StringIO(payload), io.StringIO(), {})
    hk.run(base, io.StringIO(payload), io.StringIO(), {})  # the default 1 s wait: nothing to watch
    hk.run(["--home", "/opt/yk/home", "--harness", "cursor", "--event", "postToolUse", "--max-wait", "70"],
           io.StringIO(json.dumps({"hook_event_name": "postToolUse", "cursor_version": "1.0"})), io.StringIO(), {})
    assert calls == [(70.0, 4242), (1.0, None), (70.0, None)]


def test_agent_gpid_is_the_parent_of_our_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = "  4242\n"
    assert hk._agent_gpid() == 4242
    assert seen[0][0] in ("/bin/ps", "/usr/bin/ps") and seen[0][1:] == ["-o", "ppid=", "-p", str(os.getppid())]
    out = "1\n"  # reparented to init/launchd: no agent to watch
    assert hk._agent_gpid() is None
    out = ""  # ps found no such process
    assert hk._agent_gpid() is None

    def no_ps(argv: list[str], **kw: Any) -> Any:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", no_ps)
    assert hk._agent_gpid() is None


def test_agent_gpid_for_real_is_a_live_process() -> None:
    v = hk._agent_gpid()
    assert v is None or (isinstance(v, int) and v > 1)
    if v is not None:
        try:
            os.kill(v, 0)  # exists (ProcessLookupError otherwise)
        except PermissionError:
            pass  # another user's process (a CI runner's or container's): it exists too


# ------------------------------------------------------------------ main
@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """``os._exit`` recorded instead of ending the test process."""
    codes: list[int] = []
    monkeypatch.setattr(os, "_exit", codes.append)
    return codes


def test_the_guard_exits_0(exits: list[int]) -> None:
    hk._guard(signal.SIGALRM, None)
    assert exits == [0]


def test_main_runs_the_hook_and_exits_0(monkeypatch: pytest.MonkeyPatch, exits: list[int]) -> None:
    monkeypatch.setattr(hk, "ask_broker", lambda *a, **k: (CTX, None))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(POST)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    before = signal.getitimer(signal.ITIMER_REAL)
    hk.main(ARGV + ["--max-wait", "1"])
    assert exits == [0]
    assert json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"] == "[switchboard] hello"
    assert signal.getitimer(signal.ITIMER_REAL) == before  # an explicit --max-wait arms no guard


def test_main_without_max_wait_arms_the_hard_guard(monkeypatch: pytest.MonkeyPatch, exits: list[int]) -> None:
    monkeypatch.setattr(hk, "ask_broker", lambda *a, **k: (None, None))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(POST)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "argv", ["switchboard_hook.py", *ARGV])  # argv=None reads sys.argv
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    try:
        hk.main()
        left, interval = signal.getitimer(signal.ITIMER_REAL)
        handler = signal.getsignal(signal.SIGALRM)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)
    assert exits == [0]
    assert handler is hk._guard
    assert 0 < left <= hk.HARD_GUARD_S and interval == 0


def test_main_swallows_anything_and_still_exits_0(monkeypatch: pytest.MonkeyPatch, exits: list[int]) -> None:
    def boom(*a: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(hk, "run", boom)
    hk.main(ARGV + ["--max-wait", "1"])
    assert exits == [0]
