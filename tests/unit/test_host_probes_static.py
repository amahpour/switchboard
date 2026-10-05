"""No direct probe of a participant's process outside ``broker/hosts.py`` (DESIGN.md §27.5.6).

A remote participant's pids are pids on its own host; looking one up in this
machine's process table asks about some unrelated local process (and could end,
or vouch for, the wrong session). So the broker asks ``HostViews.view(p.host)``.
This AST scan keeps it that way: in the scanned modules, ``proc.alive``,
``proc.ancestry``, ``proc.ancestry_to_root``, ``proc.info``, ``proc.argv_many``,
``proc.argv`` and ``read_registry`` may not appear at all (called, passed as a
function, or imported by name), except in the (file, function) pairs in
``ALLOWED``, each with its reason: code about this machine's own processes (a
local socket's kernel peer, the broker's pidfile, this machine's Codex daemon),
never about a participant row.

Scanned: every module of the package except ``broker/hosts.py`` (the views
themselves) and ``broker/proc.py`` (the process table); the four the build spec
names (``broker/agents.py``, ``adapters/claude.py``, ``store.py``,
``broker/app.py``) are among them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "src" / "switchboard"
PROBES = frozenset({"alive", "ancestry", "ancestry_to_root", "info", "argv_many", "argv"})
REGISTRY = "read_registry"
MUST_SCAN = ("broker/agents.py", "adapters/claude.py", "store.py", "broker/app.py")
EXEMPT = {
    "broker/hosts.py": "the host views themselves",
    "broker/proc.py": "the process table itself",
}
# (file, function) pairs that may probe, and why: each is about a process of this
# machine that is not a participant row. A participant's own probes (hook_event,
# _remember_model, check_liveness, CodexAdapter.live, ...) go through HostViews,
# so none of those is here.
ALLOWED: dict[tuple[str, str], str] = {
    ("broker/peer.py", "Peer.from_socket"): "the kernel peer of a connection to this broker's own socket",
    ("broker/peer.py", "ProcessPeerPolicy.__init__"): "default chain and argv readers for a local human peer",
    ("broker/peer.py", "AllowAllHumans.__init__"): "default chain reader for a local peer (test policy)",
    ("broker/peer.py", "verify_mcp_peer"): "a local MCP server's own ancestry (its kernel peer's chain)",
    ("broker/peer.py", "claude_registry_socket"): "the registry file of an MCP server's own parent Claude on"
    " the machine it runs on (verify_mcp_peer, the MCP server's"
    " own guard), never a participant row's pid",
    ("broker/daemon.py", "_write_pidfile"): "the broker's own pid",
    ("broker/daemon.py", "_pid_matches"): "the pidfile's broker process",
    ("mcp/server.py", "_parent_argv"): "the MCP server's own parent process",
    (
        "adapters/codex.py",
        "CodexAdapter.refresh_clients",
    ): "lsof peers of this machine's Codex control socket, and agent pids of local rows only (_joined)",
    (
        "adapters/codex.py",
        "_codex_app_server",
    ): "this machine's Codex app-servers, named by lsof or by a local mcp.hello",
    # the satellite (M8c) is the remote host's own view: it runs there and probes only the
    # machine it runs on, which is exactly what the broker's RemoteView of that host relays
    ("remote/satellite.py", "_is_satellite"): "an older satellite of this home, before taking it over, or the"
    " newer one its replace marker names",
    ("remote/satellite.py", "Satellite.__init__"): "the satellite's own start time",
    ("remote/satellite.py", "main"): "the satellite's own start time, for its pidfile",
    ("remote/satellite.py", "Satellite.chain"): "its own kernel peer's chain (a hook on its own machine)",
    ("remote/satellite.py", "Satellite.send_alive"): "the watched pids, which are pids on its own machine",
    (
        "remote/satellite.py",
        "Satellite.claude_status",
    ): "a watched Claude on its own machine: that pid, and its"
    " registry file in this home's sessions dir (M8d)",
    ("remote/satellite.py", "exposure"): "the satellite's own ancestors (sshd's session process), which hold"
    " the far ends of its stdio (M8e)",
    ("remote/satellite.py", "Satellite.last_mile"): "the Codex process a local MCP server was attested under,"
    " on its own machine, before relaying its wake (issue #63)",
    # a remote Codex wake (issue #63) runs in the MCP server on the thread's own machine
    ("mcp/codex_wake.py", "tui_attached"): "lsof peers of that machine's own Codex control socket",
    # the dialer (§31.7) runs on the machine that dials in and looks only at its own pidfile
    ("remote/dialer.py", "running_pid"): "the pidfile's dialer process, on the machine it runs on",
    ("remote/dialer.py", "main"): "the dialer's own start time, for its pidfile",
}


def scanned_files() -> list[str]:
    out = []
    for p in sorted(PKG.rglob("*.py")):
        rel = p.relative_to(PKG).as_posix()
        if rel not in EXEMPT:
            out.append(rel)
    return out


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []  # (line, function, what)
        self.stack: list[str] = []
        self.proc_names = {"proc"}  # names bound to the switchboard.broker.proc module
        self.claude_names: set[str] = set()  # names bound to the claude_registry (or adapters.claude) module

    def _func(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _hit(self, node: ast.AST, what: str) -> None:
        self.hits.append((getattr(node, "lineno", 0), self._func(), what))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name.endswith("broker.proc"):
                self.proc_names.add(a.asname or a.name)
            if a.name.endswith(("adapters.claude", "claude_registry")) and a.asname:
                self.claude_names.add(a.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for a in node.names:
            if mod.endswith("broker") and a.name == "proc":
                self.proc_names.add(a.asname or "proc")
            if mod.endswith("adapters") and a.name == "claude":
                self.claude_names.add(a.asname or "claude")
            if a.name == "claude_registry":
                self.claude_names.add(a.asname or "claude_registry")
            if mod.endswith("broker.proc") and a.name in PROBES:
                self._hit(node, f"from {mod} import {a.name}")
            if a.name == REGISTRY:
                self._hit(node, f"from {mod} import {REGISTRY}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            if node.value.id in self.proc_names and node.attr in PROBES:
                self._hit(node, f"proc.{node.attr}")
            if node.value.id in self.claude_names and node.attr == REGISTRY:
                self._hit(node, f"{node.value.id}.{REGISTRY}")
        else:
            dotted = _dotted(node)  # switchboard.broker.proc.alive, switchboard.adapters.claude.read_registry
            if dotted and (
                (dotted.endswith(".broker.proc." + node.attr) and node.attr in PROBES)
                or dotted.endswith((".adapters.claude." + REGISTRY, ".claude_registry." + REGISTRY))
            ):
                self._hit(node, dotted)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == REGISTRY and isinstance(node.ctx, ast.Load):
            self._hit(node, REGISTRY)

    def visit_Call(self, node: ast.Call) -> None:
        # getattr(proc, "alive") and friends
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in self.proc_names
        ):
            self._hit(node, "getattr(proc, ...)")
        self.generic_visit(node)


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _hits(source: str, rel: str) -> list[tuple[int, str, str]]:
    s = _Scan()
    s.visit(ast.parse(source, filename=rel))
    return s.hits


def direct_probes(source: str, rel: str) -> list[str]:
    return [
        f"{rel}:{line} in {func}: {what}"
        for line, func, what in _hits(source, rel)
        if (rel, func) not in ALLOWED
    ]


def test_no_direct_participant_probes_outside_hosts() -> None:
    files = scanned_files()
    assert set(MUST_SCAN) <= set(files)
    assert all((PKG / rel).is_file() for rel in EXEMPT)  # no stale exemption
    hits = []
    for rel in files:
        hits += direct_probes((PKG / rel).read_text(encoding="utf-8"), rel)
    assert hits == []


def test_every_allowed_function_still_probes() -> None:
    """No stale allowance: each (file, function) in ALLOWED still has a probe, so a
    renamed function can't leave a hole that a new function of the old name fills."""
    for rel, func in ALLOWED:
        assert rel in scanned_files()
        funcs = {f for _l, f, _w in _hits((PKG / rel).read_text(encoding="utf-8"), rel)}
        assert func in funcs, (rel, func)


def test_the_scan_sees_probes_where_they_belong() -> None:
    """Control: the host views and the process table do probe (so the scanner works)."""
    assert direct_probes((PKG / "broker/hosts.py").read_text(encoding="utf-8"), "broker/hosts.py")
    assert _hits((PKG / "broker/peer.py").read_text(encoding="utf-8"), "broker/peer.py")


def _with_line(rel: str, anchor: str, line: str) -> str:
    """``rel``'s source with ``line`` inserted (same indentation) before the first line containing anchor."""
    src = (PKG / rel).read_text(encoding="utf-8").splitlines()
    for i, text in enumerate(src):
        if anchor in text:
            indent = text[: len(text) - len(text.lstrip())]
            return "\n".join([*src[:i], indent + line, *src[i:]]) + "\n"
    raise AssertionError(f"{anchor!r} not in {rel}")


@pytest.mark.parametrize(
    ("rel", "anchor", "line"),
    [
        # the regression the design names: a liveness check straight on the desktop's process table
        (
            "broker/agents.py",
            "if self.engine.adapter(p).defer_end(p):",
            "if not proc.alive(p.agent_pid, p.agent_start): pass",
        ),
        (
            "broker/agents.py",
            "if self._same_mcp(existing, mc.ident):",
            "proc.ancestry(existing.agent_pid, 8)",
        ),
        ("broker/app.py", "state.recovery = ", "state.store.recover_on_start(proc.alive)"),
        (
            "adapters/claude.py",
            "data = view_of.read_registry(p.agent_pid)",
            "data = read_registry(self.cfg.claude.sessions_dir, p.agent_pid)",
        ),
        ("store.py", "def recover_on_start(", "from switchboard.broker.proc import alive"),
        # the review's finds: CodexAdapter.live on the desktop's process table, and a hook
        # resolver that falls back to this machine's argv for another host's chain
        (
            "adapters/codex.py",
            "if not p.agent_pid or not self._local_view().alive(p.agent_pid, p.agent_start):",
            "if not proc.alive(p.agent_pid, p.agent_start): pass",
        ),
        (
            "adapters/codex.py",
            'self._rebind(p, o, (ident.agent_pid, ident.agent_start), "mcp_hello")',
            "proc.alive(ident.agent_pid, ident.agent_start)",
        ),
        (
            "broker/peer.py",
            "sid_key = session_key(harness, host, sid) if sid else None",
            "argv_fn = argv_fn or proc.argv_many",
        ),
    ],
)
def test_a_direct_probe_added_back_is_caught(rel: str, anchor: str, line: str) -> None:
    assert direct_probes((PKG / rel).read_text(encoding="utf-8"), rel) == []
    hits = direct_probes(_with_line(rel, anchor, line), rel)
    assert len(hits) == 1, hits


def test_aliases_and_getattr_are_caught() -> None:
    src = (
        "import switchboard.broker.proc as P\n"
        "from switchboard.broker import proc as pr\n"
        "from switchboard.adapters import claude as cl\n"
        "def f(p):\n"
        "    P.alive(p.agent_pid, None)\n"
        "    pr.info(p.agent_pid)\n"
        "    getattr(pr, 'alive')(1, 2)\n"
        "    cl.read_registry('d', p.agent_pid)\n"
        "    g = pr.argv_many\n"
        "    import switchboard.broker.proc\n"
        "    switchboard.broker.proc.ancestry(p.agent_pid)\n"
        "    switchboard.adapters.claude.read_registry('d', 1)\n"
        "    from switchboard import claude_registry as cr\n"
        "    cr.read_registry('d', p.agent_pid)\n"
        "    switchboard.claude_registry.read_registry('d', 1)\n"
        "    from switchboard.claude_registry import read_registry\n"
    )
    hits = direct_probes(src, "x.py")
    assert len(hits) == 10, hits
    # a host view's own method of the same name is the sanctioned route
    assert direct_probes("def f(v, p):\n    v.read_registry(p.agent_pid)\n    v.alive(1, 2)\n", "x.py") == []
