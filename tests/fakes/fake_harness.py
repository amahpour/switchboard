"""A stand-in harness process (``claude``, ``codex``, ``cursor-agent`` or
``devin acp``) for contract tests.

Copied to a path that ends in ``/claude``, ``/codex``, ``/cursor-agent`` or
``/devin`` (run with ``acp``), so the broker's argv matcher sees that harness,
it does what the harness does that switchboard relies on: for Claude it writes
``<sessions>/<pid>.json`` with its messaging socket path; it starts ``switchboard
mcp`` as its own child; it calls tools (optionally with ``_meta``, e.g.
Codex's ``threadId``); and it runs hook commands as its children with a
payload on stdin. Driven by JSON lines on stdin, answering on stdout. A
command with a ``tag`` runs in the background (a parked stop hook, a blocking
``wait()``); ``collect`` waits for it. It never posts to any socket.
"""

import asyncio
import json
import os
import subprocess
import sys

import mcp.types as mt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def attach(path):
    """Connect to an app-server socket the way a Codex TUI does, and keep reading."""
    from websockets.asyncio.client import unix_connect

    ws = await unix_connect(path, uri="ws://localhost/", ping_interval=None, compression=None, proxy=None)
    await ws.send(
        json.dumps(
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-tui", "version": "0"}}}
        )
    )
    await ws.recv()

    async def drain():
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    asyncio.get_running_loop().create_task(drain())
    return ws


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


async def main():
    home = os.environ["YK_FAKE_HOME"]
    reg = None
    if "CLAUDE_CODE_MESSAGING_SOCKET" in os.environ:
        sessions = os.environ["YK_FAKE_SESSIONS"]
        os.makedirs(sessions, exist_ok=True)
        reg = os.path.join(sessions, f"{os.getpid()}.json")
        sock = os.environ.get("YK_FAKE_REG_SOCKET") or os.environ["CLAUDE_CODE_MESSAGING_SOCKET"]
        with open(reg, "w") as f:
            json.dump({"pid": os.getpid(), "messagingSocketPath": sock, "status": "idle"}, f)
    env = {k: v for k, v in os.environ.items() if not k.startswith("YK_FAKE_")}
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "switchboard", "mcp", "--home", home], env=env
    )
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(
                r,
                w,
                client_info=mt.Implementation(
                    name=os.environ.get("YK_FAKE_CLIENT", "claude-code"), version="1"
                ),
            ) as s:
                await s.initialize()
                reply({"ready": True, "pid": os.getpid()})
                loop = asyncio.get_running_loop()
                tui = []  # Codex stand-in: "TUI" connections to a (fake) app-server socket
                bg: dict[str, asyncio.Task] = {}

                async def run_tool(cmd):
                    res = await s.call_tool(cmd["name"], cmd["args"], meta=cmd.get("meta"))
                    return {"result": json.loads(res.content[0].text)}

                async def run_hook(cmd):
                    p = await asyncio.create_subprocess_shell(
                        cmd["command"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    out, _err = await asyncio.wait_for(
                        p.communicate(json.dumps(cmd["payload"]).encode()), cmd.get("timeout", 20)
                    )
                    return {"rc": p.returncode, "stdout": out.decode()}

                while True:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        break
                    cmd = json.loads(line)
                    if cmd["op"] in ("tool", "hook") and cmd.get("tag"):
                        fn = run_tool if cmd["op"] == "tool" else run_hook
                        bg[cmd["tag"]] = asyncio.create_task(fn(cmd))
                        reply({"started": cmd["tag"]})
                    elif cmd["op"] == "collect":
                        t = bg.pop(cmd["tag"])
                        try:
                            reply(await asyncio.wait_for(asyncio.shield(t), cmd.get("timeout", 30)))
                        except asyncio.TimeoutError:
                            bg[cmd["tag"]] = t
                            reply({"pending": True})
                    elif cmd["op"] == "tool":
                        reply(await run_tool(cmd))
                    elif cmd["op"] == "hook":
                        p = subprocess.run(
                            cmd["command"],
                            shell=True,
                            input=json.dumps(cmd["payload"]).encode(),
                            capture_output=True,
                            env=env,
                            timeout=20,
                        )
                        reply({"rc": p.returncode, "stdout": p.stdout.decode()})
                    elif cmd["op"] == "attach":
                        tui.append(await attach(cmd["path"]))
                        reply({"attached": True})
                    elif cmd["op"] == "detach":
                        for ws in tui:
                            await ws.close()
                        tui.clear()
                        reply({"detached": True})
                    elif cmd["op"] == "quit":
                        break
    finally:
        if reg:
            try:
                os.unlink(reg)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
