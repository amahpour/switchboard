#!/usr/bin/env python3
"""Scripted stand-ins for the two-host tests and the fake-remote demo (DESIGN.md §27.13 T2, T3).

Not a test module (pytest never collects it): the ``twohost`` tests run it inside the
containers (``/opt/switchboard/bin/python /opt/switchboard-src/tests/twohost/actors.py``),
and ``tests/live/m8_demo.py --scripted`` runs its ``bench`` inside the fake remote. It
needs only an installed switchboard (and its ``mcp`` dependency) plus this repo's
``tests/fakes`` and ``tests/fixtures``. Every command prints JSON lines on stdout.

``bench``: the remote machine's agent, following docs/DEMO-FPGA.md's bench prompt by
    script. ``--mode inbox`` (default) is a stand-in Claude Code session
    (``fakes/fake_harness.py`` copied to a path ending in ``/claude``, a fake inbox
    socket, a session registry in the home's Claude sessions dir), so the satellite
    attests it and the broker wakes it through its inbox (``claude:inbox``), the path a
    real Claude takes; ``--mode wait`` is a plain MCP client that loops on ``wait()``
    (an ``unknown`` harness, ``mcp-only``). For each ``artifact:`` line that mentions it,
    it pulls (``via:pull``), checks the sha256 and never flashes on a mismatch, runs
    ``openFPGALoader``, then the UART test, and answers one ``result:`` line with
    ``reply_to``. Close its stdin (or send ``{"op": "quit"}``) to stop it.
``agent``: any member, driven by JSON commands on stdin (``say``, ``read``, ``wait``,
    ``who``, ``quit``); ``--test-session`` makes it a ``test`` harness (a test-mode
    broker on the same machine only).
``build``: write a stand-in bitstream (random bytes; ``--broken`` adds the marker the
    fake board fails on) and print its path, sha256 and size.
``rpc``: one request on a home's socket, the answer printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

from fakes.fake_claude_inbox import FakeInbox  # noqa: E402  (stdlib only)

FAKE_HARNESS = TESTS / "fakes" / "fake_harness.py"
PAYLOADS = TESTS / "fixtures" / "payloads" / "claude"

# docs/DEMO-FPGA.md §5, DESIGN.md §27.9
ART_RE = re.compile(
    r"artifact: (?P<rel>[A-Za-z0-9_./-]+) sha256:(?P<sha>[0-9a-f]{64}) size:(?P<size>\d+)"
    r" board:(?P<board>[A-Za-z0-9_.-]+)(?: via:(?P<via>push|pull))?"
)
ITEM_RE = re.compile(r"^- id=(?P<id>\d+) .*?from=(?P<from>\S+) .*?text=(?P<text>\".*\")\s*$", re.M)
UART_RE = re.compile(r"^(PASS|FAIL) (\d+)/(\d+)")
ID_RE = re.compile(r"^- id=(\d+) ", re.M)  # every item, a "not shown here" stub included


_EMIT = threading.Lock()


def emit(**kw: Any) -> None:
    with _EMIT:  # the inbox watcher emits from its own thread
        print(json.dumps(kw), flush=True)


def watch_inbox(c: "StandInClaude", stop: threading.Event) -> None:
    """Every frame the broker offers the inbox, when it arrives: ``ev=inbox``, from its own
    thread, so a frame that arrives while the session is in an approval prompt (its main
    loop asleep in ``--hold-s``) is seen at once (m8_demo's hold check)."""
    seen = 0
    while not stop.is_set():
        frames = c.inbox.frames()
        for _conn, frame in frames[seen:]:
            body = frame["message"]["content"]
            emit(
                ev="inbox",
                t=time.time(),
                head=body.splitlines()[0][:200] if body else "",
                ids=[int(x) for x in ID_RE.findall(body)],
            )
        seen = len(frames)
        time.sleep(0.05)


def base_env(**extra: str) -> dict[str, str]:
    """A clean child env: no harness variables from whoever started us (§0)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "SWITCHBOARD_TEST")
    }
    env.setdefault("LANG", "C.UTF-8")
    env.update(extra)
    return env


def items(text: str) -> list[dict[str, Any]]:
    """The message items of a rendered batch (``- id=… from=… text="…"``)."""
    out = []
    for m in ITEM_RE.finditer(text or ""):
        try:
            body = json.loads(m.group("text"))
        except ValueError:
            continue
        out.append({"id": int(m.group("id")), "from": m.group("from"), "text": body})
    return out


# ------------------------------------------------------------------ the bench's work
class Bench:
    """What docs/DEMO-FPGA.md's bench prompt asks, for one ``artifact:`` line."""

    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.drop = Path(os.path.expanduser(a.drop))
        self.done: set[int] = set()
        self.before_flash: Any = None  # the stand-in Claude's approval prompt (--hold-s)

    def jobs_in(self, found: list[dict[str, Any]]) -> list[tuple[int, re.Match[str]]]:
        jobs = []
        for it in found:
            m = ART_RE.search(it["text"])
            if m is None or it["id"] in self.done or f"@{self.a.name}" not in it["text"]:
                continue
            self.done.add(it["id"])
            jobs.append((it["id"], m))
        return jobs

    def run(self, m: re.Match[str]) -> tuple[str, dict[str, Any]]:
        t0 = time.monotonic()
        rel, want = m.group("rel"), m.group("sha")
        facts: dict[str, Any] = {"rel": rel, "sha256": want}
        # docs/DEMO-FPGA.md §5's bench rule: relative, and no part starting with a dot
        if rel.startswith("/") or any(not p or p.startswith(".") for p in rel.split("/")):
            return f'result: {want[:12]} verify=fail note="bad path"', facts
        pull = "skip"
        if m.group("via") == "pull":
            pull = "ok" if self.pull(rel) else "fail"
            if pull == "fail":
                return f"result: {want[:12]} pull=fail verify=skip flash=skip uart=skip t=0s", facts
        path = self.drop / rel
        try:
            got = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            got = None
        facts["got"] = got
        if got != want:
            return f"result: {want[:12]} pull={pull} verify=fail flash=skip uart=skip t=0s", facts
        if self.before_flash is not None:
            self.before_flash()
        fl = subprocess.run(
            ["openFPGALoader", "-b", self.a.board, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            env=base_env(),
        )
        flash = "ok" if fl.returncode == 0 else "fail"
        facts["flash_out"] = fl.stdout.strip().splitlines()[-1:] + fl.stderr.strip().splitlines()[-1:]
        uart, uart_lines = "skip", []
        if flash == "ok":
            ut = subprocess.run(
                [
                    sys.executable if self.a.uart_python is None else self.a.uart_python,
                    os.path.expanduser(self.a.uart_test),
                    os.path.expanduser(self.a.tty),
                    str(self.a.baud),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=base_env(),
            )
            uart_lines = ut.stdout.strip().splitlines()
            v = UART_RE.match(uart_lines[0]) if uart_lines else None
            uart = (
                f"pass({v.group(2)}/{v.group(3)})"
                if v and v.group(1) == "PASS"
                else f"fail({v.group(2)}/{v.group(3)})"
                if v
                else "fail"
            )
        dt = time.monotonic() - t0
        text = f"result: {got[:12]} pull={pull} verify=ok flash={flash} uart={uart} t={dt:.0f}s"
        if uart_lines[1:]:
            text += "\n" + "\n".join(uart_lines[1:11])
        facts.update(flash=flash, uart=uart)
        return text, facts

    def pull(self, rel: str) -> bool:
        """``rsync -t <pull source>:<rel> <drop>/<rel>`` with the ssh command given (a key
        whose line on the desktop is ``restrict,command="rrsync -ro …"``)."""
        if not self.a.pull_src or not self.a.pull_ssh:
            return False
        dest = self.drop / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["rsync", "-t", "-e", self.a.pull_ssh, f"{self.a.pull_src}:{rel}", str(dest)],
            capture_output=True,
            text=True,
            timeout=60,
            env=base_env(),
        )
        emit(ev="pull", rc=r.returncode, err=r.stderr.strip()[-300:])
        return r.returncode == 0


def stdin_closed() -> threading.Event:
    """Set when stdin ends or says quit: how the tests stop a long-running actor."""
    ev = threading.Event()

    def watch() -> None:
        for line in sys.stdin:
            if line.strip() == '{"op": "quit"}' or '"quit"' in line:
                break
        ev.set()

    threading.Thread(target=watch, daemon=True).start()
    return ev


# ----------------------------------------------------------- a stand-in Claude session
def fixture(name: str, sid: str, **over: Any) -> dict[str, Any]:
    d = json.loads((PAYLOADS / f"{name}.json").read_text())
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    d["session_id"] = sid
    d.update(over)
    return d


class StandInClaude:
    """``fakes/fake_harness.py`` as ``…/claude``: it writes ``<sessions>/<pid>.json`` naming
    its inbox socket, runs ``switchboard mcp`` as its child and hook commands as its
    children, so the satellite's kernel checks see a Claude session (§27.5.3)."""

    def __init__(self, home: str, sessions: str):
        from switchboard.install.common import hook_command
        from switchboard.paths import hook_sha12

        self.home, self.sessions = home, sessions
        self._hook_command = lambda ev: hook_command(sys.executable, home, hook_sha12(), "claude", ev)
        self.dir = Path(tempfile.mkdtemp(prefix="yk-bench-"))
        exe = self.dir / "claude"
        shutil.copy(FAKE_HARNESS, exe)
        self.inbox_path = str(self.dir / "inbox.sock")
        self.inbox = FakeInbox(self.inbox_path)
        self.sid = str(uuid.uuid4())
        env = base_env(
            YK_FAKE_HOME=home,
            YK_FAKE_SESSIONS=sessions,
            CLAUDECODE="1",
            CLAUDE_CODE_MESSAGING_SOCKET=self.inbox_path,
            CLAUDE_CODE_MESSAGING_TOKEN=secrets.token_hex(16),
            CLAUDE_CODE_SESSION_ID=self.sid,
        )
        self.p = subprocess.Popen(
            [sys.executable, str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True
        )
        hello = self.recv()
        self.pid = hello["pid"]

    def recv(self) -> dict[str, Any]:
        assert self.p.stdout is not None
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("the stand-in Claude exited")
        return json.loads(line)

    def send(self, obj: dict[str, Any]) -> dict[str, Any]:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()
        return self.recv()

    def tool(self, name: str, **args: Any) -> dict[str, Any]:
        return self.send({"op": "tool", "name": name, "args": args})["result"]

    def hook(self, name: str, **over: Any) -> str:
        payload = fixture(name, self.sid, **over)
        r = self.send(
            {"op": "hook", "command": self._hook_command(payload["hook_event_name"]), "payload": payload}
        )
        return r.get("stdout", "")

    def set_registry(self, status: str) -> None:
        p = Path(self.sessions) / f"{self.pid}.json"
        d = json.loads(p.read_text())
        d["status"], d["statusUpdatedAt"] = status, int(time.time() * 1000)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(p)

    def close(self) -> None:
        if self.p.poll() is None:
            try:
                self.send({"op": "quit"})
            except Exception:
                pass
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self.inbox.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def default_sessions(home: str) -> str:
    from switchboard.config import load
    from switchboard.paths import Paths

    return os.path.expanduser(str(load(Paths.from_home(home)).claude.sessions_dir))


def bench_inbox(a: argparse.Namespace) -> int:
    stop = stdin_closed()
    work = Bench(a)
    c = StandInClaude(a.home, os.path.expanduser(a.sessions) if a.sessions else default_sessions(a.home))
    if a.hold_s > 0:

        def ask_to_flash() -> None:
            """Claude asks before running openFPGALoader: its registry says ``waiting`` while
            the prompt is open (the broker holds deliveries); the owner approves after
            ``--hold-s`` seconds."""
            c.set_registry("waiting")
            emit(ev="waiting", t=time.time())
            time.sleep(a.hold_s)
            c.set_registry("busy")
            emit(ev="approved", t=time.time())

        work.before_flash = ask_to_flash
    try:
        j = c.tool("join", room=a.room, screen_name=a.name)
        emit(ev="joined", ok=bool(j.get("ok")), tier=j.get("tier"), pid=c.pid, error=j.get("error"))
        if not j.get("ok"):
            return 1

        def hook(name: str, **over: Any) -> list[dict[str, Any]]:
            """Run a hook; what it hands the session mid-turn (``additionalContext``: messages
            that arrived while it was busy) is read as Claude would read it."""
            out = c.hook(name, **over).strip()
            if not out:
                return []
            try:
                ctx = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")
            except (ValueError, AttributeError):
                return []
            emit(
                ev="context",
                hook=name,
                head=ctx.splitlines()[0][:200] if ctx else "",
                ids=[int(x) for x in ID_RE.findall(ctx)],
            )
            got = items(ctx)
            if "not shown here" in ctx:
                got += items(c.tool("read", room=a.room).get("text", ""))
            return got

        # a turn ends after the join: the session is idle, its hooks seen (§27.7)
        hook("PostToolUse_mcp")
        hook("Stop")
        threading.Thread(target=watch_inbox, args=(c, stop), daemon=True).start()
        seen = 0
        jobs = 0
        while not stop.is_set() and not (a.jobs and jobs >= a.jobs):
            frames = c.inbox.frames()
            if len(frames) <= seen:
                time.sleep(0.05)
                continue
            _conn, frame = frames[seen]
            seen += 1
            body = frame["message"]["content"]
            # Claude starts a turn from the frame: UserPromptSubmit carries its batch token
            pending = hook("UserPromptSubmit", prompt=body)
            c.set_registry("busy")
            emit(
                ev="woken",
                via="inbox",
                t=time.time(),
                head=body.splitlines()[0][:200],
                ids=[int(x) for x in ID_RE.findall(body)],
            )
            pending += items(body)
            pending += items(c.tool("read", room=a.room).get("text", ""))
            pending += hook("PostToolUse_mcp")
            while True:  # the turn goes on while there is work, as a session's would
                todo = work.jobs_in(pending)
                pending = []
                if not todo:
                    break
                for mid, m in todo:
                    emit(ev="job", id=mid, t=time.time())
                    text, facts = work.run(m)
                    res = c.tool("say", room=a.room, text=text, reply_to=mid)
                    pending += hook("PostToolUse_mcp")
                    jobs += 1
                    emit(ev="result", reply_to=mid, text=text, ok=bool(res.get("ok")), facts=facts)
            hook("Stop")
            c.set_registry("idle")
        return 0
    finally:
        c.close()


async def bench_wait(a: argparse.Namespace) -> int:
    import mcp.types as mt
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stop = stdin_closed()
    work = Bench(a)
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "switchboard", "mcp", "--home", a.home], env=base_env()
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, client_info=mt.Implementation(name="bench-standin", version="1")) as s:
            await s.initialize()

            async def call(tool: str, **args: Any) -> dict[str, Any]:
                res = await s.call_tool(tool, args)
                return json.loads(res.content[0].text)  # type: ignore[union-attr]

            j = await call("join", room=a.room, screen_name=a.name)
            emit(ev="joined", ok=bool(j.get("ok")), tier=j.get("tier"), pid=os.getpid(), error=j.get("error"))
            if not j.get("ok"):
                return 1
            jobs = 0
            while not stop.is_set() and not (a.jobs and jobs >= a.jobs):
                res = await call("wait", room=a.room, timeout_s=a.wait_s)
                text = res.get("text") or ""
                found = items(text)
                if found:
                    emit(ev="woken", via="wait", t=time.time(), ids=[x["id"] for x in found])
                if "not shown here" in text:
                    found += items((await call("read", room=a.room)).get("text", ""))
                for mid, m in work.jobs_in(found):
                    emit(ev="job", id=mid, t=time.time())
                    out, facts = work.run(m)
                    said = await call("say", room=a.room, text=out, reply_to=mid)
                    jobs += 1
                    emit(ev="result", reply_to=mid, text=out, ok=bool(said.get("ok")), facts=facts)
                if found and not jobs:
                    await call("pass", room=a.room)
            return 0


# ------------------------------------------------------------------- a plain member
async def agent(a: argparse.Namespace) -> int:
    import mcp.types as mt
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = ["-I", "-m", "switchboard", "mcp", "--home", a.home]
    if a.test_session:
        argv += ["--harness", "test", "--test-session", a.test_session]
    params = StdioServerParameters(command=sys.executable, args=argv, env=base_env())
    loop = asyncio.get_running_loop()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, client_info=mt.Implementation(name="actor", version="1")) as s:
            await s.initialize()

            async def call(tool: str, **args: Any) -> dict[str, Any]:
                res = await s.call_tool(tool, {k: v for k, v in args.items() if v is not None})
                return json.loads(res.content[0].text)  # type: ignore[union-attr]

            j = await call("join", room=a.room, screen_name=a.name)
            emit(ev="joined", ok=bool(j.get("ok")), tier=j.get("tier"), error=j.get("error"))
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    return 0
                cmd = json.loads(line)
                op = cmd.pop("op")
                if op == "quit":
                    return 0
                tool = {"pass": "pass"}.get(op, op)
                emit(ev=op, **(await call(tool, **cmd)))


# ------------------------------------------------------------------------- helpers
def build(a: argparse.Namespace) -> int:
    out = Path(os.path.expanduser(a.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    data = b"FAKEBIT1" + secrets.token_bytes(4096) + (b"BROKEN" if a.broken else b"") + a.tag.encode()
    out.write_bytes(data)
    emit(path=str(out), sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    return 0


def rpc(a: argparse.Namespace) -> int:
    from switchboard.mcp.client import RpcError, call_sync
    from switchboard.paths import Paths

    try:
        emit(
            ok=True, result=call_sync(Paths.from_home(a.home).sock, a.method, json.loads(a.params), a.timeout)
        )
    except RpcError as e:
        emit(ok=False, code=e.code, message=str(e))
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="actors.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench")
    b.add_argument("--home", required=True)
    b.add_argument("--mode", choices=("inbox", "wait"), default="inbox")
    b.add_argument("--sessions", help="the Claude sessions dir (default: the home's config)")
    b.add_argument("--room", default="#fpga")
    b.add_argument("--name", default="bench")
    b.add_argument("--board", default="fake")
    b.add_argument("--drop", default="~/fpga/in")
    b.add_argument("--tty", default="~/bench/ttyFAKE0")
    b.add_argument("--baud", type=int, default=115200)
    b.add_argument("--uart-test", default="~/bench/uart_test.py")
    b.add_argument("--uart-python", default="/usr/bin/python3")
    b.add_argument("--pull-src", help="rsync source for via:pull, e.g. dev@desk")
    b.add_argument("--pull-ssh", help="the ssh command rsync uses for a pull (-e)")
    b.add_argument("--jobs", type=int, default=0, help="stop after this many results (0: until stdin ends)")
    b.add_argument("--wait-s", type=int, default=50)
    b.add_argument(
        "--hold-s",
        type=float,
        default=0.0,
        help="inbox mode: an approval prompt before each flash, open this long (the registry says waiting)",
    )
    g = sub.add_parser("agent")
    g.add_argument("--home", required=True)
    g.add_argument("--room", default="#fpga")
    g.add_argument("--name", required=True)
    g.add_argument("--test-session")
    k = sub.add_parser("build")
    k.add_argument("--out", required=True)
    k.add_argument("--broken", action="store_true")
    k.add_argument("--tag", default="")
    r = sub.add_parser("rpc")
    r.add_argument("--home", required=True)
    r.add_argument("method")
    r.add_argument("params", nargs="?", default="{}")
    r.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args()
    if a.cmd == "bench":
        return bench_inbox(a) if a.mode == "inbox" else asyncio.run(bench_wait(a))
    if a.cmd == "agent":
        return asyncio.run(agent(a))
    if a.cmd == "build":
        return build(a)
    return rpc(a)


if __name__ == "__main__":
    sys.exit(main())
