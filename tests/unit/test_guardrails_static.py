"""Static guardrail test (DESIGN.md §11): forbidden strings live only in guardrails.py."""

from __future__ import annotations

import re
from pathlib import Path

from switchboard import guardrails

SRC = Path(__file__).resolve().parents[2] / "src" / "switchboard"

# Spelled out here (not imported) so a bad edit to guardrails.py can't hide a string.
REQUIRED = [
    "hooks.state",
    "trusted_hash",
    "crossSessionInbound",
    "--dangerously",
    "--approve-for-me",
    "--approve-mcps",
    "--trust",
    "approvalPolicy",
    "sandboxPolicy",
    "network_access",
    "claude/channel",
    "thread/resume",
    "permissionDecision",
    "updatedInput",
    "updated_mcp_tool_output",
]
PATTERNS = [re.compile(re.escape(s)) for s in REQUIRED] + [re.compile(r'"trust"\s*:\s*true', re.I)]
# The vendored Mermaid (DESIGN.md §33) bundles KaTeX, whose option table documents KaTeX's own
# command line, "-T, --trust": browser code, never part of anything switchboard sends a harness.
# That one occurrence is allowed (the maintainer's yes, #57's pull request); any other hit in the
# file, or a second one, still fails, and so does a Mermaid update that changes it.
KNOWN_HITS = {"web/static/vendor/mermaid/mermaid.min.js": {'cli:"-T, --trust"': 1}}


def source_files() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".html", ".css", ".toml", ".json"}]


def test_denylist_is_complete() -> None:
    for s in REQUIRED:
        assert s in guardrails.ALL_FORBIDDEN
    assert '"trust": true' in guardrails.ALL_FORBIDDEN


def test_no_forbidden_strings_outside_guardrails() -> None:
    files = source_files()
    assert len(files) > 10
    hits = []
    for p in files:
        if p.name == "guardrails.py":
            continue
        text = p.read_text(errors="replace")
        for known, n in KNOWN_HITS.get(p.relative_to(SRC).as_posix(), {}).items():
            assert text.count(known) == n, (p.name, known)
            text = text.replace(known, "")
        for pat in PATTERNS:
            if pat.search(text):
                hits.append(f"{p.relative_to(SRC)}: {pat.pattern}")
    assert hits == []


def test_claude_inbox_never_sends_priority() -> None:
    inbox = SRC / "mcp" / "claude_inbox.py"
    if inbox.exists():  # arrives in M3
        assert '"priority"' not in inbox.read_text() and "'priority'" not in inbox.read_text()


def test_find_forbidden_runtime_check() -> None:
    assert guardrails.find_forbidden({"threadId": "t", "input": []}) == []
    assert guardrails.find_forbidden({"approvalPolicy": "never"})
    assert guardrails.find_forbidden('{"trust":true}')
    assert guardrails.find_forbidden(["codex", "--dangerously-bypass-approvals"])


def test_no_permission_request_hooks_or_decisions_in_hook_script() -> None:
    hook = (SRC / "hook" / "switchboard_hook.py").read_text()
    assert "PermissionRequest" not in hook
    assert '"decision"' not in hook or "block" in hook
