"""The Kubernetes patch that adds joinly beside the broker (#591, DESIGN.md §52; docs/DEPLOY.md).

joinly has no sign-in and takes its settings from whoever connects, so what keeps it private is a
few lines of YAML: it listens on the pod's localhost, it is offered to nobody, and its probes
reach it there. None of that is checked by Kubernetes. Each test here names the way a quiet edit
would break it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from switchboard.config import MeetingCfg

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "kubernetes"
PATCH = DEPLOY / "meetings" / "joinly.yaml"
ZOOM_PATCH = DEPLOY / "meetings" / "joinly-zoom-fix.yaml"


def _pod(path: Path) -> dict[str, Any]:
    """The pod spec of the one StatefulSet in a patch file."""
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    assert len(docs) == 1, "a patch is one document"
    assert docs[0]["kind"] == "StatefulSet" and docs[0]["metadata"]["name"] == "switchboard"
    return docs[0]["spec"]["template"]["spec"]


def _container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in pod["containers"] if c["name"] == name]
    return found


def test_joinly_listens_on_the_pods_localhost_and_is_offered_to_nobody() -> None:
    """The sidecar's only protection is that it is not reachable: its image listens on 0.0.0.0, which
    gives every pod in the cluster its port at the pod's IP with or without a Service. So the patch
    sets JOINLY_SERVER_HOST to loopback, declares no port, and the broker is pointed at loopback.
    Fails if any of the three is dropped or the broker's address stops being the same loopback."""
    pod = _pod(PATCH)
    joinly = _container(pod, "joinly")
    env = {e["name"]: e["value"] for e in joinly["env"]}
    assert env["JOINLY_SERVER_HOST"] == "127.0.0.1"
    assert "ports" not in joinly, "a containerPort offers joinly to the cluster, even if nothing reads it"
    broker_env = {e["name"]: e["value"] for e in _container(pod, "switchboard")["env"]}
    url = broker_env["SWITCHBOARD_JOINLY_URL"]
    assert MeetingCfg(joinly_url=url).joinly_url == url, "the broker would refuse its own address"
    # the host above, the image's JOINLY_SERVER_PORT, and joinly's /mcp/
    assert url == "http://127.0.0.1:8000/mcp/"
    services = [d for d in yaml.safe_load_all((DEPLOY / "switchboard.yaml").read_text()) if d]
    assert all(d["kind"] != "Service" or all(p["port"] != 8000 for p in d["spec"]["ports"]) for d in services)


def test_joinly_runs_with_the_tightest_context_it_was_seen_to_start_in() -> None:
    """joinly started and served a session with a read-only root, no capabilities and no
    privilege escalation, as the image's own user (uid 1001: its home, with the speech models, is
    mode 0700). Fails if the patch loosens any of that, or runs it as root, or as the broker's uid
    (which cannot read the models)."""
    ctx = _container(_pod(PATCH), "joinly")["securityContext"]
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["capabilities"] == {"drop": ["ALL"]}
    assert ctx["runAsNonRoot"] is True and ctx["runAsUser"] == 1001 and ctx["runAsGroup"] == 1001
    assert not ctx.get("privileged")


def test_joinly_is_probed_from_inside_and_cannot_unready_the_broker() -> None:
    """A kubelet's httpGet and tcpSocket probes go to the pod's IP, where a joinly on 127.0.0.1
    does not answer, so the probes must be exec ones and the pod would never be Ready otherwise. And
    no readinessProbe: a pod is Ready only when every container is, so one that failed would take
    the broker out of its Service and answer every browser and agent with a 503. Fails if a probe
    is switched to httpGet or tcpSocket, or a readiness probe is added."""
    joinly = _container(_pod(PATCH), "joinly")
    assert "readinessProbe" not in joinly
    for name in ("startupProbe", "livenessProbe"):
        probe = joinly[name]
        assert set(probe) & {"httpGet", "tcpSocket", "grpc"} == set(), name
        assert "127.0.0.1:8000/health" in " ".join(probe["exec"]["command"]), name


def test_every_mount_has_a_volume_and_chromium_gets_room() -> None:
    """A mount with no volume is a pod that never starts, and the shared memory and the temporary
    folder are what joinly needs: /dev/shm backed by memory and bigger than the default 64 MB, and a
    writable /tmp on a read-only root, where PulseAudio keeps its state too (XDG_CONFIG_HOME;
    without a writable place for it no session opens). Fails if one is dropped or renamed."""
    pod = _pod(PATCH)
    joinly = _container(pod, "joinly")
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = {m["mountPath"]: m["name"] for m in joinly["volumeMounts"]}
    assert set(mounts.values()) <= set(volumes)
    assert {"/tmp", "/dev/shm"} <= set(mounts)
    env = {e["name"]: e["value"] for e in joinly["env"]}
    assert env["XDG_CONFIG_HOME"] in mounts, "PulseAudio's state must be on a writable mount"
    shm = volumes[mounts["/dev/shm"]]["emptyDir"]
    assert shm["medium"] == "Memory" and shm["sizeLimit"] == "1Gi"
    resources = joinly["resources"]
    assert resources["requests"]["memory"] == "2Gi" and resources["limits"]["memory"] == "4Gi"
    assert "cpu" not in resources["limits"], "throttling makes the transcript fall behind"


def test_the_zoom_fix_is_a_patch_of_its_own_over_the_files_docs_name() -> None:
    """The Zoom file is mounted from a ConfigMap with subPath, over the one file; `optional: true`
    does not let the pod start without it (kubelet makes the subPath a directory and the mount
    fails with "not a directory"), so it is a second patch that only those who made the ConfigMap
    apply. Fails if the mount path drifts from DEPLOY.md's `docker run`, if the mount becomes
    writable, or if the volume reaches the sidecar patch."""
    pod = _pod(ZOOM_PATCH)
    (mount,) = _container(pod, "joinly")["volumeMounts"]
    assert mount["readOnly"] is True and mount["subPath"] == "zoom.py"
    (volume,) = pod["volumes"]
    assert mount["name"] == volume["name"] and volume["configMap"]["name"] == "joinly-zoom-fix"
    deploy_md = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert f"{mount['mountPath']}:ro" in deploy_md, "DEPLOY.md's docker run mounts the same file"
    assert "joinly-zoom-fix" not in PATCH.read_text().replace("joinly-zoom-fix.yaml", "")


def test_the_patch_pins_the_image_the_docs_run_and_the_base_has_no_joinly() -> None:
    """One joinly release across the compose line, the design and the manifest, and a base
    manifest that stays as it is for anyone who doesn't follow calls. Fails if the tags drift
    apart, or if joinly or its address creeps into the base."""
    image = _container(_pod(PATCH), "joinly")["image"]
    assert re.fullmatch(r"ghcr\.io/joinly-ai/joinly:v\d+\.\d+\.\d+", image), "a release, not :latest"
    assert image in (ROOT / "docs" / "DEPLOY.md").read_text()
    assert image in (ROOT / "docs" / "DESIGN.md").read_text()
    lines = (DEPLOY / "switchboard.yaml").read_text().splitlines()
    code = [line for line in lines if not line.lstrip().startswith("#")]  # a comment may point to the patch
    assert "joinly" not in "\n".join(code).lower()
