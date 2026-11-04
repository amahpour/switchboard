"""Files a room holds (#544): a person uploads, an agent uploads with a one-time link and
downloads with another, a private room hides its files, delete and the sweep remove them,
and a token is never stored or logged.

Each case uses the in-process broker and a scripted agent. The clock is a ``FakeClock``:
expiry is an advance, never a sleep. A token in the database, the log or a message fails
the search at the end of the agent's upload.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import httpx
import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from test_passkeys import hosted_broker, stop
from test_people import join_as_bob, set_up

from switchboard.config import Config, FilesCfg

DAY = 86400.0
UPLOAD_RE = re.compile(r"(http://\S+/api/upload/([A-Za-z0-9_-]+))")
DOWNLOAD_RE = re.compile(r"download: (http://\S+/api/files/[0-9a-f]{32}\?t=([A-Za-z0-9_-]+))")


@pytest.fixture
def world(tmp_home: Path):
    clock = FakeClock()
    cfg = Config(human_name="alice", files=FilesCfg(max_mb=1)).with_delivery(quiet_s=0.0, max_hold_s=0.0)
    b = InProcBroker(tmp_home, cfg, clock=clock).start()
    web = b.web_client()
    made = web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers())
    assert made.status_code == 200, made.text
    b.web = web
    yield b, clock
    web.close()
    b.stop()


def _upload_headers(b: InProcBroker, name: str) -> dict[str, str]:
    headers = b.write_headers()
    headers["Content-Type"] = "application/octet-stream"
    headers["X-Switchboard-Filename"] = name
    return headers


def _put(url: str, body: bytes) -> httpx.Response:
    """The agent's curl: no Origin, no session. The token in the URL is the capability."""
    return httpx.put(url, content=body, timeout=10.0)


def _tokens_absent(b: InProcBroker, *tokens: str) -> None:
    """A token is a capability. The database keeps its hash, and no message holds the token."""
    blob = b.paths.db.read_bytes()
    wal = Path(str(b.paths.db) + "-wal")
    if wal.is_file():
        blob += wal.read_bytes()
    texts = b.state.store.con.execute("SELECT text FROM messages").fetchall()
    for token in tokens:
        raw = token.encode()
        assert raw not in blob
        assert all(token not in row[0] for row in texts)


async def test_a_person_uploads_and_the_download_is_an_attachment(world) -> None:
    """The signed-in page posts the bytes, then the artifact line. The download is an
    attachment of opaque bytes. Fails if the file is missing, or served inline, or without
    nosniff and no-store."""
    b, _clock = world
    body = b"hello from alice"
    r = b.web.post("/api/rooms/build/files", content=body, headers=_upload_headers(b, "notes.txt"))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["name"] == "notes.txt" and got["size"] == len(body)
    assert got["sha256"] == hashlib.sha256(body).hexdigest()
    down = b.web.get(f"/api/files/{got['id']}")
    assert down.status_code == 200
    assert down.content == body
    assert down.headers["content-disposition"].startswith("attachment;")
    assert "notes.txt" in down.headers["content-disposition"]
    assert down.headers["content-type"].split(";")[0] == "application/octet-stream"
    assert down.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in down.headers["cache-control"]
    line = f"artifact: notes.txt | {len(body)} | sha256:{got['sha256']} | switchboard:file/{got['id']}"
    said = b.web.post("/api/rooms/build/say", json={"text": line}, headers=b.write_headers())
    assert said.status_code == 200, said.text
    hist = b.web.get("/api/rooms/build/messages")
    assert hist.status_code == 200
    msg = next(m for m in hist.json()["messages"] if m["id"] == said.json()["id"])
    assert msg["files"] == [{"id": got["id"], "name": "notes.txt", "state": "stored"}]


async def test_an_agent_uploads_once_and_reads_a_download_link(
    world, caplog: pytest.LogCaptureFixture
) -> None:
    """A local-path artifact line adds a curl line. That token stores the file once, then
    fails. The other agent's read gains a download link that works for 10 minutes.
    A sha256 that does not match is deleted. A body over the cap is refused. No token is
    stored, logged or written into a message. Fails if a second use or an expired link works,
    or the token is in the database."""
    b, clock = world
    caplog.set_level(logging.INFO, logger="switchboard")
    body = b"hello"
    sha = hashlib.sha256(body).hexdigest()
    line = f"artifact: notes.txt | {len(body)} | sha256:{sha} | /Users/someone/notes.txt"
    async with FakeAgent(b.home, "ada") as ada, FakeAgent(b.home, "bea") as bea:
        assert (await ada.join("#build", "Ada"))["ok"]
        assert (await bea.join("#build", "Bea"))["ok"]
        said = await ada.say("#build", line)
        assert "share" in said, said
        found = UPLOAD_RE.search(said["share"])
        assert found, said["share"]
        url, token = found.group(1), found.group(2)
        assert token not in said["share"].split(url)[0]
        put = _put(url, body)
        assert put.status_code == 201, put.text
        again = _put(url, body)
        assert again.status_code == 409, again.text
        read = await bea.read("#build")
        link = DOWNLOAD_RE.search(read["text"] or "")
        assert link, read["text"]
        down_url, down_token = link.group(1), link.group(2)
        got = httpx.get(down_url, timeout=10.0)
        assert got.status_code == 200 and got.content == body
        clock.advance(600)
        expired = httpx.get(down_url, timeout=10.0)
        missing = b.web.get("/api/files/" + "ab" * 16)
        assert expired.status_code == 404 and expired.json() == missing.json()
        _tokens_absent(b, token, down_token)
        assert token not in caplog.text and down_token not in caplog.text

        late = "artifact: later.txt | /Users/someone/later.txt"
        said_late = await ada.say("#build", late)
        late_url = UPLOAD_RE.search(said_late["share"]).group(1)
        late_token = UPLOAD_RE.search(said_late["share"]).group(2)
        clock.advance(600)
        refused = _put(late_url, b"x")
        assert refused.status_code == 410, refused.text
        _tokens_absent(b, late_token)
        assert late_token not in caplog.text

        wrong = "ab" * 32
        bad_line = f"artifact: bad.bin | 4 | sha256:{wrong} | /Users/someone/bad.bin"
        said_bad = await ada.say("#build", bad_line)
        bad_url, bad_token = (
            UPLOAD_RE.search(said_bad["share"]).group(1),
            UPLOAD_RE.search(said_bad["share"]).group(2),
        )
        mismatch = _put(bad_url, b"nope")
        assert mismatch.status_code == 400 and "sha256" in mismatch.text
        assert _put(bad_url, b"nope").status_code == 409
        file_id = b.state.store.con.execute("SELECT id FROM files WHERE name='bad.bin'").fetchone()[0]
        assert b.state.store.file_get(file_id).state != "stored"
        assert not (b.paths.files_dir / file_id).is_file()
        _tokens_absent(b, bad_token)

        big = "artifact: big.bin | /Users/someone/big.bin"
        said_big = await ada.say("#build", big)
        big_url = UPLOAD_RE.search(said_big["share"]).group(1)
        over = _put(big_url, b"x" * (1024 * 1024 + 1))
        assert over.status_code == 413, over.text
        assert "larger than 1 MB" in over.text


def test_a_private_rooms_file_is_a_404_to_someone_outside_it() -> None:
    """Who may read the room may download. Anyone else gets the same 404 as a missing file.
    Fails if the admin, who is not in the room, can fetch it, or the 404 says it exists."""
    b = hosted_broker()
    try:
        admin = set_up(b)
        bob = join_as_bob(b, admin)
        made = bob.post("/api/rooms", {"name": "#secret", "private": True})
        assert made.status_code == 200, made.text
        headers = bob.headers(True)
        headers["Content-Type"] = "application/octet-stream"
        headers["X-Switchboard-Filename"] = "notes.txt"
        up = bob.c.post("/api/rooms/secret/files", content=b"secret", headers=headers)
        bob.take(up)
        assert up.status_code == 200, up.text
        file_id = up.json()["id"]
        own = bob.get(f"/api/files/{file_id}")
        assert own.status_code == 200 and own.content == b"secret"
        outsider = admin.get(f"/api/files/{file_id}")
        missing = admin.get("/api/files/" + "cd" * 16)
        assert outsider.status_code == 404 and outsider.json() == missing.json()
    finally:
        stop(b)


async def test_deleting_a_room_removes_its_files(world) -> None:
    """``switchboard rooms delete`` removes the rows and the bytes. Fails if the file is
    still in the table or still on disk after the room is gone."""
    b, _clock = world
    body = b"gone"
    r = b.web.post("/api/rooms/build/files", content=body, headers=_upload_headers(b, "gone.txt"))
    assert r.status_code == 200, r.text
    file_id = r.json()["id"]
    assert (b.paths.files_dir / file_id).is_file()
    plan = b.call("room.delete", {"room": "#build", "dry_run": True})
    removed = b.call(
        "room.delete",
        {
            "room": "#build",
            "room_id": plan["room_id"],
            "name": plan["name"],
            "created_at": plan["created_at"],
        },
    )
    assert removed["removed"]["files"] == 1
    assert b.state.store.file_get(file_id) is None
    assert not (b.paths.files_dir / file_id).is_file()


async def test_the_sweep_removes_expired_files_and_keeps_a_fresh_one(world) -> None:
    """Stored files past the retention setting go, and so does a pending upload whose token
    has expired. A file stored after that stays. Fails if the sweep uses wall time, or
    deletes a file that is still inside the window."""
    b, clock = world
    old = b.web.post("/api/rooms/build/files", content=b"old", headers=_upload_headers(b, "old.txt"))
    assert old.status_code == 200, old.text
    old_id = old.json()["id"]
    async with FakeAgent(b.home, "ada") as ada:
        assert (await ada.join("#build", "Ada"))["ok"]
        pending = await ada.say("#build", "artifact: wait.txt | /Users/someone/wait.txt")
        await ada.leave("#build")
    assert "share" in pending
    clock.advance(31 * DAY)
    # the page's session is bound to the broker clock, so a month later it signs in again
    b.web.close()
    b.web = b.web_client()
    fresh = b.web.post("/api/rooms/build/files", content=b"new", headers=_upload_headers(b, "new.txt"))
    assert fresh.status_code == 200, fresh.text
    fresh_id = fresh.json()["id"]
    gone = b.state.attachments.sweep()
    assert old_id in gone
    assert fresh_id not in gone
    assert b.state.store.file_get(old_id) is None
    assert not (b.paths.files_dir / old_id).is_file()
    kept = b.state.store.file_get(fresh_id)
    assert kept is not None and kept.state == "stored"
    assert (b.paths.files_dir / fresh_id).is_file()
    pending_left = b.state.store.con.execute("SELECT id FROM files WHERE name='wait.txt'").fetchone()
    assert pending_left is None
