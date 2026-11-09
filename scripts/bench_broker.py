"""Measure broker sizing over loopback HTTP/MCP (DESIGN.md §12, §30; #690).

Linux, from a checkout with the test dependencies installed:
    uv run --frozen python scripts/bench_broker.py --broker-cpu 0 \
        --agents 20 60 --wait-agents 0 100 500 1000 --seconds 5

Use the machine's suite lock when sharing it. Only the broker is pinned; the load
client runs separately. Each row gets a fresh test-mode broker and temp home,
with delivery throttles off. Calls are closed-loop (next call after the answer),
rooms hold up to five agents, and reads request 20 messages. Reports achieved
throughput and nearest-rank p50/p99, not a guaranteed production ceiling. RSS,
CPU seconds / wall second, and file descriptors are the broker process alone.
No TLS, browser WebSockets, model calls, old database or meeting bot is included.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Any

# Running a script puts scripts/, not the checkout, on sys.path.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp.types as mt  # noqa: E402

from switchboard import __version__  # noqa: E402
from switchboard.mcp.client import call_sync, ping  # noqa: E402
from switchboard.paths import Paths  # noqa: E402
from tests.fakes.fake_agent import FakeAgent  # noqa: E402


@contextmanager
def broker(cpu: int | None) -> Iterator[tuple[Path, int]]:
    """A child with an allowlisted environment; always reap it before removing its home."""
    with tempfile.TemporaryDirectory(prefix="sb-bench-") as directory:
        home = Path(directory)
        (home / ".switchboard-test").touch()
        (home / "config.toml").write_text(
            'human_name = "alice"\n[delivery]\nrate_limit_s = 0\nbudget_per_hour = 1000000000\n'
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": directory,
            "LANG": "C.UTF-8",
            "LOGNAME": "alice",
            "SWITCHBOARD_TEST": "1",
        }
        if "COVERAGE_PROCESS_CONFIG" in os.environ:
            env["COVERAGE_PROCESS_CONFIG"] = os.environ["COVERAGE_PROCESS_CONFIG"]
        command = [
            sys.executable,
            "-m",
            "switchboard",
            "start",
            "--foreground",
            "--home",
            directory,
            "--port",
            "0",
            "--test-mode",
            "--test-trust-uds",
        ]
        if cpu is not None:
            command = ["taskset", "-c", str(cpu), *command]
        with (home / "child.log").open("w+") as log:
            proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while not ping(Paths.from_home(home).sock, timeout=0.2):
                    if proc.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError("benchmark broker did not start: " + log.read())
                    time.sleep(0.01)  # poll readiness, not an assumed startup delay
                yield home, proc.pid
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(5)


def sample(pid: int) -> dict[str, float]:
    """Current RSS (not the high-water mark) and cumulative user + system CPU."""
    proc = Path(f"/proc/{pid}")
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    rss = int((proc / "statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return {
        "rss_mib": rss / 1024**2,
        "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        "fds": len(list((proc / "fd").iterdir())),
    }


def room(index: int) -> str:
    return f"bench-{index // 5}"


async def connect(stack: AsyncExitStack, home: Path, count: int) -> list[FakeAgent]:
    agents = []
    for i in range(count):
        if i % 5 == 0:
            await asyncio.to_thread(call_sync, Paths.from_home(home).sock, "room.create", {"name": room(i)})
        agent = await stack.enter_async_context(FakeAgent(home, f"bench-{i}", ack="immediate"))
        await agent.join(room(i), f"agent-{i}")
        agents.append(agent)
    # Complete setup deliveries before measuring calls or opening waits.
    for i, agent in enumerate(agents):
        await agent.read(room(i), limit=20)
        agent.calls.clear()  # FakeAgent records replies; don't retain benchmark traffic.
    return agents


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000


async def throughput(home: Path, pid: int, count: int, operation: str, seconds: float) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        agents = await connect(stack, home, count)
        # Seed 20 chat lines per full room; repeated read() polls drain to empty.
        for i, agent in enumerate(agents):
            for _ in range(4):
                await agent.say(room(i), "benchmark message")
        before = sample(pid)
        started = time.perf_counter()
        deadline = started + seconds

        async def load(i: int, agent: FakeAgent) -> list[float]:
            latencies = []
            while True:
                t = time.perf_counter()
                if operation == "read":
                    await agent.read(room(i), limit=20)
                elif operation == "hook":
                    # The hook is deliberately hidden from tools/list. Send the
                    # plugin's tools/call directly: call_tool() would discover
                    # schemas after every reply and add an unrelated list call.
                    assert agent.session is not None
                    result = await agent.session.send_request(
                        mt.CallToolRequest(
                            params=mt.CallToolRequestParams(
                                name="hook", arguments={"event": "PostToolUse", "tool_name": "Read"}
                            )
                        ),
                        mt.CallToolResult,
                    )
                    assert not result.is_error, result
                else:
                    await agent.say(room(i), "benchmark message")
                latencies.append(time.perf_counter() - t)
                agent.calls.clear()
                if time.perf_counter() >= deadline:
                    return latencies

        latencies = [
            v for group in await asyncio.gather(*(load(i, a) for i, a in enumerate(agents))) for v in group
        ]
        elapsed = time.perf_counter() - started
        after = sample(pid)
        return {
            "operation": operation,
            "agents": count,
            "calls": len(latencies),
            "seconds": elapsed,
            "calls_s": len(latencies) / elapsed,
            "p50_ms": percentile(latencies, 0.5),
            "p99_ms": percentile(latencies, 0.99),
            "cpu_cores": (after["cpu_s"] - before["cpu_s"]) / elapsed,
        }


async def idle(home: Path, pid: int, count: int, seconds: float) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        agents = await connect(stack, home, count)

        stopping = asyncio.Event()

        async def listen(i: int, agent: FakeAgent) -> None:
            while not stopping.is_set():
                await agent.wait(room(i), timeout_s=600)
                agent.calls.clear()

        tasks = [asyncio.create_task(listen(i, a)) for i, a in enumerate(agents)]
        try:
            # Test agents are idle exactly when their wait sink is open. Check every
            # room through the real UDS; a failed wait must not masquerade as low CPU.
            deadline = time.monotonic() + 30
            while True:
                for task in tasks:
                    if task.done():
                        task.result()
                members = []
                for i in range(0, count, 5):
                    result = await asyncio.to_thread(
                        call_sync, Paths.from_home(home).sock, "room.who", {"room": room(i)}
                    )
                    members.extend(result["members"])
                opened = sum(m["status"] == "idle" for m in members)
                if opened == count:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"only {opened}/{count} agents opened wait()")
                await asyncio.sleep(0.01)
            before = sample(pid)
            started = time.perf_counter()
            await asyncio.sleep(seconds)  # the explicit idle CPU measurement window
            elapsed = time.perf_counter() - started
            for task in tasks:
                if task.done():
                    task.result()
            after = sample(pid)
            return {
                "agents": count,
                "open_waits": opened,
                "rss_mib": after["rss_mib"],
                "cpu_cores": (after["cpu_s"] - before["cpu_s"]) / elapsed,
                "fds": after["fds"],
                "seconds": elapsed,
            }
        finally:
            # Finish open responses before closing MCP streams. Cancelling a
            # thousand waits at once can race their SSE readers during teardown.
            stopping.set()
            try:
                for i in range(0, count, 5):
                    await asyncio.to_thread(
                        call_sync,
                        Paths.from_home(home).sock,
                        "human.say",
                        {"room": room(i), "text": "benchmark complete"},
                    )
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="+", type=int, default=[20])
    parser.add_argument("--wait-agents", nargs="+", type=int, default=[0, 100, 500])
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--broker-cpu", type=int, help="pin only the child broker with taskset")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("RSS and CPU sampling requires Linux /proc")
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    if any(n < 1 for n in args.agents) or any(n < 0 for n in args.wait_agents):
        parser.error("--agents must be positive; --wait-agents must be nonnegative")
    if args.broker_cpu is not None and args.broker_cpu not in os.sched_getaffinity(0):
        parser.error("--broker-cpu must be a CPU in the current affinity set")
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.machine(),  # some Linux ARM systems do not report a model name
    )
    report: dict[str, Any] = {
        "version": __version__,
        "python": platform.python_version(),
        "cpu": cpu,
        "broker_cpu": args.broker_cpu,
        "throughput": [],
        "idle": [],
    }
    for count in args.agents:
        for operation in ("read", "hook", "say"):
            with broker(args.broker_cpu) as (home, pid):
                report["throughput"].append(
                    asyncio.run(throughput(home, pid, count, operation, args.seconds))
                )
            print(f"measured {operation}: {count} agents", file=sys.stderr, flush=True)
    for count in args.wait_agents:
        with broker(args.broker_cpu) as (home, pid):
            report["idle"].append(asyncio.run(idle(home, pid, count, args.seconds)))
        print(f"measured wait: {count} agents", file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
