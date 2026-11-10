"""Data archives verify before restore and preserve the replaced home (DESIGN.md §57)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from switchboard import backup, cli, db
from switchboard.paths import Paths
from switchboard.store import Store


def seed(home: Path, value: str = "before") -> Paths:
    paths = Paths.from_home(home)
    paths.ensure()
    con = db.open_db(paths.db)
    Store(con).meta_set("test_value", value)
    Store(con).meta_set("passkey_rp_id", "old.example.com")
    con.close()
    return paths


def test_archive_is_private_data_only_and_restore_keeps_old_data(tmp_home: Path, tmp_path: Path) -> None:
    """Config belongs to the deployment; a restore must retain both the old DB and its files."""
    source = seed(tmp_home)
    source.config.write_text("deployment secret")
    archive = backup.create(source, tmp_path / "saved.tar")
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive) as tar:
        assert set(tar.getnames()) == {"switchboard.db", "manifest.json"}
        assert tar.extractfile("manifest.json") is not None
    dest = seed(tmp_path / "dest", "current")
    (dest.files_dir / ("a" * 32)).write_bytes(b"old file")
    dest.config.write_text("keep this")
    result = backup.restore(dest, archive, hostname="new.example.com")
    assert "passkeys" in result.warning.lower() and "enrol" in result.warning.lower()
    assert result.previous is not None
    assert (result.previous / "files" / ("a" * 32)).read_bytes() == b"old file"
    with db.connect(result.previous / "switchboard.db", readonly=True) as con:
        assert Store(con).meta_get("test_value") == "current"
    with db.connect(dest.db, readonly=True) as con:
        assert Store(con).meta_get("test_value") == "before"
    assert dest.config.read_text() == "keep this"
    assert dest.db.stat().st_uid == os.getuid()
    assert dest.db.stat().st_mode & 0o777 == 0o600
    assert not archive.exists() and archive.with_name(archive.name + ".used").exists()


def test_pending_archive_is_consumed_once_and_multiple_candidates_are_refused(tmp_home: Path) -> None:
    """An operator must choose a single archive; a used one cannot restore a second time."""
    paths = seed(tmp_home)
    paths.restore_dir.mkdir(mode=0o700)
    archive = backup.create(paths, paths.restore_dir / "saved.tar")
    backup.create(paths, paths.restore_dir / "other.tar")
    with pytest.raises(backup.BackupError, match="one archive"):
        backup.restore_pending(paths, "old.example.com")
    (paths.restore_dir / "other.tar").unlink()
    assert backup.restore_pending(paths, "old.example.com") is not None
    assert not archive.exists()
    assert backup.restore_pending(paths, "old.example.com") is None


def test_archive_never_overwrites_an_existing_file_or_symlink(tmp_home: Path, tmp_path: Path) -> None:
    """An output name is exclusive, even when it points back at the live database."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    original = archive.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.create(paths, archive)
    assert archive.read_bytes() == original
    link = tmp_path / "link.tar"
    link.symlink_to(paths.db)
    with pytest.raises(backup.BackupError):
        backup.create(paths, link)
    assert paths.db.read_bytes().startswith(b"SQLite format 3")


def rewrite(archive: Path, name: str, data: bytes, *, kind: bytes = tarfile.REGTYPE) -> None:
    with tarfile.open(archive) as tar:
        entries = [(m.name, tar.extractfile(m).read()) for m in tar if m.isfile()]
    with tarfile.open(archive, "w") as tar:
        for old_name, old_data in entries:
            info = tarfile.TarInfo(old_name)
            info.size = len(old_data)
            tar.addfile(info, io.BytesIO(old_data))
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(data)
        info.linkname = "switchboard.db"
        tar.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("config.toml", tarfile.REGTYPE),
        ("files/" + "a" * 32, tarfile.SYMTYPE),
        ("switchboard.db", tarfile.REGTYPE),
    ],
)
def test_unsafe_members_are_refused_before_the_home_changes(
    tmp_home: Path, tmp_path: Path, name: str, kind: bytes
) -> None:
    """No extraction API may honor paths, links, duplicates or deployment configuration from a tar."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    rewrite(archive, name, b"bad", kind=kind)
    before = paths.db.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.restore(paths, archive)
    assert paths.db.read_bytes() == before
    assert archive.exists()
    assert not paths.restore_marker.exists()


def test_corrupt_copy_and_forged_manifest_are_refused(tmp_home: Path, tmp_path: Path) -> None:
    """A tar checksum alone does not detect damaged payload bytes."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    raw = archive.read_bytes()
    archive.write_bytes(raw.replace(b"before", b"broken", 1))
    with pytest.raises(backup.BackupError, match="hash"):
        backup.restore(paths, archive)
    archive.unlink()
    backup.create(paths, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember("manifest.json")
        manifest = json.load(tar.extractfile(member))
    manifest["schema"] += 1
    raw = archive.read_bytes()
    data = json.dumps(manifest).encode()
    assert len(data) == member.size
    archive.write_bytes(raw[: member.offset_data] + data + raw[member.offset_data + member.size :])
    with pytest.raises(backup.BackupError):
        backup.restore(paths, archive)


def test_newer_schema_is_refused_without_displacing_current_db(tmp_home: Path, tmp_path: Path) -> None:
    """A downgrade cannot install a newer DB, even if the archive is otherwise valid."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION - 1)
        with pytest.raises(backup.BackupError, match="schema"):
            backup.restore(paths, archive)
    assert not paths.restore_marker.exists()


def test_older_schema_restores_then_migrates_with_the_normal_start_path(
    tmp_home: Path, tmp_path: Path
) -> None:
    """A real released DB is restored unchanged; open_db migrates it only at start."""
    paths = Paths.from_home(tmp_home)
    paths.ensure()
    fixture = Path(__file__).resolve().parents[1] / "fixtures/db/v0_37_2.sql"
    with sqlite3.connect(paths.db) as con:
        con.executescript(fixture.read_text())
        version = db.schema_version(con)
    archive = backup.create(paths, tmp_path / "old.tar")
    backup.restore(paths, archive)
    with db.connect(paths.db, readonly=True) as con:
        assert db.schema_version(con) == version
    con = db.open_db(paths.db)
    assert db.schema_version(con) == db.SCHEMA_VERSION
    con.close()


def test_cli_reports_backup_restore_and_errors(
    tmp_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Both home flag positions work and errors are short terminal messages."""
    seed(tmp_home)
    archive = tmp_path / "saved.tar"
    assert cli.main(["backup", str(archive), "--home", str(tmp_home)]) == 0
    assert "Backup written" in capsys.readouterr().out
    assert cli.main(["--home", str(tmp_home), "restore", str(archive)]) == 0
    assert "Restored" in capsys.readouterr().out
    assert cli.main(["backup", str(archive), "--home", str(tmp_path / "missing")]) == 1
    assert "switchboard:" in capsys.readouterr().err


def attachment(paths: Paths) -> tuple[str, bytes]:
    import hashlib

    file_id, data = "b" * 32, b"room attachment\x00\xff"
    con = db.connect(paths.db)
    store = Store(con)
    room = store.create_room("#files", "alice", 60, 6)
    store.file_insert(
        file_id=file_id,
        room_id=room.id,
        name="notes.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type="application/octet-stream",
        uploader_name="alice",
        uploader_kind="human",
        state="stored",
        created_at=0,
        stored_at=0,
    )
    con.close()
    (paths.files_dir / file_id).write_bytes(data)
    (paths.files_dir / ("." + file_id + ".part")).write_bytes(b"unfinished upload")
    return file_id, data


def test_stored_attachment_round_trips_but_partial_uploads_do_not(tmp_home: Path, tmp_path: Path) -> None:
    """Only immutable files named by the database snapshot travel with it."""
    paths = seed(tmp_home)
    file_id, data = attachment(paths)
    archive = backup.create(paths, tmp_path / "files.tar")
    with tarfile.open(archive) as tar:
        assert set(tar.getnames()) == {"switchboard.db", "manifest.json", f"files/{file_id}"}
    dest = Paths.from_home(tmp_path / "dest")
    backup.restore(dest, archive)
    assert (dest.files_dir / file_id).read_bytes() == data
    assert (dest.files_dir / file_id).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["missing", "size", "hash", "symlink"])
def test_attachment_race_or_damage_removes_the_partial_archive(
    tmp_home: Path, tmp_path: Path, damage: str
) -> None:
    """Retention may delete a snapshotted file; do not publish an incomplete backup."""
    paths = seed(tmp_home)
    file_id, data = attachment(paths)
    path = paths.files_dir / file_id
    if damage == "missing":
        path.unlink()
    elif damage == "size":
        path.write_bytes(b"short")
    elif damage == "hash":
        path.write_bytes(b"x" * len(data))
    else:
        path.unlink()
        path.symlink_to(paths.db)
    archive = tmp_path / "bad.tar"
    with pytest.raises(backup.BackupError):
        backup.create(paths, archive)
    assert not archive.exists()
    assert not list(tmp_home.glob(".backup-*"))


def test_interrupted_restore_leaves_a_recovery_marker_on_io_failure(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only this process owns restored files; an interrupted replacement cannot serve mixed data."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    real = Path.rename

    def refuse(self: Path, dest: Path) -> Path:
        if self.name == "files":
            raise OSError("simulated disk failure")
        return real(self, dest)

    monkeypatch.setattr(Path, "rename", refuse)
    with pytest.raises(backup.BackupError):
        backup.restore(paths, archive)
    assert paths.restore_marker.exists()
    previous = tmp_home / paths.restore_marker.read_text().strip()
    assert (previous / "switchboard.db").exists()
    with pytest.raises(backup.BackupError, match="interrupted"):
        backup.restore_pending(paths, "old.example.com")
    with pytest.raises(backup.BackupError, match="interrupted"):
        backup.restore(paths, archive)


def test_hostname_warning_covers_unknown_same_and_changed_hostname() -> None:
    """Old archives may predate recorded RP ids; do not silently imply their passkeys will work."""
    assert backup.hostname_warning("example.com", "example.com") == ""
    assert backup.hostname_warning("example.com", None) == ""
    assert "did not record" in backup.hostname_warning(None, "example.com")
    assert "hostname changed" in backup.hostname_warning("old.example.com", "new.example.com")
    assert "old.example.com" not in backup.hostname_warning("old.example.com", "new.example.com")


def test_restored_modes_and_ownership_ignore_the_tar_metadata(tmp_home: Path, tmp_path: Path) -> None:
    """A tar created elsewhere may carry permissive modes and root ownership."""
    paths = seed(tmp_home)
    file_id, data = attachment(paths)
    archive = backup.create(paths, tmp_path / "saved.tar")
    with tarfile.open(archive) as tar:
        entries = [(m, tar.extractfile(m).read()) for m in tar]
    with tarfile.open(archive, "w") as tar:
        for member, content in entries:
            member.uid = member.gid = 0
            member.mode = 0o7777
            tar.addfile(member, io.BytesIO(content))
    dest = Paths.from_home(tmp_path / "dest")
    backup.restore(dest, archive)
    assert (dest.files_dir / file_id).read_bytes() == data
    for path in (dest.db, dest.files_dir / file_id):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o7777 == 0o600
    assert dest.files_dir.stat().st_mode & 0o777 == 0o700


def test_used_archive_is_never_replayed_or_overwritten(tmp_home: Path, tmp_path: Path) -> None:
    """A prior consumed archive remains intact when an operator picks the same name again."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    used = archive.with_name(archive.name + ".used")
    used.write_bytes(b"keep the previous archive")
    with pytest.raises(backup.BackupError, match="already used"):
        backup.restore(paths, archive)
    with pytest.raises(backup.BackupError, match="already used"):
        backup.restore(paths, used)
    assert used.read_bytes() == b"keep the previous archive"
    assert not paths.restore_marker.exists()


def test_restore_rejects_invalid_deployment_config_before_touching_data(
    tmp_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hostname checks need a valid deployment origin; configuration errors must be usable CLI errors."""
    paths = seed(tmp_home)
    archive = backup.create(paths, tmp_path / "saved.tar")
    paths.config.write_text("not valid toml")
    assert cli.main(["restore", str(archive), "--home", str(tmp_home)]) == 1
    assert "invalid deployment configuration" in capsys.readouterr().err
    assert archive.exists() and not paths.restore_marker.exists()


@pytest.mark.parametrize("kind", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_SPARSE])
def test_extension_headers_are_refused_before_reading_their_payload(
    tmp_home: Path, tmp_path: Path, kind: bytes
) -> None:
    """PAX, GNU longname and sparse metadata cannot allocate memory before member validation."""
    paths = seed(tmp_home)
    archive = tmp_path / "extended.tar"
    info = tarfile.TarInfo("extended")
    info.type, info.size = kind, 1024 * 1024 * 1024
    archive.write_bytes(info.tobuf(format=tarfile.GNU_FORMAT))
    with pytest.raises(backup.BackupError, match="extended header"):
        backup.restore(paths, archive)
    assert not paths.restore_marker.exists()
