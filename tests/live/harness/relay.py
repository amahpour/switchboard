"""What the live tests of a hosted call need around Docker (DESIGN.md §54, #623). Test-only.

A hosted call is two containers on the machine of the person whose agent started it
(``plugins/claude/bin/meeting-host``): joinly and the relay, one Compose project per call. These helpers
build switchboard's own image from the checkout (the relay is in it, and the release that has it is not out
yet), reach a throwaway broker that listens on this machine's loopback from inside a container (a forwarder
on Docker's bridge address, and ``--add-host`` to name it), put the two pinned joinly fixes where the script
looks for them without a download when the person running the test has them, and look at what is left of a
project.

Nothing here pulls an image. The script pulls one that is missing, which is why a live test names the
images it needs in advance (the skip message of the test says which).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from .tmuxdrv import REAL_HOME
from .world import REAL_PATH

REPO = Path(__file__).resolve().parents[3]
PINS = REPO / "plugins" / "claude" / "meeting" / "fixes.pins"
LABEL = "com.switchboard.call"
# One line: the digest scan needs the pin contiguous. The tag and digest are the compose file's.
JOINLY_IMAGE = (
    "ghcr.io/joinly-ai/joinly:v0.5.4@sha256:d19ca294eb24625d69d8069d0168cb5214af3652fbf721eb07480177eb4b9ea0"  # noqa: E501
)
FIXES_ENV = "SWITCHBOARD_LIVE_FIXES_DIR"
IMAGE_ENV = "SWITCHBOARD_LIVE_RELAY_IMAGE"

DOCKER_ENV = {
    **{
        k: os.environ[k]
        for k in (
            "PATH",
            "LANG",
            "XDG_RUNTIME_DIR",
            "DOCKER_HOST",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
        if k in os.environ
    },
    "HOME": REAL_HOME,  # tests/conftest.py points $HOME at a temp dir, where docker finds no config
}


def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], env=DOCKER_ENV, capture_output=True, text=True, timeout=timeout, check=False
    )


def skip_reason() -> str | None:
    """Why a live test of a hosted call can't run here, or None: Docker, Compose and the joinly image (3.7 GB,
    which the test never pulls: DEPLOY.md and the meeting-self-test skill say how)."""
    if shutil.which("docker", path=REAL_PATH) is None:
        return "docker is not on PATH"
    if docker("compose", "version", timeout=30).returncode != 0:
        return "docker compose is not installed"
    if docker("image", "inspect", JOINLY_IMAGE, timeout=30).returncode != 0:
        return f"the joinly image is not here: docker pull {JOINLY_IMAGE}"
    return None


def build_relay_image(tag: str = "switchboard:relay-live") -> str:
    """Build switchboard's image from this checkout (the relay is in it) and return its tag. The base images
    are never pulled (``--pull=false``): a machine that has built the image before has them."""
    r = docker(
        "build", "--pull=false", "-q", "-t", tag, "-f", "deploy/image/Dockerfile", str(REPO), timeout=900
    )
    if r.returncode != 0:
        raise RuntimeError(f"could not build the relay's image: {(r.stderr or r.stdout)[-400:]}")
    return tag


def relay_image() -> str:
    """The image the relay runs from: ``SWITCHBOARD_LIVE_RELAY_IMAGE`` if the person names one, else a build
    of this checkout."""
    return os.environ.get(IMAGE_ENV) or build_relay_image()


def pinned() -> dict[str, str]:
    """``{file name: sha256}`` of the fixes the plugin pins."""
    out = {}
    for line in PINS.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, sha, _url = line.split()
            out[name] = sha
    return out


def seed_fixes(dest: Path) -> bool:
    """Copy the two pinned fixes into ``dest`` from the folder ``SWITCHBOARD_LIVE_FIXES_DIR`` names, if both
    are there and are exactly the pinned files (checked here, as the script checks them). True when they were;
    False means the script will fetch them, which needs the network and is a download the person running the
    test should expect."""
    src = os.environ.get(FIXES_ENV)
    if not src:
        return False
    want = pinned()
    for name, sha in want.items():
        f = Path(src) / name
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != sha:
            return False
    dest.mkdir(parents=True, exist_ok=True)
    for name in want:
        shutil.copy(Path(src) / name, dest / name)
        (dest / name).chmod(0o644)
    dest.chmod(0o755)
    return True


def gateway_ip() -> str:
    """Docker's default bridge address on this machine (what ``host-gateway`` means to a container)."""
    r = docker("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}", timeout=30)
    ip = r.stdout.strip()
    if r.returncode != 0 or not ip:
        raise RuntimeError("could not find docker's bridge address")
    return ip


class Forwarder:
    """A TCP forwarder from ``listen_ip:port`` to ``127.0.0.1:port``: a container on Docker's bridge reaches a
    throwaway broker that only listens on this machine's loopback (``--add-host name:host-gateway`` names the
    bridge address inside the container). It listens on the bridge address alone, never on a LAN address."""

    def __init__(self, listen_ip: str, port: int) -> None:
        self.listen_ip, self.port = listen_ip, port
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> Forwarder:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.listen_ip, self.port))
        srv.listen(32)
        srv.settimeout(0.5)
        self._server = srv

        def pipe(a: socket.socket, b: socket.socket) -> None:
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        def serve() -> None:
            while not self._stop.is_set():
                try:
                    client, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                try:
                    upstream = socket.create_connection(("127.0.0.1", self.port))
                except OSError:
                    client.close()
                    continue
                threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
                threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

        self._thread = threading.Thread(target=serve, name="live-forwarder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(5)


# -------------------------------------------------------------------- what is left of a call
def projects() -> dict[str, list[dict[str, str]]]:
    """Every hosted call's Compose project on this machine (running or exited), with its containers: their
    service, state and exit code."""
    r = docker(
        "ps",
        "-a",
        "--filter",
        f"label={LABEL}",
        "--format",
        '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'
        "\t{{.State}}",
        timeout=30,
    )
    out: dict[str, list[dict[str, str]]] = {}
    for line in r.stdout.splitlines():
        cid, project, service, state = line.split("\t")
        code = docker("inspect", "--format", "{{.State.ExitCode}}", cid, timeout=30).stdout.strip()
        out.setdefault(project, []).append({"id": cid, "service": service, "state": state, "exit": code})
    return out


def running(project: str) -> bool:
    return any(c["state"] == "running" for c in projects().get(project, []))


def logs(project: str, service: str = "relay", tail: int | None = 40) -> str:
    """The service's log. ``tail`` is how many lines; ``None`` is the whole log (kept on a failure)."""
    cs = [c for c in projects().get(project, []) if c["service"] == service]
    if not cs:
        return ""
    args = ["logs", cs[0]["id"]]
    if tail is not None:
        args[1:1] = ["--tail", str(tail)]
    got = docker(*args, timeout=60)
    return (got.stdout or "") + (got.stderr or "")


def remove(names: list[str]) -> None:
    """Remove every container and network of these projects, whatever state they are in."""
    for project in names:
        ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}", timeout=30)
        for cid in ids.stdout.split():
            docker("rm", "-f", cid, timeout=60)
        docker("network", "rm", f"{project}_default", timeout=30)
