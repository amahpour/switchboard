"""What the live driver reads off a harness's terminal (test harness only; unit-tested).

Pure functions on a captured tmux screen, so the matching can be tested without a
live CLI (tests/unit/test_live_screens.py).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

_QUOTA_WORDS = re.compile(r"(?i)(quota|usage limit).{0,60}(exceeded|reached|exhausted)")
_REMAINING = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*% remaining")
# Claude Code on a model the account can't use: "There's an issue with the selected model (x).
# It may not exist or you may not have access to it." (after the first API call), or a CLI error
_CLAUDE_MODEL_ERR = re.compile(
    r"(?i)(issue with the selected model|may not exist or you may not have access"
    r"|model.{0,40}(not (found|available|supported)|invalid|unknown))"
)
# an approval selector (or any confirm dialog) on a Devin screen: typed text or Enter would reach it
_DEVIN_SELECTOR = re.compile(
    r"(?i)(↵ confirm|esc cancel|approve once|switch to bypass|allow (once|always|for)"
    r"|do you want to (run|allow|proceed))"
)


def devin_quota_left(screen: str) -> float | None:
    """The "N% remaining" Devin's banner shows, as a number, or None."""
    m = _REMAINING.search(screen)
    return float(m.group(1)) if m else None


def devin_quota_exhausted(screen: str) -> bool:
    """Devin's quota is used up: an explicit message, or exactly 0% remaining
    (not 10%, 50% or 100%)."""
    return bool(_QUOTA_WORDS.search(screen)) or devin_quota_left(screen) == 0.0


def claude_model_error(screen: str) -> bool:
    """Claude Code says the chosen model can't be used (the driver then falls back to haiku)."""
    return bool(_CLAUDE_MODEL_ERR.search(screen))


def devin_selector_open(screen: str) -> bool:
    """An approval selector or confirm dialog is on the Devin screen: never type or press Enter."""
    return bool(_DEVIN_SELECTOR.search(screen))


def private_tmp() -> str:
    """A per-user temp dir (macOS ``/var/folders/.../T``, mode 0700) for the scratch repo, so
    Claude's folder-trust entry for it isn't a path under world-writable ``/tmp`` that another
    account could re-create later. Falls back to the system temp dir when neither is private."""
    cands: list[str] = []
    try:
        out = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        cands.append(out)
    except (OSError, subprocess.SubprocessError):
        pass
    cands.append(os.environ.get("TMPDIR", ""))
    for c in cands:
        c = c.rstrip("/")
        try:
            st = os.stat(c) if c else None
        except OSError:
            continue
        if st is not None and st.st_uid == os.getuid() and not (st.st_mode & 0o077):
            return os.path.realpath(c)  # /var -> /private/var: the path the CLIs see as their cwd
    return tempfile.gettempdir()
