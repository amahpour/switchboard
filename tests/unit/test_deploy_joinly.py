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
VOICE_PATCH = DEPLOY / "meetings" / "joinly-voice-patch.yaml"
PREROLL_PATCH = DEPLOY / "meetings" / "joinly-preroll-fix.yaml"


def _pod(path: Path) -> dict[str, Any]:
    """The pod spec of the one StatefulSet in a patch file."""
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    assert len(docs) == 1, "a patch is one document"
    assert docs[0]["kind"] == "StatefulSet" and docs[0]["metadata"]["name"] == "switchboard"
    return docs[0]["spec"]["template"]["spec"]


def _container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in pod["containers"] if c["name"] == name]
    return found


def _assert_names_the_cuda_image(text: str, image: str) -> None:
    """The CUDA build of the same release, with its own digest. Appending ``-cuda`` to a
    digest-pinned CPU reference is not that image."""
    name, digest = image.split("@", 1)
    found = re.search(re.escape(f"{name}-cuda") + r"@sha256:[0-9a-f]{64}", text)
    assert found, f"the cuda image of {name} is not named"
    assert found.group(0).rsplit("@", 1)[1] != digest, "the cuda pin is the cpu digest"


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


def test_the_patch_gives_joinly_the_memory_for_the_calls_the_broker_will_follow() -> None:
    """One live call takes about 2.7 GiB in joinly (measured, #604), so the sidecar's memory limit
    has to be at least 2.7 GiB x max_calls + 0.5 GiB. The patch leaves the broker's ``max_calls`` at
    the default and sets no variable, so the limit must fit that default; if someone sets
    SWITCHBOARD_MEETING_MAX_CALLS in the patch, the limit must grow with it. Fails if the limit is
    cut below what the default needs, or the variable is raised without the memory (#607)."""
    pod = _pod(PATCH)
    broker_env = {e["name"]: e["value"] for e in _container(pod, "switchboard")["env"]}
    calls = int(broker_env.get("SWITCHBOARD_MEETING_MAX_CALLS", MeetingCfg().max_calls))
    limit = _container(pod, "joinly")["resources"]["limits"]["memory"]
    assert limit.endswith("Gi"), "the sizing below reads the limit in Gi"
    assert float(limit.removesuffix("Gi")) >= 2.7 * calls + 0.5, f"{limit} cannot hold {calls} live call(s)"
    assert MeetingCfg().max_calls == 1, "the default must stay what the 4Gi limit holds"


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


def test_the_preroll_fix_is_a_patch_of_its_own_over_the_files_docs_name() -> None:
    """The first-word fix (#620) is one file over joinly's transcription controller, mounted from a
    ConfigMap with subPath, so it is a patch of its own for the same reason as the Zoom fix (a pod can't
    start without a ConfigMap its subPath names). Fails if the mount path drifts from DEPLOY.md's
    `docker run`, if the mount becomes writable, if its volume reaches the sidecar patch or the Zoom
    patch, or if the controller is mounted as the whole folder (which holds other files)."""
    pod = _pod(PREROLL_PATCH)
    (mount,) = _container(pod, "joinly")["volumeMounts"]
    assert mount["readOnly"] is True and mount["subPath"] == "transcription.py"
    assert mount["mountPath"].endswith("/joinly/controllers/transcription/default.py")
    (volume,) = pod["volumes"]
    assert mount["name"] == volume["name"] and volume["configMap"]["name"] == "joinly-preroll-fix"
    deploy_md = (ROOT / "docs" / "DEPLOY.md").read_text()
    assert f"{mount['mountPath']}:ro" in deploy_md, "DEPLOY.md's docker run mounts the same file"
    assert "joinly-preroll-fix" not in PATCH.read_text().replace("joinly-preroll-fix.yaml", "")
    assert "joinly-preroll-fix" not in ZOOM_PATCH.read_text()


def test_the_voice_patch_is_a_patch_of_its_own_over_the_two_files_docs_name() -> None:
    """The voice patch (#713) is two files of joinly in one ConfigMap, each mounted with subPath over the
    image's copy, so it is a patch of its own for the same reason as the other fixes. Fails if a mount path
    drifts from DEPLOY.md's `docker run` or the plugin's compose file, if a mount is writable, if the volume
    reaches the sidecar patch or another fix, or if the keys are not the repository's file names."""
    pod = _pod(VOICE_PATCH)
    mounts = _container(pod, "joinly")["volumeMounts"]
    assert {m["subPath"] for m in mounts} == {"server.py", "kokoro.py"}
    assert all(m["readOnly"] is True for m in mounts)
    (volume,) = pod["volumes"]
    assert {m["name"] for m in mounts} == {volume["name"]}
    assert volume["configMap"]["name"] == "joinly-voice-patch"
    deploy_md = (ROOT / "docs" / "DEPLOY.md").read_text()
    compose = (ROOT / "plugins" / "claude" / "meeting" / "compose.yaml").read_text()
    for m in mounts:
        assert f"{m['mountPath']}:ro" in deploy_md, "DEPLOY.md's docker run mounts the same file"
        assert f"{m['mountPath']}:ro" in compose, "and so does the hosted call's compose file"
        assert (ROOT / "deploy" / "joinly" / m["subPath"]).is_file()
    for other in (PATCH, ZOOM_PATCH, PREROLL_PATCH):
        assert "joinly-voice-patch" not in other.read_text()


def test_the_preroll_fix_is_pinned_to_one_commit_everywhere_it_is_named() -> None:
    """The fixed controller (#620) is named in DEPLOY.md's download commands, in the Kubernetes patch's
    comment and in the self-test, which also pins its sha256. They must name one commit of the fork:
    otherwise the self-test would try one file while operators run another. Fails if one of them is moved
    and the others are not, or if one of them stops naming a commit at all (a branch moves)."""
    url = re.compile(
        r"raw\.githubusercontent\.com/amahpour/joinly/([0-9a-f]{40})/joinly/controllers/transcription/default\.py"
    )
    docs = url.findall((ROOT / "docs" / "DEPLOY.md").read_text())
    patch = url.findall(PREROLL_PATCH.read_text())
    selftest = (ROOT / "tests" / "live" / "test_meeting_selftest.py").read_text()
    pinned = re.findall(r'PREROLL_FIX_COMMIT = "([0-9a-f]{40})"', selftest)
    assert len(docs) >= 2 and len(patch) == 1 and len(pinned) == 1, "each place names it by a full commit"
    assert set(docs) == set(patch) == set(pinned), "one commit, in the docs, the patch and the self-test"


def test_the_patch_pins_the_image_the_docs_run_and_the_base_has_no_joinly() -> None:
    """One joinly release across the compose line, the design and the manifest, and a base
    manifest that stays as it is for anyone who doesn't follow calls. Fails if the tags drift
    apart, or if joinly or its address creeps into the base."""
    image = _container(_pod(PATCH), "joinly")["image"]
    assert re.fullmatch(r"ghcr\.io/joinly-ai/joinly:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), (
        "a release pinned by digest, not :latest"
    )
    assert image in (ROOT / "docs" / "DEPLOY.md").read_text()
    assert image in (ROOT / "docs" / "DESIGN.md").read_text()
    lines = (DEPLOY / "switchboard.yaml").read_text().splitlines()
    code = [line for line in lines if not line.lstrip().startswith("#")]  # a comment may point to the patch
    assert "joinly" not in "\n".join(code).lower()


def test_the_patch_tells_how_to_use_a_gpu_in_comments_and_asks_for_none_itself() -> None:
    """The patch is the CPU bot, and a GPU is a few lines an operator adds (#617, the Helm chart's
    ``meetings.gpu`` and Compose's ``meetings-gpu`` are the same). They sit in the file as comments, so the
    patch applies on a cluster with no GPU node: nothing in it may ask for one. And the comment names the
    ``-cuda`` build of the release the patch pins, so a bump of the pin makes this fail until the comment
    follows. Fails if a GPU request, a selector or a toleration becomes live, or the comment drifts."""
    text = PATCH.read_text()
    image = _container(_pod(PATCH), "joinly")["image"]
    _assert_names_the_cuda_image(text, image)
    for needle in (
        "nvidia.com/gpu",
        "nodeSelector",
        "tolerations",
        "SWITCHBOARD_MEETING_STT_MODEL",
    ):
        assert needle in text, f"the GPU comment lost {needle}"
    for line in text.splitlines():
        if "-cuda" in line or "nvidia.com/gpu" in line:
            assert line.lstrip().startswith("#"), f"live GPU line: {line}"
    pod = _pod(PATCH)
    assert "nodeSelector" not in pod and "tolerations" not in pod
    live = yaml.safe_dump(_container(pod, "joinly"))
    assert "nvidia" not in live and "-cuda" not in live
    assert "SWITCHBOARD_MEETING_STT_MODEL" not in {e["name"] for e in _container(pod, "switchboard")["env"]}


def test_the_deploy_guide_and_the_meetings_guide_name_the_model_the_gpu_and_the_cache() -> None:
    """A setting nobody can find is not one: DEPLOY.md has to give the variable, the four models, the GPU
    profile and value, the ``-cuda`` image and the cache folder the download goes to, and the meetings
    guide's setup has to name the setting and send the reader there (#617). Fails if the guide stops
    naming one of them, or the guide's folder drifts from the chart's."""
    from switchboard.config import STT_MODEL_ENV, STT_MODELS

    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    image = _container(_pod(PATCH), "joinly")["image"]
    needles = (
        STT_MODEL_ENV,
        'stt_model = "large-v3-turbo"',
        "meetings-gpu",
        "meetings.gpu.enabled",
        "meetings.sttModel",
        "nvidia.com/gpu",
        "/home/app/.cache/huggingface",
        *STT_MODELS,
    )
    for needle in needles:
        assert needle in deploy, f"DEPLOY.md does not mention {needle}"
    _assert_names_the_cuda_image(deploy, image)
    # The short guide and its reference page (the setup in full) are one guide to this check.
    guide = (ROOT / "docs" / "guide" / "meetings.md").read_text() + (
        ROOT / "docs" / "guide" / "meetings-reference.md"
    ).read_text()
    assert "stt_model" in guide and STT_MODEL_ENV in guide and "large-v3-turbo" in guide
