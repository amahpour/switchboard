"""How to share a file is written down, because switchboard stores none (#544).

Agents post a path on their own machine that nobody else can open. The usage section is the
workaround that already works: a link renders, except one back to the broker, and a file lives
somewhere the reader can already reach. The research note is there so a reader of the
2026-10-12 spike doesn't follow the local MCP server that #385 removed. Each check fails if
that sentence is missing or no longer says what it has to.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONVENTION = "artifact: <name>, <size>, sha256:<hash>, <url>"


def _usage_section() -> str:
    text = (ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    heading = "## Sharing files"
    start = text.index(heading)
    rest = text[start + len(heading) :]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def test_usage_says_how_to_share_a_file() -> None:
    """Fails if Sharing files drops the facts a reader needs: switchboard stores no files, a
    link already renders, a link to the broker's own host is refused so a message can't send
    someone to /logout, the three ways a file is shared today, the one-line name a reader
    checks a fetch against, and that anything fetched is untrusted input."""
    body = _usage_section()
    assert "doesn't store files" in body
    assert "already render" in body
    assert "broker's own host" in body
    assert "refused" in body
    assert "/logout" in body
    assert "path on a shared machine" in body
    assert "gist" in body
    assert "pull request" in body
    assert "commit SHA" in body
    assert CONVENTION in body
    assert "untrusted input" in body


def test_file_sharing_research_points_at_the_usage_section() -> None:
    """Fails if the spike still reads as current. Its top, before the verdict, must say the
    mechanism predates #385 and the local MCP server it relies on is gone, and link to the
    usage section a reader should follow instead."""
    text = (ROOT / "docs/research/file-sharing.md").read_text(encoding="utf-8")
    head, sep, _rest = text.partition("**Verdict**")
    assert sep, "the spike's verdict is the line the note has to come before"
    assert "#385" in head
    assert "local MCP server" in head
    assert "predates" in head
    assert "gone" in head
    assert "USAGE.md#sharing-files" in head
