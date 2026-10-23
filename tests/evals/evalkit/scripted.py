"""Scripted agents on an in-process broker: a world that needs no model (DESIGN.md §12.7). Test-only.

The same ``World`` the real runner plays against, with ``FakeAgent`` (``tests/fakes``) as each agent: an MCP
client of a test-mode broker's ``/mcp`` that does what its *policy* says. A policy is a plain function from
the agent's name and the messages it was just handed to a ``Move`` (say something, or pass). That is how
each check is shown to fail and to pass without a model (``tests/integration/test_evals_graders.py``), and
how the cap on tokens, a harness error and a loop-guard pause are shown to work.

Only importable under pytest: it needs ``tests/conftest.py`` and ``tests/fakes``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conftest import InProcBroker, make_tmp_home
from fakes.fake_agent import FakeAgent

from switchboard.config import Config

from .record import Tokens
from .scenario import Scenario
from .trial import HarnessError, Limits

LINE = re.compile(r"^- id=(\d+) .*?from=(\S+) kind=(\S+) .*?to_you=(yes|no)", re.M)


@dataclass(frozen=True)
class Item:
    """One message of a batch an agent was handed."""

    id: int
    sender: str
    kind: str  # human | agent
    to_you: bool


@dataclass(frozen=True)
class Move:
    say: str | None = None
    reply_to: int | None = (
        None  # a message id; a reply to a person or an @mention is exempt from the rate limit
    )
    passes: bool = False
    # an agent that posts anyway when told another agent already answered (DESIGN.md §8.8): it sends
    # the same text again with the ``since`` it was given. Without it the agent takes the refusal.
    insist: bool = False


Policy = Callable[[str, list[Item]], Move]


def parse(text: str) -> list[Item]:
    return [Item(int(i), frm, kind, to == "yes") for i, frm, kind, to in LINE.findall(text)]


class ScriptedWorld:
    """Agents that follow policies, in a room of a broker in this process. Implements ``trial.World``."""

    human = "alice"

    def __init__(
        self,
        policies: dict[str, Policy],
        *,
        tokens_per_batch: int = 1_000,
        fail_to_join: bool = False,
    ) -> None:
        self.policies = policies
        self.tokens_per_batch = tokens_per_batch
        self.fail_to_join = fail_to_join
        self.stopped = False
        self.waiting: dict[str, bool] = {}
        self.spent: dict[str, int] = {}
        self.errors: dict[str, BaseException] = {}
        self._broker: InProcBroker | None = None
        self._home: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: list[asyncio.Future[Any]] = []
        self._room = ""
        self._web: Any = None

    @property
    def db_path(self) -> Path:
        assert self._broker is not None
        return Path(self._broker.paths.db)

    def _run(self, coro: Any, timeout: float = 30.0) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def start(self, scenario: Scenario, rules: str, limits: Limits) -> None:
        if self.fail_to_join:
            raise HarnessError("timeout", "the agents did not join (scripted)")
        missing = set(scenario.agents) - set(self.policies)
        assert not missing, f"no policy for {sorted(missing)}"
        self._home = make_tmp_home()
        cfg = Config(human_name=self.human).with_delivery(quiet_s=0.0, max_hold_s=0.0)
        self._broker = InProcBroker(self._home, cfg).start()
        self._web = self._broker.web_client()
        hdr = self._broker.write_headers()
        slug = scenario.room.lstrip("#")
        assert self._web.post("/api/rooms", json={"name": scenario.room}, headers=hdr).status_code == 200
        r = self._web.put(
            f"/api/rooms/{slug}/wake",
            json={"budget_per_hour": limits.budget_per_hour, "hop_limit": limits.hop_limit},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        if rules:
            assert (
                self._web.put(f"/api/rooms/{slug}/rules", json={"text": rules}, headers=hdr).status_code
                == 200
            )
        self._room = scenario.room
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="evals-scripted", daemon=True)
        self._thread.start()
        for name in scenario.agents:
            joined: concurrent.futures.Future[bool] = concurrent.futures.Future()
            self._tasks.append(asyncio.run_coroutine_threadsafe(self._drive(name, joined), self._loop))
            joined.result(timeout=30)  # re-raises what stopped the agent from joining

    async def _drive(self, name: str, joined: concurrent.futures.Future[bool]) -> None:
        """One agent, from its connection to its end in one task (the MCP client's scopes need that)."""
        try:
            async with FakeAgent(self._home, name) as agent:
                await agent.join(self._room, name)
                self.spent[name] = 0
                self.waiting[name] = True
                joined.set_result(True)
                while not self.stopped:
                    r = await agent.wait(self._room, 1)
                    if r.get("status") != "messages":
                        continue  # timed out, or the room is paused: still waiting
                    self.waiting[name] = False
                    self.spent[name] += self.tokens_per_batch
                    move = self.policies[name](name, parse(r["text"]))
                    if move.say is not None:
                        res = await agent.say(self._room, move.say, reply_to=move.reply_to)
                        if move.insist and res.get("code") == "taken":
                            await agent.say(
                                self._room, move.say, reply_to=move.reply_to, since=res["next_since"]
                            )
                    elif move.passes:
                        await agent.pass_(self._room)
                    self.waiting[name] = True
        except BaseException as e:  # a policy that raised: the agent is gone, and idle() says so
            self.errors[name] = e
            if not joined.done():
                joined.set_exception(e)

    def say(self, text: str) -> int:
        assert self._web is not None and self._broker is not None
        r = self._web.post(
            f"/api/rooms/{self._room.lstrip('#')}/say",
            json={"text": text},
            headers=self._broker.write_headers(),
        )
        assert r.status_code == 200, r.text
        return int(r.json()["id"])

    def idle(self) -> bool:
        if self.errors:
            name, e = next(iter(self.errors.items()))
            raise HarnessError("crash", f"{name}'s policy raised {type(e).__name__}: {e}")
        return all(self.waiting.values())

    def tokens(self) -> dict[str, Tokens]:
        return {n: Tokens(input=v, calls=v // max(1, self.tokens_per_batch)) for n, v in self.spent.items()}

    def stop(self) -> None:
        self.stopped = True  # each agent's loop ends within the second its wait() has left
        for t in self._tasks:
            try:
                t.result(timeout=15)
            except BaseException:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(10)
            if not self._loop.is_running():
                self._loop.close()
        if self._web is not None:
            self._web.close()
        if self._broker is not None:
            self._broker.stop()
        if self._home is not None:
            shutil.rmtree(self._home, ignore_errors=True)
