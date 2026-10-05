"""The satellite executes nothing and opens no network socket; the broker's remote
links have one spawn site (DESIGN.md §27.4.8, §27.12 never-do 13 and 17)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from conftest import child_env

PKG = Path(__file__).resolve().parents[2] / "src" / "switchboard"
# the satellite and everything under remote/ that it runs (proto, config, describe), and the
# Claude registry reader it shares with the broker (M8d)
NO_SPAWN = (
    "remote/satellite.py",
    "remote/proto.py",
    "remote/config.py",
    "remote/describe.py",
    "claude_registry.py",
)
SPAWN_MODULES = frozenset({"subprocess", "pty", "pexpect", "multiprocessing", "tmux", "libtmux"})
SPAWN_OS = frozenset(
    {
        "system",
        "popen",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "fork",
        "forkpty",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
    }
)
SPAWN_ASYNCIO = frozenset(
    {"create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell"}
)
NETWORK = frozenset(
    {
        "AF_INET",
        "AF_INET6",
        "AF_PACKET",
        "SOCK_DGRAM",
        "SOCK_RAW",
        "create_connection",
        "start_server",
        "create_server",
        "getaddrinfo",
        "gethostbyname",
        "create_datagram_endpoint",
    }
)


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def problems(source: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in SPAWN_MODULES:
                    out.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in SPAWN_MODULES:
                out.append(f"from {node.module} import")
            for a in node.names:
                if (mod == "os" and a.name in SPAWN_OS) or a.name in SPAWN_ASYNCIO or a.name in NETWORK:
                    out.append(f"from {node.module} import {a.name}")
        elif isinstance(node, ast.Attribute):
            d = _dotted(node)
            if d.startswith("os.") and node.attr in SPAWN_OS:
                out.append(d)
            if node.attr in SPAWN_ASYNCIO:
                out.append(d)
            if node.attr in NETWORK:
                out.append(d)
            if d.startswith(("subprocess.", "pty.")):
                out.append(d)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "tmux" in node.value:
            out.append("a tmux string")
        if isinstance(node, ast.Call) and _dotted(node.func).endswith("open_connection"):
            # only over an fd it already has (sock=): never a host and port
            if node.args or any(k.arg != "sock" and k.arg != "limit" for k in node.keywords):
                out.append("open_connection to an address")
    return out


def test_satellite_and_proto_have_no_spawn_pty_or_network() -> None:
    for rel in NO_SPAWN:
        src = (PKG / rel).read_text()
        assert problems(src) == [], rel


# A machine that dials its broker (DESIGN.md §31.7): the network lives in the dialer, which
# spawns nothing; the keys and handshake module shared by both ends does neither.
def test_the_dialer_spawns_nothing_and_the_link_keys_touch_no_network() -> None:
    dialer = (PKG / "remote" / "dialer.py").read_text()
    assert _spawn_calls(dialer) == []
    assert not [
        p
        for p in problems(dialer)
        if not p.startswith(("open_connection", "socket."))
        and "create_connection" not in p
        and "getaddrinfo" not in p
    ], problems(dialer)
    assert "shell=True" not in dialer and "subprocess" not in dialer
    assert problems((PKG / "remote" / "linkkey.py").read_text()) == []
    # the satellite never imports the dialer, the websockets client or truststore
    sat = (PKG / "remote" / "satellite.py").read_text()
    for mod in ("websockets", "truststore", "remote.dialer", "remote.join"):
        assert (
            f"import {mod}" not in sat and f"from switchboard.{mod}" not in sat and f"from {mod}" not in sat
        )


def test_the_scanner_sees_what_it_should() -> None:
    bad = {
        "import subprocess": 1,
        "import pty": 1,
        "from subprocess import run": 1,
        "os.system('x')": 1,
        "os.execv('/bin/sh', [])": 1,
        "from os import fork": 1,
        "asyncio.create_subprocess_exec('x')": 1,
        "socket.socket(socket.AF_INET)": 1,
        "asyncio.open_connection(host='x', port=1)": 1,
        "asyncio.open_connection('x', 1)": 1,
        "asyncio.open_connection(sock=s, limit=9)": 0,
        "loop.create_connection(p, 'h', 1)": 1,
        "x = 'tmux new'": 1,
        "os.getpid()": 0,
    }
    for src, n in bad.items():
        assert len(problems(src)) == n, src


def _spawn_calls(source: str) -> list[str]:
    calls = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            d = _dotted(node.func)
            name = d.rsplit(".", 1)[-1]
            if (
                name in SPAWN_ASYNCIO
                or name in {"Popen", "run", "call", "check_call", "check_output"}
                and d.startswith("subprocess")
                or (d.startswith("os.") and name in SPAWN_OS)
            ):
                calls.append(d)
    return calls


def test_broker_remote_has_one_spawn_site() -> None:
    src = (PKG / "broker" / "remote.py").read_text()
    assert _spawn_calls(src) == ["asyncio.create_subprocess_exec"]
    tree = ast.parse(src)
    # no shell anywhere, and the child's argv is built by one function
    assert "create_subprocess_shell" not in src and "shell=True" not in src
    fns = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            isinstance(c, ast.Call) and _dotted(c.func) == "asyncio.create_subprocess_exec"
            for c in ast.walk(n)
        )
    ]
    assert fns == ["_spawn"]


def test_the_satellite_loads_no_adapter() -> None:
    """The satellite reads Claude's registry through the leaf ``claude_registry`` module, so
    none of the broker's adapters (Codex's app-server client and the rest) is loaded into
    it (M8d review): what runs on the remote machine stays what §27.4.8 lists."""
    code = (
        "import sys, switchboard.remote.satellite, switchboard.config; "
        "print(sorted(m for m in sys.modules if m.startswith(('switchboard.adapters', 'switchboard.delivery',"
        " 'switchboard.broker.agents', 'switchboard.broker.rpc', 'switchboard.broker.remote',"
        " 'switchboard.remote.pairing', 'switchboard.install', 'switchboard.remote.dialer', 'websockets',"
        " 'truststore'))))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env(),
        check=True,
    )
    assert out.stdout.strip() == "[]", out.stdout


def test_pairing_runs_only_the_system_ssh_tools() -> None:
    """``remote add|remove|doctor`` run ``/usr/bin/ssh`` and ``/usr/bin/ssh-keygen`` and nothing
    else, from one function that checks its argv[0], with no shell (DESIGN.md §27.5.8)."""
    src = (PKG / "remote" / "pairing.py").read_text()
    tree = ast.parse(src)
    calls = [
        (fn.name, _dotted(c.func))
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and _dotted(c.func) in _spawn_calls(src)
    ]
    assert calls == [("_run", "subprocess.run")], calls
    assert "shell=True" not in src and "os.system" not in src
    import pytest as _pytest

    from switchboard.remote import pairing

    for bad in (["/bin/sh", "-c", "id"], ["ssh", "-G", "x"], ["/usr/bin/scp"], []):
        with _pytest.raises(pairing.PairingError):
            pairing._run(bad)
