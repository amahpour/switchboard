"""OpenCode 2.1.4 with the V2 plugin, in a private standalone server (§12.4, #513).

Opt-in only. HOME, XDG directories, project, config and all state are temporary. Only the provider
API key is inherited; no real OpenCode config or database is read, and no permission is answered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, child_env

from switchboard.broker import agent_tokens

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEY = os.environ.get("OPENAI_API_KEY", "")
if not {"opencode", "all"}.intersection(os.environ.get("SWITCHBOARD_LIVE", "").split(",")):
    pytest.skip(
        "set SWITCHBOARD_LIVE=opencode to run the private OpenCode live test", allow_module_level=True
    )
if not shutil.which("opencode") or not PROVIDER_KEY:
    pytest.skip(
        "opencode and OPENAI_API_KEY are needed (no real harness config is read)", allow_module_level=True
    )


def until(check: Any, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)  # polling the named event, never a guessed completion time
    raise AssertionError("private OpenCode probe did not reach the expected event")


def test_real_opencode_join_wake_and_approval(broker: InProcBroker, tmp_path: Path) -> None:
    """Real tools join/say, the broker's idle envelope starts a second turn, and a real pending
    permission blocks it."""
    broker.call("room.create", {"name": "#build"})
    token = broker.on_loop(agent_tokens.make, broker.state, None, "laptop", "alice")["token"]
    project = tmp_path / "project"
    project.mkdir()
    driver = tmp_path / "driver"
    driver.mkdir()
    shutil.copyfile(ROOT / "tests/live/opencode_driver.ts", driver / "index.ts")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    ready, events = tmp_path / "ready", tmp_path / "events.jsonl"
    config = {
        "plugins": [str(ROOT / "plugins/opencode"), str(driver)],
        "mcp": {
            "servers": {
                "switchboard": {
                    "type": "remote",
                    "url": f"{broker.base}/mcp",
                    "oauth": False,
                    "headers": {"Authorization": "Bearer {env:SWITCHBOARD_AGENT_TOKEN}"},
                }
            }
        },
    }
    env = child_env(
        tmp_path,
        OPENAI_API_KEY=PROVIDER_KEY,
        OPENCODE_CONFIG_DIR=str(config_dir),
        OPENCODE_CONFIG_CONTENT=json.dumps(config),
        PWD=str(project),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "xdg-config"),
        SWITCHBOARD_URL=broker.base,
        SWITCHBOARD_AGENT_TOKEN=token,
        PROBE_READY=str(ready),
        PROBE_EVENTS=str(events),
    )
    log = tmp_path / "opencode.log"
    started = time.monotonic()
    with log.open("w") as out:
        proc = subprocess.Popen(
            [
                "opencode",
                "run",
                "--standalone",
                "-m",
                "openai/gpt-5.4-nano",
                "--format",
                "json",
                "Call probe_hold exactly once, wait for it, then reply LIVE_DONE. Do nothing else.",
            ],
            cwd=project,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    client = None
    try:
        until(lambda: ready.exists() or proc.poll() is not None)
        assert ready.exists(), log.read_text()
        client = httpx.Client(base_url=f"http://127.0.0.1:{ready.read_text()}", timeout=15)
        until(lambda: client.post("/ready").json().get("holding"))

        def control(path: str, **body: Any) -> Any:
            r = client.post(path, json=body)
            assert r.status_code == 200, (path, r.text)
            return r.json()

        session = control("/create")["id"]
        control(
            "/prompt",
            sessionID=session,
            delivery="queue",
            text=(
                "Join switchboard room #build as opencode-1, then say LIVE_JOIN_OK there. "
                "Find tools with search if needed. End your turn after saying it; do not call wait(). "
                "When switchboard wakes you with a request, follow it and end your turn again."
            ),
        )

        def history_has(marker: str) -> bool:
            return any(
                marker in m["text"] and m.get("from") == "opencode-1"
                for m in broker.call("room.history", {"room": "#build"})["messages"]
            )

        def member() -> dict[str, Any] | None:
            return next(
                (
                    m
                    for m in broker.call("room.who", {"room": "#build"})["members"]
                    if m["name"] == "opencode-1"
                ),
                None,
            )

        def listening() -> bool:
            def check() -> bool:
                p = broker.state.store.find_participant("opencode", f"opencode@laptop:{session}")
                return bool(p and broker.state.engine.sinks.parks_for(p.id))

            return broker.on_loop(check)

        until(lambda: history_has("LIVE_JOIN_OK"))
        until(listening)
        assert member()["status"] == "idle"
        wake_at = time.monotonic()
        wake_unix_ms = time.time() * 1000
        broker.call(
            "human.say", {"room": "#build", "text": "@opencode-1 say LIVE_WAKE_OK in this room and end."}
        )
        until(lambda: history_has("LIVE_WAKE_OK"))
        until(listening)
        trace = control("/state", sessionID=session)
        assert Path(trace["info"]["location"]["directory"]).resolve() == project.resolve()
        assert "source" in json.dumps(trace["context"]) and "switchboard" in json.dumps(trace["context"])
        wake_s = time.monotonic() - wake_at
        starts = [json.loads(e) for e in events.read_text().splitlines()]
        wake_ms = (
            next(
                e["at"]
                for e in starts
                if e["type"] == "session.execution.started"
                and e["data"]["sessionID"] == session
                and e["at"] >= wake_unix_ms
            )
            - wake_unix_ms
        )
        control("/ask", sessionID=session)
        until(lambda: control("/state", sessionID=session)["pending"])
        until(lambda: member()["status"] == "waiting-approval")
        before = len([e for e in events.read_text().splitlines() if "session.execution.started" in e])
        broker.call("human.say", {"room": "#build", "text": "@opencode-1 say MUST_NOT_RUN"})
        assert not listening() and not history_has("MUST_NOT_RUN")
        assert control("/state", sessionID=session)["pending"]
        after = len([e for e in events.read_text().splitlines() if "session.execution.started" in e])
        assert before == after
        control("/remove", sessionID=session)
        until(lambda: member()["status"] == "offline")
        assert token not in log.read_text()
        print(
            f"LIVE RESULTS opencode: real join/say; queue idle turn {wake_ms:.0f}ms, reply {wake_s:.2f}s; "
            "pending approval held; deleted offline; isolated config; "
            f"elapsed {time.monotonic() - started:.1f}s"
        )
        print(
            "LIVE RESULTS opencode cost:",
            trace["info"].get("cost"),
            "target session (plus bootstrap hold turn)",
        )
        control("/release")
        proc.wait(timeout=30)
    finally:
        if client:
            client.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
