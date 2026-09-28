"""The Claude session registry reader (``switchboard.claude_registry``, DESIGN.md §27.16 M8d):
it never blocks, never raises and never follows a final symlink, whatever a same-user
process leaves at ``<sessions_dir>/<pid>.json``; ``registry_status`` bounds the time."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from switchboard.broker.peer import claude_registry_socket
from switchboard.claude_registry import MAX_BYTES, read_registry, registry_status


def test_a_plain_file_reads(tmp_path: Path) -> None:
    (tmp_path / "7.json").write_text('{"pid": 7, "status": "busy", "messagingSocketPath": "/tmp/s.sock"}')
    assert read_registry(str(tmp_path), 7) == {"pid": 7, "status": "busy", "messagingSocketPath": "/tmp/s.sock"}
    assert claude_registry_socket(str(tmp_path), 7) == "/tmp/s.sock"
    assert read_registry(str(tmp_path), 8) is None  # missing


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"pid": 7, "status": "idle", "messagingSocketPath": "/tmp/s.sock"}')
    (tmp_path / "7.json").symlink_to(real)
    assert read_registry(str(tmp_path), 7) is None
    assert claude_registry_socket(str(tmp_path), 7) is None


def test_a_fifo_never_blocks(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "7.json")
    got: list[object] = []
    t = threading.Thread(target=lambda: got.append((read_registry(str(tmp_path), 7),
                                                    claude_registry_socket(str(tmp_path), 7))), daemon=True)
    t.start()
    t.join(5.0)
    assert not t.is_alive(), "reading a FIFO blocked"
    assert got == [(None, None)]


@pytest.mark.parametrize("content", [
    b"[" * 200_000 + b"]" * 200_000,  # deeper than the JSON parser recurses
    b'{"statusUpdatedAt": ' + b"9" * 5000 + b"}",  # more digits than Python parses
    b"not json",
    b"\xff\xfe",  # not UTF-8
    b"[1, 2]",  # not an object
])
def test_what_does_not_parse_is_unreadable(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "7.json").write_bytes(content)
    assert read_registry(str(tmp_path), 7) is None
    assert claude_registry_socket(str(tmp_path), 7) is None


def test_a_file_over_a_mebibyte_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "7.json").write_text('{"status": "idle", "x": "' + "a" * MAX_BYTES + '"}')
    assert read_registry(str(tmp_path), 7) is None


@pytest.mark.parametrize(("upd", "since"), [
    (1790000000123, 1790000000.123),  # epoch ms
    (1790000000, 1790000000.0),  # seconds tolerated
    (10**400, None),  # would overflow a float
    (float("inf"), None),
    (float("nan"), None),
    (-5, None),
    (0, None),
    (True, None),
    ("1790000000123", None),
])
def test_registry_status_bounds_the_time(upd: object, since: float | None) -> None:
    status, got = registry_status({"status": "idle", "statusUpdatedAt": upd})
    assert status == "idle"
    assert got == (pytest.approx(since) if since is not None else None)


def test_registry_status_odd_status() -> None:
    assert registry_status({"status": 7}) == (None, None)
    assert registry_status({"status": "x" * 33}) == (None, None)
    assert registry_status({}) == (None, None)
