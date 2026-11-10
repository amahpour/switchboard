"""Admin-only live archives: fresh checks, background work and expiring downloads (DESIGN.md §57)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from test_passkeys import Browser
from test_people import ADMIN_PW, add, join_as_bob, set_up, signin

from switchboard import backup
from switchboard.broker.auth import sha256_hex
from switchboard.broker.hub import WsSubscriber

pytest_plugins = ("test_people",)


def finish(broker: InProcBroker) -> None:
    """Wait for the actual worker task and its notices, rather than a wall-clock guess."""

    async def done() -> None:
        await asyncio.shield(broker.state.backups.task)

    asyncio.run_coroutine_threadsafe(done(), broker.loop).result(15)


def check(admin: Browser) -> None:
    assert admin.post("/api/signin/check", {"password": ADMIN_PW}).status_code == 200


def start(admin: Browser) -> str:
    response = admin.post("/api/backups")
    assert response.status_code == 202, response.text
    return response.json()["id"]


def download(job_id: str) -> str:
    return f"/api/backups/{job_id}/download"


def subscriber(broker: InProcBroker, browser: Browser) -> WsSubscriber:
    sub = WsSubscriber()
    sub.sid_hash = sha256_hex(browser.cookies["switchboard_session"])
    broker.on_loop(broker.state.hub.add, sub)
    return sub


def notices(broker: InProcBroker, sub: WsSubscriber) -> list[dict[str, Any]]:
    def take() -> list[dict[str, Any]]:
        found = []
        while not sub.q.empty():
            found.append(sub.q.get_nowait())
        return [f for f in found if isinstance(f, dict) and f.get("backup")]

    return broker.on_loop(take)


def test_backup_and_download_require_admin_and_a_fresh_check(ticking: tuple[InProcBroker, FakeClock]) -> None:
    """A stolen old admin session, an ordinary teammate and no session all fail closed."""
    broker, _clock = ticking
    admin = set_up(broker)
    assert admin.get("/api/backups").json() == {"running": False, "retry_after": 0, "job": None}
    bob = join_as_bob(broker, admin)
    anon = Browser(broker)
    try:
        for path in ("/api/backups", "/api/backups/" + "a" * 32, download("a" * 32)):
            assert anon.get(path).status_code == 401
            assert bob.get(path).status_code == 403
        assert anon.post("/api/backups").status_code == 401
        assert bob.post("/api/backups").status_code == 403
        broker.on_loop(lambda: broker.state.passkey_checks.clear())
        refused = admin.post("/api/backups")
        assert refused.status_code == 403 and refused.json()["error"] == "reauth"
        check(admin)
        job = start(admin)
        finish(broker)
        broker.on_loop(lambda: broker.state.passkey_checks.clear())
        for path in (f"/api/backups/{job}", download(job)):
            refused = admin.get(path)
            assert refused.status_code == 403 and refused.json()["error"] == "reauth"
        check(admin)
        assert admin.get(download(job)).status_code == 200
    finally:
        anon.close()
        bob.close()
        admin.close()


def test_background_backup_does_not_block_rooms_and_notices_reach_only_admins(
    ticking: tuple[InProcBroker, FakeClock], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Block the disk worker at a handshake while the event loop handles a room write and another request."""
    broker, _clock = ticking
    admin = set_up(broker)
    bob = join_as_bob(broker, admin)
    bob_id = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob")
    assert admin.post(f"/api/people/{bob_id}/admin", {"admin": True}).status_code == 200
    one_time = add(admin, "carol")["password"]
    carol = Browser(broker)
    assert signin(carol, "carol@example.com", one_time).status_code == 200
    assert carol.post("/api/me/password", {"password": "carol's own secret 1"}).status_code == 200
    a, b, c = subscriber(broker, admin), subscriber(broker, bob), subscriber(broker, carol)
    entered, release = threading.Event(), threading.Event()
    original = backup.create

    def blocked(*args: Any, **kwargs: Any) -> Path:
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup, "create", blocked)
    try:
        job = start(admin)
        assert entered.wait(10)
        assert admin.post("/api/rooms", {"name": "#during"}).status_code == 200
        assert admin.post("/api/rooms/during/say", {"text": "rooms carry on"}).status_code == 200
        second = admin.post("/api/backups")
        assert second.status_code == 409 and second.json()["error"] == "backup_running"
        release.set()
        finish(broker)
        mine, other, ordinary = notices(broker, a), notices(broker, b), notices(broker, c)
        assert any(f["backup"].get("download") and f["backup"]["id"] == job for f in mine)
        assert other and all(not f["backup"].get("download") for f in other)
        assert not ordinary
        result = admin.get(download(job))
        assert result.status_code == 200 and result.headers["content-type"].startswith("application/x-tar")
        assert result.headers["cache-control"] == "no-store"
        assert "attachment;" in result.headers["content-disposition"]
        assert result.headers["x-content-type-options"] == "nosniff"
        # the archive, taken while rooms were being written, passes phase 1's own verification
        copy = tmp_path / "downloaded.tar"
        copy.write_bytes(result.content)
        stage = tmp_path / "stage"
        stage.mkdir()
        assert backup._unpack(copy, stage)["schema"]
        events = broker.on_loop(lambda: broker.state.store.recent_events(kinds=["backup"], limit=20))
        assert {e.data["what"] for e in events} >= {"started", "ready", "download"}
        assert all("person" in e.data and "id" in e.data for e in events)
        assert any("download" in f["text"].lower() for f in notices(broker, b))
    finally:
        release.set()
        finish(broker)
        carol.close()
        bob.close()
        admin.close()


def test_link_expires_even_when_the_admin_checks_again(ticking: tuple[InProcBroker, FakeClock]) -> None:
    """Refreshing identity must not extend an old backup's download lifetime."""
    broker, clock = ticking
    admin = set_up(broker)
    try:
        job = start(admin)
        finish(broker)
        assert admin.get(f"/api/backups/{job}").status_code == 200
        clock.advance(601)
        check(admin)
        assert admin.get(download(job)).status_code == 410
        assert admin.get(f"/api/backups/{job}").status_code == 410
    finally:
        admin.close()


def test_a_revoked_admin_cannot_download_a_ready_archive(ticking: tuple[InProcBroker, FakeClock]) -> None:
    """Roles are read on every download, rather than cached at backup creation."""
    broker, _clock = ticking
    admin = set_up(broker)
    bob = join_as_bob(broker, admin)
    try:
        pid = next(p["id"] for p in admin.get("/api/people").json()["people"] if p["name"] == "bob")
        assert admin.post(f"/api/people/{pid}/admin", {"admin": True}).status_code == 200
        job = start(bob)
        finish(broker)
        assert bob.get(f"/api/backups/{job}").status_code == 200
        assert admin.post(f"/api/people/{pid}/admin", {"admin": False}).status_code == 200
        assert bob.get(download(job)).status_code == 403
        assert admin.get(download(job)).status_code == 200
    finally:
        bob.close()
        admin.close()


def test_rate_limit_and_retention_bound_the_archives(ticking: tuple[InProcBroker, FakeClock]) -> None:
    """Finished jobs still consume the five-minute rate limit; only the latest three files remain."""
    broker, clock = ticking
    admin = set_up(broker)
    try:
        for i in range(5):
            if i:
                clock.advance(300)
                check(admin)
            job = start(admin)
            finish(broker)
            response = admin.post("/api/backups")
            assert response.status_code == 429 and response.json()["error"] == "slow_down"
        root = broker.paths.home / "backups"
        files = list(root.glob("*.tar"))
        assert len(files) == 3
        assert root.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
        assert not list(root.glob("*.part"))
        assert admin.get(download(job)).status_code == 200
    finally:
        admin.close()


def test_failed_backup_is_reported_without_a_download_or_internal_error(
    ticking: tuple[InProcBroker, FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk/copy failure removes its partial file and gives only a fixed, useful failure notice."""
    broker, _clock = ticking
    admin = set_up(broker)

    def fail(*args: Any, **kwargs: Any) -> Path:
        raise backup.BackupError("private internal failure detail")

    monkeypatch.setattr(backup, "create", fail)
    try:
        job = start(admin)
        finish(broker)
        status = admin.get("/api/backups").json()
        assert status["job"]["state"] == "failed"
        assert "private internal" not in json.dumps(status)
        assert admin.get(download(job)).status_code == 409
        assert not list((broker.paths.home / "backups").glob("*.part"))
        events = broker.on_loop(lambda: broker.state.store.recent_events(kinds=["backup"], limit=20))
        assert {e.data["what"] for e in events} == {"started", "failed"}
    finally:
        admin.close()


def test_clients_cannot_choose_a_path_or_request_an_unknown_job(
    ticking: tuple[InProcBroker, FakeClock],
) -> None:
    """The endpoint takes no archive paths or scheduling tokens from a client."""
    broker, _clock = ticking
    admin = set_up(broker)
    try:
        assert admin.post("/api/backups", {"path": "config.toml"}).status_code == 400
        assert admin.get(download("a" * 32)).status_code == 404
        assert admin.get(download("not-an-id")).status_code == 404
        assert not (broker.paths.home / "backups").exists()
    finally:
        admin.close()


def test_desktop_login_link_does_not_bypass_the_fresh_check(broker: InProcBroker) -> None:
    """Until desktop reauthentication is decided, a login-link session cannot stand in for a password."""
    client = broker.web_client()
    try:
        response = client.post(
            "/api/backups", json={}, headers={"Origin": broker.origin, "X-Switchboard": "1"}
        )
        assert response.status_code == 403 and response.json()["error"] == "reauth"
    finally:
        client.close()
