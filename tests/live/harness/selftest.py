"""The pure parts of the meeting self-test (DESIGN.md §12.4, §52). Test-only: never packaged.

``tests/live/test_meeting_selftest.py`` puts a real call, two joinly bots and a throwaway broker
together. What needs no call is here, so ``tests/unit/test_meeting_selftest.py`` checks it on every run:

- how words are compared (speech-to-text of text-to-speech is close to the sentence, never equal),
- how the meeting link is read from its file and kept out of every output,
- what a batch, a transcript and a participant list look like when parsed,
- and the report: JSON and a Markdown table of the steps.

The link carries the meeting's passcode. It is held as a ``SecretLink`` whose ``repr`` is a placeholder,
so a traceback with locals shows nothing, and every string that reaches a report goes through ``scrub``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from switchboard.meeting.links import LinkError, parse_link

# A step passes when this share of the expected sentence's content words is in what was seen. Speech
# is turned into sound, sent through a call's audio, and turned back into text: a word is respelled
# ("hear" for "here") or lost in the codec's first syllable, and Whisper adds or drops a small word.
# Three words in five is "most of it arrived"; a line that is only small words, or another sentence, is
# nowhere near. Every comparison in the test reads this one value.
MATCH_THRESHOLD = 0.6

REPORT_JSON = "meeting-selftest.json"
REPORT_MD = "meeting-selftest.md"
LINK_MODE = 0o600
LINK_FILE_MAX = 4096
CELL_MAX = 240  # characters of an expected or seen string in the Markdown table
MIN_PART = 6  # the shortest piece of the link that is scrubbed on its own (a shorter one is a common word)

# Words that carry no meaning of their own. They are left out of both sides before comparing, so "can
# you hear me" is compared on "hear". Short on purpose: the more that are dropped, the fewer words a
# sentence has, and the coarser 60 percent gets.
STOPWORDS = frozenset(
    "a an the and or but of to in on at for with by from as is are was were be been am i you he she it "
    "we they me my your our us this that these those can do does did so if then there here".split()
)
_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty"
).split()
_APOSTROPHES = re.compile(r"['’‘`´]")
_TOKEN = re.compile(r"[a-z0-9]+")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PWD = re.compile(r"pwd=\S+", re.IGNORECASE)
REDACTED = "[link]"


class PinnedFileError(Exception):
    """A pinned download whose bytes are not the ones pinned."""


def check_pinned(data: bytes, sha256: str) -> bytes:
    """``data`` if its sha256 is ``sha256``, else ``PinnedFileError``. The Zoom fix the self-test mounts
    into joinly is fetched from a pinned commit, and only the exact file checked here runs (§52)."""
    got = hashlib.sha256(data).hexdigest()
    if got != sha256.lower():
        raise PinnedFileError(f"the pinned file's sha256 is {got}, not {sha256.lower()}")
    return data


# ------------------------------------------------------------------------ words
def words(text: str) -> list[str]:
    """``text`` as lowercase words: case, punctuation and apostrophes gone ("can't" is "cant"), and a
    number up to twenty as its word ("Claude 1" is "claude one"), because speech-to-text writes either.
    Past twenty a number stays as digits. Letters outside ASCII are folded where Unicode has an ASCII
    form, and dropped otherwise."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    folded = _APOSTROPHES.sub("", folded)
    out = []
    for token in _TOKEN.findall(folded):
        out.append(
            _NUMBER_WORDS[int(token)] if token.isdigit() and int(token) < len(_NUMBER_WORDS) else token
        )
    return out


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS]


def match_ratio(expected: str, seen: str) -> float:
    """The share of ``expected``'s content words that ``seen`` has (0.0 to 1.0). Extra words in
    ``seen`` neither help nor hurt: the line may carry more than was asked for. A sentence with no
    content word matches nothing: there is nothing to look for."""
    want = content_words(expected)
    if not want:
        return 0.0
    have = set(content_words(seen))
    return sum(1 for w in want if w in have) / len(want)


def matches(expected: str, seen: str) -> bool:
    return match_ratio(expected, seen) >= MATCH_THRESHOLD


# ------------------------------------------------------------- the link, kept secret
class SecretLink:
    """The meeting's join link, which holds its passcode. It prints as a placeholder (``str``, ``repr``,
    ``format``, a traceback's locals); ``reveal()`` is the one way to the text, used where the link is
    handed to joinly and nowhere else."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<meeting link>"

    __str__ = __repr__

    @property
    def platform(self) -> str:
        return parse_link(self._value).platform

    def parts(self) -> list[str]:
        return secret_parts(self._value)


def secret_parts(url: str) -> list[str]:
    """The link and the pieces of it that give it away: the query string, each long query value (the
    passcode) and the meeting's id, which is the last piece of the path. Longest first, so a scrub
    that replaces the whole link does not leave its pieces half replaced."""
    parts = urllib.parse.urlsplit(url)
    found = {url}
    if parts.query:
        found.add(parts.query)
        found.update(v for _, v in urllib.parse.parse_qsl(parts.query) if len(v) >= MIN_PART)
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if len(last) >= MIN_PART:
        found.add(last)
    found.discard("")
    return sorted(found, key=len, reverse=True)


def scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` with every secret piece, any ``https://`` address and any ``pwd=`` replaced by a
    placeholder. Errors that joinly or the client library raise can quote the link; this runs on all
    of them before they are shown or written."""
    for piece in sorted(set(secrets), key=len, reverse=True):
        if piece:
            text = text.replace(piece, REDACTED)
    return _PWD.sub(REDACTED, _URL.sub(REDACTED, text))


def leaks(secrets: Iterable[str], blobs: Mapping[str, str | bytes]) -> list[str]:
    """The names of the blobs (the broker's database, its messages, its log) that hold any secret
    piece. Names only: what was found is never echoed."""
    pieces = [p for p in secrets if p]
    found = []
    for name, blob in blobs.items():
        for piece in pieces:
            needle: str | bytes = piece.encode() if isinstance(blob, bytes) else piece
            if needle in blob:  # type: ignore[operator]
                found.append(name)
                break
    return found


class LinkFileError(Exception):
    """The link file can't be used. The message says why and never holds what the file contains."""


def read_link_file(path: str | os.PathLike[str]) -> SecretLink:
    """The meeting link in the file at ``path``, which must be a plain file of the current user whose
    mode is exactly 0600. The link has the call's passcode in it, so a file other people can read, or a
    symlink that something else can swap, is refused rather than used. The file is opened without
    following links and checked through the open descriptor, so what was checked is what is read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise LinkFileError(
            "the link file can't be opened: it must be a plain file, not a symlink, with mode 0600"
        ) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LinkFileError("the link file is not a plain file")
        if st.st_uid != os.geteuid():
            raise LinkFileError("the link file is not owned by the user running the test")
        if stat.S_IMODE(st.st_mode) != LINK_MODE:
            raise LinkFileError(
                "the link file's mode is not 0600 (chmod 600 it: it holds the call's passcode)"
            )
        if st.st_size > LINK_FILE_MAX:
            raise LinkFileError("the link file is too large to be one link")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(LINK_FILE_MAX + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise LinkFileError("the link file is not text") from None
    try:
        parse_link(text)
    except LinkError:
        raise LinkFileError("the link file does not hold one Zoom, Meet or Teams https link") from None
    return SecretLink(text)


# --------------------------------------------------------- what joinly and the broker send
@dataclass(frozen=True)
class BatchItem:
    id: int
    sender: str
    kind: str
    to_you: bool
    text: str


_ITEM = re.compile(r"^- id=(\d+) .*?\bfrom=(\S+) kind=(\S+) .*?\bto_you=(yes|no)\b.*?\btext=(.*)$")


def batch_items(batch: str) -> list[BatchItem]:
    """The messages in an agent's rendered batch (``envelope.render_item``): their id, sender, kind,
    whether they are addressed to the agent, and their text, which the envelope quotes as a JSON string."""
    found = []
    for line in batch.splitlines():
        m = _ITEM.match(line)
        if m is None:
            continue
        rest = m.group(5)
        try:
            text, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            text = rest  # a stub: "(not shown here; call read(...))"
        found.append(BatchItem(int(m.group(1)), m.group(2), m.group(3), m.group(4) == "yes", str(text)))
    return found


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    end: float
    speaker: str | None
    role: str


def _payload(result: Any) -> Any:
    """The JSON a joinly tool returned, whatever shape the client library gave it: the structured part,
    else the text of the first block. None when there is neither."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            try:
                return json.loads(text)
            except ValueError:
                return None
    return None


def segments_of(result: Any) -> list[Segment]:
    """The transcript segments in a ``get_transcript`` result. Anything that is not a segment is
    skipped, so a shape change shows as an empty transcript and a failed step, not an exception."""
    data = _payload(result)
    if isinstance(data, Mapping) and isinstance(data.get("result"), Mapping):
        data = data["result"]
    items = data.get("segments") if isinstance(data, Mapping) else None
    out = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        start, end = item.get("start"), item.get("end")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            continue
        if isinstance(end, bool) or not isinstance(end, (int, float)):
            continue
        speaker = item.get("speaker")
        out.append(
            Segment(
                text=str(item.get("text") or ""),
                start=float(start),
                end=float(end),
                speaker=speaker if isinstance(speaker, str) and speaker.strip() else None,
                role=str(item.get("role") or "participant"),
            )
        )
    return out


def heard_since(segments: Iterable[Segment], after: float) -> str:
    """What the other participants said after time ``after`` (seconds into the call): the text of the
    ``participant`` segments that end later. The bot's own speech is the ``assistant`` role and never
    counts as heard."""
    return " ".join(s.text for s in segments if s.role == "participant" and s.end > after).strip()


def last_end(segments: Iterable[Segment]) -> float:
    """Where the transcript stands now: the latest end of any segment, 0.0 when it is empty."""
    return max((s.end for s in segments), default=0.0)


def names_of(result: Any) -> list[str]:
    """The names in a ``get_participants`` result."""
    data = _payload(result)
    if isinstance(data, Mapping):
        data = data.get("result", data.get("root"))
    names = []
    for item in data if isinstance(data, list) else []:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def has_name(names: Iterable[str], word: str) -> bool:
    """Whether one of ``names`` has ``word`` among its words ("transcribing" finds "switchboard
    (transcribing)", also with Zoom's " (Me)" or a host's suffix after it)."""
    return any(word in words(n) for n in names)


# -------------------------------------------------------------------------- report
@dataclass
class StepResult:
    n: int
    name: str
    ok: bool
    expected: str
    seen: str
    seconds: float
    ratio: float | None = None
    attempted: bool = True

    @classmethod
    def not_attempted(cls, n: int, name: str, expected: str, why: str) -> StepResult:
        """A step that could not start because something before it failed. It counts as failed."""
        return cls(n, name, False, expected, f"not attempted: {why}", 0.0, None, False)


def failed_steps(steps: Iterable[StepResult]) -> list[str]:
    return [f"{s.n} ({s.name})" for s in steps if not s.ok]


def _cell(text: str) -> str:
    one_line = " ".join(text.split()).replace("|", "\\|")
    return one_line if len(one_line) <= CELL_MAX else one_line[: CELL_MAX - 1] + "…"


def passed(steps: list[StepResult], meta: Mapping[str, Any]) -> bool:
    """A run passes when it recorded steps, every one passed, and nothing else went wrong (a container or a
    bot left behind, a piece of the link found in what the broker kept)."""
    return bool(steps) and all(s.ok for s in steps) and not meta.get("problems")


def markdown(steps: list[StepResult], meta: Mapping[str, Any]) -> str:
    verdict = "PASS" if passed(steps, meta) else "FAIL"
    lines = [
        "# Meeting self-test",
        "",
        f"**{verdict}**: {sum(s.ok for s in steps)} of {len(steps)} steps passed"
        f"{', but there are problems (below)' if meta.get('problems') else ''} "
        f"(a step with words passes at {MATCH_THRESHOLD:.0%} of the expected content words).",
        "",
    ]
    for key, value in meta.items():
        if not isinstance(value, (dict, list)):
            lines.append(f"- {key}: {_cell(str(value))}")
    lines += [
        "",
        "| # | Step | Result | Expected | Seen | Match | Seconds |",
        "|---|------|--------|----------|------|-------|---------|",
    ]
    for s in steps:
        result = "pass" if s.ok else ("FAIL" if s.attempted else "FAIL (not attempted)")
        match = "" if s.ratio is None else f"{s.ratio:.0%}"
        lines.append(
            f"| {s.n} | {_cell(s.name)} | {result} | {_cell(s.expected)} | {_cell(s.seen)} | {match} "
            f"| {s.seconds:.1f} |"
        )
    problems = meta.get("problems")
    if isinstance(problems, list) and problems:
        lines += ["", "Problems:", *[f"- {_cell(str(p))}" for p in problems]]
    return "\n".join(lines) + "\n"


def _scrubbed(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, Mapping):
        return {k: _scrubbed(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v, secrets) for v in value]
    return value


class ReportLeak(Exception):
    """A secret was still in the report after scrubbing. Nothing was written."""


def write_report(
    out_dir: str | os.PathLike[str],
    steps: list[StepResult],
    secrets: Iterable[str],
    meta: Mapping[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write the steps as JSON and as a short Markdown table into ``out_dir`` (made if missing), and
    return both paths. Every string is scrubbed of ``secrets`` first, and if any piece of them is still
    there afterwards nothing is written and ``ReportLeak`` is raised, without saying what it was."""
    pieces = [p for p in secrets if p]
    steps = [StepResult(**_scrubbed(asdict(s), pieces)) for s in steps]
    meta = _scrubbed(dict(meta or {}), pieces)
    body = {
        "result": "pass" if passed(steps, meta) else "fail",
        "threshold": MATCH_THRESHOLD,
        **meta,
        "steps": [asdict(s) for s in steps],
    }
    as_json = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    as_md = markdown(steps, meta)
    if leaks(pieces, {"json": as_json, "markdown": as_md}):
        raise ReportLeak("the report still held part of the meeting link, so it was not written")
    folder = Path(out_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    json_path, md_path = folder / REPORT_JSON, folder / REPORT_MD
    _write_private(json_path, as_json)
    _write_private(md_path, as_md)
    return json_path, md_path


def _write_private(path: Path, text: str) -> None:
    """Mode 0600 from the first byte: the lines of a call are in it, and the umask is not ours to guess."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    path.chmod(0o600)  # a file that already existed keeps its old mode through O_CREAT
