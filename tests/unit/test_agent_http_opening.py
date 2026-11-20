"""What an initialize keeps so a restart can open its session again, as the same client (DESIGN.md §49.2,
#823, #903)."""

from __future__ import annotations

import json
from typing import Any

from switchboard.broker import agent_http


def initialize(name: str) -> bytes:
    params = {"protocolVersion": "2025-06-18", "clientInfo": {"name": name, "version": "1"}}
    return json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": params}).encode()


def test_a_known_name_that_would_clean_into_an_unknown_one_is_not_kept(monkeypatch: Any) -> None:
    """A session is classified from its raw name and a reopened one from the kept, cleaned name, so they must
    read the same. The reverse of the integration test's names that clean into a known client: a name the
    broker classifies whose cleaned form it wouldn't (no shipped name is one, so the table is patched) is not
    kept either, so a reopen can't lose the client it was; an unknown name that stays unknown is kept,
    cleaned. Fails if a kept name could classify otherwise than the raw one (#903, F12)."""
    monkeypatch.setitem(agent_http.CLIENTS, "Two  Spaces", "cursor")
    assert agent_http._opening(initialize("Two  Spaces")) is None
    assert agent_http._opening(initialize("Cursor")) == ("Cursor", "1", "2025-06-18")
    assert agent_http._opening(initialize("\x00my-client")) == ("my-client", "1", "2025-06-18")
