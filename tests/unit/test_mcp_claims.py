"""What an MCP server says about itself in ``mcp.hello``: its switchboard version and its tools
(#249). Claims from outside, cut and checked here, used only to tell a person which members
can't use the review board, and granting nothing."""

from __future__ import annotations

from typing import Any

import pytest

from switchboard import __version__
from switchboard.broker.agents import MAX_TOOLS, behind_by_minor, tools_claim, version_claim
from switchboard.mcp import server as srv
from switchboard.mcp.client import BrokerConn


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (None, None),  # a server from before the list
        ("review", None),
        ({"review": True}, None),
        ([], ()),
        (["say", "review", "join", "review"], ("join", "review", "say")),
        (["review", 7, None, "Review", "re view", "x" * 33, "../x"], ("review",)),
        (["say\n"], ()),  # a trailing newline cannot break a who() row
    ],
)
def test_the_tool_list_is_a_checked_claim(value: Any, want: tuple[str, ...] | None) -> None:
    assert tools_claim(value) == want


def test_the_tool_list_is_read_up_to_a_count() -> None:
    names = [f"t{'a' * (i % 20)}_{chr(97 + i % 26)}" for i in range(500)]
    got = tools_claim(names + ["review"])
    assert got is not None and len(got) <= MAX_TOOLS and "review" not in got


@pytest.mark.parametrize(
    ("value", "want"),
    [("0.22.0", "0.22.0"), ("0.23.0.dev3+g1a2b3c", "0.23.0.dev3+g1a2b3c"), (None, None), (22, None),
     ("", None), ("0.22 beta", None), ("<b>", None), ("1" * 33, None), ("0.23.0\n", None)],
)  # fmt: skip
def test_the_version_is_a_checked_claim(value: Any, want: str | None) -> None:
    assert version_claim(value) == want


@pytest.mark.parametrize(
    ("claim", "broker", "behind"),
    [
        ("0.22.9", "0.24.0", True),
        ("0.23.0", "0.24.0", True),
        ("0.24.0", "0.24.0", False),
        ("0.24.0.dev3", "0.24.0", False),
        ("0.24.0", "0.24.9", False),
        ("1.9.0", "2.0.0", True),
        (None, "0.24.0", False),
        ("odd", "0.24.0", False),
    ],
)
def test_upgrade_note_compares_reported_minor_release(claim: str | None, broker: str, behind: bool) -> None:
    assert behind_by_minor(claim, broker) is behind


async def test_the_server_reports_the_tools_it_registers_and_its_version(tmp_path) -> None:
    """mcp.hello's list is the tools build_server registers, so it can't drift from them."""
    from fastmcp import Client

    st = srv.McpState(
        BrokerConn(tmp_path / "no.sock", backoff=(0.05, 0.1)),
        env={},
        parent_argv="/bin/zsh",
        ppid=None,
        sessions_dir=str(tmp_path),
    )
    async with Client(srv.build_server(st), mode="legacy") as c:
        registered = {t.name for t in await c.list_tools()}
    params = st.hello_params()
    assert set(params["tools"]) == registered == set(srv.TOOL_NAMES)
    assert params["version"] == __version__
    assert tools_claim(params["tools"]) == tuple(sorted(registered))
