"""Files a room holds (DESIGN.md §53).

The database keeps one row per file. The bytes live behind ``FolderStore``: a private folder
next to the database, written by renaming a temp file into place. A later object store can
implement the same three operations (adopt, open, delete). Nothing here logs a token. A token
is a capability for one file; only its sha256 is stored.

The page resolves a card against the files of its message (``message_frame``). Messages are
not edited. An agent's ``read()`` or delivery gains a download line beside the rendered text;
that line is not written into the message.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from switchboard.paths import ensure_private_dir

log = logging.getLogger("switchboard.files")

DAY_S = 86400.0
UPLOAD_TTL_S = 600.0
DOWNLOAD_TTL_S = 600.0
RETENTION_DEFAULT = 30
RETENTION_MAX = 365
RETENTION_KEY = "files_retention_days"
NAME_MAX = 200
WHERE_MAX = 2048
SIZE_MAX = 32
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FILE_REF_RE = re.compile(r"^switchboard:file/([0-9a-f]{32})$")
SIZE_RE = re.compile(r"^[0-9][0-9.,]*(?: ?[A-Za-z]{1,12})?$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DISPOSITION_BAD = re.compile(r"[^A-Za-z0-9._-]")


def file_expired(
    *,
    state: str,
    now: float,
    retention_days: int,
    stored_at: float | None,
    upload_expires_at: float | None,
) -> bool:
    """Whether this file is due to be removed at ``now``.

    A stored file expires ``retention_days`` after ``stored_at``. 0 means never. A pending
    upload expires with its token. Any other state is not kept.
    """
    if state == "pending":
        return upload_expires_at is None or now >= upload_expires_at
    if state == "stored":
        if retention_days <= 0 or stored_at is None:
            return False
        return now >= stored_at + retention_days * DAY_S
    return True


def content_disposition(name: str) -> str:
    """``Content-Disposition: attachment`` with one quoted ASCII filename.

    The directory, quotes, newlines and anything that is not a token character are dropped,
    so the header cannot name another header or another path.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = base.replace("\r", "").replace("\n", "")
    cleaned = _DISPOSITION_BAD.sub("_", base).strip("._")[:NAME_MAX]
    if not cleaned:
        cleaned = "download"
    return f'attachment; filename="{cleaned}"'


def clean_name(raw: str) -> str:
    """A file's display name: the last path segment, no controls, at most 200 characters."""
    name = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = name.strip()[:NAME_MAX]
    return name or "file"


def clean_type(raw: str | None) -> str:
    """The content type the sender claimed, kept only as a note. Never used to serve the file."""
    if not raw:
        return ""
    text = "".join(ch for ch in raw if ch >= " " and ch != "\x7f")
    return text.strip()[:200]


def shell_quote(path: str) -> str:
    """A single-quoted shell word. A quote inside the path ends and reopens the quotes."""
    return "'" + path.replace("'", "'\\''") + "'"


def upload_path_word(path: str) -> str:
    """``path`` as one shell word for the upload command. Agents often write a home-relative path
    (``~/work/build.log``), and a quoted ``~`` is not expanded, so curl found no such file (#587):
    ``~/`` becomes ``"$HOME"/`` with the rest still single-quoted. Any other path is quoted whole,
    ``~user/`` forms included, so nothing else in it is ever expanded."""
    if path.startswith("~/"):
        return '"$HOME"/' + shell_quote(path[2:])
    return shell_quote(path)


def share_line(name: str, path: str, base: str, token: str) -> str:
    """The one line ``say()`` adds so the agent can upload this file."""
    return f"To share {name}, run: curl --fail -T {upload_path_word(path)} {base}/api/upload/{token}"


def download_line(base: str, file_id: str, token: str) -> str:
    """The one line ``read()`` or a delivery adds so this agent can fetch the file."""
    return f"download: {base}/api/files/{file_id}?t={token}"


def new_file_id() -> str:
    return secrets.token_hex(16)


def new_token() -> tuple[str, str]:
    """32 random bytes, and the sha256 that is all the database keeps of them."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hashes_match(stored: str, got: str) -> bool:
    if len(stored) != len(got):
        return False
    return hmac.compare_digest(stored, got)


@dataclass(frozen=True)
class ArtifactLine:
    """One artifact line. ``kind`` is ``path`` (an upload), ``url`` or ``stored``."""

    name: str
    size: str
    sha256: str
    where: str

    @property
    def kind(self) -> str:
        low = self.where.lower()
        if low.startswith("http://") or low.startswith("https://"):
            return "url"
        if FILE_REF_RE.fullmatch(self.where):
            return "stored"
        return "path"

    @property
    def file_id(self) -> str | None:
        m = FILE_REF_RE.fullmatch(self.where)
        return m.group(1) if m else None

    @property
    def byte_size(self) -> int | None:
        """A size written as digits only is an exact byte count. ``12 KB`` is not."""
        if self.size.isdigit():
            return int(self.size)
        return None


def _fence(line: str) -> tuple[str, int] | None:
    i = 0
    while i < 3 and i < len(line) and line[i] == " ":
        i += 1
    if i < len(line) and line[i] in (" ", "\t"):
        return None
    rest = line[i:]
    if not rest or rest[0] not in "`~":
        return None
    ch = rest[0]
    n = 0
    while n < len(rest) and rest[n] == ch:
        n += 1
    if n < 3:
        return None
    if ch == "`" and "`" in rest[n:]:
        return None
    return ch, n


def _has_control(s: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in s)


def parse_artifact_line(line: str) -> ArtifactLine | None:
    """One line, or None when it is not the artifact grammar (the same rules as ``md.js``)."""
    i = 0
    while i < 3 and i < len(line) and line[i] == " ":
        i += 1
    if i < len(line) and line[i] in (" ", "\t"):
        return None
    rest = line[i:]
    if not rest.startswith("artifact:"):
        return None
    after = rest[len("artifact:") :]
    if not after or after[0] not in (" ", "\t"):
        return None
    body = after.strip(" \t")
    if not body or _has_control(body):
        return None
    fields = body.split(" | ")
    if not 2 <= len(fields) <= 4:
        return None
    for field in fields:
        if not field or field != field.strip(" \t") or _has_control(field):
            return None
    name, where = fields[0], fields[-1]
    if not name or len(name) > NAME_MAX or not where or len(where) > WHERE_MAX:
        return None
    size, sha = "", ""
    mid = fields[1:-1]
    if len(mid) == 2:
        if len(mid[0]) > SIZE_MAX or not SIZE_RE.fullmatch(mid[0]):
            return None
        if not mid[1].startswith("sha256:") or not SHA_RE.fullmatch(mid[1][7:]):
            return None
        size, sha = mid[0], mid[1][7:].lower()
    elif len(mid) == 1:
        if mid[0].startswith("sha256:") and SHA_RE.fullmatch(mid[0][7:]):
            sha = mid[0][7:].lower()
        elif len(mid[0]) <= SIZE_MAX and SIZE_RE.fullmatch(mid[0]):
            size = mid[0]
        else:
            return None
    return ArtifactLine(name, size, sha, where)


def artifact_lines(text: str) -> list[ArtifactLine]:
    """The artifact lines of ``text``, skipping a fenced block (an example, not a file)."""
    out: list[ArtifactLine] = []
    open_fence: tuple[str, int] | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        fence = _fence(line)
        if fence is not None and (
            open_fence is None or fence[0] == open_fence[0] and fence[1] >= open_fence[1]
        ):
            open_fence = None if open_fence is not None else fence
            continue
        if open_fence is not None:
            continue
        art = parse_artifact_line(line)
        if art is not None:
            out.append(art)
    return out


def file_refs(text: str) -> list[str]:
    """The stored-file ids an artifact line names (``switchboard:file/<id>``)."""
    return [a.file_id for a in artifact_lines(text) if a.file_id]


class FolderStore:
    """Bytes in ``<home>/files/<id>``, mode 0600, in a 0700 directory. ``adopt`` renames."""

    def __init__(self, root: Path):
        self.root = root

    def ensure(self) -> None:
        ensure_private_dir(self.root)

    def _checked(self, file_id: str) -> Path:
        if not FILE_ID_RE.fullmatch(file_id):
            raise ValueError("bad file id")
        return self.root / file_id

    def part_path(self, file_id: str) -> Path:
        self._checked(file_id)
        return self.root / f".{file_id}.part"

    def adopt(self, file_id: str, src: Path) -> None:
        dest = self._checked(file_id)
        os.chmod(src, 0o600)
        os.replace(src, dest)

    def path(self, file_id: str) -> Path | None:
        dest = self._checked(file_id)
        return dest if dest.is_file() else None

    def delete(self, file_id: str) -> None:
        try:
            self._checked(file_id).unlink()
        except FileNotFoundError:
            pass
        part = self.root / f".{file_id}.part"
        try:
            part.unlink()
        except FileNotFoundError:
            pass


class Spool:
    """A temp file the upload is written to, hashed as it arrives, stopped at ``limit`` bytes."""

    def __init__(self, dest: Path, limit: int):
        self.dest = dest
        self.limit = limit
        self.n = 0
        self._digest = hashlib.sha256()
        self._over = False
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self._out = os.fdopen(fd, "wb")

    def write(self, chunk: bytes) -> bool:
        """False once the body has passed the limit. The partial file is removed then."""
        if self._over or not chunk:
            return not self._over
        self.n += len(chunk)
        if self.n > self.limit:
            self._over = True
            self.abort()
            return False
        self._digest.update(chunk)
        self._out.write(chunk)
        return True

    def abort(self) -> None:
        self._close()
        self.dest.unlink(missing_ok=True)

    def finish(self) -> tuple[int, str]:
        self._out.flush()
        os.fsync(self._out.fileno())
        self._close()
        os.chmod(self.dest, 0o600)
        return self.n, self._digest.hexdigest()

    def _close(self) -> None:
        if not self._out.closed:
            self._out.close()


class Attachments:
    """The broker's files: rows, bytes, one-time grants, and the retention sweep."""

    def __init__(self, store: Any, folder: FolderStore, clock: Any, cfg: Any, base: str, hub: Any = None):
        self.store = store
        self.folder = folder
        self.clock = clock
        self.cfg = cfg
        self.base = base.rstrip("/")
        self.hub = hub

    def ensure(self) -> None:
        self.folder.ensure()

    @property
    def max_bytes(self) -> int:
        return self.cfg.files.max_mb * 1024 * 1024

    @property
    def room_cap(self) -> int:
        return self.cfg.files.room_mb * 1024 * 1024

    def retention_days(self) -> int:
        raw = self.store.setting_get(RETENTION_KEY)
        if raw is None:
            return RETENTION_DEFAULT
        try:
            days = int(raw)
        except ValueError:
            return RETENTION_DEFAULT
        if days == 0 or 1 <= days <= RETENTION_MAX:
            return days
        return RETENTION_DEFAULT

    def set_retention(self, days: int) -> int:
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or not (days == 0 or 1 <= days <= RETENTION_MAX)
        ):
            raise ValueError(f"retention is 1 to {RETENTION_MAX} days, or 0 for never")
        self.store.setting_set(RETENTION_KEY, str(days))
        return days

    def prepare_pending(self, room_id: int, uploader: str, text: str) -> tuple[list[str], str]:
        """A pending file and a curl line for each local-path artifact line in ``text``."""
        notes: list[str] = []
        ids: list[str] = []
        now = self.clock.now()
        for art in artifact_lines(text):
            if art.kind != "path":
                continue
            if art.byte_size is not None and art.byte_size > self.max_bytes:
                notes.append(f"{art.name} is larger than {self.cfg.files.max_mb} MB, so it was not accepted")
                continue
            file_id = new_file_id()
            token, hashed = new_token()
            self.store.file_insert(
                file_id=file_id,
                room_id=room_id,
                name=art.name[:NAME_MAX],
                size=art.byte_size,
                sha256=art.sha256 or None,
                content_type="",
                uploader_name=uploader[:100] or "agent",
                uploader_kind="agent",
                state="pending",
                created_at=now,
                stored_at=None,
            )
            self.store.grant_insert(
                token_hash=hashed,
                file_id=file_id,
                kind="upload",
                expires_at=now + UPLOAD_TTL_S,
            )
            ids.append(file_id)
            notes.append(share_line(art.name, art.where, self.base, token))
            log.info("upload offered for file %s in room %d", file_id, room_id)
        return ids, "\n".join(notes)

    def drop(self, file_ids: Sequence[str]) -> None:
        """Forget pending files a say did not keep (the queue was full)."""
        for file_id in file_ids:
            got = self.store.file_get(file_id)
            if got is not None and got.state == "pending":
                self.store.delete_file(file_id)
                self.folder.delete(file_id)

    def begin_upload(self, token: str) -> tuple[Any, int] | tuple[None, tuple[int, str]]:
        """The pending file and the most bytes this request may send, or an error pair."""
        grant = self.store.grant_get(token_hash(token))
        if grant is None or grant.kind != "upload":
            return None, (404, "no such upload")
        now = self.clock.now()
        if grant.used_at is not None:
            return None, (409, "this upload link was already used")
        if now >= grant.expires_at:
            return None, (410, "this upload link has expired")
        got = self.store.file_get(grant.file_id)
        if got is None or got.state != "pending":
            return None, (404, "no such upload")
        room_left = self.room_cap - self.store.room_stored_bytes(got.room_id)
        if room_left <= 0:
            return None, (413, f"this room's files would pass {self.cfg.files.room_mb} MB")
        return got, min(self.max_bytes, room_left)

    def length_refusal(self, declared: int, limit: int) -> tuple[int, str] | None:
        if declared > self.max_bytes:
            return 413, f"file is larger than {self.cfg.files.max_mb} MB"
        if declared > limit:
            return 413, f"this room's files would pass {self.cfg.files.room_mb} MB"
        return None

    def accept_upload(
        self, token: str, part: Path, size: int, digest: str, content_type: str
    ) -> tuple[int, str]:
        """Check the spooled body against the line, then keep it. The token works once."""
        grant = self.store.grant_get(token_hash(token))
        if grant is None or grant.kind != "upload":
            part.unlink(missing_ok=True)
            return 404, "no such upload"
        now = self.clock.now()
        if grant.used_at is not None:
            part.unlink(missing_ok=True)
            return 409, "this upload link was already used"
        if now >= grant.expires_at:
            part.unlink(missing_ok=True)
            return 410, "this upload link has expired"
        got = self.store.file_get(grant.file_id)
        if got is None or got.state != "pending":
            part.unlink(missing_ok=True)
            return 404, "no such upload"
        if size > self.max_bytes:
            part.unlink(missing_ok=True)
            return 413, f"file is larger than {self.cfg.files.max_mb} MB"
        if self.store.room_stored_bytes(got.room_id) + size > self.room_cap:
            part.unlink(missing_ok=True)
            return 413, f"this room's files would pass {self.cfg.files.room_mb} MB"
        if not self.store.grant_use(grant.token_hash, now):
            part.unlink(missing_ok=True)
            return 409, "this upload link was already used"
        mismatch = ""
        if got.sha256 and not hashes_match(got.sha256, digest):
            mismatch = "sha256 does not match the message; the upload was deleted"
        elif got.size is not None and got.size != size:
            mismatch = "size does not match the message; the upload was deleted"
        if mismatch:
            part.unlink(missing_ok=True)
            self.folder.delete(got.id)
            log.info("upload rejected for file %s", got.id)
            return 400, mismatch
        self.folder.adopt(got.id, part)
        stored = self.store.file_mark_stored(
            got.id, size=size, sha256=digest, content_type=clean_type(content_type), stored_at=now
        )
        if stored is not None:
            self._notify(stored)
        log.info("file %s stored (%d bytes)", got.id, size)
        return 201, f"stored {got.name} ({size} bytes)"

    def store_human(
        self, room_id: int, uploader: str, name: str, part: Path, size: int, digest: str, content_type: str
    ) -> Any:
        """A person's upload is stored at once. The message that names it comes after."""
        return self._keep(room_id, uploader, "human", name, part, size, digest, content_type)

    def store_snapshot(
        self, room_id: int, uploader: str, kind: str, name: str, data: bytes, content_type: str = ""
    ) -> Any:
        """A picture of a call's screen, already in memory, stored at once like a person's upload.

        ``kind`` is who asked: ``human`` for a person's button or command, ``meeting`` for a line
        on the call. Raises ``ValueError`` with a short reason when it passes a size cap (§53).
        """
        if len(data) > self.max_bytes:
            raise ValueError(f"the picture is larger than {self.cfg.files.max_mb} MB")
        if self.store.room_stored_bytes(room_id) + len(data) > self.room_cap:
            raise ValueError(f"this room's files would pass {self.cfg.files.room_mb} MB")
        part = self.folder.part_path(new_file_id())
        spool = Spool(part, self.max_bytes)
        try:
            spool.write(data)
            size, digest = spool.finish()
            return self._keep(room_id, uploader, kind, name, part, size, digest, content_type)
        except BaseException:
            spool.abort()
            raise

    def _keep(
        self,
        room_id: int,
        uploader: str,
        kind: str,
        name: str,
        part: Path,
        size: int,
        digest: str,
        content_type: str,
    ) -> Any:
        now = self.clock.now()
        file_id = new_file_id()
        self.folder.adopt(file_id, part)
        return self.store.file_insert(
            file_id=file_id,
            room_id=room_id,
            name=clean_name(name),
            size=size,
            sha256=digest,
            content_type=clean_type(content_type),
            uploader_name=uploader[:100] or kind,
            uploader_kind=kind,
            state="stored",
            created_at=now,
            stored_at=now,
        )

    def for_download(self, file_id: str, token: str | None, *, can_read: bool) -> Any:
        """The stored file, or None. A token is the whole permission. Without one, ``can_read``
        is the room check. Either miss is the same as a file that is not there."""
        if not FILE_ID_RE.fullmatch(file_id):
            return None
        got = self.store.file_get(file_id)
        if got is None or got.state != "stored" or self.folder.path(file_id) is None:
            return None
        if token is not None:
            grant = self.store.grant_get(token_hash(token))
            if (
                grant is None
                or grant.kind != "download"
                or grant.file_id != file_id
                or self.clock.now() >= grant.expires_at
            ):
                return None
            return got
        return got if can_read else None

    def download_notes(self, items: Sequence[Any], membership_id: int) -> str:
        """One short-lived link per stored file these messages name. Not written into them."""
        lines: list[str] = []
        seen: set[str] = set()
        now = self.clock.now()
        for item in items:
            text = str(getattr(item, "text", "") or "")
            mid = getattr(item, "message_id", None)
            if mid is None:
                mid = getattr(item, "id", None)
            files = self.store.files_for_message(int(mid)) if mid else []
            by_name = {f.name: f for f in files if f.state == "stored"}
            for art in artifact_lines(str(text)):
                chosen = None
                if art.kind == "stored" and art.file_id:
                    row = self.store.file_get(art.file_id)
                    if row is not None and row.state == "stored" and (mid is None or row.message_id == mid):
                        chosen = row
                elif art.kind == "path" and art.name in by_name:
                    chosen = by_name[art.name]
                if chosen is None or chosen.id in seen:
                    continue
                seen.add(chosen.id)
                token, hashed = new_token()
                self.store.grant_insert(
                    token_hash=hashed,
                    file_id=chosen.id,
                    kind="download",
                    expires_at=now + DOWNLOAD_TTL_S,
                    membership_id=membership_id,
                )
                lines.append(download_line(self.base, chosen.id, token))
        return "\n".join(lines)

    def delete_bytes(self, file_ids: Sequence[str]) -> None:
        for file_id in file_ids:
            self.folder.delete(file_id)

    def sweep(self) -> list[str]:
        """Remove files the expiry rule says are due, and download grants that have lapsed."""
        now = self.clock.now()
        days = self.retention_days()
        gone: list[str] = []
        for got in self.store.files_for_sweep():
            if file_expired(
                state=got.state,
                now=now,
                retention_days=days,
                stored_at=got.stored_at,
                upload_expires_at=got.upload_expires_at,
            ):
                self.store.delete_file(got.id)
                self.folder.delete(got.id)
                gone.append(got.id)
        self.store.delete_expired_grants(now)
        if gone:
            log.info("files swept: %s", " ".join(gone))
        return gone

    def _notify(self, got: Any) -> None:
        if self.hub is None or got.message_id is None:
            return
        room = self.store.room_by_id(got.room_id)
        if room is None:
            return
        self.hub.file_stored(
            room.name,
            {"message_id": got.message_id, "file": {"id": got.id, "name": got.name, "state": "stored"}},
        )
