"""Background admin backups and short-lived download state (DESIGN.md §57.1)."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from switchboard import backup
from switchboard.broker import people
from switchboard.broker.hub import WsSubscriber
from switchboard.broker.service import ServiceError
from switchboard.paths import ensure_private_dir

if TYPE_CHECKING:
    from switchboard.broker.app import BrokerState

log = logging.getLogger("switchboard.backups")
RATE_S = 300.0
DOWNLOAD_S = 600.0
KEEP = 3
ID_RE = re.compile(r"[0-9a-f]{32}\Z")
ARCHIVE_RE = re.compile(r"backup-([0-9a-f]{32})\.(tar|part)\Z")


class Refused(ServiceError):
    HTTP = {
        **ServiceError.HTTP,
        "unavailable": 503,
        "backup_running": 409,
        "slow_down": 429,
        "not_ready": 409,
        "expired": 410,
    }


@dataclass
class Job:
    id: str
    person: int | None
    name: str
    state: str = "running"
    expires_at: float = 0.0
    size: int = 0


class Backups:
    def __init__(self, state: BrokerState):
        self.state = state
        self.root = state.paths.home / "backups"
        self.jobs: dict[str, Job] = {}
        self.task: asyncio.Task[None] | None = None
        self.next_at = float("-inf")
        self.closing = False

    def _path(self, job_id: str) -> Path:
        return self.root / f"backup-{job_id}.tar"

    def _frame(self, job: Job, person: int | None) -> dict[str, Any]:
        valid = job.state == "ready" and self.state.clock.now() < job.expires_at
        return {
            "id": job.id,
            "state": "expired" if job.state == "ready" and not valid else job.state,
            "expires_at": job.expires_at,
            "download": valid and job.person == person,
            "size": job.size,
        }

    def status(self, who: people.Who) -> dict[str, Any]:
        latest = next((j for j in reversed(list(self.jobs.values())) if j.person == who.person_id), None)
        return {
            "running": self.task is not None and not self.task.done(),
            "retry_after": math.ceil(max(0, self.next_at - self.state.clock.now())),
            "job": self._frame(latest, who.person_id) if latest else None,
        }

    def start(self, who: people.Who) -> Job:
        if self.closing:
            raise Refused("unavailable", "the broker is shutting down")
        if self.task is not None and not self.task.done():
            raise Refused("backup_running", "a backup is already running")
        now = self.state.clock.now()
        if now < self.next_at:
            raise Refused("slow_down", "one backup every five minutes; try again later")
        job = Job(secrets.token_hex(16), who.person_id, who.name)
        self.state.store.add_event("backup", data={"what": "started", "person": who.name, "id": job.id})
        # Bound memory too. Files are pruned by the worker before another archive is written.
        self.jobs = dict(list(self.jobs.items())[-(KEEP - 1) :])
        self.jobs[job.id] = job
        self.next_at = now + RATE_S
        self.task = asyncio.create_task(self._run(job))
        self._notify(job, f"{job.name} started a backup. Rooms and agents can keep working.")
        return job

    def ready(self, job_id: str) -> Job:
        if not ID_RE.fullmatch(job_id) or job_id not in self.jobs:
            raise Refused("not_found", "no such backup")
        job = self.jobs[job_id]
        if job.state != "ready":
            raise Refused("not_ready", "this backup is not ready to download")
        if self.state.clock.now() >= job.expires_at:
            raise Refused("expired", "this backup link expired; make a new backup")
        return job

    def downloaded(self, job: Job, who: people.Who) -> None:
        # Record authorization before FileResponse starts streaming. A failed audit write
        # cannot serve bytes. A client disconnect still leaves an honest download-start event.
        self.state.store.add_event(
            "backup", data={"what": "download", "person": who.name, "id": job.id, "size": job.size}
        )
        self._notify(job, f"{who.name} started downloading a broker backup.")

    def _make(self, job: Job) -> int:
        root = ensure_private_dir(self.root)
        archives = []
        for path in root.iterdir():
            match = ARCHIVE_RE.fullmatch(path.name)
            if match is None:
                continue
            if match[2] == "part":
                path.unlink()  # an interrupted job has no live download after a restart
            else:
                archives.append(path)
        archives.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for old in archives[KEEP - 1 :]:
            old.unlink()
        part = root / f"backup-{job.id}.part"
        try:
            backup.create(self.state.paths, part)
            size = part.stat().st_size
            # A hard link publishes the completed file without ever overwriting a name.
            os.link(part, self._path(job.id), follow_symlinks=False)
            return size
        finally:
            part.unlink(missing_ok=True)

    async def _run(self, job: Job) -> None:
        try:
            job.size = await asyncio.to_thread(self._make, job)
            job.expires_at = self.state.clock.now() + DOWNLOAD_S
            self.state.store.add_event(
                "backup", data={"what": "ready", "person": job.name, "id": job.id, "size": job.size}
            )
            job.state = "ready"
            self._notify(job, f"{job.name}'s backup is ready. Download links work for ten minutes.")
        except Exception:
            job.state = "failed"
            log.warning("backup failed; check free space, permissions and stored attachments")
            try:
                self.state.store.add_event(
                    "backup", data={"what": "failed", "person": job.name, "id": job.id}
                )
                self._notify(
                    job, "Backup failed. Check free space and stored files; try again after five minutes."
                )
            except Exception:
                log.warning("backup failure could not be recorded")

    def _notify(self, job: Job, text: str) -> None:
        for sub in list(self.state.hub.subs):
            if not isinstance(sub, WsSubscriber) or sub.closed or sub.sid_hash is None:
                continue
            if not self.state.store.web_session_active(sub.sid_hash):
                continue
            who = people.who_of(self.state, sub.sid_hash)
            if who is None or who.must_reset or not who.is_admin:
                continue
            frame = self._frame(job, who.person_id)
            sub.offer(sub.format("notice", None, {"level": "info", "text": text, "backup": frame}))

    async def close(self) -> None:
        self.closing = True
        if self.task is not None:
            # Keep the store alive until the worker and its audit event finish. Cancelling
            # to_thread's await would leave disk work running after the database closes.
            await asyncio.shield(self.task)
