"""What the live driver reads off a harness's terminal (test harness only; unit-tested).

Pure functions on a captured tmux screen, so the matching can be tested without a live CLI
(tests/unit/test_live_harness.py).
"""

from __future__ import annotations

import re

_QUOTA_WORDS = re.compile(
    r"(?i)(quota|usage limit).{0,60}(exceeded|reached|exhausted|hit)|hit your usage limit"
)
_REMAINING = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*% remaining")
# an approval prompt (or any confirm dialog) on a screen: typed text or Enter would reach it, and the
# driver only ever declines one with Esc
_APPROVAL = {
    "claude": re.compile(
        r"(?i)(do you want to proceed|this command requires approval|esc to cancel · tab to amend)"
    ),
    "codex": re.compile(
        r"(?i)(would you like to run|allow codex to|yes, proceed|approve this|esc to cancel)"
    ),
    "devin": re.compile(
        r"(?i)(↵ confirm|esc cancel|approve once|switch to bypass|allow (once|always|for)"
        r"|do you want to (run|allow|proceed))"
    ),
    # Grok Build's permission prompt (1.0.89, seen live): "Yes, proceed", "No, reject", "Always allow:
    # <rule>", "Never allow: <rule>", and its docs' "Allow once" / "Reject once"
    "grok": re.compile(
        r"(?i)(yes, proceed|no, reject|always allow: |never allow: |allow once|reject once"
        r"|always-approve mode\))"
    ),
    "cursor": re.compile(
        r"(?i)(run this (mcp tool|command)|run \(once\)|skip \(esc or n\)"
        r"|allow .{0,30}(once|always)|\[a\] allow|waiting for approval)"
    ),
}
_CLAUDE_READY = re.compile(r"(?i)(shortcuts|manual mode|accept edits|bypass permissions|auto mode)")
_CLAUDE_RESUME_ID = re.compile(r"claude --resume ([0-9a-f]{8}-[0-9a-f-]{27})")
_CODEX_USAGE = re.compile(r"Token usage: total=([\d,]+)")


def quota_left(screen: str) -> float | None:
    """The "N% remaining" Devin's banner shows, as a number, or None."""
    m = _REMAINING.search(screen)
    return float(m.group(1)) if m else None


def quota_all(screen: str) -> list[float]:
    """Every "N% remaining" on a screen (Devin shows one each time it starts), oldest first."""
    return [float(x) for x in _REMAINING.findall(screen)]


def quota_exhausted(screen: str) -> bool:
    """A usage limit is hit: an explicit message, or exactly 0% remaining (not 10%, 50% or 100%)."""
    return bool(_QUOTA_WORDS.search(screen)) or quota_left(screen) == 0.0


def approval_open(harness: str, screen: str) -> bool:
    """An approval prompt or confirm dialog is on the screen: never type, press Enter or approve."""
    pat = _APPROVAL.get(harness)
    return bool(pat and pat.search(screen))


def claude_dialog(screen: str) -> str | None:
    """What the driver does at Claude Code's start-up dialogs: the key to press, or None.

    Only the folder-trust dialog for the run's own scratch folder is answered (Enter on "Yes, I trust this
    folder", Down from "No, exit" first, checked before Enter). The Chrome offer is dismissed with Esc. A
    bypass-mode warning is never answered: the caller fails the run."""
    if "Yes, I accept" in screen:
        return "FAIL"
    if "Yes, I trust this folder" in screen:
        return "Enter" if re.search(r"❯\s*(\d\.\s*)?Yes, I trust this folder", screen) else "Down"
    if "Claude in Chrome" in screen:
        return "Escape"
    return None


def claude_onboarding(screen: str) -> str | None:
    """A throwaway config directory starts Claude Code as a first run: it asks for a text style and then
    shows its security notes. Both are answered with Enter (the default, and "continue"): the key to press,
    or None."""
    if "Choose the text style" in screen:
        return "Enter"
    if "Security notes:" in screen and "Press Enter to continue" in screen:
        return "Enter"
    return None


def claude_ready(screen: str) -> bool:
    return bool(_CLAUDE_READY.search(screen)) and "Yes, I trust this folder" not in screen


def claude_resume_id(screen: str) -> str | None:
    """The session id Claude Code prints on exit ("claude --resume <id>")."""
    m = _CLAUDE_RESUME_ID.search(screen)
    return m.group(1) if m else None


def prompt_line(screen: str, marker: str) -> str:
    """The text on the CLI's live input line: the last line that starts with ``marker`` (Codex ``›``, Devin
    ``❭``, Cursor ``→``). A sent message is echoed higher up, so this is what has not been sent."""
    for line in reversed(screen.splitlines()):
        s = line.strip()
        if s.startswith(marker):
            return s[len(marker) :].strip()
    return ""


def claude_input_text(screen: str) -> str:
    """What is in Claude Code's input box: the text between the last two rows of ``─``. Empty when the
    box can't be found."""
    rows = [i for i, x in enumerate(screen.splitlines()) if len(x) >= 20 and set(x.strip()) == {"─"}]
    if len(rows) < 2:
        return ""
    return " ".join(screen.splitlines()[rows[-2] + 1 : rows[-1]]).strip()


# Grok's first-run question about a folder's trust, in the phrasings it may use
_GROK_TRUST = re.compile(
    r"Do you trust|trust (?:this|the) (?:folder|directory|workspace)|Trust (?:folder|workspace)", re.I
)
# its "yes" row, with the key that picks it (Grok Build 1.0.92: "Yes, proceed   y")
_GROK_TRUST_YES = re.compile(r"^\s*(?:❯\s*)?Yes, proceed\s+y\s*$", re.M)


def grok_trust_prompt(screen: str) -> bool:
    """Whether Grok is asking whether to trust a folder."""
    return bool(_GROK_TRUST.search(screen))


def grok_dialog(screen: str, workspaces: set[str]) -> str | None:
    """What the driver does at Grok Build's start-up trust question: the key to press, or None.

    Only the question for the run's own scratch folder is answered, as for Claude Code (``claude_dialog``),
    with the maintainer's yes of 2026-11-22 (#928): ``y`` on "Yes, proceed" when the folder it names, on a
    line of its own, is one of ``workspaces`` (the scratch folder's path as given and resolved). Any other
    trust question, or one whose rows don't read as expected, is ``FAIL``: the caller fails the run and
    answers nothing."""
    if not grok_trust_prompt(screen):
        return None
    named = {line.strip() for line in screen.splitlines()}
    if named & workspaces and _GROK_TRUST_YES.search(screen):
        return "y"
    return "FAIL"


def grok_input_text(screen: str) -> str:
    """What is in Grok Build's input box: the rows between its last ``╭`` and ``╰`` borders, without the
    side borders and the ``❯`` it starts with, joined. Empty when the box can't be found."""
    lines = screen.splitlines()
    tops = [i for i, x in enumerate(lines) if x.strip().startswith("╭")]
    ends = [i for i, x in enumerate(lines) if x.strip().startswith("╰")]
    if not tops or not ends or ends[-1] <= tops[-1]:
        return ""
    rows = [x.strip().strip("│").strip() for x in lines[tops[-1] + 1 : ends[-1]]]
    text = " ".join(r for r in rows if r)
    return text.removeprefix("❯").strip()


def claude_agent_finished(screen: str) -> bool:
    """Claude Code reported a background agent as finished (``Agent "<task>" finished · 4s``)."""
    return bool(re.search(r'Agent ".*" finished', screen))


def codex_ready(screen: str) -> bool:
    return bool(re.search(r"^\s*[›>] ", screen, re.M)) and "Do you trust" not in screen


def codex_tokens(screen: str) -> int | None:
    """Total tokens Codex prints when it quits ("Token usage: total=16,642 ..."), or None."""
    m = _CODEX_USAGE.search(screen)
    return int(m.group(1).replace(",", "")) if m else None


def devin_ready(screen: str) -> bool:
    return ("❭" in screen and "Ask Devin" in screen) or "Guide Devin" in screen


def devin_waiting(screen: str) -> bool:
    """Devin is inside its switchboard wait() call (the call is the last tool line, and it is running)."""
    lines = [x for x in screen.splitlines() if x.strip()]
    calls = [i for i, x in enumerate(lines) if "mcp__switchboard_https__wait(" in x]
    return bool(calls) and "Running tools" in "\n".join(lines[calls[-1] :])
