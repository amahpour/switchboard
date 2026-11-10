"""The Helm chart (#608, docs/DEPLOY.md, "Helm") is the manifests in deploy/kubernetes/ with values, and
each test here names the way a quiet edit would let the two drift apart.

The chart's job is to render what ``switchboard.yaml`` and ``meetings/`` say, so the central tests render
it with ``helm template`` and compare the result with those files: the same Service, StatefulSet and
Ingress, the same patch for joinly, apart from a short list of named differences (labels, the explicit pull
policy, and the memory worked out from ``meetings.maxCalls``). A new line in a manifest that the chart
doesn't have, or the other way round, fails there. The guards in ``test_deploy_joinly.py`` and
``test_deploy_ingress.py`` hold for the chart through that comparison.

The tests that render need a helm: ``SWITCHBOARD_HELM``, or the pinned one scripts/lint.py fetches into
``.lint-tools/``. Without one they are skipped, so a machine without helm still runs the rest; lint.py
runs this file with ``SWITCHBOARD_REQUIRE_HELM=1``, where a missing helm fails instead (CI's lint job).
The tests on the files alone always run.
"""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import child_env

from switchboard import __version__

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "switchboard"
KUBE = ROOT / "deploy" / "kubernetes"
PUBLIC_URL = "https://sb.example.com"  # the manifest's own placeholder host
SET_BASE = (f"publicUrl={PUBLIC_URL}", "humanName=alice")  # what switchboard.yaml sets


def _find_helm() -> str | None:
    """SWITCHBOARD_HELM, else the pinned helm scripts/lint.py leaves in .lint-tools/ (not the PATH's: the
    one lint pins is the one these tests are written against)."""
    if env := os.environ.get("SWITCHBOARD_HELM"):
        return env
    tools = ROOT / ".lint-tools"
    found = sorted(p for p in tools.glob("helm-v*") if p.suffix != ".gz") if tools.is_dir() else []
    return str(found[-1]) if found else None


HELM = _find_helm()
if HELM is None and os.environ.get("SWITCHBOARD_REQUIRE_HELM") == "1":
    pytest.fail(
        "no helm, and SWITCHBOARD_REQUIRE_HELM=1 says the chart's render tests must run: "
        "run scripts/lint.py (it fetches the pinned helm) or set SWITCHBOARD_HELM",
        pytrace=False,
    )
needs_helm = pytest.mark.skipif(
    HELM is None,
    reason="no helm: SWITCHBOARD_HELM, or scripts/lint.py's (SWITCHBOARD_REQUIRE_HELM=1 makes this an error)",
)


# ------------------------------------------------------------------ the files alone
def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _manifest() -> dict[str, dict[str, Any]]:
    """deploy/kubernetes/switchboard.yaml by kind."""
    return {d["kind"]: d for d in yaml.safe_load_all((KUBE / "switchboard.yaml").read_text()) if d}


def _patch(name: str) -> dict[str, Any]:
    return _yaml(KUBE / "meetings" / name)


def test_the_chart_installs_the_release_it_is_part_of() -> None:
    """``image.tag`` is empty, so the pod runs the chart's appVersion, and a release moves that to its own
    version (release.py, #608). Fails if the tag is pinned in the values (it would sit on an old release),
    if appVersion lags the package, or if the default image becomes ``latest``."""
    chart = _yaml(CHART / "Chart.yaml")
    values = _yaml(CHART / "values.yaml")
    assert chart["apiVersion"] == "v2" and chart["name"] == "switchboard" and chart["type"] == "application"
    assert chart["appVersion"] == __version__, "release.py moves appVersion with the version"
    assert values["image"]["tag"] == "", "a pinned tag would not follow the chart"
    assert values["image"]["repository"] == "ghcr.io/amahpour/switchboard"
    assert "latest" not in json.dumps(values), "a release, never :latest"


def test_broker_memory_limit_leaves_room_for_connected_agents() -> None:
    """Both deployment paths give the broker 1Gi; scheduling requests stay unchanged (#690).

    Comparing the chart and manifest alone misses accidentally reverting both to 512Mi.
    """
    resources = {"requests": {"cpu": "100m", "memory": "192Mi"}, "limits": {"memory": "1Gi"}}
    assert _yaml(CHART / "values.yaml")["resources"] == resources
    containers = _manifest()["StatefulSet"]["spec"]["template"]["spec"]["containers"]
    assert next(c for c in containers if c["name"] == "switchboard")["resources"] == resources


def test_the_values_say_what_the_manifests_say() -> None:
    """The chart's defaults are the manifests' numbers and strings: the Ingress's timeout and body-size
    annotations (what keeps an agent's 15-minute wait open, #384), the broker's resources, the 20 GiB claim,
    the class and the TLS secret, and joinly's release. Fails if one is changed in the manifest and not in
    the values, or the other way round."""
    values = _yaml(CHART / "values.yaml")
    manifest = _manifest()
    ingress = manifest["Ingress"]
    assert values["ingress"]["annotations"] == ingress["metadata"]["annotations"]
    assert values["ingress"]["className"] == ingress["spec"]["ingressClassName"]
    assert values["ingress"]["tls"]["secretName"] == ingress["spec"]["tls"][0]["secretName"]
    sts = manifest["StatefulSet"]["spec"]
    (broker,) = sts["template"]["spec"]["containers"]
    assert values["resources"] == broker["resources"]
    assert (
        values["persistence"]["size"]
        == sts["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]
    )
    (joinly,) = [
        c for c in _patch("joinly.yaml")["spec"]["template"]["spec"]["containers"] if c["name"] == "joinly"
    ]
    pinned = values["meetings"]["image"]
    assert f"{pinned['repository']}:{pinned['tag']}@{pinned['digest']}" == joinly["image"]
    assert values["meetings"]["enabled"] is False, "the broker alone is the default"
    assert "replicas" not in values, "one broker on one volume: there is nothing to scale (issue #35)"


def test_max_calls_is_bounded_as_the_broker_bounds_it() -> None:
    """The schema refuses a ``meetings.maxCalls`` the broker would refuse at start (``[meeting] max_calls``,
    1 to 50, #607), so a bad value fails ``helm template`` and not a crash-looping pod. The ceiling is read
    from the broker's config once it has it; until then this only checks the schema's own numbers."""
    from switchboard import config

    bound = _yaml(CHART / "values.schema.json")["properties"]["meetings"]["properties"]["maxCalls"]
    assert bound["type"] == "integer" and bound["minimum"] == 1
    ceiling = getattr(config, "MAX_CALLS_CEILING", None)
    if ceiling is not None:
        assert bound["maximum"] == ceiling
    env = getattr(config, "MAX_CALLS_ENV", None)
    template = (CHART / "templates" / "statefulset.yaml").read_text()
    assert f"name: {env or 'SWITCHBOARD_MEETING_MAX_CALLS'}\n" in template


def test_the_model_value_is_bounded_as_the_broker_bounds_it() -> None:
    """The schema refuses a ``meetings.sttModel`` the broker would refuse at start (``[meeting]
    stt_model``, #617), so a bad name fails ``helm template`` and not a crash-looping pod. Its names are the
    broker's own, and the chart hands the value to the broker as the variable that sets it. Fails if the
    schema's list drifts from ``config.STT_MODELS`` (the empty string, "joinly's own choice", aside), or the
    variable is renamed on one side."""
    from switchboard import config

    bound = _yaml(CHART / "values.schema.json")["properties"]["meetings"]["properties"]["sttModel"]
    assert bound["enum"] == ["", *config.STT_MODELS]
    template = (CHART / "templates" / "statefulset.yaml").read_text()
    assert f"name: {config.STT_MODEL_ENV}\n" in template
    assert _yaml(CHART / "values.yaml")["meetings"]["sttModel"] == "", "unset leaves the model to joinly"


def test_the_gpu_values_default_to_off_and_to_no_scheduling_rules() -> None:
    """The bot runs on a CPU, anywhere, unless ``meetings.gpu.enabled`` says otherwise: the defaults ask for
    no GPU and set no node selector and no tolerations, and the schema knows exactly those three keys (the
    count is fixed at one GPU). Fails if the GPU becomes the default or a key is added without the schema."""
    gpu = _yaml(CHART / "values.yaml")["meetings"]["gpu"]
    assert gpu == {"enabled": False, "nodeSelector": {}, "tolerations": []}
    schema = _yaml(CHART / "values.schema.json")["properties"]["meetings"]["properties"]["gpu"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"enabled", "nodeSelector", "tolerations"}


# ------------------------------------------------------------------ rendering
class Rendered:
    """A ``helm template`` run: the documents by kind, or why it failed."""

    def __init__(self, proc: subprocess.CompletedProcess[str]):
        self.returncode = proc.returncode
        self.stderr = proc.stderr
        self.docs: dict[str, dict[str, Any]] = (
            {d["kind"]: d for d in yaml.safe_load_all(proc.stdout) if d} if proc.returncode == 0 else {}
        )

    @property
    def pod(self) -> dict[str, Any]:
        return self.docs["StatefulSet"]["spec"]["template"]["spec"]

    def container(self, name: str) -> dict[str, Any]:
        (found,) = [c for c in self.pod["containers"] if c["name"] == name]
        return found


@pytest.fixture(scope="module")
def render(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Rendered]:
    """``render("a=b", "c=d", values={...})``: helm template of the chart, release ``switchboard`` (the
    name the manifests use), with those --set values and a values file. Each run is cached."""
    home = tmp_path_factory.mktemp("helm-home")
    cache: dict[str, Rendered] = {}

    def run(*sets: str, values: dict[str, Any] | None = None, string: bool = False) -> Rendered:
        assert HELM is not None
        key = json.dumps([sets, values, string])
        if key not in cache:
            cmd = [HELM, "template", "switchboard", str(CHART), "--namespace", "switchboard"]
            for item in sets:
                cmd += ["--set-string" if string else "--set", item]
            if values is not None:
                file = home / f"values-{len(cache)}.yaml"
                file.write_text(yaml.safe_dump(values))
                cmd += ["-f", str(file)]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=child_env(home), timeout=60)
            cache[key] = Rendered(proc)
        return cache[key]

    return run


def _merge(base: Any, patch: Any) -> Any:
    """Kubernetes' strategic merge, for the shape these patches have: mappings merge, lists of named items
    (containers, env, volumes, mounts) merge by name, and anything else is replaced."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for key, value in patch.items():
            out[key] = _merge(base[key], value) if key in base else copy.deepcopy(value)
        return out
    if isinstance(base, list) and isinstance(patch, list):
        # a mount is keyed by where it goes (Kubernetes' patch key for volumeMounts), the rest by name: the
        # voice patch (#713) mounts two files from one volume
        def key(i: Any) -> Any:
            return i.get("mountPath", i.get("name")) if isinstance(i, dict) else None

        keyed = [key(i) for i in (*base, *patch)]
        if all(k is not None for k in keyed):
            merged = {key(i): i for i in base}
            for item in patch:
                k = key(item)
                merged[k] = _merge(merged[k], item) if k in merged else item
            return list(merged.values())
    return copy.deepcopy(patch)


def _comparable(docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The documents with what Helm adds taken out and named lists in name order, to compare with the
    manifests. Not compared, each on purpose (docs/DEPLOY.md, "Helm" lists them): the labels (Helm's
    standard set and the component), the selectors that carry them, the pull policy the chart states
    outright (Kubernetes' own default for a release tag), and the claim template's apiVersion and kind
    (stated so Argo CD sees no difference from the live object)."""
    out = copy.deepcopy(docs)
    for doc in out.values():
        doc["metadata"].pop("labels", None)
    out["Service"]["spec"].pop("selector")
    sts = out["StatefulSet"]["spec"]
    sts.pop("selector")
    sts["template"]["metadata"].pop("labels")
    if not sts["template"]["metadata"]:
        del sts["template"]["metadata"]
    for claim in sts["volumeClaimTemplates"]:
        claim.pop("apiVersion", None)
        claim.pop("kind", None)
    for c in sts["template"]["spec"]["containers"]:
        c.pop("imagePullPolicy", None)

    def order(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: order(v) for k, v in x.items()}
        if isinstance(x, list):
            items = [order(i) for i in x]
            if items and all(isinstance(i, dict) and "name" in i for i in items):
                items.sort(key=lambda i: i["name"])
            return items
        return x

    return order(out)  # type: ignore[no-any-return]


def _mi(quantity: str) -> int:
    """A memory quantity of Mi or Gi, in Mi."""
    if quantity.endswith("Gi"):
        return int(quantity[:-2]) * 1024
    assert quantity.endswith("Mi"), quantity
    return int(quantity[:-2])


@needs_helm
def test_the_defaults_render_the_manifest(render: Callable[..., Rendered]) -> None:
    """With only a public URL (and the admin's name, which the manifest sets), the chart renders what
    deploy/kubernetes/switchboard.yaml is: the broker alone, no joinly, no service-link variables, the same
    security contexts, probes, resources, claim and Ingress. Fails on any line the manifest has and the
    chart doesn't, or the reverse, apart from the differences ``_comparable`` names."""
    chart = render(*SET_BASE)
    assert chart.returncode == 0, chart.stderr
    assert set(chart.docs) == {"Service", "StatefulSet", "Ingress"}
    assert _comparable(chart.docs) == _comparable(_manifest())
    assert chart.pod["enableServiceLinks"] is False, (
        "#595: a Service named switchboard breaks SWITCHBOARD_PORT"
    )
    assert "annotations" not in chart.docs["StatefulSet"]["spec"]["template"]["metadata"]


@needs_helm
@pytest.mark.parametrize(
    "fixes",
    [(), ("zoom",), ("preroll",), ("zoom", "preroll"), ("voice",), ("zoom", "preroll", "voice")],
    ids=[
        "bot",
        "bot-and-zoom-fix",
        "bot-and-preroll-fix",
        "bot-and-both-fixes",
        "bot-and-voice-patch",
        "bot-and-all-three",
    ],
)
def test_the_meeting_bot_renders_the_patch(render: Callable[..., Rendered], fixes: tuple[str, ...]) -> None:
    """``meetings.enabled`` is deploy/kubernetes/meetings/joinly.yaml applied to the manifest, and
    ``meetings.zoomFix.configMap``, ``meetings.prerollFix.configMap`` and
    ``meetings.voicePatch.configMap`` are joinly-zoom-fix.yaml, joinly-preroll-fix.yaml and
    joinly-voice-patch.yaml: joinly beside the broker on the pod's loopback with no port, the exec probes
    and no readiness probe, uid 1001, the same volumes, the default-container annotation. What differs is
    named: the broker is told how many calls (the variable), and joinly's memory comes from ``maxCalls``
    (the next test). Fails on any other line."""
    sets = [*SET_BASE, "meetings.enabled=true"]
    patches = [_patch("joinly.yaml")]
    for fix in fixes:  # the ConfigMap names the patches use
        if fix == "voice":  # #713: two files in one ConfigMap, `meetings.voicePatch`
            sets.append("meetings.voicePatch.configMap=joinly-voice-patch")
            patches.append(_patch("joinly-voice-patch.yaml"))
            continue
        sets.append(f"meetings.{fix}Fix.configMap=joinly-{fix}-fix")
        patches.append(_patch(f"joinly-{fix}-fix.yaml"))
    chart = render(*sets)
    assert chart.returncode == 0, chart.stderr
    expected = _manifest()
    for patch in patches:
        expected["StatefulSet"] = _merge(expected["StatefulSet"], patch)

    got = _comparable(chart.docs)
    # the two differences: joinly's resources (sized by maxCalls), and the broker's extra variable
    sts_pod = got["StatefulSet"]["spec"]["template"]["spec"]
    (joinly,) = [c for c in sts_pod["containers"] if c["name"] == "joinly"]
    joinly_resources = joinly.pop("resources")
    (broker,) = [c for c in sts_pod["containers"] if c["name"] == "switchboard"]
    env = {e["name"]: e["value"] for e in broker["env"]}
    broker["env"] = [e for e in broker["env"] if e["name"] != "SWITCHBOARD_MEETING_MAX_CALLS"]
    want = _comparable(expected)
    want_pod = want["StatefulSet"]["spec"]["template"]["spec"]
    (want_joinly,) = [c for c in want_pod["containers"] if c["name"] == "joinly"]
    want_joinly.pop("resources")
    assert got == want
    assert (
        env["SWITCHBOARD_MEETING_MAX_CALLS"] == "1"
        and env["SWITCHBOARD_JOINLY_URL"] == "http://127.0.0.1:8000/mcp/"
    )
    assert "cpu" not in joinly_resources["limits"], "throttling makes the transcript fall behind"
    # the privacy of the sidecar, stated outright and not only by equality with the patch
    assert chart.container("joinly").get("ports") is None
    assert all(p["port"] != 8000 for p in chart.docs["Service"]["spec"]["ports"])
    assert "readinessProbe" not in chart.container("joinly")
    assert {e["name"]: e["value"] for e in chart.container("joinly")["env"]}[
        "JOINLY_SERVER_HOST"
    ] == "127.0.0.1"
    assert chart.docs["StatefulSet"]["spec"]["template"]["metadata"]["annotations"] == {
        "kubectl.kubernetes.io/default-container": "switchboard"
    }


@needs_helm
def test_the_zoom_fix_is_mounted_only_when_its_config_map_is_named(render: Callable[..., Rendered]) -> None:
    """No ConfigMap named, no mount and no volume: the pod must not wait for a ConfigMap nobody made
    (DEPLOY.md explains why subPath makes it fatal). Named, the one file is mounted read-only over the
    image's copy, from that ConfigMap. Fails if the volume is always added, or if the mount stops being
    read-only or reaches the broker."""
    plain = render(*SET_BASE, "meetings.enabled=true")
    assert "joinly-zoom-fix" not in json.dumps(plain.docs)
    named = render(*SET_BASE, "meetings.enabled=true", "meetings.zoomFix.configMap=zoom-mine")
    (volume,) = [v for v in named.pod["volumes"] if v["name"] == "joinly-zoom-fix"]
    assert volume["configMap"] == {"name": "zoom-mine"}
    (mount,) = [m for m in named.container("joinly")["volumeMounts"] if m["name"] == "joinly-zoom-fix"]
    assert mount["readOnly"] is True and mount["subPath"] == "zoom.py"
    assert (
        mount["mountPath"]
        == _patch("joinly-zoom-fix.yaml")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0][
            "mountPath"
        ]
    )
    assert all(m["name"] != "joinly-zoom-fix" for m in named.container("switchboard")["volumeMounts"])
    # the bot off: a named ConfigMap with no bot mounts nothing
    off = render(*SET_BASE, "meetings.zoomFix.configMap=zoom-mine")
    assert "zoom-mine" not in json.dumps(off.docs)


@needs_helm
def test_the_preroll_fix_is_mounted_only_when_its_config_map_is_named(
    render: Callable[..., Rendered],
) -> None:
    """The first-word fix (#620) is one file over joinly's transcription controller, from the ConfigMap
    ``meetings.prerollFix.configMap`` names. Unnamed, there is no mount and no volume: a pod must not wait
    for a ConfigMap nobody made. Named, it is read-only, subPath of the one file, the same path the
    Kubernetes patch mounts and DEPLOY.md's ``docker run`` mounts, and it never reaches the broker or the
    Zoom fix's volume. Fails if the volume is always added, the mount becomes writable, or the paths drift."""
    plain = render(*SET_BASE, "meetings.enabled=true")
    assert "joinly-preroll-fix" not in json.dumps(plain.docs)
    named = render(*SET_BASE, "meetings.enabled=true", "meetings.prerollFix.configMap=preroll-mine")
    (volume,) = [v for v in named.pod["volumes"] if v["name"] == "joinly-preroll-fix"]
    assert volume["configMap"] == {"name": "preroll-mine"}
    (mount,) = [m for m in named.container("joinly")["volumeMounts"] if m["name"] == "joinly-preroll-fix"]
    assert mount["readOnly"] is True and mount["subPath"] == "transcription.py"
    assert (
        mount["mountPath"]
        == (
            _patch("joinly-preroll-fix.yaml")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0][
                "mountPath"
            ]
        )
    )
    assert all(m["name"] != "joinly-preroll-fix" for m in named.container("switchboard")["volumeMounts"])
    assert "joinly-zoom-fix" not in json.dumps(named.docs), "the two fixes are named separately"
    # the bot off: a named ConfigMap with no bot mounts nothing
    off = render(*SET_BASE, "meetings.prerollFix.configMap=preroll-mine")
    assert "preroll-mine" not in json.dumps(off.docs)


@needs_helm
@pytest.mark.parametrize("calls", [1, 2, 3, 7, 50])
def test_max_calls_sizes_joinly_and_tells_the_broker(render: Callable[..., Rendered], calls: int) -> None:
    """A live call holds about 2.7 GiB and 1 CPU (measured, #604), so joinly asks for 2.7 GiB a call plus
    0.5 GiB, rounded up to 0.5 GiB, with a limit 0.5 GiB above, and a CPU for each call; the broker gets the
    same number as ``SWITCHBOARD_MEETING_MAX_CALLS``, which refuses a call past it. Fails if the chart's
    whole-number arithmetic drifts from the formula, or the two numbers (broker and joinly) come apart.
    One call is 3.5 GiB and a 4 GiB limit, as #604 puts it; three are 9 GiB."""
    chart = render(*SET_BASE, "meetings.enabled=true", f"meetings.maxCalls={calls}")
    assert chart.returncode == 0, chart.stderr
    resources = chart.container("joinly")["resources"]
    request_mi = math.ceil((2.7 * 1024 * calls + 512) / 512) * 512
    assert _mi(resources["requests"]["memory"]) == request_mi
    assert _mi(resources["limits"]["memory"]) == request_mi + 512
    assert resources["requests"]["cpu"] == str(calls)
    env = {e["name"]: e["value"] for e in chart.container("switchboard")["env"]}
    assert env["SWITCHBOARD_MEETING_MAX_CALLS"] == str(calls)
    anchors = {1: (3584, 4096), 3: (9216, 9728)}
    if calls in anchors:
        assert (_mi(resources["requests"]["memory"]), _mi(resources["limits"]["memory"])) == anchors[calls]


CACHE = "/home/app/.cache/huggingface"  # joinly's image user's Hugging Face cache, as docs/DEPLOY.md names it


@needs_helm
def test_the_gpu_variant_asks_for_the_cuda_image_and_one_gpu(render: Callable[..., Rendered]) -> None:
    """``meetings.gpu.enabled`` (#617) is the same bot on a GPU: joinly's ``-cuda`` build of the same release,
    and one ``nvidia.com/gpu`` in its limits (an extended resource goes in the limits, and the scheduler
    takes the request from it). The memory and CPU are the ones ``maxCalls`` works out, as they are with no
    GPU, and the broker's container asks for none. Fails if the image stays the CPU one, the GPU is asked
    for by the wrong container or in the wrong place, or the GPU changes the sizes."""
    values = _yaml(CHART / "values.yaml")["meetings"]["image"]
    cpu = render(*SET_BASE, "meetings.enabled=true", "meetings.maxCalls=2")
    gpu = render(*SET_BASE, "meetings.enabled=true", "meetings.maxCalls=2", "meetings.gpu.enabled=true")
    assert gpu.returncode == 0, gpu.stderr
    assert cpu.container("joinly")["image"] == (f"{values['repository']}:{values['tag']}@{values['digest']}")
    assert gpu.container("joinly")["image"] == (
        f"{values['repository']}:{values['tag']}-cuda@{values['cudaDigest']}"
    )
    limits = gpu.container("joinly")["resources"]["limits"]
    assert limits["nvidia.com/gpu"] == 1
    assert "nvidia.com/gpu" not in gpu.container("joinly")["resources"]["requests"]
    assert {k: v for k, v in limits.items() if k != "nvidia.com/gpu"} == cpu.container("joinly")["resources"][
        "limits"
    ]
    assert (
        gpu.container("joinly")["resources"]["requests"] == cpu.container("joinly")["resources"]["requests"]
    )
    assert "nvidia.com/gpu" not in json.dumps(gpu.container("switchboard")), "the broker needs no GPU"
    assert "nvidia.com/gpu" not in json.dumps(cpu.docs), "a CPU install asks for no GPU"
    # the cuda image sets JOINLY_DEVICE itself (verified on the image's config), so the chart adds nothing
    assert {e["name"] for e in gpu.container("joinly")["env"]} == {
        e["name"] for e in cpu.container("joinly")["env"]
    }
    assert "nodeSelector" not in gpu.pod and "tolerations" not in gpu.pod, "no rule unless one is given"


@needs_helm
def test_gpu_node_selector_and_tolerations_reach_the_pod_only_with_the_gpu_on(
    render: Callable[..., Rendered],
) -> None:
    """A GPU node usually carries a taint and a label, so ``meetings.gpu.nodeSelector`` and ``tolerations``
    are written into the pod as given (the broker shares that pod, so it goes where the GPU is). With the GPU
    off they are not rendered: they are not the CPU install's rules. Fails if either is dropped, changed,
    or applied to a pod that has no GPU."""
    selector = {"nvidia.com/gpu.present": "true"}
    tolerations = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    gpu = {"enabled": True, "nodeSelector": selector, "tolerations": tolerations}
    on = render(values={"publicUrl": PUBLIC_URL, "meetings": {"enabled": True, "gpu": gpu}})
    assert on.returncode == 0, on.stderr
    assert on.pod["nodeSelector"] == selector and on.pod["tolerations"] == tolerations
    off = render(
        values={"publicUrl": PUBLIC_URL, "meetings": {"enabled": True, "gpu": {**gpu, "enabled": False}}}
    )
    assert off.returncode == 0, off.stderr
    assert "nodeSelector" not in off.pod and "tolerations" not in off.pod
    assert "nvidia.com/gpu" not in json.dumps(off.docs)
    no_bot = render(values={"publicUrl": PUBLIC_URL, "meetings": {"gpu": gpu}})
    assert no_bot.returncode == 0, no_bot.stderr
    assert "nodeSelector" not in no_bot.pod and "nvidia.com/gpu" not in json.dumps(no_bot.docs)


@needs_helm
def test_the_cuda_tag_is_not_added_twice_and_the_gpu_needs_the_bot(render: Callable[..., Rendered]) -> None:
    """``meetings.image.tag`` follows joinly's releases, and the GPU image is that tag with ``-cuda``.
    The digest does not follow the tag: ``digest`` is the CPU index and ``cudaDigest`` is the CUDA
    image's own index, so a new release is those three values together. A tag that already says
    ``-cuda`` is left alone, GPU on or off, and then the CUDA digest is the one appended. Without the
    bot the GPU value renders nothing. Fails if a tag becomes ``v0.5.4-cuda-cuda`` or the digest is dropped.
    """
    pinned = _yaml(CHART / "values.yaml")["meetings"]["image"]
    named = render(*SET_BASE, "meetings.enabled=true", "meetings.image.tag=v0.5.4-cuda")
    assert named.container("joinly")["image"] == (
        f"{pinned['repository']}:v0.5.4-cuda@{pinned['cudaDigest']}"
    )
    both = render(
        *SET_BASE, "meetings.enabled=true", "meetings.image.tag=v0.5.4-cuda", "meetings.gpu.enabled=true"
    )
    assert both.container("joinly")["image"] == (f"{pinned['repository']}:v0.5.4-cuda@{pinned['cudaDigest']}")
    later = render(
        *SET_BASE, "meetings.enabled=true", "meetings.image.tag=v0.6.0", "meetings.gpu.enabled=true"
    )
    assert later.container("joinly")["image"] == (
        f"{pinned['repository']}:v0.6.0-cuda@{pinned['cudaDigest']}"
    )
    off = render(*SET_BASE, "meetings.gpu.enabled=true")
    assert off.returncode == 0, off.stderr
    assert "joinly" not in json.dumps(off.docs) and "nvidia.com/gpu" not in json.dumps(off.docs)


@needs_helm
@pytest.mark.parametrize("model", ["base", "small", "distil-large-v3", "large-v3-turbo"])
def test_the_model_reaches_the_broker_and_gets_joinly_a_place_to_keep_it(
    render: Callable[..., Rendered], model: str
) -> None:
    """``meetings.sttModel`` (#617) is the broker's ``SWITCHBOARD_MEETING_STT_MODEL``, and joinly then
    downloads the model on the first call. joinly's root is read-only and its image keeps its models in the
    user's home, so the chart gives it a writable folder at the Hugging Face cache (an emptyDir, sized for the
    largest model with room, gone when the pod is). Fails if the broker is not told, if the download has
    nowhere to go, or if the folder is mounted into the broker or made read-only."""
    chart = render(*SET_BASE, "meetings.enabled=true", f"meetings.sttModel={model}")
    assert chart.returncode == 0, chart.stderr
    env = {e["name"]: e["value"] for e in chart.container("switchboard")["env"]}
    assert env["SWITCHBOARD_MEETING_STT_MODEL"] == model
    (mount,) = [m for m in chart.container("joinly")["volumeMounts"] if m["mountPath"] == CACHE]
    assert "readOnly" not in mount and "subPath" not in mount
    (volume,) = [v for v in chart.pod["volumes"] if v["name"] == mount["name"]]
    assert volume["emptyDir"]["sizeLimit"] == "4Gi" and "medium" not in volume["emptyDir"]
    assert all(m["mountPath"] != CACHE for m in chart.container("switchboard")["volumeMounts"])
    assert chart.container("joinly")["securityContext"]["readOnlyRootFilesystem"] is True, (
        "the root stays read-only"
    )


@needs_helm
def test_no_model_leaves_the_bot_as_it_was_and_the_model_needs_the_bot(
    render: Callable[..., Rendered],
) -> None:
    """Unset (the default, or an empty string), joinly keeps its own model from its image: no variable on
    the broker and no cache folder, so a default install is the one the patch describes. A model with the
    bot off renders nothing. Fails if an empty value is rendered as ``SWITCHBOARD_MEETING_STT_MODEL=`` or
    mounts a folder that would hide the image's own copy of its model."""
    for chart in (
        render(*SET_BASE, "meetings.enabled=true"),
        render(*SET_BASE, "meetings.enabled=true", "meetings.sttModel="),
        render(*SET_BASE, "meetings.enabled=true", "meetings.gpu.enabled=true"),
    ):
        assert chart.returncode == 0, chart.stderr  # a refused render has no documents to find nothing in
        assert "SWITCHBOARD_MEETING_STT_MODEL" not in json.dumps(chart.docs)
        assert CACHE not in json.dumps(chart.docs)
    off = render(*SET_BASE, "meetings.sttModel=large-v3-turbo")
    assert off.returncode == 0, off.stderr
    assert "SWITCHBOARD_MEETING_STT_MODEL" not in json.dumps(off.docs) and CACHE not in json.dumps(off.docs)


@needs_helm
def test_a_gpu_value_or_model_the_chart_does_not_know_is_refused(render: Callable[..., Rendered]) -> None:
    """A model outside the broker's list, a second GPU (the count is fixed at one), a flag that is not a
    boolean and tolerations that are not a list fail at ``helm template``, naming the key. Fails if the
    schema lets any of them through to a pod that would crash-loop or sit Pending."""
    on = (*SET_BASE, "meetings.enabled=true")
    for bad in (
        "meetings.sttModel=large-v3",
        "meetings.sttModel=turbo",
        "meetings.sttModel=Systran/faster-whisper-base",
        "meetings.gpu.count=2",
        "meetings.gpu.enabled=maybe",
        "meetings.gpu.tolerations=gpu",
        "meetings.gpu.nodeSelector=gpu",
    ):
        refused = render(*on, bad)
        key = bad.split("=")[0].rsplit(".", 1)[-1]
        assert refused.returncode != 0 and key in refused.stderr, bad


@needs_helm
def test_one_broker_found_by_its_component(render: Callable[..., Rendered]) -> None:
    """One replica, one ReadWriteOnce claim, in every shape of the values: the broker's lock refuses a
    second on the volume. The pods are selected by name, instance and a ``broker`` component, which a
    StatefulSet can't change once it exists, so a second kind of pod can be added later without taking over
    this one's selector (#608 asks for no name that assumes a single part forever). Fails if the Service
    selects something else than the pods carry, or the component drops out of the selector."""
    for sets in (SET_BASE, (*SET_BASE, "meetings.enabled=true", "meetings.maxCalls=4")):
        chart = render(*sets)
        sts = chart.docs["StatefulSet"]
        assert sts["spec"]["replicas"] == 1
        (claim,) = sts["spec"]["volumeClaimTemplates"]
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
        selector = sts["spec"]["selector"]["matchLabels"]
        assert selector["app.kubernetes.io/component"] == "broker"
        pod_labels = sts["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= pod_labels.items(), "the selector must match its own pods"
        assert chart.docs["Service"]["spec"]["selector"] == selector
        assert (
            chart.docs["Ingress"]["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"]
            == (chart.docs["Service"]["metadata"]["name"])
        )
        assert sts["spec"]["serviceName"] == chart.docs["Service"]["metadata"]["name"]
    # the release name makes the names, so two releases never collide, and "switchboard" gives switchboard-0
    other = subprocess.run(
        [HELM or "", "template", "sb", str(CHART), "--set", f"publicUrl={PUBLIC_URL}"],
        capture_output=True,
        text=True,
        check=True,
        env=child_env(),
    )
    assert "name: sb-switchboard\n" in other.stdout


@needs_helm
def test_the_chart_does_not_render_without_a_public_url(render: Callable[..., Rendered]) -> None:
    """The broker refuses to start without ``SWITCHBOARD_PUBLIC_URL`` and refuses one with a path, so the
    chart fails first, saying what to set. Fails if an empty or missing URL renders (a pod that crash-loops
    is the only message then), or if a path, a bare host or a trailing slash gets through."""
    missing = render()
    assert missing.returncode != 0 and "publicUrl is required" in missing.stderr
    assert "https://sb.example.com" in missing.stderr, "the message gives an example"
    for bad in (
        "sb.example.com",
        "https://sb.example.com/",
        "https://sb.example.com/app",
        "https://u@sb.example.com",
    ):
        refused = render(f"publicUrl={bad}")
        assert refused.returncode != 0 and "publicUrl must be an origin" in refused.stderr, bad
    for good in (
        "https://sb.example.com",
        "https://sb.example.com:8443",
        "http://switchboard.localhost:7419",
    ):
        assert render(f"publicUrl={good}").returncode == 0, good


@needs_helm
def test_a_value_the_chart_does_not_know_or_cannot_use_is_refused(render: Callable[..., Rendered]) -> None:
    """A misspelled key, a call count outside what the broker accepts and a count that is not a number
    fail at ``helm template`` (the values schema), and not by being ignored or by the broker stopping at
    start with its own message. Fails if the schema loses ``additionalProperties: false`` or the bounds."""
    for bad in ("meetings.enabeld=true", "ingres.enabled=false", "image.pullPolicy=Sometimes"):
        refused = render(f"publicUrl={PUBLIC_URL}", bad)
        assert refused.returncode != 0 and "schema" in refused.stderr, bad
    for calls in ("0", "-1", "51", "1.5"):
        refused = render(f"publicUrl={PUBLIC_URL}", "meetings.enabled=true", f"meetings.maxCalls={calls}")
        assert refused.returncode != 0 and "maxCalls" in refused.stderr, calls
    string = render(f"publicUrl={PUBLIC_URL}", "meetings.maxCalls=2", string=True)
    assert string.returncode != 0 and "maxCalls" in string.stderr, "a string is not a count"


@needs_helm
def test_the_ingress_follows_the_public_url_and_can_be_changed(render: Callable[..., Rendered]) -> None:
    """The Ingress host is the public URL's host (the broker answers to no other, 421), with the port left
    out; ``ingress.host`` overrides it; TLS and the whole Ingress can be turned off; and an annotation set
    to null is dropped, which is how another controller's setup removes the ingress-nginx ones. Fails if
    the host keeps a port, if disabling TLS leaves a ``tls`` block, or if the annotations can't be dropped."""
    ported = render("publicUrl=https://sb.example.com:8443")
    ing = ported.docs["Ingress"]["spec"]
    assert ing["rules"][0]["host"] == "sb.example.com" and ing["tls"][0]["hosts"] == ["sb.example.com"]
    assert (
        render(f"publicUrl={PUBLIC_URL}", "ingress.host=other.example.com").docs["Ingress"]["spec"]["rules"][
            0
        ]["host"]
        == "other.example.com"
    )
    no_tls = render(f"publicUrl={PUBLIC_URL}", "ingress.tls.enabled=false").docs["Ingress"]["spec"]
    assert "tls" not in no_tls
    assert "Ingress" not in render(f"publicUrl={PUBLIC_URL}", "ingress.enabled=false").docs
    annotations = {
        "nginx.ingress.kubernetes.io/proxy-read-timeout": None,
        "nginx.ingress.kubernetes.io/proxy-send-timeout": None,
        "nginx.ingress.kubernetes.io/proxy-body-size": None,
        "cert-manager.io/cluster-issuer": "letsencrypt",
    }
    other = render(
        values={"publicUrl": PUBLIC_URL, "ingress": {"className": "alb", "annotations": annotations}}
    )
    assert other.docs["Ingress"]["metadata"]["annotations"] == {
        "cert-manager.io/cluster-issuer": "letsencrypt"
    }
    assert other.docs["Ingress"]["spec"]["ingressClassName"] == "alb"
    unclassed = render(values={"publicUrl": PUBLIC_URL, "ingress": {"className": ""}})
    assert "ingressClassName" not in unclassed.docs["Ingress"]["spec"]
    secret = render(values={"publicUrl": PUBLIC_URL, "ingress": {"tls": {"secretName": ""}}})
    assert secret.returncode != 0 and "secretName" in secret.stderr


@needs_helm
def test_persistence_image_and_name_values_reach_the_pod(render: Callable[..., Rendered]) -> None:
    """The volume's size and class, the image and its tag, and the admin's name reach the StatefulSet; the
    class is left out when empty (the cluster's default) and the admin's name is not set when empty (so
    Settings can rename). Fails if a value is read from the wrong place or an empty one is rendered."""
    mirror, tag = "registry.example.com/sb", "1.2.3"
    chart = render(
        f"publicUrl={PUBLIC_URL}",
        "persistence.size=50Gi",
        "persistence.storageClass=gp3",
        "image.repository=" + mirror,
        "image.tag=" + tag,
        "image.pullPolicy=Always",
    )
    claim = chart.docs["StatefulSet"]["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["resources"]["requests"]["storage"] == "50Gi" and claim["storageClassName"] == "gp3"
    broker = chart.container("switchboard")
    # The broker's own image stays tag-only, including a mirror: the release pins the tag before the
    # image exists, so the chart does not append a digest.
    assert broker["image"] == f"{mirror}:{tag}" and "@sha256:" not in broker["image"]
    assert broker["imagePullPolicy"] == "Always"
    default = render(f"publicUrl={PUBLIC_URL}")
    assert "storageClassName" not in default.docs["StatefulSet"]["spec"]["volumeClaimTemplates"][0]["spec"]
    assert [e["name"] for e in default.container("switchboard")["env"]] == ["SWITCHBOARD_PUBLIC_URL"]
    assert default.container("switchboard")["image"] == f"ghcr.io/amahpour/switchboard:{__version__}"


@needs_helm
def test_the_notes_say_where_the_one_time_password_is() -> None:
    """After an install the notes name the pod (``<release>-0``), the command that prints the admin's
    one-time password (DEPLOY.md, "Signing in"), and, with the bot on, the container to read it from.
    Fails if the notes name a pod that isn't there or leave the password out."""
    assert HELM is not None
    out = subprocess.run(
        [
            HELM,
            "install",
            "switchboard",
            str(CHART),
            "--dry-run=client",
            "--namespace",
            "ns1",
            "--set",
            f"publicUrl={PUBLIC_URL}",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env(),
    ).stdout
    assert "kubectl -n ns1 logs switchboard-0 |" in out and "one-time password" in out
    assert f"Sign in at {PUBLIC_URL} as admin" in out
    bot = subprocess.run(
        [
            HELM,
            "install",
            "switchboard",
            str(CHART),
            "--dry-run=client",
            "--namespace",
            "ns1",
            "--set",
            f"publicUrl={PUBLIC_URL}",
            "--set",
            "meetings.enabled=true",
            "--set",
            "ingress.enabled=false",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env(),
    ).stdout
    assert "logs switchboard-0 -c switchboard" in bot and "ingress.enabled is false" in bot
