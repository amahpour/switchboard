"""The satellite's own guards, in-process (DESIGN.md §27.16, M8d review): a registry read that
fails in a way nobody expected reads as unreadable, and a link frame whose handling fails
drops that frame, never the link's reader (which answers the pings that keep the link up)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from switchboard import claude_registry
from switchboard.broker import proc
from switchboard.paths import Paths
from switchboard.remote import proto
from switchboard.remote.satellite import Satellite


def sat(tmp_path: Path) -> Satellite:
    return Satellite(Paths.from_home(tmp_path), "fpga-pi", test_mode=True, sessions_dir=str(tmp_path),
                     harden_state="none")


def test_a_registry_read_that_raises_reads_as_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = proc.info(os.getpid())
    assert info is not None
    s = sat(tmp_path)
    (tmp_path / f"{os.getpid()}.json").write_text('{"status": "idle"}')
    assert s.claude_status(os.getpid(), info.start, None) == ("idle", None)

    def boom(*_a: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(claude_registry, "read_registry", boom)
    assert s.claude_status(os.getpid(), info.start, None) == (None, None)


def test_a_failing_frame_never_ends_the_link_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = sat(tmp_path)
    s.welcome = {"t": "welcome"}
    handled: list[int] = []

    def on_frame(f: dict[str, Any]) -> None:
        if not handled:
            handled.append(-1)
            raise RuntimeError("a bug in one frame's handling")
        handled.append(f["n"])

    monkeypatch.setattr(s, "on_frame", on_frame)

    async def go() -> None:
        s.done = asyncio.Event()
        reader = asyncio.StreamReader()
        for n in (1, 2, 3):
            reader.feed_data(proto.encode(proto.ping(n)))
        reader.feed_eof()
        await s._read_link(reader)

    asyncio.run(go())
    assert handled == [-1, 2, 3] and s.bye_why == "eof"  # read on to the end of the link
