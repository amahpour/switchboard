"""The Docker Compose example's meeting bot (#606, docs/DEPLOY.md "A meeting bot").

joinly has no sign-in and takes its settings from whoever connects, so in the example it is an opt-in
profile, publishes no port, and is reached by the broker alone on the project's network. These check the
lines that keep it so: a change that published its port, or made it start with every `up`, would fail here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "deploy" / "compose" / "compose.yaml").read_text(encoding="utf-8"))


def test_the_meeting_bot_is_opt_in_and_reachable_by_the_broker_alone() -> None:
    """Fails if joinly runs without the `meetings` profile, publishes or exposes a port, or loses the
    shared memory its Chromium needs."""
    bot = COMPOSE["services"]["joinly"]
    assert bot["profiles"] == ["meetings"]
    assert "ports" not in bot and "expose" not in bot and "network_mode" not in bot
    assert bot["image"].startswith("ghcr.io/joinly-ai/joinly:v") and not bot["image"].endswith(":latest")
    assert bot["shm_size"] == "1gb"


def test_the_broker_finds_the_bot_only_when_it_is_told_to() -> None:
    """The broker's joinly address comes from the environment and is empty by default, so without the
    profile `/meeting` says how to set it up instead of failing to reach a bot that isn't there."""
    env = COMPOSE["services"]["switchboard"]["environment"]
    assert env["SWITCHBOARD_JOINLY_URL"] == "${SWITCHBOARD_JOINLY_URL:-}"
