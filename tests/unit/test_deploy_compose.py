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
    name, digest = bot["image"].split("@", 1)
    assert name.startswith("ghcr.io/joinly-ai/joinly:v") and not name.endswith(":latest")
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert bot["shm_size"] == "1gb"


def test_the_broker_finds_the_bot_only_when_it_is_told_to() -> None:
    """The broker's joinly address comes from the environment and is empty by default, so without the
    profile `/meeting` says how to set it up instead of failing to reach a bot that isn't there."""
    env = COMPOSE["services"]["switchboard"]["environment"]
    assert env["SWITCHBOARD_JOINLY_URL"] == "${SWITCHBOARD_JOINLY_URL:-}"


def test_the_broker_passes_the_stt_model_on_and_it_is_empty_by_default() -> None:
    """``SWITCHBOARD_MEETING_STT_MODEL`` (#617) reaches the broker from the ``.env`` file, and unset it is
    empty, which the broker reads as unset: joinly keeps the model its image was built with. Fails if the
    example names a model for everyone (a first call would then download 1.6 GB), or the variable is not
    passed on and a `.env` line does nothing."""
    env = COMPOSE["services"]["switchboard"]["environment"]
    assert env["SWITCHBOARD_MEETING_STT_MODEL"] == "${SWITCHBOARD_MEETING_STT_MODEL:-}"


def test_the_gpu_bot_is_the_same_release_with_cuda_on_a_profile_of_its_own() -> None:
    """``meetings-gpu`` (#617) is the ``meetings`` profile on one NVIDIA GPU: joinly's ``-cuda`` build of
    the same release, with one device reserved the way Compose says (``deploy.resources.reservations``). It
    is as private as the CPU one (no port, no host network) and has the shared memory Chromium needs. The
    two profiles are separate services, so turning on both is a choice, and a CPU install has no ``deploy``
    block that would stop it starting on a machine with no GPU. Fails if the tag drifts from the CPU
    image's, if the device reservation is lost or asks for more, or if the bot is published."""
    cpu = COMPOSE["services"]["joinly"]
    gpu = COMPOSE["services"]["joinly-gpu"]
    assert cpu["profiles"] == ["meetings"] and gpu["profiles"] == ["meetings-gpu"]
    assert "deploy" not in cpu, "a machine with no GPU must still start the CPU bot"
    cpu_name, cpu_digest = cpu["image"].split("@", 1)
    gpu_name, gpu_digest = gpu["image"].split("@", 1)
    # Same release, two images: appending ``-cuda`` after ``@sha256`` is not the CUDA build.
    assert gpu_name == cpu_name + "-cuda" and cpu_digest != gpu_digest
    assert gpu_name.startswith("ghcr.io/joinly-ai/joinly:v") and gpu_name.endswith("-cuda")
    assert cpu_digest.startswith("sha256:") and gpu_digest.startswith("sha256:")
    assert "ports" not in gpu and "expose" not in gpu and "network_mode" not in gpu
    assert gpu["shm_size"] == "1gb"
    assert gpu["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    assert "JOINLY_DEVICE" not in str(gpu.get("environment")), "the cuda image sets its own device"
