"""The container image (issue #34, docs/DEPLOY.md). Marker ``image``, opt-in; needs Docker, and
Playwright's Chromium for the UI test.

    docker build -t switchboard:dev .
    SWITCHBOARD_IMAGE=switchboard:dev uv run pytest -m image tests/image   # unset: builds switchboard:test

The CI job ``image`` builds the image and runs these. They start it the way a platform does:
on a Docker network behind a proxy that terminates TLS (Caddy, with a certificate from its own
local CA), with ``SWITCHBOARD_PUBLIC_URL=https://sb.test:<port>``. They check that the container
runs as the unprivileged user, answers /healthz and Docker's health check, prints one claim link
in its log and is claimed from it in Chromium with a passkey (a virtual authenticator) and
signed in to again with it, with no ``docker exec`` anywhere (issue #41), takes a second
container of the same image as a machine that pairs with a code, dials ``wss://`` through the
proxy and has a stand-in agent talk in a room, until Remove stops its dialer, gives a sign-in link
through ``docker exec`` too, serves the UI to Chromium over https with its WebSocket over wss,
stops cleanly on ``docker stop`` and keeps its data (and its owner) across a restart. They also
mount the two kinds of volume platforms give it: a disk that belongs to root (Render) and a
Kubernetes volume with an ``fsGroup``. The UI's screenshots go to
``$SWITCHBOARD_E2E_ARTIFACTS/image/`` (CI uploads them).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.image

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("SWITCHBOARD_IMAGE") or ""
ARTIFACTS = Path(os.environ.get("SWITCHBOARD_E2E_ARTIFACTS") or ROOT / "e2e-artifacts") / "image"
# the proxy in front (the tag and the digest, as the Dockerfile pins its bases)
CADDY = "caddy:2.10-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
CADDY_ROOT = "/data/caddy/pki/authorities/local/root.crt"
# The docker CLI's env, taken at import (before the suite's clean-env fixture moves HOME to a
# temp dir, where the CLI finds neither its config nor its context): what it needs to reach
# the daemon, and nothing of any harness (tests/twohost does the same).
DOCKER_ENV = {
    k: v
    for k, v in os.environ.items()
    if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")
    or k.startswith("DOCKER_")
    or k.startswith("BUILDX_")
}
WAIT_S = 30.0
LINK_RE = re.compile(r"https://sb\.test:\d+/login\?t=[A-Za-z0-9_-]+")
# the admin's one-time password and the same as a link (DESIGN.md §32.4): the link is group 1
CLAIM_RE = re.compile(
    r"switchboard isn't set up yet\. Sign in at https://sb\.test:\d+ as admin with the one-time"
    r" password [0-9A-Z-]+ \(it works once, for 60 min\), then choose your own password or passkey\."
    r" Or open (https://sb\.test:\d+/setup#t=[0-9A-Z-]+)"
)
# Chromium's virtual authenticator: a platform passkey with user verification (issue #41)
AUTHENTICATOR = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}


def docker(
    *args: str, check: bool = True, timeout: float = 120.0, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, env=DOCKER_ENV, input=stdin
    )
    if check and r.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} -> {r.returncode}:\n{r.stdout[-3000:]}{r.stderr[-3000:]}"
        )
    return r


def docker_missing() -> str | None:
    if shutil.which("docker") is None:
        return "no docker"
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
            env=DOCKER_ENV,
        )
        return None if r.returncode == 0 else "the docker daemon isn't running"
    except (OSError, subprocess.TimeoutExpired):
        return "docker doesn't answer"


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_for(what: str, fn: Any, timeout: float = WAIT_S) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            got = fn()
            if got:
                return got
        except Exception:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.25)


def bridge_links_ready(bridge: str, links: list[Any], addresses: list[Any]) -> bool:
    """All of this stack's host interfaces finished IPv6 duplicate-address detection."""
    names = {bridge, *(link["ifname"] for link in links)}
    if len(names) != 3:  # the bridge, broker and Caddy
        return False
    ready = {
        interface["ifname"]
        for interface in addresses
        if any(
            addr.get("family") == "inet6" and addr.get("scope") == "link" and not addr.get("tentative", False)
            for addr in interface.get("addr_info", [])
        )
    }
    return names <= ready


def wait_for_docker_links(network: str) -> None:
    """A new Linux Docker bridge changes Chromium's network while IPv6 addresses arrive.
    Wait for the actual link event before opening a browser through the published proxy."""
    if sys.platform != "linux" or shutil.which("ip") is None:
        return  # Docker Desktop's bridge is in its VM, not on the browser's host
    network_id = docker("network", "inspect", "-f", "{{.Id}}", network).stdout.strip()
    bridge = f"br-{network_id[:12]}"
    if subprocess.run(["ip", "link", "show", "dev", bridge], capture_output=True).returncode:
        return  # rootless Docker keeps its bridge in another network namespace
    disabled = Path(f"/proc/sys/net/ipv6/conf/{bridge}/disable_ipv6")
    if disabled.exists() and disabled.read_text().strip() == "1":
        return

    def ready() -> bool:
        links = subprocess.run(
            ["ip", "-j", "link", "show", "master", bridge],
            capture_output=True,
            text=True,
            check=True,
        )
        addresses = subprocess.run(
            ["ip", "-j", "-6", "addr", "show"], capture_output=True, text=True, check=True
        )
        return bridge_links_ready(bridge, json.loads(links.stdout), json.loads(addresses.stdout))

    wait_for("Docker bridge and container links to finish IPv6 setup", ready, timeout=10.0)


class Pinned(http.client.HTTPSConnection):
    """https to ``sb.test`` for real (SNI, Host, the certificate checked against Caddy's CA),
    with the name resolved to 127.0.0.1 here instead of by DNS."""

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ the stack
class Stack:
    """The image behind Caddy on a network of their own, with a named volume for /data."""

    def __init__(self, image: str, work: Path) -> None:
        self.image = image
        self.work = work
        tag = uuid.uuid4().hex[:8]
        self.net = f"sbimg-{tag}"
        self.volume = f"sbimg-{tag}"
        self.broker = f"sbimg-broker-{tag}"
        self.caddy = f"sbimg-caddy-{tag}"
        self.port = free_port()  # the proxy's, on this machine and in the public URL
        self.public = f"https://sb.test:{self.port}"
        self.ca = work / "caddy-root.crt"
        self.direct = 0  # the broker's own port, published on 127.0.0.1 (a platform's health check)

    def up(self) -> "Stack":
        docker("network", "create", self.net)
        docker("volume", "create", self.volume)
        docker(
            "run",
            "-d",
            "--name",
            self.broker,
            "--network",
            self.net,
            "--network-alias",
            "switchboard",
            "-v",
            f"{self.volume}:/data",
            "-p",
            "127.0.0.1::7419",
            "-e",
            f"SWITCHBOARD_PUBLIC_URL={self.public}",
            self.image,
        )
        self.direct = int(docker("port", self.broker, "7419/tcp").stdout.split(":")[-1])
        caddyfile = self.work / "Caddyfile"
        caddyfile.write_text(
            "{\n\tadmin off\n\tauto_https disable_redirects\n\tskip_install_trust\n}\n\n"
            f"https://sb.test:{self.port} {{\n\ttls internal\n\treverse_proxy switchboard:7419\n}}\n"
        )
        # `sb.test` on this network is the proxy: a machine container dials the public URL through it
        docker(
            "create",
            "--name",
            self.caddy,
            "--network",
            self.net,
            "--network-alias",
            "sb.test",
            "-p",
            f"127.0.0.1:{self.port}:{self.port}",
            CADDY,
        )
        docker("cp", str(caddyfile), f"{self.caddy}:/etc/caddy/Caddyfile")
        docker("start", self.caddy)
        wait_for("Caddy's local CA", lambda: docker("cp", f"{self.caddy}:{CADDY_ROOT}", str(self.ca)))
        wait_for("the broker's /healthz", lambda: self.healthz() == "ok\n")
        wait_for("the proxy", lambda: self.https("GET", "/healthz")[0] == 200)
        wait_for_docker_links(self.net)
        return self

    def down(self) -> None:
        for c in (self.caddy, self.broker):
            docker("rm", "-f", "-v", c, check=False)
        docker("volume", "rm", "-f", self.volume, check=False)
        docker("network", "rm", self.net, check=False)

    def healthz(self) -> str:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.direct}/healthz", timeout=5) as r:
            return r.read().decode()

    def https(
        self, method: str, path: str, headers: dict[str, str] | None = None, body: str | None = None
    ) -> tuple[int, Any, str]:
        c = Pinned("sb.test", self.port, context=ssl.create_default_context(cafile=str(self.ca)), timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.headers, r.read().decode(errors="replace")
        finally:
            c.close()

    def cli(self, *args: str, tty: bool = False) -> str:
        """`switchboard …` in the container, as `docker exec` runs it (as root: the image's
        switchboard drops to the unprivileged user)."""
        return docker("exec", *(["-t"] if tty else []), self.broker, "switchboard", *args).stdout

    def login_link(self) -> str:
        m = LINK_RE.search(self.cli("login", tty=True))  # a sign-in link needs a terminal
        assert m, "no sign-in link"
        return m.group(0)

    def claim_lines(self) -> list[str]:
        """Every claim link the container has printed, from its log (no exec)."""
        return [m.group(1) for m in CLAIM_RE.finditer(docker("logs", self.broker).stdout)]

    def session(self) -> str:
        """A signed-in session's Cookie header, from a fresh link followed through the proxy."""
        status, headers, _ = self.https("GET", self.login_link().removeprefix(self.public))
        assert status == 303
        return headers["set-cookie"].split(";")[0]

    def create_room(self, cookie: str, name: str) -> None:
        """Through the proxy, as the page does: rooms are created from a web session only
        (`switchboard create` says so)."""
        status, _, body = self.https(
            "POST",
            "/api/rooms",
            {
                "Cookie": cookie,
                "Origin": self.public,
                "X-Switchboard": "1",
                "Content-Type": "application/json",
            },
            json.dumps({"name": name}),
        )
        assert status == 200, body


class Owner:
    """The owner's browser, through the proxy over https: the public Origin on writes, and the
    (Secure) cookies kept by hand."""

    def __init__(self, stack: Stack):
        self.s = stack
        self.cookies: dict[str, str] = {}

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else {}
        if method != "GET":
            h.update({"Origin": self.s.public, "X-Switchboard": "1", "Content-Type": "application/json"})
        status, headers, text = self.s.https(
            method, path, h, json.dumps(body or {}) if method != "GET" else None
        )
        for sc in headers.get_all("set-cookie") or []:
            name, _, value = sc.split(";")[0].partition("=")
            if "max-age=0" in sc.lower():
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text


# the stand-in agent on the machine: a scripted MCP client (the mcp package is in the image)
AGENT = """
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="/opt/switchboard/bin/switchboard",
                                   args=["mcp", "--home", sys.argv[1]])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for tool, args in (("join", {"room": "#lab", "screen_name": "bench"}),
                               ("say", {"room": "#lab", "text": "hello over wss, through the proxy"})):
                res = await s.call_tool(tool, args)
                print(json.dumps({"tool": tool, "error": res.is_error, "text": res.content[0].text[:200]}))

asyncio.run(main())
"""


@pytest.fixture(scope="module")
def image() -> str:
    why = docker_missing()
    if why:
        pytest.skip(why)
    if IMAGE:
        docker("image", "inspect", IMAGE)
        return IMAGE
    revision = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    docker(
        "build",
        "--build-arg",
        f"BUILD_COMMIT={revision}",
        "-t",
        "switchboard:test",
        str(ROOT),
        timeout=1200.0,
    )
    return "switchboard:test"


@pytest.fixture(scope="module")
def stack(image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    s = Stack(image, tmp_path_factory.mktemp("image"))
    try:
        yield s.up()
    except BaseException:
        print(docker("logs", s.broker, check=False).stdout[-3000:])
        raise
    finally:
        s.down()


# ----------------------------------------------------------------------- tests
def test_browser_waits_for_the_docker_bridge_and_both_container_links() -> None:
    """A proxy health check can pass before Linux finishes IPv6 address assignment;
    Chromium sees the later assignment as a network change during a request."""
    bridge = "br-example"
    links = [{"ifname": "veth-broker"}, {"ifname": "veth-caddy"}]
    addresses = [
        {"ifname": name, "addr_info": [{"family": "inet6", "scope": "link", "flags": []}]}
        for name in (bridge, "veth-broker")
    ]
    assert not bridge_links_ready(bridge, links, addresses)
    addresses.append(
        {"ifname": "veth-caddy", "addr_info": [{"family": "inet6", "scope": "link", "flags": []}]}
    )
    assert bridge_links_ready(bridge, links, addresses)
    assert not bridge_links_ready(bridge, links[:1], addresses)
    addresses[-1]["addr_info"][0]["tentative"] = True
    assert not bridge_links_ready(bridge, links, addresses)


def test_image_reports_its_revision_label(stack: Stack) -> None:
    """The running broker's full commit and the installed CLI agree with the OCI revision."""
    label = docker(
        "image",
        "inspect",
        stack.image,
        "--format",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
    ).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", label), label
    status = json.loads(stack.cli("status", "--json"))
    assert status["commit"] == label
    shown = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/opt/switchboard/bin/switchboard",
        stack.image,
        "--version",
    ).stdout.strip()
    assert shown.endswith(f"({label[:7]})"), shown


def test_claim_it_from_the_log_and_sign_in_with_a_passkey(stack: Stack, playwright: Any) -> None:
    """The first thing that happens to a fresh deployment (issues #41, #61): the log holds one
    one-time password and its link, opened in Chromium through the proxy; a passkey (a virtual
    authenticator) instead of a password sets the broker up; signing off and signing in again
    uses the passkey. No ``docker exec`` anywhere."""
    links = stack.claim_lines()
    assert len(links) == 1 and links[0].startswith(f"{stack.public}/setup#t=")
    logs = docker("logs", stack.broker).stdout
    assert "no admin yet: a one-time password is on stdout" in logs
    status, _, body = stack.https("GET", "/setup")
    assert status == 200 and "Choose how you&rsquo;ll sign in" in body
    status, _, body = stack.https("GET", "/api/auth/state")
    assert status == 200 and json.loads(body) == {
        "hosted": True,
        "claimed": False,
        "passkeys": False,
        "claim": True,
        "passkeys_work": True,
        "password": True,
        "sso": "coming soon",
    }

    problems: list[str] = []
    browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP sb.test 127.0.0.1"])
    try:
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 760},
            timezone_id="UTC",
            locale="en-US",
        )
        page = ctx.new_page()
        page.on(
            "console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None
        )
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable", {"enableUI": False})
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": AUTHENTICATOR})
        page.goto(links[0])
        page.wait_for_selector("#step-choose:visible", timeout=15_000)
        page.wait_for_url(f"{stack.public}/setup", timeout=15_000)
        assert page.url == f"{stack.public}/setup"  # the one-time password left the address bar
        page.fill("#setup-email", "alice@example.com")  # setup asks for the admin's email (#192)
        page.click("#passkey-btn")  # a passkey instead of a password
        page.wait_for_selector("#step-backup:visible, #setup-error:visible", timeout=15_000)
        error = page.locator("#setup-error")
        assert not error.is_visible(), f"the passkey setup failed: {error.text_content()}"
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / "claimed.png"))
        page.click("#skip-btn")
        page.wait_for_selector("#st-conn:text-is('Connected')", timeout=15_000)
        assert page.url == f"{stack.public}/"
        # signed in by the claim, with a Secure cookie; the claim link is spent
        got = json.loads(stack.https("GET", "/api/auth/state")[2])
        assert (got["claimed"], got["passkeys"], got["claim"]) == (True, True, False)
        assert stack.https("GET", "/setup")[0] == 303
        # sign off, then in again with the passkey
        page.click("#me-settings")
        page.click("#settings-sign-out")
        page.wait_for_selector("#passkey-btn:visible", timeout=15_000)
        page.screenshot(path=str(ARTIFACTS / "sign-in-passkey.png"))
        page.click("#passkey-btn")
        page.wait_for_selector("#st-conn:text-is('Connected'), #login-error:visible", timeout=15_000)
        error = page.locator("#login-error")
        assert not error.is_visible(), f"the passkey sign-in failed: {error.text_content()}"
        assert page.url == f"{stack.public}/"
        ctx.close()
    finally:
        browser.close()
    assert not problems, problems
    assert len(stack.claim_lines()) == 1  # nothing more was printed once claimed
    assert "switchboard claimed with a passkey" in docker("logs", stack.broker).stdout


def test_a_machine_dials_in_through_the_proxy(image: str, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Issue #41 part 2 as a platform runs it: a fresh broker, claimed with a passkey over https;
    a second container of the same image as a machine that trusts the proxy's CA through its
    system store (``update-ca-certificates``, which truststore reads); ``remote join`` with a
    code from the API, which starts the dialer; approve; the machine's agent posts in a room
    over ``wss://`` through the proxy; Remove, and the dialer stops for good."""
    from fakes.fake_authenticator import SoftAuthenticator

    s = Stack(image, tmp_path_factory.mktemp("dialin")).up()
    machine = f"sbimg-machine-{uuid.uuid4().hex[:8]}"
    try:
        owner = Owner(s)
        auth = SoftAuthenticator("sb.test", s.public)
        token = s.claim_lines()[0].split("#t=", 1)[1]
        status, got = owner.call("POST", "/api/setup/begin", {"token": token})
        assert status == 200, got
        status, got = owner.call(
            "POST", "/api/setup/finish", {"credential": auth.register(got["options"]), "name": "laptop"}
        )
        assert status == 200, got
        assert owner.call("POST", "/api/rooms", {"name": "#lab"})[0] == 200
        status, got = owner.call("POST", "/api/machines/pair", {"name": "work-laptop"})
        assert status == 200, got
        code = got["code"]
        assert got["join"] == f"switchboard remote join {s.public} {code}"

        docker("run", "-d", "--name", machine, "--network", s.net, "--entrypoint", "sleep", image, "infinity")
        docker("cp", str(s.ca), f"{machine}:/usr/local/share/ca-certificates/sb-test-ca.crt")
        docker("exec", machine, "update-ca-certificates")
        r = docker(
            "exec", machine, "switchboard", "remote", "join", s.public, code, "--home", "/data/machine"
        )
        assert "This machine's key: SHA256:" in r.stdout and "Paired as work-laptop" in r.stdout, r.stdout
        assert "switchboard dialer running" in r.stdout, r.stdout
        fp = r.stdout.split("This machine's key: ", 1)[1].split()[0]
        status, got = owner.call("GET", "/api/machines")
        [m] = got["machines"]
        assert m["key_fp"] == fp and m["state"] == "pending"
        wait_for(
            "the machine dialed in", lambda: owner.call("GET", "/api/machines")[1]["machines"][0]["dialed_in"]
        )
        assert owner.call("POST", "/api/machines/work-laptop/approve")[0] == 200
        wait_for("the link up", lambda: owner.call("GET", "/api/machines")[1]["machines"][0]["state"] == "up")

        out = docker(
            "exec",
            "-i",
            "-u",
            "switchboard",
            machine,
            "/opt/switchboard/bin/python",
            "-",
            "/data/machine",
            stdin=AGENT,
        ).stdout
        assert '"error": false' in out and '"error": true' not in out, out
        status, got = owner.call("GET", "/api/rooms/lab/messages")
        said = [(x["from"], x.get("host"), x["text"]) for x in got["messages"] if x["kind"] == "chat"]
        assert said == [("bench", "work-laptop", "hello over wss, through the proxy")]

        assert owner.call("POST", "/api/machines/work-laptop/remove")[0] == 200
        wait_for(
            "the dialer stopped",
            lambda: (
                "stopped (removed)"
                in docker("exec", machine, "switchboard", "status", "--home", "/data/machine").stdout
            ),
        )
        cmdlines = docker(
            "exec", machine, "sh", "-c", "for f in /proc/[0-9]*/cmdline; do tr '\\000' ' ' < $f; echo; done"
        ).stdout
        assert "start --foreground" not in cmdlines  # the dialer is gone
    finally:
        docker("rm", "-f", machine, check=False)
        s.down()


def test_it_refuses_to_start_without_a_public_url(image: str) -> None:
    r = docker("run", "--rm", image, check=False)
    assert r.returncode == 1
    assert "listening on 0.0.0.0 needs --public-url" in r.stderr


def test_it_runs_as_the_unprivileged_user_and_answers_health_checks(stack: Stack) -> None:
    # every process of the container (tini, the broker) runs as the switchboard user
    pids = docker(
        "exec",
        stack.broker,
        "sh",
        "-c",
        "for p in /proc/[0-9]*; do [ \"${p#/proc/}\" = $$ ] || grep -H '^Uid:' $p/status; done",
    ).stdout
    uids = re.findall(r"^/proc/(\d+)/status:Uid:\s+(\d+)", pids, re.M)
    assert (
        ("1", "10001") in uids and all(uid == "10001" for pid, uid in uids if pid != "1") and len(uids) >= 2
    )
    # its home: the user's own and private, on the volume
    assert docker("exec", stack.broker, "stat", "-c", "%u %a", "/data/switchboard").stdout.split() == [
        "10001",
        "700",
    ]
    # /healthz on the container's own port, with no Host of the public URL; and Docker's check
    assert stack.healthz() == "ok\n"
    wait_for(
        "Docker's health check",
        lambda: docker("inspect", "-f", "{{.State.Health.Status}}", stack.broker).stdout.strip() == "healthy",
        timeout=60.0,
    )
    # anything else at the container's own address is refused (only the public URL's Host)
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{stack.direct}/")
        raise AssertionError("the UI answered without the public URL's Host")
    except urllib.error.HTTPError as e:
        assert e.code == 421 and e.read().decode() == f"open {stack.public}/\n"


def test_sign_in_and_the_ui_over_https(stack: Stack, playwright: Any) -> None:
    """Through the proxy: the page, its CSP (wss to the public URL only), a one-time link that
    sets a Secure cookie, then the real UI in Chromium, connected over wss, with a message."""
    status, headers, body = stack.https("GET", "/")
    assert status == 200 and "switchboard login" in body
    assert f"connect-src 'self' wss://sb.test:{stack.port};" in headers["content-security-policy"]
    link = stack.login_link()
    assert link.startswith(f"{stack.public}/login?t=")
    status, headers, _ = stack.https("GET", link.removeprefix(stack.public))
    assert status == 303
    cookie = [p.strip().lower() for p in headers["set-cookie"].split(";")]
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie

    problems: list[str] = []
    browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP sb.test 127.0.0.1"])
    try:
        # Caddy's local CA isn't in Chromium's trust store; the check above verified the chain
        ctx = browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 760},
            timezone_id="UTC",
            locale="en-US",
        )
        page = ctx.new_page()
        page.on(
            "console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None
        )
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        page.goto(stack.login_link())
        page.wait_for_selector("#st-conn:text-is('Connected')", timeout=15_000)
        assert page.url == f"{stack.public}/"
        created = page.evaluate("""async () => (await fetch('/api/rooms', {method: 'POST',
            headers: {'X-Switchboard': '1', 'Content-Type': 'application/json'},
            body: JSON.stringify({name: '#smoke'})})).status""")
        assert created == 200  # the browser's own Origin, the public one, passed the check
        stack.cli(
            "say",
            "#smoke",
            "hello from **inside the container**: this page came through the proxy over https",
        )
        page.click('#tabs .room[data-room="#smoke"]')
        page.wait_for_selector("#log .line.k-chat:has-text('inside the container')", timeout=15_000)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / "ui-over-https.png"))
        ctx.close()
    finally:
        browser.close()
    assert not problems, problems


def test_a_clean_stop_and_the_data_across_a_restart(stack: Stack) -> None:
    stack.create_room(stack.session(), "#kept")
    t0 = time.monotonic()
    docker("stop", "-t", "10", stack.broker)
    assert time.monotonic() - t0 < 8  # stopped by SIGTERM, not killed at the timeout
    assert docker("inspect", "-f", "{{.State.ExitCode}}", stack.broker).stdout.strip() == "0"
    assert "broker stopped" in docker("logs", stack.broker).stdout
    docker("start", stack.broker)
    stack.direct = int(docker("port", stack.broker, "7419/tcp").stdout.split(":")[-1])
    wait_for("the restarted broker", lambda: stack.healthz() == "ok\n")
    assert "#kept" in stack.cli("rooms")
    # the owner lives on the volume: still claimed, and no new claim link (issue #41)
    assert len(stack.claim_lines()) == 1
    assert json.loads(stack.https("GET", "/api/auth/state")[2])["claimed"] is True


@pytest.mark.parametrize("platform", ["render", "kubernetes"])
def test_the_volumes_platforms_mount(image: str, platform: str) -> None:
    """A Render disk is root's (0755): the container starts as root and makes its home in it.
    A Kubernetes volume with fsGroup 10001 is root:10001, 2770, and the pod runs as 10001 from
    the start (with the manifest's securityContext): the broker makes its home itself. Either
    way it starts twice (a redeploy)."""
    tag = uuid.uuid4().hex[:8]
    vol, name = f"sbimg-{platform}-{tag}", f"sbimg-{platform}-{tag}"
    mode = "0:0 755" if platform == "render" else "0:10001 2770"
    owner, perms = mode.split()
    try:
        docker("volume", "create", vol)
        docker(
            "run",
            "--rm",
            "-v",
            f"{vol}:/data",
            "--entrypoint",
            "sh",
            image,
            "-c",
            f"chown {owner} /data && chmod {perms} /data",
        )
        # Kubernetes as deploy/kubernetes/switchboard.yaml runs it: the user from the start, a
        # read-only root filesystem with /tmp an emptyDir, no capabilities, no privilege escalation
        user = (
            []
            if platform == "render"
            else [
                "--user",
                "10001:10001",
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ]
        )
        for boot in (1, 2):
            docker(
                "run",
                "-d",
                "--name",
                name,
                *user,
                "-v",
                f"{vol}:/data",
                "-e",
                "SWITCHBOARD_PUBLIC_URL=https://sb.example.com",
                image,
            )
            wait_for(f"boot {boot}", lambda: "broker up" in docker("logs", name).stdout)
            got = docker("exec", name, "stat", "-c", "%u %a", "/data/switchboard").stdout.split()
            assert got[0] == "10001" and got[1] in ("700", "2700"), (
                got
            )  # 2700: the fsGroup's setgid, inherited
            docker("stop", "-t", "10", name)
            assert docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip() == "0"
            docker("rm", name)
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", vol, check=False)
