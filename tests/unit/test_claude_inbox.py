"""The Claude inbox poster (DESIGN.md §6.4, FINDINGS §2 1.2/1.4): frame format,
connection hold, the refusal guard, labels and ids. Every post here goes to a
fake socket in a temp dir; the build session's own inbox is never touched
(``clean_env`` strips CLAUDE_* from the environment)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
from fakes.fake_claude_inbox import FakeInbox

from switchboard.mcp import claude_inbox as ci


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch):
    d = Path(tempfile.mkdtemp(prefix="yk-ib-", dir="/tmp"))
    ib = FakeInbox(str(d / "in.sock"))
    monkeypatch.setenv(ci.SOCKET_ENV, ib.path)
    yield ib
    ib.close()
    for p in d.iterdir():
        p.unlink()
    d.rmdir()


def target(ib: FakeInbox) -> ci.InboxTarget:
    t = ci.verify_target("claude", ib.path, ib.path, os.getppid())
    assert t is not None
    return t


def test_frame_is_auth_then_one_plain_string_user_frame_held_open(inbox: FakeInbox) -> None:
    t0 = ci.post(
        target(inbox), "tok-x", "[switchboard] #build: hi", "switchboard:#build/alice", "yk-b7-00aa11bb", 0.3
    )
    [(conn, frame)] = inbox.wait_frames(1)
    assert conn.lines[0] == {"type": "auth", "token": "tok-x"}
    assert frame == {
        "type": "user",
        "message": {"role": "user", "content": "[switchboard] #build: hi"},
        "from": "switchboard:#build/alice",
        "msg_id": "yk-b7-00aa11bb",
    }
    assert isinstance(frame["message"]["content"], str)
    assert "priority" not in conn.raw.decode()  # never an urgency key
    for _ in range(100):
        if conn.t_close is not None:
            break
        time.sleep(0.01)
    assert conn.held_s is not None and conn.held_s >= 0.25  # Claude checks the live poster's pid
    assert abs(t0 - conn.t_accept) < 1.0


def test_no_token_means_no_auth_line(inbox: FakeInbox) -> None:
    ci.post(target(inbox), None, "[switchboard] x", "switchboard:#r/a", "yk-b1-00000000", 0)
    [(conn, _f)] = inbox.wait_frames(1)
    assert [x["type"] for x in conn.lines] == ["user"]


def test_guard_refuses_anything_but_the_verified_parent_socket(inbox: FakeInbox, monkeypatch) -> None:
    t = target(inbox)
    with pytest.raises(ci.InboxRefused):
        ci.post(None, "t", "[switchboard] x", "f", "m")
    # another parent (we were reparented: the session is gone)
    with pytest.raises(ci.InboxRefused):
        ci.post(ci.InboxTarget(sock=t.sock, ppid=t.ppid + 1), "t", "[switchboard] x", "f", "m")
    # the env names another socket
    monkeypatch.setenv(ci.SOCKET_ENV, t.sock + ".other")
    with pytest.raises(ci.InboxRefused):
        ci.post(t, "t", "[switchboard] x", "f", "m")
    monkeypatch.setenv(ci.SOCKET_ENV, t.sock)
    # not a socket
    reg = Path(t.sock).with_name("plain")
    reg.write_text("x")
    monkeypatch.setenv(ci.SOCKET_ENV, str(reg))
    with pytest.raises(ci.InboxRefused):
        ci.post(ci.InboxTarget(sock=str(reg), ppid=os.getppid()), "t", "[switchboard] x", "f", "m")
    assert inbox.frames() == []


def test_verify_target_needs_claude_and_the_registry_socket() -> None:
    assert ci.verify_target("claude", "/tmp/a.sock", "/tmp/a.sock", 42) == ci.InboxTarget("/tmp/a.sock", 42)
    assert ci.verify_target("claude", "/tmp/a.sock", "/tmp/b.sock", 42) is None
    assert ci.verify_target("claude", "/tmp/a.sock", None, 42) is None
    assert ci.verify_target("test", "/tmp/a.sock", "/tmp/a.sock", 42) is None
    assert ci.verify_target("unknown", "/tmp/a.sock", "/tmp/a.sock", 42) is None
    assert ci.verify_target("claude", None, None, 42) is None


def test_bad_text_is_refused(inbox: FakeInbox) -> None:
    with pytest.raises(ValueError):
        ci.post(target(inbox), "t", "", "f", "m")
    with pytest.raises(ValueError):
        ci.post(target(inbox), "t", "x" * (ci.MAX_TEXT + 1), "f", "m")


def test_labels_and_ids() -> None:
    assert ci.sender_label("#build", "alice") == "switchboard:#build/alice"
    assert ci.sender_label("#Build", "Codex-1\n\x1b[2J") == "switchboard:#build/codex-12j"
    assert ci.sender_label("", "") == "switchboard:#room/switchboard"
    ids = {ci.message_id(7) for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("yk-b7-") for i in ids)


def test_source_never_mentions_an_urgency_key() -> None:
    src = Path(ci.__file__).read_text()
    assert '"priority"' not in src and "'priority'" not in src
