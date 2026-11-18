"""The hosted bot's Compose file (plugins/claude/meeting/compose.yaml, DESIGN.md section 54, #623)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "plugins" / "claude" / "meeting" / "compose.yaml"


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


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_relay_turns_off_the_images_broker_health_check(harness: str) -> None:
    """The relay container is switchboard's image, whose HEALTHCHECK polls a broker on
    127.0.0.1:7419. Nothing in that container listens there, so Docker always reported the
    relay unhealthy (#760), and that label hid a real failure (#758). Fails if the relay
    service inherits the image check, or if the file no longer says why it is off."""
    path = ROOT / "plugins" / harness / "meeting" / "compose.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["services"]["relay"].get("healthcheck") == {"disable": True}
    assert "127.0.0.1:7419" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_hosted_joinly_passes_the_bot_video_switch_through(harness: str) -> None:
    """SWITCHBOARD_BOT_VIDEO (#820) must reach the joinly container, where the patched server.py reads it:
    the host's value, or empty (the logo on). Fails if the switch stops at the host script."""
    doc = yaml.safe_load(
        (ROOT / "plugins" / harness / "meeting" / "compose.yaml").read_text(encoding="utf-8")
    )
    env = doc["x-joinly"]["environment"]
    assert env["SWITCHBOARD_BOT_VIDEO"] == "${SWITCHBOARD_BOT_VIDEO:-}"
