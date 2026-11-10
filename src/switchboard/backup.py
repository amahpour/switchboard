"""Live data archives and offline restore (DESIGN.md §57).

An uncompressed tar streams bytes from disk. Only named regular data files are read or
restored; tar paths, owners and modes are never applied. Configuration stays with the deployment.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Self

from switchboard import db
from switchboard.paths import Paths, ensure_private_dir
from switchboard.store import Store

MANIFEST_MAX = 16 * 1024 * 1024
MEMBERS_MAX = 100_000
FILE_NAME = re.compile(r"files/[0-9a-f]{32}\Z")


class BackupError(RuntimeError):
    """A backup or restore was refused; no archive content is quoted in the error."""


@dataclass(frozen=True)
class Restored:
    previous: Path
    warning: str


class _DataInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive: tarfile.TarFile) -> Self:
        # Our names fit ordinary tar headers. Refuse extension headers *before* tarfile
        # buffers their data or expands a sparse map; the later member check is too late.
        info = cls.frombuf(archive.fileobj.read(tarfile.BLOCKSIZE), archive.encoding, archive.errors)
        if info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or info.size < 0:
            raise BackupError("archive contains an unsafe or extended header")
        info.offset_data = archive.fileobj.tell()
        info.offset = info.offset_data - tarfile.BLOCKSIZE
        blocks = (info.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
        archive.offset = info.offset_data + blocks * tarfile.BLOCKSIZE
        return info


def _regular(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise BackupError("archive data must be regular files, never links or devices")
    return os.fdopen(fd, "rb")


def _digest(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _add(tar: tarfile.TarFile, name: str, stream: BinaryIO) -> str:
    digest = _digest(stream)
    stream.seek(0)
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = os.fstat(stream.fileno()).st_size
    tar.addfile(info, stream)
    return digest


def create(paths: Paths, archive: str | os.PathLike) -> Path:
    """A verified live DB snapshot and its stored attachments, never overwriting an archive.

    A deletion racing the attachment copy fails this attempt, rather than publishing a
    backup with missing bytes. Uploads after the SQLite snapshot belong to the next backup.
    """
    dest = Path(archive)
    made = False
    complete = False
    try:
        if not paths.db.is_file() or paths.db.is_symlink():
            raise BackupError("no regular switchboard.db to back up")
        ensure_private_dir(paths.home)
        ensure_private_dir(paths.files_dir)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        made = True
        with (
            os.fdopen(fd, "wb") as output,
            tempfile.TemporaryDirectory(prefix=".backup-", dir=paths.home) as td,
        ):
            os.fchmod(output.fileno(), 0o600)
            con = db.connect(paths.db, readonly=True)
            try:
                # Counts and SQLite's backup see exactly the same WAL read snapshot. A deferred
                # read never reserves the writer lock, so rooms and hooks can keep committing.
                with db.read_snapshot(con):
                    version = db.schema_version(con)
                    if version is None or not 1 <= version <= db.SCHEMA_VERSION:
                        raise BackupError("unsupported database schema")
                    store = Store(con)
                    rp_id = store.meta_get("passkey_rp_id")
                    files = store.backup_files(limit=MEMBERS_MAX) if version >= 40 else []
                    if len(files) + 2 > MEMBERS_MAX:
                        raise BackupError("too many files for one backup archive")
                    copy, counts = db.backup_verified(
                        con, Path(td) / "switchboard.db", tables=db.tables_of(version), what="data backup"
                    )
            finally:
                con.close()
            hashes: dict[str, str] = {}
            with tarfile.open(fileobj=output, mode="w|", format=tarfile.GNU_FORMAT) as tar:
                with _regular(copy) as stream:
                    hashes["switchboard.db"] = _add(tar, "switchboard.db", stream)
                for file_id, size, digest in files:
                    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
                        raise BackupError("invalid stored attachment id")
                    name = f"files/{file_id}"
                    with _regular(paths.files_dir / file_id) as stream:
                        if os.fstat(stream.fileno()).st_size != size:
                            raise BackupError("stored attachment size changed")
                        hashes[name] = _add(tar, name, stream)
                        if hashes[name] != digest:
                            raise BackupError("stored attachment hash changed")
                manifest = json.dumps(
                    {"format": 1, "schema": version, "rp_id": rp_id, "counts": counts, "sha256": hashes}
                ).encode()
                if len(manifest) > MANIFEST_MAX:
                    raise BackupError("backup manifest is too large")
                info = tarfile.TarInfo("manifest.json")
                info.mode, info.size = 0o600, len(manifest)
                tar.addfile(info, io.BytesIO(manifest))
            output.flush()
            os.fsync(output.fileno())
        _sync_dir(dest.parent)
        complete = True
        return dest
    except (OSError, sqlite3.Error, db.SchemaError, ValueError) as e:
        raise BackupError("backup failed; check free space, permissions and stored attachments") from e
    finally:
        # Exceptions of any kind must not leave a partial archive that looks complete.
        if made and not complete:
            dest.unlink(missing_ok=True)


def _unpack(archive: Path, stage: Path) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    manifest = None
    with _regular(archive) as source, tarfile.open(fileobj=source, mode="r|", tarinfo=_DataInfo) as tar:
        total = os.fstat(source.fileno()).st_size
        used = 0
        seen: set[str] = set()
        for member in tar:
            name = member.name
            if (
                name in seen
                or len(seen) >= MEMBERS_MAX
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                or not (name in ("switchboard.db", "manifest.json") or FILE_NAME.fullmatch(name))
            ):
                raise BackupError("archive contains an unexpected, duplicate or unsafe member")
            seen.add(name)
            used += member.size
            if member.size < 0 or used > total:
                raise BackupError("archive member sizes exceed the archive size")
            stream = tar.extractfile(member)
            if stream is None:
                raise BackupError("missing archive data")
            if name == "manifest.json":
                if member.size > MANIFEST_MAX:
                    raise BackupError("backup manifest is too large")
                manifest = json.load(stream)
            else:
                path = stage / name
                with path.open("xb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                with path.open("rb") as saved:
                    hashes[name] = _digest(saved)
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise BackupError("unknown or missing archive manifest")
        if "switchboard.db" not in hashes or hashes != manifest.get("sha256"):
            raise BackupError("archive hash verification failed")
    con = db.connect(stage / "switchboard.db", readonly=True)
    try:
        version = db.schema_version(con)
        if type(version) is not int or not 1 <= version <= db.SCHEMA_VERSION:
            raise BackupError("archive database schema is not supported by this release")
        if (
            version != manifest.get("schema")
            or db.integrity_ok(con) is not None
            or db.row_counts(con, db.tables_of(version)) != manifest.get("counts")
        ):
            raise BackupError("archive database verification failed")
        files = Store(con).backup_files(limit=MEMBERS_MAX) if version >= 40 else []
        expected = {"switchboard.db"}
        for file_id, size, digest in files:
            name = f"files/{file_id}"
            if name not in hashes or hashes[name] != digest or (stage / name).stat().st_size != size:
                raise BackupError("archive attachments do not match the database")
            expected.add(name)
        if expected != set(hashes):
            raise BackupError("archive has unreferenced attachment data")
    finally:
        con.close()
    return manifest


def hostname_warning(old: object, hostname: str | None) -> str:
    if hostname is None or old == hostname:
        return ""
    return (
        "Passkeys from the previous hostname will not work here. "
        + ("The backup did not record its passkey hostname. " if old is None else "The hostname changed. ")
        + "Sign in with a password and enrol a new passkey in Settings > Security. "
        "If no password works, use SWITCHBOARD_RESET_OWNER once to claim the owner again; "
        "this removes all people and credentials, so invite your team again."
    )


def _restore_locked(paths: Paths, archive: Path, hostname: str | None) -> Restored:
    if paths.restore_marker.exists():
        raise BackupError("an interrupted restore needs recovery; see DEPLOY.md Backups before starting")
    if archive.is_symlink():
        raise BackupError("restore archive must be a regular file, never a symlink")
    archive = archive.parent.resolve() / archive.name
    if archive.is_relative_to(paths.files_dir):
        raise BackupError("restore archive must be outside the current files directory")
    if archive.name.endswith(".used") or archive.with_name(archive.name + ".used").exists():
        raise BackupError("archive was already used, or its .used name is taken")
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=paths.home) as td:
        stage = Path(td)
        (stage / "files").mkdir(mode=0o700)
        manifest = _unpack(archive, stage)
        _sync_dir(stage / "files")
        previous = Path(tempfile.mkdtemp(prefix="before-restore-", dir=paths.home))
        # A crash anywhere after this durable marker fails closed on the next start. The
        # recovery folder holds the displaced data, including any uncheckpointed WAL.
        with paths.restore_marker.open("x") as marker:
            os.fchmod(marker.fileno(), 0o600)
            marker.write(previous.name + "\n")
            marker.flush()
            os.fsync(marker.fileno())
        _sync_dir(paths.home)
        for name in ("switchboard.db", "switchboard.db-wal", "switchboard.db-shm", "files"):
            path = paths.home / name
            if path.is_symlink():
                raise BackupError("restore refuses symlinked current data")
            if path.exists():
                path.rename(previous / name)
        (stage / "switchboard.db").rename(paths.db)
        (stage / "files").rename(paths.files_dir)
        _sync_dir(previous)
        _sync_dir(paths.home)
        archive.rename(archive.with_name(archive.name + ".used"))
        _sync_dir(archive.parent)
        paths.restore_marker.unlink()
        _sync_dir(paths.home)
        return Restored(previous, hostname_warning(manifest.get("rp_id"), hostname))


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _home_lock(paths: Paths) -> Iterator[None]:
    paths.ensure()
    fd = os.open(paths.lockfile, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("stop the broker before restoring; its home lock is held") from None
        yield
    finally:
        os.close(fd)


def restore(paths: Paths, archive: str | os.PathLike, *, hostname: str | None = None) -> Restored:
    """Manual offline restore, under the same exclusive home lock as the broker."""
    try:
        with _home_lock(paths):
            return _restore_locked(paths, Path(archive), hostname)
    except (OSError, sqlite3.Error, db.SchemaError, tarfile.TarError, ValueError) as e:
        raise BackupError(
            "restore failed verification or I/O; check the archive, permissions and free space"
        ) from e


def restore_pending(paths: Paths, hostname: str) -> Restored | None:
    """Called only by startup under its home lock, before opening the database or listening."""
    if paths.restore_marker.exists():
        raise BackupError("an interrupted restore needs recovery; see DEPLOY.md Backups before starting")
    if not paths.restore_dir.exists() and not paths.restore_dir.is_symlink():
        return None
    ensure_private_dir(paths.restore_dir)
    candidates = []
    for path in paths.restore_dir.iterdir():
        if not path.name.endswith(".used"):
            candidates.append(path)
            if len(candidates) > 1:
                raise BackupError("restore/ must contain exactly one archive (besides .used archives)")
    if not candidates:
        return None
    try:
        return _restore_locked(paths, candidates[0], hostname)
    except (OSError, sqlite3.Error, db.SchemaError, tarfile.TarError, ValueError) as e:
        raise BackupError("pending restore failed verification or I/O; the broker has not started") from e
