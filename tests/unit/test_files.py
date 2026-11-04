"""Attachments: which files are due, what an artifact line means, and a safe download name (#544).

The expiry rule is pure, so a ``FakeClock`` is the whole clock. A line whose ``<where>`` is a
local path is the one an agent's ``say()`` turns into an upload; an http(s) URL and the
``switchboard:file/<id>`` form are not. The download name is what ``Content-Disposition`` quotes,
and it cannot carry a path, a quote or a newline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import child_env

from switchboard.files import (
    artifact_lines,
    content_disposition,
    file_expired,
    share_line,
    shell_quote,
    upload_path_word,
)

DAY = 86400.0
NOW = 1_790_000_000.0


def test_a_stored_file_expires_a_retention_period_after_it_was_stored() -> None:
    """Expiry is stored_at plus the retention setting. 0 keeps the file. A pending upload expires
    with its token, not with retention. Fails if the rule uses wall time, or treats 0 as 30 days."""
    stored = NOW - 30 * DAY
    assert file_expired(state="stored", now=NOW, retention_days=30, stored_at=stored, upload_expires_at=None)
    assert not file_expired(
        state="stored", now=NOW - 1, retention_days=30, stored_at=stored, upload_expires_at=None
    )
    assert not file_expired(
        state="stored", now=NOW + 365 * DAY, retention_days=0, stored_at=stored, upload_expires_at=None
    )
    assert file_expired(state="pending", now=NOW, retention_days=0, stored_at=None, upload_expires_at=NOW)
    assert not file_expired(
        state="pending", now=NOW - 1, retention_days=30, stored_at=None, upload_expires_at=NOW
    )
    # an unknown state is not kept
    assert file_expired(state="elsewhere", now=NOW, retention_days=0, stored_at=NOW, upload_expires_at=None)


def test_a_local_path_line_is_parsed_and_a_url_is_not_an_upload() -> None:
    """The server reads the same grammar the page does. Only a path (not http(s), not the
    switchboard form) is an upload. A fenced copy of the line is an example, not a file.
    Fails if a URL or the stored form minted an upload, or a path was missed."""
    sha = "ab" * 32
    text = "\n".join(
        [
            "see this",
            f"artifact: notes.txt | 5 | sha256:{sha} | /Users/someone/notes.txt",
            "artifact: page.html | 12 KB | https://files.example.com/page.html",
            "artifact: kept.bin | switchboard:file/" + "cd" * 16,
            "```",
            "artifact: inside.txt | /tmp/inside.txt",
            "```",
            "artifact: report.zip | ~/scratch/report.zip",
        ]
    )
    got = artifact_lines(text)
    assert [(a.name, a.kind, a.byte_size, a.sha256) for a in got] == [
        ("notes.txt", "path", 5, sha),
        ("page.html", "url", None, ""),
        ("kept.bin", "stored", None, ""),
        ("report.zip", "path", None, ""),
    ]
    assert got[0].where == "/Users/someone/notes.txt"
    assert got[2].file_id == "cd" * 16


def test_a_download_name_cannot_break_the_header() -> None:
    """Content-Disposition quotes one safe token. A path, a quote, a newline and an empty name
    cannot add another header or point outside the file. Fails if any of those survive."""
    assert content_disposition("report.zip") == 'attachment; filename="report.zip"'
    assert content_disposition("../../etc/passwd") == 'attachment; filename="passwd"'
    assert content_disposition('say"hi.txt') == 'attachment; filename="say_hi.txt"'
    bad = content_disposition("a\r\nX-Evil: 1")
    assert "\r" not in bad and "\n" not in bad and bad.startswith("attachment;")
    assert content_disposition("..") == 'attachment; filename="download"'
    assert content_disposition("") == 'attachment; filename="download"'
    assert "\n" not in content_disposition("résumé.pdf")


def test_the_share_line_quotes_the_path() -> None:
    """The curl line the agent is given uses the path it wrote, quoted, and the one-time token.
    A single quote in the path stays inside the shell quoting."""
    assert shell_quote("/Users/someone/it's.txt") == "'/Users/someone/it'\\''s.txt'"
    line = share_line("notes.txt", "/Users/someone/it's.txt", "http://switchboard.localhost:7419", "tok")
    assert line == (
        "To share notes.txt, run: curl --fail -T '/Users/someone/it'\\''s.txt'"
        " http://switchboard.localhost:7419/api/upload/tok"
    )


def test_a_home_relative_path_is_expanded_by_the_shell_and_nothing_else_is() -> None:
    """#587: agents write ``~/work/build.log``. Quoted whole, the ``~`` reached curl as a literal
    character and the upload failed with no such file. A leading ``~/`` becomes ``"$HOME"/`` with
    the rest single-quoted; ``~user/`` and a ``~`` later in the path stay quoted. Fails if the line
    quotes the ``~`` again."""
    assert upload_path_word("~/work/build.log") == "\"$HOME\"/'work/build.log'"
    assert upload_path_word("~bob/x.log") == "'~bob/x.log'"
    assert upload_path_word("/srv/~/x.log") == "'/srv/~/x.log'"
    line = share_line("build.log", "~/work/it's.log", "https://sb.example.com", "tok")
    assert line == (
        "To share build.log, run: curl --fail -T \"$HOME\"/'work/it'\\''s.log' https://sb.example.com/api/upload/tok"
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
def test_the_shell_finds_a_home_relative_file_and_runs_nothing_in_its_name(tmp_path: Path) -> None:
    """The word the line hands curl, given to a real ``sh``: a file under ``$HOME`` named with
    a quote, a ``$( )`` and a space resolves to that file, and the ``$( )`` is not run. Fails if
    ``~`` is left quoted (no such file) or if the rest of the path is expanded."""
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    name = "it's $(touch ran) a.log"
    (home / "work" / name).write_text("ok\n", encoding="utf-8")
    word = upload_path_word(f"~/work/{name}")
    out = subprocess.run(
        ["sh", "-c", f"cat {word}"],
        cwd=tmp_path,
        env=child_env(home),
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0 and out.stdout == "ok\n", out.stderr
    assert not (tmp_path / "ran").exists() and not (home / "ran").exists()
