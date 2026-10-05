"""Recorded-contract fixtures carry no personal data (DESIGN.md §0, §12.3)."""

from __future__ import annotations

import getpass
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures"
# Generic markers; the identity of whoever runs the suite is added at runtime,
# so this file names nobody (DESIGN.md §0).
BANNED = ("user_email", "/Users/", "postgres://", "postgresql://", "BEGIN PRIVATE KEY")
# an API-key prefix as a whole word (so "brisk-otter-1" is not one)
SECRET_PREFIX = re.compile(r"(?i)(?<![a-z0-9])sk-")
# user home and temp paths at the start of an absolute path (not "/opt/yk/home/...")
HOME_PATH = re.compile(r"(?:^|[\s\"'=:(\[,])(?:/home/|/var/folders/|/private/var/|/root/)")
TOKENISH = re.compile(r"[A-Za-z0-9+/_=-]{24,}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def local_identity() -> list[str]:
    """This machine's user name, home directory name and git email (if any)."""
    out = {getpass.getuser(), Path.home().name}
    try:
        r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=5)
        email = r.stdout.strip()
        if email:
            out |= {email, email.split("@")[0]}
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(x for x in out if len(x) >= 4 and x not in ("root", "runner", "user"))


def entropy(s: str) -> float:
    c = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in c.values())


def high_entropy(s: str) -> list[str]:
    out = []
    for tok in TOKENISH.findall(s):
        mixed = (
            any(ch.islower() for ch in tok)
            and any(ch.isupper() for ch in tok)
            and any(ch.isdigit() for ch in tok)
        )
        if mixed and entropy(tok) > 4.0:
            out.append(tok)
    return out


def fixture_files() -> list[Path]:
    return sorted(p for p in FIX.rglob("*") if p.is_file())


def test_there_are_fixtures_for_every_harness() -> None:
    for h in ("claude", "codex", "cursor", "devin"):
        assert list((FIX / "payloads" / h).glob("*.json")), h


def test_no_personal_data_or_secrets_in_fixtures() -> None:
    hits = []
    banned = (*BANNED, *local_identity())
    for p in fixture_files():
        text = p.read_text(errors="replace")
        for b in banned:
            if b.lower() in text.lower():
                hits.append(f"{p.relative_to(FIX)}: {b if b in BANNED else '<local identity>'}")
        for _ in SECRET_PREFIX.finditer(text):
            hits.append(f"{p.relative_to(FIX)}: sk-")
        for _ in EMAIL.finditer(text):
            hits.append(f"{p.relative_to(FIX)}: an email address")
        for m in HOME_PATH.finditer(text):
            hits.append(f"{p.relative_to(FIX)}: home/temp path {m.group(0).strip()}")
        for tok in high_entropy(text):
            hits.append(f"{p.relative_to(FIX)}: high-entropy {tok[:12]}...")
    assert hits == []


def test_scanner_catches_what_it_should() -> None:
    assert EMAIL.search("reach me at someone.else@example.com please")
    assert not EMAIL.search("session@2 and a@b are not addresses")
    assert SECRET_PREFIX.search('"key": "sk-abc"') and SECRET_PREFIX.search("token=SK-abc")
    assert not SECRET_PREFIX.search('"session_id": "brisk-otter-1"')
    assert HOME_PATH.search('"cwd": "/home/someone/ws"') and HOME_PATH.search("at /var/folders/x")
    assert not HOME_PATH.search('--home "/opt/yk/home/hooks/x"')
    assert high_entropy("xoxb-9aZq7LmN2pQrT5vWx8YbC3dE")
    assert not high_entropy("00000000-0000-4000-8000-000000000001")
    assert not high_entropy("sanitized tool output")


def test_payload_fixtures_are_valid_hook_payloads() -> None:
    for p in (FIX / "payloads").rglob("*.json"):
        d = json.loads(p.read_text())
        assert isinstance(d.get("hook_event_name"), str), p
        assert "_source" in d and isinstance(d.get("_unverified"), bool), p
        assert "user_email" not in d and "env" not in d
