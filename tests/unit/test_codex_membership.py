"""Codex membership decisions belong to its adapters (issue #170, DESIGN.md §6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock
from engine_world import World

from switchboard.broker.peer import McpIdentity
from switchboard.config import Config
from switchboard.models import session_key

TID = "019a0000-0000-7000-8000-000000000170"


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config(human_name="alice"))


@pytest.mark.parametrize("host", ["", "fpga-pi"])
def test_codex_adapter_owns_thread_key_and_credential_binding(w: World, host: str) -> None:
    adapter = w.engine.adapter_for("codex", host)
    ident = McpIdentity(
        harness="codex",
        mcp_pid=8,
        mcp_start=1.0,
        agent_pid=7,
        agent_start=1.0,
        evidence="parent:codex",
        host=host,
    )
    with pytest.raises(ValueError, match="_meta.threadId"):
        adapter.session_key(ident, None)
    assert adapter.session_key(ident, TID) == session_key("codex", host, TID)
    p, _m = w.agent(TID, harness="codex", host=host)
    p = w.store.update_participant(p.id, thread_proof=1)
    assert adapter.credential_error(p, {"thread_id": TID}) is None
    assert adapter.credential_error(p, {"thread_id": "other"}) == (
        "this credential belongs to another Codex thread"
    )
    assert adapter.rebind_fields(p, same_mcp=True) == {}
    assert adapter.rebind_fields(p, same_mcp=False) == {"thread_proof": 0}
    assert adapter.keep_on_mcp_disconnect(p) is (host == "")
    assert adapter.rejoin_notice(p, "codex-1", same_mcp=True) is None
    assert adapter.rejoin_notice(p, "codex-1", same_mcp=False) == (
        "codex-1 re-joined from a new switchboard MCP server"
    )


def test_codex_thread_cannot_move_between_hosts(w: World) -> None:
    local, _m = w.agent(TID, harness="codex")
    remote = w.engine.adapter_for("codex", "fpga-pi")
    assert remote.join_conflict(w.store, TID, "fpga-pi") == "this thread is joined from another machine"
    assert remote.join_conflict(w.store, "another-thread", "fpga-pi") is None
    assert w.engine.adapter(local).join_conflict(w.store, TID, "") is None
