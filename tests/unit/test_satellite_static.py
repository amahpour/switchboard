"""The satellite executes nothing and opens no network socket; the broker's remote
links have one spawn site (DESIGN.md §27.4.8, §27.12 never-do 13 and 17)."""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "switchboard"
# the satellite and everything under remote/ that it runs (proto, config, describe)
NO_SPAWN = ("remote/satellite.py", "remote/proto.py", "remote/config.py", "remote/describe.py")
SPAWN_MODULES = frozenset({"subprocess", "pty", "pexpect", "multiprocessing", "tmux", "libtmux"})
SPAWN_OS = frozenset({"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
                      "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty", "execl",
                      "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"})
SPAWN_ASYNCIO = frozenset({"create_subprocess_exec", "create_subprocess_shell", "subprocess_exec",
                           "subprocess_shell"})
NETWORK = frozenset({"AF_INET", "AF_INET6", "AF_PACKET", "SOCK_DGRAM", "SOCK_RAW", "create_connection",
                     "start_server", "create_server", "getaddrinfo", "gethostbyname", "create_datagram_endpoint"})


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


def test_the_scanner_sees_what_it_should() -> None:
    bad = {
        "import subprocess": 1, "import pty": 1, "from subprocess import run": 1, "os.system('x')": 1,
        "os.execv('/bin/sh', [])": 1, "from os import fork": 1, "asyncio.create_subprocess_exec('x')": 1,
        "socket.socket(socket.AF_INET)": 1, "asyncio.open_connection(host='x', port=1)": 1,
        "asyncio.open_connection('x', 1)": 1, "asyncio.open_connection(sock=s, limit=9)": 0,
        "loop.create_connection(p, 'h', 1)": 1, "x = 'tmux new'": 1, "os.getpid()": 0,
    }
    for src, n in bad.items():
        assert len(problems(src)) == n, src


def _spawn_calls(source: str) -> list[str]:
    calls = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            d = _dotted(node.func)
            name = d.rsplit(".", 1)[-1]
            if name in SPAWN_ASYNCIO or name in {"Popen", "run", "call", "check_call", "check_output"} and \
                    d.startswith("subprocess") or (d.startswith("os.") and name in SPAWN_OS):
                calls.append(d)
    return calls


def test_broker_remote_has_one_spawn_site() -> None:
    src = (PKG / "broker" / "remote.py").read_text()
    assert _spawn_calls(src) == ["asyncio.create_subprocess_exec"]
    tree = ast.parse(src)
    # no shell anywhere, and the child's argv is built by one function
    assert "create_subprocess_shell" not in src and "shell=True" not in src
    fns = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
           and any(isinstance(c, ast.Call) and _dotted(c.func) == "asyncio.create_subprocess_exec"
                   for c in ast.walk(n))]
    assert fns == ["_spawn"]
