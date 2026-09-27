"""The stdlib hook script, run exactly as a harness runs it: the /bin/sh guard,
``python -I -S`` and a stub broker socket (DESIGN.md §7, §12.1)."""

from __future__ import annotations

import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import child_env, make_tmp_home
from hook_stub import StubBroker
from switchboard.hook import switchboard_hook as hk
from switchboard.install.common import hook_command
from switchboard.paths import Paths, hook_sha12, write_hook_copy

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "payloads"
ALLOWED_PARAMS = {"harness", "event", "sid", "gen", "tool", "tool_use_id", "ok", "status", "loop_count",
                  "stop_hook_active", "source", "reason", "permission_mode", "tokens", "t", "max_wait_s",
                  "join_nonce", "subagent_bg", "model"}
CTX = {"out": {"kind": "context", "text": "[switchboard] hello"}, "batch_id": 3, "ack": "a" * 32}


def load(harness: str, name: str) -> dict[str, Any]:
    d = json.loads((FIX / harness / f"{name}.json").read_text())
    return {k: v for k, v in d.items() if not k.startswith("_")}


@pytest.fixture
def home():
    h = make_tmp_home()
    write_hook_copy(Paths.from_home(h))
    yield h
    shutil.rmtree(h, ignore_errors=True)


@pytest.fixture
def stub(home):
    s = StubBroker(home)
    yield s
    s.close()


def run_hook(home: Path, harness: str, event: str, payload: Any, *, env: dict[str, str] | None = None,
             python: str | None = None, sha: str | None = None, max_wait: int | None = None,
             raw: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    cmd = hook_command(python or sys.executable, str(Paths.from_home(home).home), sha or hook_sha12(),
                       harness, event, max_wait)
    data = raw if raw is not None else json.dumps(payload).encode()
    return subprocess.run(cmd, shell=True, input=data, capture_output=True, env=child_env(**(env or {})),
                          timeout=20)


def all_fixtures():
    for h in ("claude", "codex", "cursor", "devin"):
        for p in sorted((FIX / h).glob("*.json")):
            yield h, p.stem


@pytest.mark.parametrize("harness,name", list(all_fixtures()))
def test_every_fixture_relays_only_allowlisted_fields(home, stub, harness: str, name: str) -> None:
    payload = load(harness, name)
    event = payload["hook_event_name"]
    r = run_hook(home, harness, event, payload)
    assert r.returncode == 0 and r.stdout == b""
    if event in hk.HANDLED[harness]:
        assert len(stub.requests) == 1, stub.requests
        params = stub.requests[0]["params"]
        assert set(params) <= ALLOWED_PARAMS
        assert params["harness"] == harness and params["event"] == event
        blob = json.dumps(params)
        for k in ("cwd", "transcript_path", "prompt", "last_assistant_message", "tool_input", "workspace_roots"):
            v = payload.get(k)
            if isinstance(v, str) and len(v) > 8:
                assert v not in blob
        sid = payload.get("session_id") or payload.get("conversation_id")
        assert params.get("sid") == sid
    else:
        assert stub.requests == []


def test_claude_context_output_and_ack(home) -> None:
    s = StubBroker(home, lambda req: CTX)
    try:
        for name in ("PostToolUse_bash", "PostToolUseFailure_bash", "UserPromptSubmit", "SessionStart_clear"):
            payload = load("claude", name)
            ev = payload["hook_event_name"]
            r = run_hook(home, "claude", ev, payload)
            assert r.returncode == 0
            assert json.loads(r.stdout) == {"hookSpecificOutput": {"hookEventName": ev,
                                                                    "additionalContext": "[switchboard] hello"}}
        deadline = time.monotonic() + 3
        while len(s.acks) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert s.acks == [{"batch_id": 3, "ack": "a" * 32}] * 4
        # Stop and SessionEnd never print for Claude, whatever the broker says
        for name in ("Stop", "SessionEnd_exit"):
            payload = load("claude", name)
            r = run_hook(home, "claude", payload["hook_event_name"], payload)
            assert r.returncode == 0 and r.stdout == b""
        cont = {"out": {"kind": "continue", "text": "go on"}, "batch_id": 4, "ack": "b" * 32}
        s.reply = lambda req: cont
        payload = load("claude", "Stop")
        assert run_hook(home, "claude", "Stop", payload).stdout == b""
    finally:
        s.close()


def test_oversized_context_prints_nothing_and_is_not_acked(home) -> None:
    """The hook never cuts a batch (a cut batch would be acked as fully seen);
    the broker fits batches to this limit, so this is only a safety net."""
    big = {"out": {"kind": "context", "text": "[switchboard] " + "y" * hk.CONTEXT_MAX["claude"]},
           "batch_id": 5, "ack": "c" * 32}
    s = StubBroker(home, lambda req: big)
    try:
        payload = load("claude", "PostToolUse_bash")
        r = run_hook(home, "claude", "PostToolUse", payload)
        assert r.returncode == 0 and r.stdout == b"" and len(s.requests) == 1
        time.sleep(0.2)
        assert s.acks == []
    finally:
        s.close()


def test_harness_mismatch_exits_quietly(home, stub) -> None:
    # a Claude hook under Cursor's import (cursor_version in stdin) or Devin's env
    cur = load("cursor", "postToolUse_shell")
    r = run_hook(home, "claude", "postToolUse", cur)
    assert r.returncode == 0 and r.stdout == b""
    dv = load("claude", "PostToolUse_bash")
    r = run_hook(home, "claude", "PostToolUse", dv, env={"DEVIN_PROJECT_DIR": "/ws"})
    assert r.returncode == 0 and r.stdout == b""
    r = run_hook(home, "claude", "PostToolUse", dv, env={"CHISEL_SESSION_DB": "/ws/x.db"})
    assert r.stdout == b""
    assert stub.requests == []


def test_event_name_mismatch_exits_quietly(home, stub) -> None:
    payload = load("claude", "PostToolUse_bash")
    r = run_hook(home, "claude", "Stop", payload)
    assert r.returncode == 0 and r.stdout == b"" and stub.requests == []


def test_missing_file_or_interpreter_exits_0_not_2(home) -> None:
    payload = load("claude", "UserPromptSubmit")
    r = run_hook(home, "claude", "UserPromptSubmit", payload, sha="0" * 12)
    assert r.returncode == 0 and r.stdout == b""
    r = run_hook(home, "claude", "UserPromptSubmit", payload, python="/nonexistent/python3")
    assert r.returncode == 0 and r.stdout == b""
    # a bare missing script would exit 2 (blocking a sync hook): the guard is what prevents it
    bare = subprocess.run([sys.executable, "/nonexistent/hook.py"], capture_output=True)
    assert bare.returncode == 2


def test_broker_down_or_no_home_exits_0(home) -> None:
    payload = load("claude", "PostToolUse_bash")
    r = run_hook(home, "claude", "PostToolUse", payload)
    assert r.returncode == 0 and r.stdout == b""
    shutil.rmtree(home / "run", ignore_errors=True)
    shutil.rmtree(home / "hooks", ignore_errors=True)
    r = run_hook(home, "claude", "PostToolUse", payload)
    assert r.returncode == 0 and r.stdout == b""


def test_garbage_and_empty_stdin(home, stub) -> None:
    for raw in (b"", b"not json", b"[1,2]", b"\xff\xfe\x00garbage", b"null"):
        r = run_hook(home, "claude", "PostToolUse", None, raw=raw)
        assert r.returncode == 0 and r.stdout == b""
    assert stub.requests == []


def test_socket_that_is_not_ours_is_never_used(home, monkeypatch) -> None:
    sock = str(Paths.from_home(home).sock)
    Path(sock).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(sock).write_text("not a socket")
    out = io.StringIO()
    payload = json.dumps(load("claude", "PostToolUse_bash"))
    argv = ["--home", str(home), "--harness", "claude", "--event", "PostToolUse"]
    assert hk.run(argv, io.StringIO(payload), out, {}) is None and out.getvalue() == ""
    os.unlink(sock)
    s = StubBroker(home, lambda req: CTX)
    try:
        monkeypatch.setattr(hk.os, "getuid", lambda: os.getuid() + 1)
        assert hk.run(argv, io.StringIO(payload), out, {}) is None
        assert s.requests == []
        monkeypatch.undo()
        assert hk.run(argv, io.StringIO(payload), out, {}) is not None
        assert len(s.requests) == 1
    finally:
        s.close()


def test_token_extraction_from_every_payload_shape() -> None:
    tok = "yk:b12.0123abcd"
    shapes = [
        {"tool_response": {"success": True, "output": f"x {tok} y", "error": None}},  # Devin
        {"tool_response": {"content": [{"type": "text", "text": f"{{\"text\":\"{tok}\"}}"}]}},  # Codex MCP
        {"tool_output": json.dumps({"content": [{"type": "text", "text": tok}]})},  # Cursor JSON string
        {"tool_response": [{"type": "text", "text": tok}]},  # Claude MCP content list
        {"prompt": f"[switchboard] ... batch {tok} ..."},  # inbox prompt
    ]
    for s in shapes:
        assert hk.extract_tokens(s) == [[12, "0123abcd"]], s
    many = {"prompt": " ".join(f"yk:b{i}.0123abcd" for i in range(50))}
    assert len(hk.extract_tokens(many)) == 20
    # defanged tokens (what peers get) never match
    assert hk.extract_tokens({"prompt": "yk_:b12.0123abcd YK:b1.0123abcd"}) == []


def test_join_nonce_only_from_the_join_tool() -> None:
    p = {"tool_name": "mcp__switchboard__join", "tool_response": [{"type": "text", "text": "join yk:j0123456789abcdef"}]}
    assert hk.build_params(p, "claude", "PostToolUse", 1.0, 1.0)["join_nonce"] == "0123456789abcdef"
    p2 = {**p, "tool_name": "Bash"}
    assert "join_nonce" not in hk.build_params(p2, "claude", "PostToolUse", 1.0, 1.0)
    cur = {"tool_name": "MCP:join", "tool_output": json.dumps({"content": [{"text": "yk:j0123456789abcdef"}]})}
    assert hk.build_params(cur, "cursor", "postToolUse", 1.0, 1.0)["join_nonce"] == "0123456789abcdef"


def test_devin_background_subagent_flag() -> None:
    p = load("devin", "PreToolUse_run_subagent_bg")
    assert hk.build_params(p, "devin", "PreToolUse", 1.0, 1.0)["subagent_bg"] is True
    p2 = {**p, "tool_input": {**p["tool_input"], "is_background": False}}
    assert "subagent_bg" not in hk.build_params(p2, "devin", "PreToolUse", 1.0, 1.0)


def test_the_model_name_is_relayed_only_when_it_looks_like_one() -> None:
    """For the report's model per participant (DESIGN.md §12.6): a short identifier only."""
    for model in ("claude-sonnet-4-6", "gpt-5.5", "claude-haiku-4-5-20251001", "swe-1-6-slow", "claude:4@x",
                  "claude-3-5-sonnet-v2@20241022"):
        assert hk.build_params({"model": model}, "codex", "Stop", 1.0, 1.0)["model"] == model
    for bad in ("/Users/someone/model", "org/model", "a b", "x" * 65, "", None, 5, {"id": "gpt"}, "-flag",
                "someone@example.com", "first.last@example.com", "a@b@c", "m@" + "v" * 70):
        assert "model" not in hk.build_params({"model": bad}, "codex", "Stop", 1.0, 1.0)


def test_five_megabyte_payload_parses(home, stub) -> None:
    payload = load("claude", "PostToolUse_bash")
    payload["tool_response"] = {"stdout": "x" * (5 * 1024 * 1024) + " yk:b9.0123abcd"}
    r = run_hook(home, "claude", "PostToolUse", payload)
    assert r.returncode == 0
    assert len(stub.requests) == 1
    assert stub.requests[0]["params"].get("tokens", []) == []  # beyond the 256 KiB scan window


def _timings(home: Path, n: int) -> list[float]:
    payload = load("claude", "PostToolUse_bash")
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = run_hook(home, "claude", "PostToolUse", payload)
        out.append(time.perf_counter() - t0)
        assert r.returncode == 0
    return out


def test_hook_latency_loose(home, stub) -> None:
    ts = _timings(home, 30)
    p95 = statistics.quantiles(ts, n=100, method="inclusive")[94]
    print(f"hook p50={statistics.median(ts)*1000:.1f} ms p95={p95*1000:.1f} ms (n=30, incl. shell spawn)")
    assert p95 < 0.3


@pytest.mark.perf
def test_hook_latency_strict(home, stub) -> None:
    ts = _timings(home, 40)
    p95 = statistics.quantiles(ts, n=100, method="inclusive")[94]
    print(f"hook p50={statistics.median(ts)*1000:.1f} ms p95={p95*1000:.1f} ms (n=40)")
    assert p95 < 0.1


def test_devin_ok_comes_from_tool_response_success() -> None:
    ok = load("devin", "PostToolUse_mcp_wait")
    assert hk.build_params(ok, "devin", "PostToolUse", 1.0, 1.0)["ok"] is True
    failed = {**ok, "tool_response": {"success": False, "output": "", "error": "cancelled"}}
    assert hk.build_params(failed, "devin", "PostToolUse", 1.0, 1.0)["ok"] is False
    # other harnesses don't read a nested success (e.g. a Claude tool's own result object)
    cl = {**load("claude", "PostToolUse_bash"), "tool_response": {"success": False}}
    assert hk.build_params(cl, "claude", "PostToolUse", 1.0, 1.0)["ok"] is True


def test_cursor_and_devin_outputs_through_the_real_guard(home) -> None:
    cont = {"out": {"kind": "continue", "text": "[switchboard] go on"}, "batch_id": 7, "ack": "d" * 32}
    s = StubBroker(home, lambda req: cont if req["params"]["event"] in ("stop", "Stop") else CTX)
    try:
        r = run_hook(home, "cursor", "postToolUse", load("cursor", "postToolUse_shell"))
        assert json.loads(r.stdout) == {"additional_context": "[switchboard] hello"}
        r = run_hook(home, "cursor", "stop", load("cursor", "stop"), max_wait=35)
        assert json.loads(r.stdout) == {"followup_message": "[switchboard] go on"}
        assert s.requests[-1]["params"]["max_wait_s"] == 35.0 and s.requests[-1]["params"]["status"] == "completed"
        dv = {"DEVIN_PROJECT_DIR": "/ws"}
        r = run_hook(home, "devin", "PostToolUse", load("devin", "PostToolUse_read"), env=dv)
        assert json.loads(r.stdout) == {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                                "additionalContext": "[switchboard] hello"}}
        r = run_hook(home, "devin", "Stop", load("devin", "Stop"), env=dv)
        assert json.loads(r.stdout) == {"decision": "block", "reason": "[switchboard] go on"}
        # never on PreToolUse, whatever the broker says
        r = run_hook(home, "devin", "PreToolUse", load("devin", "PreToolUse_read"), env=dv)
        assert r.stdout == b""
        deadline = time.monotonic() + 3
        while len(s.acks) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(s.acks) == 4
    finally:
        s.close()


def test_a_parked_cursor_stop_waits_for_the_broker(home) -> None:
    """The stop hook long-polls (``--max-wait``) instead of the 1.5 s guard."""

    def slow(req: dict[str, Any]) -> dict[str, Any]:
        time.sleep(2.5)
        return {"out": {"kind": "continue", "text": "[switchboard] late"}, "batch_id": 1, "ack": "e" * 32}

    s = StubBroker(home, slow)
    try:
        t0 = time.monotonic()
        r = run_hook(home, "cursor", "stop", load("cursor", "stop"), max_wait=40)
        assert json.loads(r.stdout) == {"followup_message": "[switchboard] late"} and time.monotonic() - t0 >= 2.4
        # without --max-wait the hard 1.5 s guard ends it, printing nothing
        r = run_hook(home, "cursor", "stop", load("cursor", "stop"))
        assert r.returncode == 0 and r.stdout == b""
    finally:
        s.close()
