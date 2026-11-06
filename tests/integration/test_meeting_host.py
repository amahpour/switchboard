"""The host script of a hosted call: ``plugins/claude/bin/meeting-host`` and its PowerShell twin (#623,
DESIGN.md §54).

The script is run as the agent runs it, with a stand-in ``docker`` that records what it was asked (its
arguments and the ``SB_`` variables it was given) and a real ``curl`` behind a wrapper that records the
same. The server is a stub on a loopback port that records the registration and answers as told, or the
real broker for the one test that runs the whole path. What the stand-ins record is how this file shows
what the script does with the two secrets it handles: the meeting link and the call's key never reach an
argument or a variable of any process it starts, and the files they are in are the owner's alone.

The script exists twice, ``sh`` for Linux and macOS and PowerShell for Windows (the same as the listener
and the session script): the tests marked ``impl`` run both, the PowerShell one wherever ``pwsh`` is
installed (CI sets ``SWITCHBOARD_REQUIRE_PWSH=1``, which makes a missing one an error). What only a real
GPU, a real Docker Desktop or a real Windows can show is in the pull request that added them."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeClock, make_tmp_home
from test_agent_plugins import CURRENT_IMPL, PWSH, impl  # noqa: F401  (impl is a fixture)
from test_meeting_hosted import Host, Relay, World

from switchboard.meeting import keys

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "claude"
LINK = "https://zoom.us/j/123456789?pwd=hunter2hunter2"
TOKEN = "sbat_" + "t" * 43
LISTENER = "sbl_" + "ab" * 32
FIX_ZOOM, FIX_TRANSCRIPTION = b"# a stand-in for zoom.py\n", b"# a stand-in for transcription.py\n"
REAL_PATH = os.environ.get("PATH", "/usr/bin:/bin")
CLAUDE_PID = "424242"

FAKE_DOCKER = '''#!{python}
"""A stand-in for docker: records what it is asked and answers as the test says."""
import json, os, sys

argv = sys.argv[1:]
env = os.environ
log = env["FAKE_DOCKER_LOG"]


def note(**kw):
    with open(log, "a") as f:
        f.write(json.dumps(kw) + "\\n")


sb = {{k: v for k, v in env.items() if k.startswith("SB_")}}
if argv[:1] == ["info"]:
    if "--format" in argv:
        print(env.get("FAKE_DOCKER_RUNTIMES", "io.containerd.runc.v2 runc "), end="")
    sys.exit(int(env.get("FAKE_DOCKER_INFO_RC", "0")))
if argv[:2] == ["compose", "version"]:
    sys.exit(int(env.get("FAKE_DOCKER_COMPOSE_RC", "0")))
if argv[:2] == ["image", "inspect"]:
    note(cmd="inspect", image=argv[2])
    sys.exit(1 if argv[2] in env.get("FAKE_DOCKER_MISSING", "").split(",") else 0)
if argv[:1] == ["ps"]:
    running = [p for p in env.get("FAKE_DOCKER_RUNNING", "").split(",") if p]
    for p in running:
        if f"label=com.docker.compose.project={{p}}" in argv:
            print("c-" + p)
    sys.exit(0)
if argv[:1] == ["compose"]:
    files, project, profiles, i = [], None, [], 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-f":
            files.append(argv[i + 1]); i += 2
        elif argv[i] == "-p":
            project = argv[i + 1]; i += 2
        elif argv[i] == "--profile":
            profiles.append(argv[i + 1]); i += 2
        else:
            i += 1
    sub, rest = argv[i], argv[i + 1:]
    if sub == "config" and "--images" in rest:
        print(env.get("SB_RELAY_IMAGE") or "ghcr.io/amahpour/switchboard:0.0.0")
        print("ghcr.io/joinly-ai/joinly:v0.5.4" + ("-cuda" if "gpu" in profiles else ""))
        sys.exit(0)
    call_dir = sb.get("SB_CALL_DIR", "")
    listing = {{}}
    if call_dir and os.path.isdir(call_dir):
        for name in sorted(os.listdir(call_dir)):
            st = os.stat(os.path.join(call_dir, name))
            listing[name] = oct(st.st_mode & 0o777)
    note(cmd=sub, args=rest, project=project, profiles=profiles, files=files, env=sb, listing=listing)
    if sub == "up" and env.get("FAKE_DOCKER_UP_FAIL"):
        print("no such image", file=sys.stderr)
        sys.exit(1)
    if sub == "pull" and env.get("FAKE_DOCKER_PULL_FAIL"):
        sys.exit(1)
    if sub == "logs":
        print("relay-1  | relay: key shown")
    sys.exit(0)
sys.exit(0)
'''

CHMOD_WRAPPER = """#!/bin/sh
# records the mode each file or folder has when chmod is asked to change it, then is chmod: a secret's
# file must already be private at that moment (the script's umask), not only after it
for f in "$@"; do
  case $f in
    -* | [0-7][0-7][0-7] | [0-7][0-7][0-7][0-7]) ;;
    *)
      [ -e "$f" ] || continue
      m=$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")
      if [ -d "$f" ]; then s=$(ls -A "$f" | wc -l); else s=$(wc -c <"$f"); fi
      printf '%s %s %s\\n' "$m" "$(printf '%s' "$s" | tr -d ' ')" "$f" >> "$FAKE_CHMOD_LOG"
      ;;
  esac
done
exec {chmod} "$@"
"""

CURL_WRAPPER = """#!/bin/sh
# records the arguments and the whole environment of each curl, then is curl
{{ printf '%s\\n' "ARGS: $*"; env; printf '%s\\n' '--'; }} >> "$FAKE_CURL_LOG"
exec {curl} "$@"
"""


class Stub:
    """The server, as the host script sees it: it records each registration and answers as told."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer: tuple[int, dict[str, Any] | None] | None = None  # None: the real answer for the hash
        self.fetches: list[str] = []
        self.files: dict[str, bytes] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                outer.requests.append(
                    {"path": self.path, "auth": self.headers.get("authorization"), "body": body, "raw": raw}
                )
                if outer.answer is not None:
                    status, payload = outer.answer
                    return self._send(status, json.dumps(payload).encode())
                h = (body or {}).get("key_hash", "")
                self._send(
                    200,
                    json.dumps(
                        {
                            "room": body["room"],
                            "state": "joining",
                            "platform": body["platform"],
                            "host": "alice-laptop",
                            "fingerprint": keys.fingerprint(h),
                        }
                    ).encode(),
                )

            def do_GET(self) -> None:
                outer.fetches.append(self.path)
                data = outer.files.get(self.path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(10)


class Machine:
    """One person's machine for a run of the script: a copy of the plugin whose fixes are pinned to the stub's
    files, a folder of its own for the plugin's data, the stand-ins on a PATH, and the logs they write."""

    def __init__(
        self, stub: Stub, gpu: bool = False, darwin: bool = False, plugin_name: str = "claude"
    ) -> None:
        self.stub = stub
        self.root = Path(tempfile.mkdtemp(prefix="sb-host-"))
        self.plugin = self.root / "plugin"
        shutil.copytree(
            ROOT / "plugins" / plugin_name, self.plugin, ignore=shutil.ignore_patterns("__pycache__")
        )
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.dlog, self.clog = self.root / "docker.log", self.root / "curl.log"
        self.mlog = self.root / "chmod.log"
        self.dlog.touch()
        self.clog.touch()
        self.mlog.touch()
        chmod = shutil.which("chmod", path=REAL_PATH)
        if chmod:
            self.write_exe("chmod", CHMOD_WRAPPER.format(chmod=chmod))
        self.write_exe("docker", FAKE_DOCKER.format(python=sys.executable))
        curl = shutil.which("curl", path=REAL_PATH)
        if curl:
            self.write_exe("curl", CURL_WRAPPER.format(curl=curl))
        if gpu:
            self.write_exe("nvidia-smi", "#!/bin/sh\necho 'GPU 0: Stand-in GPU (UUID: GPU-0000)'\n")
        # the script asks `uname -s` for a Mac (and for Git Bash): the machine here is whatever the test says,
        # never the one running it (a macOS runner would otherwise send every GPU case down the Mac branch)
        self.write_exe("uname", f"#!/bin/sh\necho {'Darwin' if darwin else 'Linux'}\n")
        # the plugin's fixes: pinned to the files the stub serves, and the files already in the data folder
        self.fix_dir = self.data / "joinly-fixes"
        self.pin_fixes({"zoom.py": FIX_ZOOM, "transcription.py": FIX_TRANSCRIPTION})
        self.seed_fixes()

    def tools_only(self) -> str:
        """A PATH that has the stand-ins and the ordinary tools the script uses, and no ``docker``: a folder
        of
        links to the tools, since a real docker would be found beside them in /usr/bin."""
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        needed = (
            "sh cat sed grep head cut tr od mktemp mkdir chmod rm mv dirname basename date id uname"
            " sha256sum shasum curl ps stty timeout wc env readlink pwsh"
        )
        for name in needed.split():
            found = shutil.which(name, path=REAL_PATH)
            if found and not (tools / name).exists():
                (tools / name).symlink_to(found)
        return f"{self.bin}:{tools}"

    def write_exe(self, name: str, text: str) -> None:
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def pin_fixes(self, contents: dict[str, bytes]) -> None:
        lines = ["# pins for the test"]
        for name, data in contents.items():
            self.stub.files[f"/fix/{name}"] = data
            lines.append(f"{name} {hashlib.sha256(data).hexdigest()} {self.stub.url}/fix/{name}")
        (self.plugin / "meeting" / "fixes.pins").write_text("\n".join(lines) + "\n")

    def seed_fixes(self) -> None:
        self.fix_dir.mkdir(exist_ok=True)
        (self.fix_dir / "zoom.py").write_bytes(FIX_ZOOM)
        (self.fix_dir / "transcription.py").write_bytes(FIX_TRANSCRIPTION)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{REAL_PATH}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.root),
            "FAKE_DOCKER_LOG": str(self.dlog),
            "FAKE_CURL_LOG": str(self.clog),
            "FAKE_CHMOD_LOG": str(self.mlog),
            "CLAUDE_PID": CLAUDE_PID,
            "SWITCHBOARD_AGENT_TOKEN": TOKEN,
            "SWITCHBOARD_RELAY_IMAGE": "switchboard:relay-test",
            **extra,
        }
        return {k: v for k, v in env.items() if v != ""}

    def cmd(self, args: list[str]) -> list[str]:
        if CURRENT_IMPL[0] == "sh":
            return ["sh", str(self.plugin / "bin" / "meeting-host"), *args]
        assert PWSH is not None
        return [
            PWSH, "-NoProfile", "-NonInteractive", "-File",
            str(self.plugin / "bin" / "windows" / "meeting-host.ps1"), *args,
        ]  # fmt: skip

    def run(
        self, args: list[str], stdin: str | None = LINK + "\n", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = self.cmd(args)
        full = self.env() if env is None else env
        if CURRENT_IMPL[0] != "sh":
            full = {
                **full,
                "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
                "DOTNET_EnableDiagnostics": "0",
                "POWERSHELL_TELEMETRY_OPTOUT": "1",
                "POWERSHELL_UPDATECHECK": "Off",
            }
        return subprocess.run(
            argv, input=stdin or "", env=full, capture_output=True, text=True, timeout=120, cwd=self.root
        )

    def start(self, *args: str, room: str = "#uploads", **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.run(["start", "--server", self.stub.url, "--data", str(self.data), *args, room], **kw)

    def docker(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.dlog.read_text().splitlines() if x]

    def compose(self, sub: str) -> list[dict[str, Any]]:
        return [c for c in self.docker() if c["cmd"] == sub]

    def call_dirs(self) -> list[Path]:
        calls = self.data / "calls"
        return sorted(p for p in calls.iterdir()) if calls.is_dir() else []

    def everything_a_process_was_given(self) -> str:
        """Every argument and every variable of every docker and curl the script ran."""
        return self.dlog.read_text() + self.clog.read_text()

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def stub() -> Iterator[Stub]:
    s = Stub()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def machine(stub: Stub, impl: str) -> Iterator[Machine]:  # noqa: F811
    if shutil.which("curl") is None and impl == "sh":
        pytest.skip("the script needs curl")
    m = Machine(stub)
    try:
        yield m
    finally:
        m.close()


def plan(m: Machine, *args: str, env: dict[str, str] | None = None) -> dict[str, str]:
    r = m.run(["plan", *args], stdin="", env=env)
    assert r.returncode == 0, r
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


# --------------------------------------------------------------------------------- GPU if there is one
@pytest.mark.usefixtures("impl")
@pytest.mark.parametrize(
    ("gpu", "runtimes", "darwin", "device", "reason"),
    [
        (True, "io.containerd.runc.v2 nvidia runc ", False, "gpu", "an NVIDIA GPU that Docker can use"),
        (True, "io.containerd.runc.v2 runc ", False, "cpu", "Docker has no nvidia runtime"),
        (False, "io.containerd.runc.v2 nvidia runc ", False, "cpu", "no NVIDIA GPU"),
        (True, "io.containerd.runc.v2 nvidia runc ", True, "cpu", "Docker on a Mac cannot use the GPU"),
    ],
)
def test_the_gpu_is_chosen_only_when_there_is_one_for_docker_to_use(
    stub: Stub,
    gpu: bool,
    runtimes: str,
    darwin: bool,
    device: str,
    reason: str,
) -> None:
    """GPU if there is one, CPU if not (#623), decided by local questions that pull nothing: the driver sees a
    GPU, and Docker has an ``nvidia`` runtime (the Container Toolkit). A Mac is always CPU, whatever else is
    on the PATH. The answer says which, why, and the model and image that go with it. Fails if a machine
    with a card but no toolkit is sent to the GPU image (which cannot start), or one with both is not."""
    m = Machine(stub, gpu=gpu, darwin=darwin)
    try:
        got = plan(m, env=m.env(FAKE_DOCKER_RUNTIMES=runtimes))
        assert got["device"] == device and reason in got["reason"], got
        want_model, want_image = (
            ("large-v3-turbo", "ghcr.io/joinly-ai/joinly:v0.5.4-cuda")
            if device == "gpu"
            else ("base", "ghcr.io/joinly-ai/joinly:v0.5.4")
        )
        assert (got["model"], got["joinly"]) == (want_model, want_image)
    finally:
        m.close()


def test_wsl_s_nvidia_smi_is_found_off_the_path(stub: Stub) -> None:
    """On Windows with WSL2 and Docker Desktop, the driver's ``nvidia-smi`` lives in ``/usr/lib/wsl/lib``,
    which is often not on the PATH: the script found no GPU on such a machine (a GTX 1660 with Docker's nvidia
    runtime) and chose the CPU. With nothing on the PATH it now asks that file too, and only when it answers
    with a GPU. Fails if the WSL path is not asked, or if a file there that sees no GPU sends it to the GPU.
    (The sh script only: the PowerShell one runs on Windows itself, where nvidia-smi.exe is on the PATH.)"""
    m = Machine(stub)
    try:
        wsl = m.root / "wsl-lib"
        wsl.mkdir()
        smi = wsl / "nvidia-smi"
        smi.write_text("#!/bin/sh\necho 'GPU 0: Stand-in GPU (UUID: GPU-0000)'\n")
        smi.chmod(0o755)
        env = m.env(
            FAKE_DOCKER_RUNTIMES="io.containerd.runc.v2 nvidia runc ", SWITCHBOARD_WSL_NVIDIA_SMI=str(smi)
        )
        CURRENT_IMPL[0] = "sh"
        got = plan(m, env=env)
        assert got["device"] == "gpu" and got["model"] == "large-v3-turbo", got
        smi.write_text("#!/bin/sh\necho 'No devices were found'\nexit 6\n")
        assert plan(m, env=env)["device"] == "cpu"
    finally:
        m.close()


@pytest.mark.usefixtures("impl")
def test_a_flag_forces_either_and_says_what_it_overrode(stub: Stub) -> None:
    m = Machine(stub, gpu=True)
    try:
        env = m.env(FAKE_DOCKER_RUNTIMES="io.containerd.runc.v2 nvidia runc ")
        cpu = plan(m, "--cpu", env=env)
        assert cpu["device"] == "cpu" and cpu["model"] == "base" and "forced by --cpu" in cpu["reason"]
        assert "detected: gpu" in cpu["reason"]
        bare = Machine(stub)
        try:
            gpu = plan(bare, "--gpu")
            assert gpu["device"] == "gpu" and gpu["model"] == "large-v3-turbo"
            assert "forced by --gpu" in gpu["reason"] and "detected: cpu" in gpu["reason"]
            assert plan(bare, "--cpu", "--model", "small")["model"] == "small"  # a model asked for is kept
            bad = bare.run(["plan", "--model", "gigantic"], stdin="")
            assert bad.returncode == 2 and "base small distil-large-v3 large-v3-turbo" in bad.stderr
        finally:
            bare.close()
    finally:
        m.close()


# --------------------------------------------------------------------------- the whole of a start
def parse_info(m: Machine) -> dict[str, str]:
    [d] = m.call_dirs()
    return dict(x.split("=", 1) for x in (d / "info").read_text().splitlines())


def test_start_registers_the_hash_of_a_key_it_made_and_starts_one_project(machine: Machine) -> None:
    """The script's whole job on a machine with Docker: it makes a key (``sbc_`` and 64 hex) in a file, sends
    the server that key's sha256 (never the key) with the room, the platform the link is on and the model,
    under the credential the plugin's other scripts hold; checks the fingerprint the server gives back is the
    one it made; starts one Compose project named for it; and prints the fingerprint and the device for the
    person to compare with the call bar. Fails if the key is sent, the room or platform is wrong, or the
    project is not
    one per call."""
    m = machine
    r = m.start()
    assert r.returncode == 0, r.stderr
    [req] = m.stub.requests
    assert req["path"] == "/mcp/meeting/host" and req["auth"] == f"Bearer {TOKEN}"
    [call] = m.call_dirs()
    key = (call / "key").read_text().strip()
    assert keys.KEY_RE.fullmatch(key)
    body = req["body"]
    assert body == {
        "room": "#uploads",
        "key_hash": keys.key_hash(key),
        "platform": "zoom",
        "model": "base",
    }
    assert key.encode() not in req["raw"]
    fp = keys.fingerprint(keys.key_hash(key))
    assert f"Key fingerprint: {fp}" in r.stdout
    assert "Hosting #uploads (Zoom) from alice-laptop." in r.stdout
    assert "Speech: CPU, model base" in r.stdout
    [up] = m.compose("up")
    assert up["project"] == "switchboard-call-" + keys.key_hash(key)[:8]
    assert up["profiles"] == ["cpu"] and up["args"] == ["-d"]
    assert up["env"]["SB_SERVER"] == m.stub.url and up["env"]["SB_CALL_DIR"] == str(call)
    assert (
        up["env"]["SB_FIXES_DIR"] == str(m.fix_dir)
        and up["env"]["SB_RELAY_USER"] == f"{os.getuid()}:{os.getgid()}"
    )
    assert up["env"].get("SB_MODEL", "") == "" and up["env"].get("SB_NO_JOIN", "") == ""  # joinly's own model
    assert up["env"]["SB_RELAY_IMAGE"] == "switchboard:relay-test"
    assert parse_info(m)["room"] == "#uploads" and parse_info(m)["fingerprint"] == keys.key_hash(key)[:8]


@pytest.mark.parametrize(
    ("link", "platform"),
    [
        ("https://us02web.zoom.us/j/123456789?pwd=abc", "zoom"),
        ("https://meet.google.com/abc-defg-hij", "meet"),
        ("https://teams.microsoft.com/l/meetup-join/19%3ameeting", "teams"),
        ("https://teams.live.com/meet/9876543210", "teams"),
    ],
)
def test_the_platform_is_read_from_the_links_host(machine: Machine, link: str, platform: str) -> None:
    r = machine.start(stdin=link + "\n")
    assert r.returncode == 0, r.stderr
    assert machine.stub.requests[0]["body"]["platform"] == platform


def test_neither_the_link_nor_the_key_is_in_any_argument_or_environment_of_anything_it_starts(
    machine: Machine,
) -> None:
    """The link goes from stdin to a file, and the key is made in a file: ``ps`` and ``/proc/<pid>/environ``
    of docker and curl show neither, and neither does anything the script prints. The credential is the
    listener key a Bash command finds in the plugin's data folder; it goes to curl on its stdin, as the
    plugin's other scripts do it, so it is not on the command line either. Fails if any of them reaches an
    argument, a
    variable, or the output."""
    m = machine
    (m.data / f"listener-key-{CLAUDE_PID}").write_text(LISTENER + "\n")
    env = {k: v for k, v in m.env().items() if k != "SWITCHBOARD_AGENT_TOKEN"}
    r = m.start(env=env)
    assert r.returncode == 0, r.stderr
    assert m.stub.requests[0]["auth"] == f"Bearer {LISTENER}"
    key = (m.call_dirs()[0] / "key").read_text().strip()
    seen = m.everything_a_process_was_given() + r.stdout + r.stderr
    for secret in (key, key[4:], LINK, "hunter2hunter2", "123456789", LISTENER, LISTENER[4:]):
        assert secret not in seen, secret[:12]
    if CURRENT_IMPL[0] == "sh":  # (the hash is what goes to the server, in the body: curl's argument)
        assert keys.key_hash(key) in seen


def test_the_files_are_the_owners_alone_and_the_link_is_in_one(machine: Machine) -> None:
    """The key and the link are written to files in a folder only their owner can enter, each mode 0600 from
    the
    moment it is written. The relay reads them there; no one else on the machine can."""
    m = machine
    assert m.start().returncode == 0
    [call] = m.call_dirs()
    assert (
        stat.S_IMODE(call.stat().st_mode) == 0o700
        and stat.S_IMODE((m.data / "calls").stat().st_mode) == 0o700
    )
    for name in ("key", "link"):
        assert stat.S_IMODE((call / name).stat().st_mode) == 0o600, name
    assert (call / "link").read_text().strip() == LINK
    assert m.compose("up")[0]["listing"].keys() >= {"key", "link", "info"}


def test_a_secrets_file_is_private_from_the_moment_it_exists(machine: Machine) -> None:
    """Not only after the script has called chmod: the script's umask makes the key's file, the link's file
    and their folders private as they are made, so there is no moment when another account could read a
    passcode. The chmod stand-in records each mode as chmod is called. Fails if a file or folder is wider
    than 0600 (0700 for a folder) before that."""
    m = machine
    assert m.start().returncode == 0
    seen = {}
    for line in m.mlog.read_text().splitlines():
        mode, size, path = line.split(" ", 2)
        seen.setdefault(path, (mode, int(size)))
    [call] = m.call_dirs()
    for name in ("key", "link"):
        mode, size = seen[str(call / name)]
        # private already, or (the PowerShell script's way, as headers.ps1's: the access list is set while
        # it is empty) holding nothing yet
        assert mode == "600" or size == 0, (name, seen)
    for folder in (call, m.data / "calls"):
        mode, entries = seen[str(folder)]
        # a folder is private from its creation (the shell's umask), or, as with the PowerShell script, made
        # private while it is still empty, before a secret is put in it
        assert mode == "700" or entries == 0, (folder.name, seen)


def test_a_check_of_the_setup_takes_no_link_and_leaves_none(machine: Machine) -> None:
    """``--no-join`` and nothing on stdin: the bot starts, joins no call, and no link file exists. The relay
    is
    told so, and the platform is the one a call can be on without saying which."""
    m = machine
    r = m.start("--no-join", stdin="")
    assert r.returncode == 0, r.stderr
    [call] = m.call_dirs()
    assert not (call / "link").exists()
    assert m.stub.requests[0]["body"]["platform"] == "unknown"
    assert m.compose("up")[0]["env"]["SB_NO_JOIN"] == "1"


@pytest.mark.usefixtures("impl")
def test_the_gpu_machine_gets_the_cuda_profile_and_the_big_model(stub: Stub) -> None:
    m = Machine(stub, gpu=True)
    try:
        r = m.start(env=m.env(FAKE_DOCKER_RUNTIMES="io.containerd.runc.v2 nvidia runc "))
        assert r.returncode == 0, r.stderr
        assert m.stub.requests[0]["body"]["model"] == "large-v3-turbo"
        [up] = m.compose("up")
        assert up["profiles"] == ["gpu"] and up["env"]["SB_MODEL"] == "large-v3-turbo"
        assert "Speech: GPU, model large-v3-turbo (an NVIDIA GPU that Docker can use)" in r.stdout
    finally:
        m.close()


def test_a_server_that_gives_back_another_fingerprint_is_not_trusted_and_nothing_starts(
    machine: Machine,
) -> None:
    """The fingerprint is how the person checks the call is theirs: if the answer names another, the script
    stops
    before it starts anything and removes what it made. Fails if it starts the containers anyway."""
    m = machine
    m.stub.answer = (
        200,
        {"room": "#uploads", "state": "joining", "platform": "zoom", "host": "x", "fingerprint": "dead·beef"},
    )
    r = m.start()
    assert r.returncode == 1 and "fingerprint" in r.stderr
    assert m.compose("up") == [] and m.call_dirs() == []


@pytest.mark.parametrize(
    ("status", "payload", "says"),
    [
        (401, {"error": "unknown_key", "message": "x"}, "did not accept this Claude Code's credential"),
        (
            403,
            {"error": "not_member", "message": "no agent on this machine is in #uploads"},
            "no agent on this machine",
        ),
        (
            409,
            {"error": "conflict", "message": "#uploads already has a call."},
            "#uploads already has a call",
        ),
        (429, {"error": "rate_limited", "message": "too many tries"}, "too many tries"),
    ],
)
def test_a_refusal_is_told_in_the_servers_words_and_leaves_nothing_behind(
    machine: Machine, status: int, payload: dict[str, str], says: str
) -> None:
    m = machine
    m.stub.answer = (status, payload)
    r = m.start()
    assert r.returncode == 1 and says in r.stderr, r.stderr
    assert m.call_dirs() == [] and m.compose("up") == []


def test_a_server_that_cannot_be_reached_is_said_so_and_leaves_nothing_behind(machine: Machine) -> None:
    m = machine
    r = m.run(["start", "--server", "http://127.0.0.1:1", "--data", str(m.data), "uploads"])
    assert r.returncode == 1 and "could not reach the server" in r.stderr
    assert m.call_dirs() == [] and m.compose("up") == []


def test_containers_that_will_not_start_are_cleaned_up_and_the_person_is_told_what_the_room_will_do(
    machine: Machine,
) -> None:
    m = machine
    r = m.start(env=m.env(FAKE_DOCKER_UP_FAIL="1"))
    assert r.returncode == 1 and "did not start" in r.stderr and "ends by itself in ten minutes" in r.stderr
    assert m.call_dirs() == []
    assert [c["cmd"] for c in m.docker() if c["cmd"] in ("up", "down")] == ["up", "down"]


# ---------------------------------------------------- what it refuses before a key exists or a word is sent
@pytest.mark.parametrize(
    ("what", "args", "stdin", "env_drop", "says", "code"),
    [
        (
            "a link on the command line",
            ["start", "uploads", "https://zoom.us/j/1?pwd=x"],
            "",
            [],
            "stdin, never on the command line",
            2,
        ),
        ("no room", ["start"], LINK, [], "which room", 2),
        ("two rooms", ["start", "a", "b"], LINK, [], "one room", 2),
        ("a room that is not a name", ["start", "up;loads"], LINK, [], "letters, digits", 2),
        ("a flag it does not know", ["start", "--bogus", "uploads"], LINK, [], "unknown argument", 2),
        ("a flag with no value", ["start", "uploads", "--server"], LINK, [], "needs a value", 2),
        ("no link on stdin", ["start", "uploads"], "", [], "read from stdin", 1),
        (
            "a link to another site",
            ["start", "uploads"],
            "https://example.com/j/1\n",
            [],
            "Zoom, Meet or Teams",
            1,
        ),
        (
            "a link that is not https",
            ["start", "uploads"],
            "http://zoom.us/j/1\n",
            [],
            "Zoom, Meet or Teams",
            1,
        ),
        (
            "a link with a space in it",
            ["start", "uploads"],
            "https://zoom.us/j/1 x\n",
            [],
            "not a meeting link",
            1,
        ),
        ("no credential", ["start", "uploads"], LINK, ["SWITCHBOARD_AGENT_TOKEN"], "no credential", 1),
    ],
)
def test_what_is_wrong_is_found_before_a_key_exists_or_the_server_hears_of_it(
    machine: Machine, what: str, args: list[str], stdin: str, env_drop: list[str], says: str, code: int
) -> None:
    """Every refusal here happens first: no request to the server, no folder, no container, and nothing that
    says the link (a link refused is not repeated). Fails if the script has made anything by then."""
    m = machine
    env = {k: v for k, v in m.env().items() if k not in env_drop}
    full = [*args[:1], "--server", m.stub.url, "--data", str(m.data), *args[1:]]
    r = m.run(full, stdin=stdin, env=env)
    assert (r.returncode, says in r.stderr) == (code, True), (what, r)
    assert m.stub.requests == [] and m.call_dirs() == [] and m.compose("up") == []
    assert "hunter2" not in r.stdout + r.stderr and "zoom.us/j/1" not in r.stdout + r.stderr


@pytest.mark.parametrize(
    ("server", "env", "says"),
    [
        ("http://sb.example.com", {}, "plain http"),
        ("ftp://sb.example.com", {}, "must start with https://"),
        ("https://sb.example.com/ x", {}, "character it should not"),
    ],
)
def test_the_servers_address_must_be_https_or_this_machines_own(
    machine: Machine, server: str, env: dict[str, str], says: str
) -> None:
    m = machine
    r = m.run(["start", "--server", server, "--data", str(m.data), "uploads"], env=m.env(**env))
    assert r.returncode == 1 and says in r.stderr
    assert m.stub.requests == []
    ok = m.run(["start", "--server", "http://127.0.0.1:9", "--data", str(m.data), "uploads"])
    assert "plain http" not in ok.stderr  # the machine's own address is taken (it cannot be reached, here)


@pytest.mark.parametrize(
    ("break_it", "says"),
    [
        ({"FAKE_DOCKER_INFO_RC": "1"}, "not running"),
        ({"FAKE_DOCKER_COMPOSE_RC": "1"}, "Docker Compose is needed"),
    ],
)
def test_docker_must_work_before_anything_is_made(
    machine: Machine, break_it: dict[str, str], says: str
) -> None:
    m = machine
    r = m.start(env=m.env(**break_it))
    assert r.returncode == 1 and says in r.stderr
    assert m.stub.requests == [] and m.call_dirs() == []


@pytest.mark.usefixtures("impl")
def test_no_docker_at_all_is_said_before_anything_is_made(stub: Stub) -> None:
    m = Machine(stub)
    try:
        (m.bin / "docker").unlink()
        r = m.start(env=m.env(PATH=m.tools_only()))
        assert r.returncode == 1 and "Docker is needed" in r.stderr
        assert m.stub.requests == [] and m.call_dirs() == []
    finally:
        m.close()


# ------------------------------------------------------------------------------- the credential it finds
def test_without_a_typed_token_it_presents_the_listener_key_of_this_claude_code(machine: Machine) -> None:
    """A Bash command is given ``CLAUDE_PID`` and not the plugin's settings (recorded live, Claude Code
    2.1.307: ``tests/fixtures/payloads/claude/PostToolUse_bash_plugin_env.json``), so a session that signed
    in through the browser, or typed its token into the plugin's settings, has only the listener key
    ``bin/headers`` left in the plugin's data folder, named for Claude Code's pid. The script finds it there
    by that pid and presents
    it. Fails if it needs a variable a Bash command is not given."""
    m = machine
    (m.data / f"listener-key-{CLAUDE_PID}").write_text(LISTENER + "\n")
    (m.data / "listener-key-999999").write_text("sbl_" + "cd" * 32 + "\n")  # another Claude Code's
    env = {k: v for k, v in m.env().items() if k != "SWITCHBOARD_AGENT_TOKEN"}
    r = m.start(env=env)
    assert r.returncode == 0, r.stderr
    assert m.stub.requests[0]["auth"] == f"Bearer {LISTENER}"


def test_a_data_folder_that_is_not_given_is_found_from_claude_codes_pid(machine: Machine) -> None:
    """A Bash command is not given ``CLAUDE_PLUGIN_DATA`` either; the skill passes it, and when it isn't
    passed
    the script looks for the one folder under Claude Code's plugin data that holds this pid's key."""
    m = machine
    folder = m.home / ".claude" / "plugins" / "data" / "switchboard-switchboard"
    folder.mkdir(parents=True)
    other = m.home / ".claude" / "plugins" / "data" / "another-plugin"
    other.mkdir(parents=True)
    (folder / f"listener-key-{CLAUDE_PID}").write_text(LISTENER + "\n")
    shutil.copytree(m.fix_dir, folder / "joinly-fixes")
    env = {k: v for k, v in m.env().items() if k != "SWITCHBOARD_AGENT_TOKEN"}
    r = m.run(["start", "--server", m.stub.url, "uploads"], env=env)
    assert r.returncode == 0, r.stderr
    assert m.stub.requests[0]["auth"] == f"Bearer {LISTENER}"
    assert (folder / "calls").is_dir() and not (other / "calls").exists()


def test_a_key_file_that_is_not_a_listener_key_is_no_credential(machine: Machine) -> None:
    m = machine
    (m.data / f"listener-key-{CLAUDE_PID}").write_text("sbl_nothex\n")
    env = {k: v for k, v in m.env().items() if k != "SWITCHBOARD_AGENT_TOKEN"}
    r = m.start(env=env)
    assert r.returncode == 1 and "no credential" in r.stderr and m.stub.requests == []


# ------------------------------------------------------------------------------------- the two fixes
def test_a_fix_that_is_not_the_pinned_file_is_never_mounted(machine: Machine) -> None:
    """Each fix is checked against the sha256 in ``meeting/fixes.pins`` every time: a file in the data folder
    that differs is replaced by a fresh one, and a fresh one that differs is refused and not kept. Fails if a
    file
    that is not the pinned one reaches joinly's mount."""
    m = machine
    (m.fix_dir / "zoom.py").write_bytes(b"# tampered\n")
    fetched = m.start()
    assert fetched.returncode == 0, fetched.stderr
    assert (m.fix_dir / "zoom.py").read_bytes() == FIX_ZOOM  # replaced from the pinned address
    assert m.stub.fetches == ["/fix/zoom.py"]  # and only that one: the other one was right
    m.stub.fetches.clear()
    (m.fix_dir / "zoom.py").unlink()
    m.stub.files["/fix/zoom.py"] = b"# what a hijacked address serves\n"
    refused = m.start()
    assert refused.returncode == 1 and "not the file meeting/fixes.pins pins" in refused.stderr
    assert not (m.fix_dir / "zoom.py").exists()
    assert not list(m.fix_dir.glob(".fetch.*"))
    assert len(m.compose("up")) == 1  # only the first start's


def test_the_fixes_are_public_files_in_a_public_folder_because_joinlys_user_is_not_yours(
    machine: Machine,
) -> None:
    m = machine
    (m.fix_dir / "zoom.py").chmod(0o600)  # a file kept from a run with a stricter umask
    assert m.start().returncode == 0
    assert stat.S_IMODE((m.fix_dir / "zoom.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((m.fix_dir / "transcription.py").stat().st_mode) == 0o644
    assert stat.S_IMODE(m.fix_dir.stat().st_mode) == 0o755


def test_a_fix_that_cannot_be_fetched_stops_before_anything_is_made(machine: Machine) -> None:
    m = machine
    shutil.rmtree(m.fix_dir)
    del m.stub.files["/fix/zoom.py"]
    r = m.start()
    assert r.returncode == 1 and "could not fetch zoom.py" in r.stderr
    assert m.stub.requests == [] and m.call_dirs() == []


def test_the_pins_are_the_ones_the_self_test_and_deploy_md_name() -> None:
    """The shipped pins (``meeting/fixes.pins``) are the files the broker's own bot runs with in the live
    self-test and the ones docs/DEPLOY.md tells an operator to download: one set. Fails if they drift
    apart."""
    text = (ROOT / "tests" / "live" / "test_meeting_selftest.py").read_text()
    pins = {}
    for line in (PLUGIN / "meeting" / "fixes.pins").read_text().splitlines():
        if line and not line.startswith("#"):
            name, sha, url = line.split()
            pins[name] = (sha, url)
    assert set(pins) == {"zoom.py", "transcription.py"}
    assert f'ZOOM_FIX_SHA256 = "{pins["zoom.py"][0]}"' in text
    assert f'PREROLL_FIX_SHA256 = "{pins["transcription.py"][0]}"' in text
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text()
    for name, (sha, url) in pins.items():
        assert url in deploy, name  # the commit-pinned address DEPLOY.md downloads from
        assert sha in deploy, name  # and the hash it checks


# ------------------------------------------------------------------------ pulling, sweeping, stopping
def test_a_missing_image_is_pulled_before_the_server_hears_of_the_call(machine: Machine) -> None:
    """The only thing that takes minutes is pulling an image, and it comes first: a call registered in the
    room is ready to start. Fails if the call is registered and then the script waits on a download, where
    the room's
    ten minutes run."""
    m = machine
    r = m.start(env=m.env(FAKE_DOCKER_MISSING="switchboard:relay-test,ghcr.io/joinly-ai/joinly:v0.5.4"))
    assert r.returncode == 0, r.stderr
    assert "Pulling: switchboard:relay-test ghcr.io/joinly-ai/joinly:v0.5.4" in r.stderr
    kinds = [c["cmd"] for c in m.docker() if c["cmd"] in ("pull", "up")]
    assert kinds == ["pull", "up"]
    assert [c for c in m.docker() if c["cmd"] == "pull"][0]["profiles"] == ["cpu"]
    failing = m.start(env=m.env(FAKE_DOCKER_MISSING="switchboard:relay-test", FAKE_DOCKER_PULL_FAIL="1"))
    assert failing.returncode == 1 and "could not pull" in failing.stderr
    assert len(m.stub.requests) == 1  # the second never told the server


def test_extra_hosts_for_a_server_the_containers_cannot_resolve_go_in_a_second_compose_file(
    machine: Machine,
) -> None:
    """``--add-host NAME:IP`` is for a server whose name only this machine knows (a LAN name, ``host-gateway``
    for a server on this machine). It is written beside the call's files and given to Compose as a second
    file, and nowhere else."""
    m = machine
    r = m.start("--add-host", "sb.internal:192.0.2.7")
    assert r.returncode == 0, r.stderr
    [up] = m.compose("up")
    [call] = m.call_dirs()
    assert up["files"] == [str(m.plugin / "meeting" / "compose.yaml"), str(call / "override.yaml")]
    assert yaml.safe_load((call / "override.yaml").read_text()) == {
        "services": {"relay": {"extra_hosts": ["sb.internal:192.0.2.7"]}}
    }
    bad = m.start("--add-host", "x;rm:1.2.3.4")
    assert bad.returncode == 2 and "NAME:IP" in bad.stderr


def test_stop_removes_the_project_and_its_files_and_status_says_what_runs(machine: Machine) -> None:
    """``stop`` brings both containers and their network down and forgets the call's files (the key, the link
    if it is still there); with a fingerprint it stops that call only; with none it stops every call on the
    machine.
    ``status`` lists what is there, what the relay last said, and whether it runs."""
    m = machine
    assert m.start().returncode == 0
    first = m.call_dirs()[0].name
    running = f"switchboard-call-{first}"
    assert m.start("--no-join", stdin="", env=m.env(FAKE_DOCKER_RUNNING=running)).returncode == 0
    second = next(p.name for p in m.call_dirs() if p.name != first)
    both = f"{running},switchboard-call-{second}"
    st = m.run(["status", "--data", str(m.data)], stdin="", env=m.env(FAKE_DOCKER_RUNNING=both))
    assert st.returncode == 0 and st.stdout.count("state=running") == 2 and "room=#uploads" in st.stdout
    assert "relay: key shown" in st.stdout  # the relay's last lines
    fp = f"{first[:4]}\u00b7{first[4:]}"
    r = m.run(["stop", "--data", str(m.data), fp], stdin="", env=m.env(FAKE_DOCKER_RUNNING=both))
    assert r.returncode == 0 and f"stopped the call with key {fp}" in r.stdout
    assert "relay reports departure" in r.stdout  # only an unreachable broker needs the silence fallback
    assert [p.name for p in m.call_dirs()] == [second]  # only that one
    downs = [c for c in m.compose("down") if c["project"] == running]
    assert downs and "--timeout" in downs[-1]["args"]
    over = m.run(["stop", "--data", str(m.data)], stdin="")  # (nothing runs now: End call had ended it)
    assert over.returncode == 0 and m.call_dirs() == [] and "stays in the room" not in over.stdout
    again = m.run(["stop", "--data", str(m.data)], stdin="")
    assert "no hosted call to stop" in again.stdout


def test_a_call_whose_containers_are_gone_is_cleared_at_the_next_start(machine: Machine) -> None:
    """When End call ends a call, the relay leaves and the containers stop on their own, and what is left is
    two exited containers and a folder. The next start removes them, and leaves alone a call that still runs.
    Fails
    if they pile up, or a running call is taken down by another's start."""
    m = machine
    assert m.start().returncode == 0
    old = m.call_dirs()[0].name
    assert m.start().returncode == 0  # nothing is running: the first call is over
    assert old not in [p.name for p in m.call_dirs()]
    assert any(c["project"] == f"switchboard-call-{old}" for c in m.compose("down"))
    live = next(p.name for p in m.call_dirs())
    assert m.start(env=m.env(FAKE_DOCKER_RUNNING=f"switchboard-call-{live}")).returncode == 0
    assert live in [p.name for p in m.call_dirs()]  # one that still runs is left alone


# --------------------------------------------------------------------------------- the files as shipped
def test_the_relays_compose_file_keeps_joinly_private_and_ties_its_life_to_the_relays() -> None:
    """The three things the file is for: joinly (which has no sign-in) is reachable by the relay alone,
    because it shares the relay's network and listens on its loopback, with no port published and no network
    of its own; joinly ends with the relay, because it shares the relay's process namespace; and the GPU is
    its own profile
    so a machine with none starts the CPU one. Fails if any of them is lost."""
    spec = yaml.safe_load((PLUGIN / "meeting" / "compose.yaml").read_text())
    services = spec["services"]
    assert set(services) == {"relay", "joinly", "joinly-gpu"}
    for name in ("joinly", "joinly-gpu"):
        s = services[name]
        assert s["network_mode"] == "service:relay" and s["pid"] == "service:relay", name
        assert "ports" not in s and "expose" not in s, name
        assert s["environment"]["JOINLY_SERVER_HOST"] == "127.0.0.1", name
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"] and s["restart"] == "no", name
        assert s["shm_size"] == "1gb"
    assert "ports" not in services["relay"] and "network_mode" not in services["relay"]
    assert services["joinly"]["profiles"] == ["cpu"] and services["joinly-gpu"]["profiles"] == ["gpu"]
    assert "deploy" not in services["joinly"], "a machine with no GPU must still start the CPU bot"
    assert services["joinly-gpu"]["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    cpu, gpu = services["joinly"]["image"], services["joinly-gpu"]["image"]
    assert gpu == cpu + "-cuda" and cpu.startswith("ghcr.io/joinly-ai/joinly:v") and ":latest" not in cpu
    mounts = [v for s in ("joinly", "joinly-gpu") for v in services[s]["volumes"]]
    assert sum(v.endswith(":ro") for v in mounts) == 4  # the two fixes, in both
    models = [v for v in services["joinly-gpu"]["volumes"] if "huggingface" in v]
    assert len(models) == 1 and not [v for v in services["joinly"]["volumes"] if "huggingface" in v]


def test_the_relay_is_given_no_secret_through_its_environment_and_runs_as_the_person() -> None:
    """The key and the link reach the relay as files in the call's folder, never as a variable: its
    environment has the server's address, the model and two switches. Run as the person (their uid),
    read-only, with no
    capability. Fails if a variable that could hold a secret is added."""
    relay = yaml.safe_load((PLUGIN / "meeting" / "compose.yaml").read_text())["services"]["relay"]
    assert set(relay["environment"]) == {
        "SWITCHBOARD_URL",
        "SWITCHBOARD_MEETING_STT_MODEL",
        "SWITCHBOARD_RELAY_NO_JOIN",
        "SWITCHBOARD_RELAY_ALLOW_HTTP",
    }
    cmd = relay["command"]
    assert cmd[:2] == ["meeting", "relay"] and "--key-file" in cmd and "--link-file" in cmd
    assert not any("sbc_" in str(x) for x in cmd)
    assert [x for x in cmd if "://" in str(x)] == [
        "http://127.0.0.1:8000/mcp/"
    ]  # joinly, on the relay's loopback
    assert relay["read_only"] is True and relay["cap_drop"] == ["ALL"]
    assert relay["user"].startswith("${SB_RELAY_USER:?") and relay["restart"] == "no"
    assert relay["volumes"] == ["${SB_CALL_DIR:?the host script sets this}:/call"]
    assert relay["image"].startswith("${SB_RELAY_IMAGE:-ghcr.io/amahpour/switchboard:")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_docker_compose_reads_the_file_and_the_profiles_pick_the_right_services() -> None:
    """The real ``docker compose config`` (it needs no daemon): the file is valid, and each profile starts the
    relay with the one joinly. Fails if the file does not parse or a profile starts both."""
    env = {
        "PATH": REAL_PATH,
        "HOME": tempfile.gettempdir(),
        "SB_CALL_DIR": "/c",
        "SB_SERVER": "https://sb.example.com",
        "SB_FIXES_DIR": "/f",
        "SB_RELAY_USER": "1000:1000",
    }
    compose = str(PLUGIN / "meeting" / "compose.yaml")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, env=env)
    if probe.returncode != 0:
        pytest.skip("docker compose is not installed")
    for profile, want in (("cpu", {"relay", "joinly"}), ("gpu", {"relay", "joinly-gpu"})):
        r = subprocess.run(
            ["docker", "compose", "-f", compose, "-p", "t", "--profile", profile, "config", "--format", "json"],  # noqa: E501
            capture_output=True, text=True, env=env, timeout=60,
        )  # fmt: skip
        assert r.returncode == 0, r.stderr
        assert set(json.loads(r.stdout)["services"]) == want, profile


async def test_the_script_registers_a_call_at_the_real_broker_with_the_key_a_claude_code_holds(
    machine: Machine,
) -> None:
    """Against the real broker, not a stub: a Claude Code session of ``alice-laptop`` that signed in through
    the browser holds only a listener key, which ``bin/headers`` left in the plugin's data folder under
    its pid. The script finds it there, registers the call for the room that session is in, and the key
    it made is the one the call's routes then accept. A second try for the same room is refused in the
    server's words. Fails if the request the script sends is not the one the route takes, or the key it
    made is not the registered one."""
    from switchboard.meeting import keys as call_keys

    w = World(make_tmp_home(), FakeClock())
    claude = Host(w.b, listener_key=LISTENER)
    m = machine
    try:
        assert (await claude.join("#build", "claude-1"))["ok"]
        (m.data / f"listener-key-{CLAUDE_PID}").write_text(LISTENER + "\n")
        env = {k: v for k, v in m.env().items() if k != "SWITCHBOARD_AGENT_TOKEN"}
        r = m.run(["start", "--server", w.b.base, "--data", str(m.data), "build"], env=env)
        assert r.returncode == 0, r.stderr
        row = w.hosted_row()
        [call] = m.call_dirs()
        key = (call / "key").read_text().strip()
        assert row["key_hash"] == call_keys.key_hash(key) and (row["state"], row["platform"]) == (
            "joining",
            "zoom",
        )
        assert (row["host"], row["model"], row["source"]) == ("alice-laptop", "base", "hosted")
        fp = call_keys.fingerprint(row["key_hash"])
        assert f"Key fingerprint: {fp}" in r.stdout and w.settings()["meeting"]["fingerprint"] == fp
        assert Relay(w.b, key).push([]).status_code == 200  # the key the script made is the one it registered
        running = {
            **env,
            "FAKE_DOCKER_RUNNING": f"switchboard-call-{call.name}",
        }  # the first call is still up
        again = m.run(["start", "--server", w.b.base, "--data", str(m.data), "build"], env=running)
        assert again.returncode == 1 and "already has a call" in again.stderr, again.stderr
        assert [p.name for p in m.call_dirs()] == [call.name]  # and the refused try left no folder behind
    finally:
        await claude.close()
        w.stop()


def test_the_script_is_executable_and_has_its_windows_twin() -> None:
    assert os.access(PLUGIN / "bin" / "meeting-host", os.X_OK)
    for p in ("bin/meeting-host.cmd", "bin/windows/meeting-host.ps1", "skills/host-meeting-bot/SKILL.md"):
        assert (PLUGIN / p).is_file(), p
    text = (PLUGIN / "bin" / "windows" / "meeting-host.ps1").read_bytes()
    assert all(b < 128 for b in text), "Windows PowerShell 5.1 reads a file without a byte-order mark as ANSI"
