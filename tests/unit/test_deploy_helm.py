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
    assert f"{pinned['repository']}:{pinned['tag']}" == joinly["image"]
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
    named = [i for i in (*(base or []), *(patch or [])) if isinstance(i, dict) and "name" in i]
    if isinstance(base, list) and isinstance(patch, list) and len(named) == len(base) + len(patch):
        merged = {i["name"]: i for i in base}
        for item in patch:
            merged[item["name"]] = _merge(merged[item["name"]], item) if item["name"] in merged else item
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
@pytest.mark.parametrize("zoom", [False, True], ids=["bot", "bot-and-zoom-fix"])
def test_the_meeting_bot_renders_the_patch(render: Callable[..., Rendered], zoom: bool) -> None:
    """``meetings.enabled`` is deploy/kubernetes/meetings/joinly.yaml applied to the manifest, and
    ``meetings.zoomFix.configMap`` is joinly-zoom-fix.yaml: joinly beside the broker on the pod's
    loopback with no port, the exec probes and no readiness probe, uid 1001, the same volumes, the
    default-container annotation. What differs is named: the broker is told how many calls (the
    variable), and joinly's memory comes from ``maxCalls`` (the next test). Fails on any other line."""
    sets = [*SET_BASE, "meetings.enabled=true"]
    patches = [_patch("joinly.yaml")]
    if zoom:
        sets.append("meetings.zoomFix.configMap=joinly-zoom-fix")  # the name the zoom patch uses
        patches.append(_patch("joinly-zoom-fix.yaml"))
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
    chart = render(
        f"publicUrl={PUBLIC_URL}",
        "persistence.size=50Gi",
        "persistence.storageClass=gp3",
        "image.repository=registry.example.com/sb",
        "image.tag=1.2.3",
        "image.pullPolicy=Always",
    )
    claim = chart.docs["StatefulSet"]["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["resources"]["requests"]["storage"] == "50Gi" and claim["storageClassName"] == "gp3"
    broker = chart.container("switchboard")
    assert broker["image"] == "registry.example.com/sb:1.2.3" and broker["imagePullPolicy"] == "Always"
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
