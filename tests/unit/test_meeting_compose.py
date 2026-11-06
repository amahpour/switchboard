"""The hosted bot's Compose file (plugins/claude/meeting/compose.yaml, DESIGN.md section 54, #623)."""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "plugins" / "claude" / "meeting" / "compose.yaml"


def test_joinly_s_tmp_lets_its_speech_engine_load_its_library() -> None:
    """joinly's root is read-only, so /tmp is its one writable place, and Kokoro's speech (espeak, through
    phonemizer) copies its shared library into a temp folder there and loads it. Docker mounts a tmpfs noexec
    unless the mount says exec, and then every speak_text fails with "Error while speaking text": the hosted
    self-test's step 4 (an @call line heard in the call) failed that way on a real Zoom call. Fails if
    joinly's /tmp loses exec, in either profile."""
    doc = yaml.safe_load(COMPOSE.read_text())
    joinlys = {name: svc for name, svc in doc["services"].items() if name != "relay"}
    assert joinlys, "the Compose file has no joinly service"
    for name, svc in joinlys.items():
        assert svc.get("read_only") is True, name
        mounts = svc.get("tmpfs") or []
        tmp = [m for m in mounts if m.split(":", 1)[0] == "/tmp"]
        assert tmp, name
        opts = tmp[0].split(":", 1)[1].split(",") if ":" in tmp[0] else []
        assert "exec" in opts and "noexec" not in opts, (name, tmp[0])
