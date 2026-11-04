"""Attachments: which files are due, what an artifact line means, and a safe download name (#544).

The expiry rule is pure, so a ``FakeClock`` is the whole clock. A line whose ``<where>`` is a
local path is the one an agent's ``say()`` turns into an upload; an http(s) URL and the
``switchboard:file/<id>`` form are not. The download name is what ``Content-Disposition`` quotes,
and it cannot carry a path, a quote or a newline.
"""

from __future__ import annotations

from switchboard.files import (
    artifact_lines,
    content_disposition,
    file_expired,
    quoted_path,
    share_line,
    shell_quote,
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


def test_the_share_line_expands_a_leading_home() -> None:
    """Agents write home-relative paths all the time: `~/...` becomes a word whose home part
    the shell still expands, and everything after it stays safely quoted. `~user/...` is
    another user's home, not the agent's own, so it stays a plain quoted word."""
    assert quoted_path("~/work/build.log") == '"$HOME"/\'work/build.log\''
    assert quoted_path("~other/x") == "'~other/x'"
    line = share_line("build.log", "~/work/build.log", "http://switchboard.localhost:7419", "tok")
    assert line == (
        'To share build.log, run: curl --fail -T "$HOME"/\'work/build.log\''
        " http://switchboard.localhost:7419/api/upload/tok"
    )
