"""Which probe calls which endpoint (DESIGN.md §30.3, #953): startup on /healthz, liveness and
readiness on /readyz, which reads the database, so a broker that is up but can't read it (a volume
another pod's SELinux level locked it out of) is restarted instead of staying broken for hours.

The image's HEALTHCHECK is checked from the Dockerfile's text; the image test CI runs for it is
``tests/image/test_image.py`` (``-m image``, Docker), which needs the container.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART_STATEFULSET = ROOT / "deploy" / "helm" / "switchboard" / "templates" / "statefulset.yaml"
MANIFEST = ROOT / "deploy" / "kubernetes" / "switchboard.yaml"
DOCKERFILE = ROOT / "deploy" / "image" / "Dockerfile"
RENDER = ROOT / "deploy" / "render" / "render.yaml"
WANT = {"startupProbe": "/healthz", "livenessProbe": "/readyz", "readinessProbe": "/readyz"}


def _probes_in_text(path: Path) -> dict[str, str]:
    """``<name>Probe`` to its httpGet path, read from the file's text (the chart is a Go template, not YAML).
    The broker container's are the first of each name: joinly's are exec probes."""
    found: dict[str, str] = {}
    for name, path_ in re.findall(r"(\w+Probe):\s*\n\s*httpGet: \{path: (/\w+)", path.read_text()):
        found.setdefault(name, path_)
    return found


@pytest.mark.parametrize("path", [CHART_STATEFULSET, MANIFEST], ids=["helm-chart", "kubernetes-manifest"])
def test_startup_probes_the_process_and_liveness_and_readiness_read_the_database(path: Path) -> None:
    """Fails when liveness or readiness stay on /healthz (a broker that can't read its database is never
    restarted: the 29 hours of #953), or when startup moves to /readyz (a slow migration would then count
    against the liveness-style failure budget instead of the startup probe's 60 s)."""
    assert _probes_in_text(path) == WANT


def test_the_manifest_parses_with_the_same_probes() -> None:
    """The text check above reads the manifest's lines; this reads its parsed container, so a change of
    layout can't make the text check pass over a different probe."""
    docs: list[Any] = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    (broker,) = [c for c in sts["spec"]["template"]["spec"]["containers"] if c["name"] == "switchboard"]
    assert {k: broker[k]["httpGet"]["path"] for k in WANT} == WANT


def test_the_image_healthcheck_calls_readyz() -> None:
    """Docker and Compose restart on the image's HEALTHCHECK only if it reads the database."""
    (line,) = [ln for ln in DOCKERFILE.read_text().splitlines() if ln.lstrip().startswith("CMD python3 -c")]
    assert "/readyz" in line and "/healthz" not in line


def test_render_restarts_a_broker_whose_database_cannot_be_read() -> None:
    """Render's check is its only liveness signal, so it reads the database too."""
    spec = yaml.safe_load(RENDER.read_text())
    assert spec["services"][0]["healthCheckPath"] == "/readyz"
