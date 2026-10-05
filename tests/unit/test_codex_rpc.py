"""The Codex app-server client's allowlists and helpers (DESIGN.md §9.3, §11 items 2 and 5)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

from switchboard import guardrails
from switchboard.adapters import codex_rpc as rpc
from switchboard.adapters.codex_rpc import CodexRpc, ForbiddenRpc, check_request

SCHEMA = Path(__file__).resolve().parents[1] / "fixtures" / "codex_schema"


# ------------------------------------------------------------ allowlists
def test_only_the_allowlisted_methods() -> None:
    assert rpc.ALLOWED_METHODS == {
        "initialize",
        "initialized",
        "thread/read",
        "thread/loaded/list",
        "turn/start",
        "turn/steer",
    }
    for m in (
        "thread/resume",
        "thread/start",
        "thread/shellCommand",
        "command/exec",
        "config/batchWrite",
        "config/value/write",
        "turn/interrupt",
        "hooks/list",
        "thread/queue/add",
        "fs/writeFile",
        "account/logout",
        "thread/unsubscribe",
        "turn/steerx",
        "",
    ):
        with pytest.raises(ForbiddenRpc):
            check_request(m, {})


def test_turn_params_are_exactly_the_allowlist() -> None:
    p = rpc.turn_start_params("t1", "[switchboard] hi", "yk-b7")
    assert p == {
        "threadId": "t1",
        "input": [{"type": "text", "text": "[switchboard] hi", "text_elements": []}],
        "clientUserMessageId": "yk-b7",
    }
    check_request("turn/start", p)
    s = rpc.turn_steer_params("t1", "turn-9", "[switchboard] hi", "yk-b8")
    assert set(s) == {"threadId", "expectedTurnId", "input", "clientUserMessageId"}
    check_request("turn/steer", s)


@pytest.mark.parametrize("field", guardrails.CODEX_OVERRIDE_FIELDS)
def test_no_override_field_can_be_sent(field: str) -> None:
    for method, base in (
        ("turn/start", rpc.turn_start_params("t", "x", "yk-b1")),
        ("turn/steer", rpc.turn_steer_params("t", "u", "x", "yk-b1")),
    ):
        with pytest.raises(ForbiddenRpc):
            check_request(method, {**base, field: "never"})


def test_turn_params_need_every_key_and_plain_text() -> None:
    base = rpc.turn_start_params("t", "x", "yk-b1")
    for k in base:
        with pytest.raises(ForbiddenRpc):
            check_request("turn/start", {kk: v for kk, v in base.items() if kk != k})
    for bad in (
        [{"type": "image", "url": "x"}],
        [{"type": "text", "text": "x", "text_elements": [], "extra": 1}],
        [],
        "x",
        [
            {"type": "text", "text": "a", "text_elements": []},
            {"type": "text", "text": "b", "text_elements": []},
        ],
    ):
        with pytest.raises(ForbiddenRpc):
            check_request("turn/start", {**base, "input": bad})
    with pytest.raises(ForbiddenRpc):
        check_request("turn/start", {**base, "threadId": ""})


def test_message_text_is_never_checked_against_the_denylist() -> None:
    """A human may well type 'approvalPolicy' in a message; only keys are checked."""
    check_request("turn/start", rpc.turn_start_params("t", 'set approvalPolicy="never" and model=x', "yk-b1"))


def test_initialize_declares_no_experimental_api() -> None:
    p = rpc.initialize_params()
    assert p["capabilities"] == {"experimentalApi": False} and p["clientInfo"]["name"] == "switchboard"
    check_request("initialize", p)
    with pytest.raises(ForbiddenRpc):
        check_request("initialize", {**p, "capabilities": {"experimentalApi": True}})
    with pytest.raises(ForbiddenRpc):
        check_request(
            "initialize", {**p, "capabilities": {"experimentalApi": False, "optOutNotificationMethods": []}}
        )


def test_thread_read_and_loaded_list_params() -> None:
    check_request("thread/read", rpc.thread_read_params("t", True))
    check_request("thread/loaded/list", rpc.loaded_list_params())
    check_request("thread/loaded/list", rpc.loaded_list_params("c1"))
    with pytest.raises(ForbiddenRpc):
        check_request("thread/read", {"threadId": "t", "includeTurns": True, "excludeTurns": True})
    with pytest.raises(ForbiddenRpc):
        check_request("thread/loaded/list", {"limit": 5, "model": "x"})


@pytest.mark.parametrize(
    "name", ["TurnStartParams", "TurnSteerParams", "ThreadReadParams", "ThreadLoadedListParams"]
)
def test_params_match_the_recorded_protocol_schema(name: str) -> None:
    """codex-cli 0.156.1 ``app-server generate-json-schema`` (v2), trimmed: every key
    switchboard sends exists in the schema, every required key is sent, and every
    schema key switchboard does not send is one it must never send (an override)."""
    schema = json.loads((SCHEMA / f"{name}.json").read_text())
    method = {
        "TurnStartParams": "turn/start",
        "TurnSteerParams": "turn/steer",
        "ThreadReadParams": "thread/read",
        "ThreadLoadedListParams": "thread/loaded/list",
    }[name]
    props = set(schema["properties"])
    sent = rpc.PARAM_KEYS[method]
    assert sent <= props
    assert set(schema.get("required", [])) <= sent
    if method == "turn/start":
        assert props - sent <= set(guardrails.CODEX_OVERRIDE_FIELDS), props - sent


# ------------------------------------------------------------ views
def test_thread_status_mapping() -> None:
    ts = rpc.thread_status
    assert ts({"type": "idle"}) == "idle"
    assert ts({"type": "active", "activeFlags": []}) == "busy"
    assert ts({"type": "active", "activeFlags": ["waitingOnApproval"]}) == "waiting-approval"
    assert ts({"type": "active", "activeFlags": ["waitingOnUserInput"]}) == "waiting-approval"
    assert ts({"type": "notLoaded"}) == "offline" and ts({"type": "systemError"}) == "offline"
    assert ts(None) is None and ts("idle") is None
    # fail closed: an unknown status type or wait flag (a later Codex) is a hold, never busy
    assert ts({"type": "weird"}) == "waiting-approval"
    assert ts({"type": "active", "activeFlags": ["waitingOnSomethingNew"]}) == "waiting-approval"
    assert ts({"type": "active"}) == "busy"


def test_active_turn_and_contains() -> None:
    th = {
        "turns": [
            {"id": "a", "status": "completed", "items": []},
            {"id": "b", "status": "inProgress", "items": [{"text": "yk:b12.0badf00d"}]},
        ]
    }
    assert rpc.active_turn_id(th) == "b"
    assert rpc.active_turn_id({"turns": [{"id": "a", "status": "interrupted"}]}) is None
    assert rpc.active_turn_id({}) is None
    assert rpc.contains(th, "yk:b12.0badf00d") and not rpc.contains(th, "yk:b13.0badf00d")


def test_the_join_proof_must_be_switchboards_own_join_result() -> None:
    """The nonce anywhere else (a command's output, a file the model printed, a
    queued prompt) proves nothing (DESIGN §9.3)."""
    needle = "yk:j0123456789abcdef"

    def th(*items: dict) -> dict:
        return {"turns": [{"id": "t", "status": "completed", "items": list(items)}]}

    good = {
        "type": "mcpToolCall",
        "id": "c",
        "server": "switchboard",
        "tool": "join",
        "status": "completed",
        "arguments": {},
        "result": {"content": [{"type": "text", "text": f"joined {needle}"}]},
    }
    assert rpc.join_proven(th(good), needle)
    assert not rpc.join_proven(th(good), "yk:jffffffffffffffff")
    for bad in (
        {**good, "server": "other"},
        {**good, "tool": "say"},
        {**good, "status": "inProgress"},
        {**good, "result": None},
        {**good, "type": "dynamicToolCall"},
        {"type": "commandExecution", "id": "x", "aggregatedOutput": needle},
        {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": needle}]},
        {**good, "result": {"content": []}, "arguments": {"text": needle}},
    ):
        assert not rpc.join_proven(th(bad), needle), bad
    assert not rpc.join_proven({}, needle) and not rpc.join_proven({"turns": "x"}, needle)


def test_server_version_from_initialize() -> None:
    assert rpc.server_version({"userAgent": "codex_cli_rs/0.156.1 (Mac OS 26.0; arm64)"}) == "0.156.1"
    assert rpc.server_version({"userAgent": "fake/0.156.1"}) == "0.156.1"
    assert rpc.server_version({}) is None and rpc.server_version(None) is None


# ------------------------------------------------------------ socket
def _short_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="yk-cx-", dir="/tmp"))


def test_check_socket() -> None:
    d = _short_dir()
    try:
        with pytest.raises(rpc.SocketRefused):
            rpc.check_socket(d / "missing.sock")
        (d / "file").write_text("x")
        with pytest.raises(rpc.SocketRefused):
            rpc.check_socket(d / "file")
        with pytest.raises(rpc.SocketRefused):
            rpc.check_socket("relative.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(d / "cx.sock"))
        link = d / "link.sock"
        os.symlink(d / "cx.sock", link)
        assert rpc.check_socket(link) == os.path.realpath(d / "cx.sock")
        os.chmod(d, 0o777)
        with pytest.raises(rpc.SocketRefused):
            rpc.check_socket(d / "cx.sock")  # a directory others can write
        os.chmod(d, 0o700)
        s.close()
    finally:
        os.chmod(d, 0o700)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


# ------------------------------------------------------------ the wire
class _Srv:
    """A scripted app-server: answers requests, and sends one server request."""

    def __init__(self, path: str):
        self.path = path
        self.got: list[dict] = []

    async def handler(self, ws) -> None:
        async for frame in ws:
            m = json.loads(frame)
            self.got.append(m)
            if m.get("method") == "initialize":
                await ws.send(json.dumps({"id": m["id"], "result": {"userAgent": "x"}}))
                await ws.send(
                    json.dumps(
                        {
                            "id": 77,
                            "method": "item/commandExecution/requestApproval",
                            "params": {"threadId": "t"},
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "method": "thread/status/changed",
                            "params": {"threadId": "t", "status": {"type": "idle"}},
                        }
                    )
                )
            elif m.get("method") == "thread/loaded/list":
                if m.get("params", {}).get("cursor"):
                    await ws.send(json.dumps({"id": m["id"], "result": {"data": ["t3"], "nextCursor": None}}))
                else:
                    await ws.send(
                        json.dumps({"id": m["id"], "result": {"data": ["t1", "t2"], "nextCursor": "c"}})
                    )
            elif m.get("method") == "turn/steer":
                await ws.send(
                    json.dumps(
                        {"id": m["id"], "error": {"code": -32600, "message": "no active turn to steer"}}
                    )
                )


async def test_client_never_answers_server_requests_and_pages_loaded_list() -> None:
    from websockets.asyncio.server import unix_serve

    d = _short_dir()
    path = str(d / "cx.sock")
    srv = _Srv(path)
    notes: list[tuple[str, dict]] = []
    server = await unix_serve(srv.handler, path)
    try:
        c = CodexRpc(path, on_notification=lambda m, p, t: notes.append((m, p)))
        await c.connect()
        assert await c.loaded_threads() == {"t1", "t2", "t3"}
        with pytest.raises(rpc.RpcError) as ei:
            await c.request("turn/steer", rpc.turn_steer_params("t", "u", "x", "yk-b1"))
        assert ei.value.code == -32600
        with pytest.raises(ForbiddenRpc):
            await c.request("thread/resume", {"threadId": "t"})
        await asyncio.sleep(0.1)
        assert c.server_requests == {"item/commandExecution/requestApproval": 1}
        assert notes and notes[0][0] == "thread/status/changed"
        await c.close()
        # nothing we sent was a response (no "result"/"error"), and no method outside the allowlist
        assert all("method" in m for m in srv.got)
        assert {m["method"] for m in srv.got} <= rpc.ALLOWED_METHODS
        assert not any(m.get("id") == 77 for m in srv.got)
    finally:
        server.close()
        await server.wait_closed()
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
