"""A seeded switchboard for the web UI (issue #19): the state behind the Playwright tests in
``tests/e2e/`` and the screenshots ``docs/media/ui_shots.py`` takes.

Not a test module (pytest collects only ``test_*.py``). It is built from the suite's own
pieces: ``conftest.InProcBroker`` (the real app on a real port, in a thread) and
``fakes.fake_agent.FakeAgent`` (a scripted MCP client driving a real ``switchboard mcp
--harness test``). The caller isolates the environment first (HOME in a temp dir, the agent
harness variables dropped, ``SWITCHBOARD_TEST=1``): ``tests/conftest.py``'s ``sanitize_env``
under pytest, ``ui_shots.isolate()`` by hand. ``~/.switchboard`` is never read or written.

What ``UIWorld.start()`` builds:

- **Two throwaway brokers**, each in its own ``/tmp/yk-*`` home: ``b`` with the seeded state
  and ``empty`` with no rooms at all (the first-run Welcome view). Both start in test mode
  (so scripted ``--harness test`` agents may join) and are switched out of it after the joins,
  so no TEST MODE band shows.
- **Three rooms**: ``#build`` (the conversation), ``#fpga-bench`` (empty) and ``#docs``
  (closed, so Closed rooms lists one). A 0600 ``remotes.toml`` names an ``fpga-pi`` remote
  that is never enabled, so nothing dials.
- **Four agents in #build** (``AGENTS``). Their tiers, approval modes, statuses and session
  ids are set through the store; their harness and host (and so their join lines) are
  rewritten with raw SQL, devin-1's parked reason goes straight into the engine, and the
  Codex adapter's tier refresh (which needs a live daemon) is switched off: none of these can
  happen to a scripted test agent. Two room notices (devin-1 parked, codex-1's approvals-off
  warning) are posted with the broker's own wording. Everything else (the messages and the
  close of #docs) goes through the real routes and MCP tools.
- **The conversation** (``md_messages()``): every Markdown construct the UI renders, a
  ``javascript:`` link that must render blocked, raw ``<script>``/``<b>`` that must stay text,
  and one http(s) link.

The agents' MCP sessions must stay open for their memberships to stay live, and their stdio
transports belong to the event loop and task that opened them. So the agents live in one
background thread running one coroutine: it starts them, seeds the broker, reports ready and
then waits for ``stop()``, which lets the same coroutine close them. The caller's own thread
stays free for synchronous work (Playwright's sync API, httpx).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from conftest import TEST_HUMAN, InProcBroker, make_tmp_home

# The four agents in #build, in join order.
AGENTS = ("claude-1", "codex-1", "devin-1", "bench")

# (harness, tier, approval mode, status, host, session id) per agent: fixtures, not real
# sessions. bench runs on the fpga-pi remote (host set), the others on this machine ("").
PROFILE = {
    "claude-1": ("claude", "claude:inbox", "prompting", "idle", "", "3f2a91c4-7d2e-4b8a-9c1e-5a6b7c8dc91e"),
    "codex-1": ("codex", "codex:daemon", "bypass", "busy", "", "019a3c2e-55d1-7c40-a0b2-6e1f0c9d2a77"),
    "devin-1": ("devin", "devin:wait-loop", "prompting", "idle", "", "devin-7c1e2b9a4f"),
    "bench": (
        "claude",
        "claude:inbox",
        "prompting",
        "idle",
        "fpga-pi",
        "8b1d0e37-2c4a-4f19-b6d3-91e0a4c5f208",
    ),
}

# devin-1's parked reason, as the engine words it
PARKED = "its turn ended without wait()"

# A remote that is configured but never enabled: the sidebar lists it, nothing dials it.
# The host and user are placeholders (a .local name and the suite's test human).
REMOTES_TOML = f'[remote.fpga-pi]\nhost = "fpga-pi.local"\nuser = "{TEST_HUMAN}"\nrooms = ["#build"]\n'

# The seeded rooms: #docs is closed once seeded, so it shows under Closed rooms.
ROOMS = ("#build", "#fpga-bench", "#docs")

# The blocked link's scheme, built at run time so no file here holds the literal.
JS_SCHEME = "java" + "script:"

# The one http(s) link in the conversation (in bench's table message).
CI_URL = "https://github.com/example/switchboard/actions/runs/123"


def md_messages() -> dict[str, str]:
    """The #build conversation, in posting order (after the design's Chosen.dc and
    MarkdownSheet.dc): every Markdown construct the UI renders, plus a blocked link and raw
    HTML, which must show as inert text."""
    return {
        "ask": "@claude-1 add input validation to `parse_port`, then @codex-1 review it.",
        "plan": (
            "On it. Plan:\n\n1. Reject non-digits and values outside 0–65535\n"
            "2. Keep `0` for 'pick a free port'\n3. Add a test for each edge"
        ),
        "done": (
            "Done in `.worktrees/claude-1`:\n\n```python\ndef parse_port(s: str) -> int:\n"
            '    if not s.isdigit():\n        raise ValueError(f"not a port: {s!r}")\n'
            "    n = int(s)\n    if not 0 <= n <= 65535:\n"
            '        raise ValueError(f"out of range: {n}")\n    return n\n```'
        ),
        "review": (
            "Two issues:\n\n- `'٢'.isdigit()` is True (Arabic-Indic digits): use "
            "`s.isascii() and s.isdigit()`\n- Leading zeros: `'0080'` passes. Intended?"
        ),
        "devin": (
            "## Open questions\n\nPicking this up from:\n\n> @claude-1 add input validation to "
            "`parse_port`, then @codex-1 review it.\n\n---\n\n"
            f"Not a link: [CI run]({JS_SCHEME}alert(document.cookie))\n\n"
            "<script>alert(1)</script> stays text, as does <b>this</b>."
        ),
        "flash": "@bench flash it once codex-1 signs off. Keep **`0`** as *pick a free port*.",
        "report": (
            "**Doing:** hardening `parse_port` in `.worktrees/claude-1`\n"
            "**Decided:** `0` means *pick a free port*\n**Open questions:** leading zeros\n"
            "**Next step:** flash once codex-1 signs off"
        ),
        "table": (
            "Bitstream flashed; UART shows `PORT_OK 8080`.\n\n| input | result |\n|:--|:--|\n"
            "| `'8080'` | ok |\n| `'0080'` | ok (see codex-1) |\n| `'٢'` | ValueError |\n\n"
            f"[CI run]({CI_URL})"
        ),
    }


def wait_js(page: Any, expression: str, arg: Any = None, timeout_ms: float = 15_000) -> Any:
    """Wait until ``expression`` (what ``page.evaluate`` takes) is truthy, and return its value.

    Not ``page.wait_for_function``: Playwright compiles that predicate with ``eval`` inside the
    page, and switchboard's CSP (``script-src 'self'``, no ``'unsafe-eval'``) refuses it whenever
    the compile runs from the page's own animation frame rather than from DevTools: a flaky
    ``EvalError`` (7 of 12 fresh pages failed, 10 of 12 with the WebSocket routed). ``evaluate``
    runs through DevTools, outside the page's CSP, every time."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        value = page.evaluate(expression, arg)
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"not true within {timeout_ms:.0f} ms: {expression}")
        page.wait_for_timeout(50)


def start_broker(home: Path) -> InProcBroker:
    """A test-mode in-process broker in ``home``, with no say() rate limit and a roomier loop
    guard, so the scripted conversation posts in one go."""
    from switchboard.config import Config

    cfg = Config(human_name=TEST_HUMAN).with_delivery(
        quiet_s=0.0, max_hold_s=0.0, rate_limit_s=0.0, hop_limit=30
    )
    return InProcBroker(home, cfg, test_mode=True).start()


def not_test_mode(b: InProcBroker) -> None:
    """Switch a broker out of test mode after the scripted joins (no TEST MODE band)."""

    def off() -> None:
        b.state.test_mode = False
        b.state.info.test_mode = False

    b.on_loop(off)


def ok(r: httpx.Response) -> dict[str, Any]:
    """The JSON of a 200 answer; anything else stops the seeding with the route and body."""
    if r.status_code != 200:
        raise RuntimeError(f"ui_world: {r.request.method} {r.request.url.path}: {r.status_code} {r.text}")
    return r.json()


async def seed(b: InProcBroker, agents: dict[str, Any]) -> None:
    """Rooms, joins, the #build conversation, the notices, the closed #docs, then each agent's
    picture profile (``PROFILE``). Runs on the agents' event loop."""
    web = b.web_client()
    h = b.write_headers()
    try:
        for room in ROOMS:
            ok(web.post("/api/rooms", json={"name": room}, headers=h))
        for name, a in agents.items():
            await a.start()
            await a.join("#build", name)
        t = md_messages()

        def human(text: str) -> int:
            return ok(web.post("/api/rooms/build/say", json={"text": text}, headers=h))["id"]

        async def say(name: str, text: str, reply_to: int | None = None) -> int:
            r = await agents[name].say("#build", text, reply_to=reply_to)
            if not r.get("posted_id"):
                raise RuntimeError(f"ui_world: {name} could not post: {r}")
            return int(r["posted_id"])

        def notice(text: str, level: str | None = None) -> None:
            b.on_loop(
                lambda: b.state.service.post_notice(b.state.store.get_room("#build"), text, level=level)
            )

        ask = human(t["ask"])
        await say("claude-1", t["plan"], reply_to=ask)
        done = await say("claude-1", t["done"])
        await say("codex-1", t["review"], reply_to=done)
        await say("devin-1", t["devin"])
        notice(f"devin-1 is parked — needs a poke ({PARKED})")
        notice(
            "⚠ codex-1 runs with approvals off: transcripts it reads can make it act without asking", "warn"
        )
        human(t["flash"])
        await say("bench", t["report"])
        await say("bench", t["table"])
        ok(web.post("/api/rooms/docs/say", json={"text": "Draft the release notes."}, headers=h))
        ok(web.post("/api/rooms/docs/command", json={"text": "/close"}, headers=h))
    finally:
        web.close()

    # The agents' profiles. The scripted agents make no more calls after this (a rewritten
    # host or harness would no longer match the MCP process that joined).
    def profile() -> None:
        from switchboard import db

        st = b.state
        s = st.store
        room = s.get_room("#build")
        for name, (harness, tier, mode, status, host, sid) in PROFILE.items():
            m = s.find_member(room.id, name)
            s.update_participant(m.participant_id, tier=tier, approval_mode=mode, session_id=sid)
            s.set_status(m.participant_id, status, "hook:PreToolUse" if status == "busy" else "hook:Stop")
            with db.tx(s.con):  # fixtures only: a test agent's harness and host never change
                s.con.execute(
                    "UPDATE participants SET harness=?, host=? WHERE id=?", (harness, host, m.participant_id)
                )
                s.con.execute(
                    "UPDATE messages SET sender_harness=?, sender_host=? WHERE sender_membership_id=?",
                    (harness, host or None, m.membership_id),
                )
                # its join line, as the broker words it for that harness and host
                where = f"{harness} on {host}" if host else harness
                s.con.execute(
                    "UPDATE messages SET text=? WHERE sender_membership_id=? AND kind='join'",
                    (f"joined ({where}, {tier})", m.membership_id),
                )
            if name in ("claude-1", "bench"):
                # these two have "read" the room: nothing left for the engine to park them over
                # (a scripted agent has no wake path, so an idle one with a mention would park)
                with db.tx(s.con):
                    s.con.execute(
                        "UPDATE deliveries SET state='handled', handled_at=? WHERE membership_id=?"
                        " AND state IN ('pending','offered','in_context')",
                        (time.time(), m.membership_id),
                    )
                    s.con.execute(
                        "UPDATE batches SET state='cancelled' WHERE membership_id=? AND state='offered'",
                        (m.membership_id,),
                    )
                st.engine.parked.pop(m.membership_id, None)
            if name == "devin-1":
                st.engine.parked[m.membership_id] = PARKED
        # the Codex adapter re-derives its members' tiers from the live daemon link, which this
        # throwaway broker doesn't have: keep the fixture's codex:daemon
        st.engine.adapters["codex"].refresh_tiers = lambda: None
        st.hub.members_changed("#build")

    b.on_loop(profile)
    not_test_mode(b)


class HostedWorld:
    """An unclaimed hosted broker (issue #41, DESIGN.md §31) for the claim page, the passkey
    sign-in page and the passkeys sheet: a test-mode in-process broker behind
    ``http://sb.localhost:<its port>``, a public URL browsers treat as a secure context (so
    passkeys work over plain http, on loopback), with one room and no agents. Its claim link
    is in ``run/test-claim-link``. The passkeys themselves are Chromium's virtual
    authenticators, added by the caller (tests/e2e/test_passkeys_ui.py, docs/media/ui_shots.py).
    """

    def __init__(self, *, keep: bool = False, host: str = "sb.localhost", test_mode: bool = False) -> None:
        self.keep = keep
        self.host = host  # "localhost" when a machine's dialer (another process) must resolve it too
        self.test_mode = test_mode  # kept on: a test machine's dialer (a test-mode satellite) may link
        self.home = make_tmp_home()
        self.b: InProcBroker | None = None

    def start(self) -> HostedWorld:
        from switchboard.broker.auth import WebOrigin

        host = self.host
        self.b = InProcBroker(
            self.home, web_origin=lambda port: WebOrigin.parse(f"http://{host}:{port}")
        ).start()
        b = self.b
        b.on_loop(lambda: b.state.service.create_room("#build"))  # the broker answers only to its public host
        if not self.test_mode:
            not_test_mode(b)  # its claim link is written already; no TEST MODE band in the pictures
        return self

    def set_test_mode(self, on: bool) -> None:
        """For the pictures: a page loaded with test mode off shows no TEST MODE band, and a test
        machine's dialer (a test-mode satellite) links only while it's on."""

        def flip() -> None:
            self.broker.state.test_mode = on
            self.broker.state.info.test_mode = on

        self.broker.on_loop(flip)

    def stop(self) -> None:
        if self.b is not None:
            self.b.stop()
            self.b = None
        if not self.keep:
            shutil.rmtree(self.home, ignore_errors=True)

    @property
    def broker(self) -> InProcBroker:
        assert self.b is not None, "HostedWorld is not started"
        return self.b

    @property
    def origin(self) -> str:
        return self.broker.state.web_origin.origin

    def claim_link(self) -> str:
        return self.broker.paths.test_claim_link.read_text().strip()


class UIWorld:
    """The seeded broker ``b``, the room-less broker ``empty`` and the four agents.

    ``start()`` returns once everything is seeded (or raises what went wrong); ``stop()``
    closes the agents, stops both brokers and removes the temp homes unless ``keep``.
    """

    def __init__(self, *, keep: bool = False, ready_timeout: float = 120.0) -> None:
        self.keep = keep
        self.ready_timeout = ready_timeout
        self.home = make_tmp_home()
        self.home2 = make_tmp_home()
        self.b: InProcBroker | None = None
        self.empty: InProcBroker | None = None
        self.n_chat = len(md_messages())  # chat rows #build shows once seeded
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._extra: list[Any] = []  # agents add_agents() joined later; closed with the others
        self._extra_named: dict[tuple[str, str], Any] = {}
        # a signed-in client for the helpers below, made before any browser opens: every sign-in
        # posts a live "new web login" notice, which an open page would show in its log
        self.web: httpx.Client | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> UIWorld:
        try:
            # 0600, created exclusively: the broker refuses a remotes.toml others can write
            fd = os.open(self.home / "remotes.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(REMOTES_TOML)
            self.b = start_broker(self.home)
            self.empty = start_broker(self.home2)
            not_test_mode(self.empty)
            self._thread = threading.Thread(target=self._agents_thread, name="ui-world-agents", daemon=True)
            self._thread.start()
            if not self._ready.wait(self.ready_timeout):
                raise TimeoutError(f"ui_world: seeding took over {self.ready_timeout:.0f}s")
            if self._error is not None:
                raise RuntimeError(f"ui_world: seeding failed: {self._error!r}") from self._error
            self.web = self.b.web_client()
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        if self.web is not None:
            self.web.close()
            self.web = None
        # the agents first (their coroutine closes them), then the brokers they talk to
        if self._loop is not None and self._stop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:  # the loop already finished (seeding failed)
                pass
        if self._thread is not None:
            self._thread.join(30)
            self._thread = None
        for x in (self.b, self.empty):
            if x is not None:
                x.stop()
        self.b = self.empty = None
        if not self.keep:
            for d in (self.home, self.home2):
                shutil.rmtree(d, ignore_errors=True)

    def _agents_thread(self) -> None:
        asyncio.run(self._agents_main())

    async def _agents_main(self) -> None:
        from fakes.fake_agent import FakeAgent

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        agents = {n: FakeAgent(self.home, f"shots-{n}") for n in AGENTS}
        try:
            assert self.b is not None
            await seed(self.b, agents)
            self._ready.set()
            await self._stop.wait()
        except BaseException as e:  # reported to start(), which raises it in the caller's thread
            self._error = e
        finally:
            self._ready.set()
            for a in [*agents.values(), *self._extra]:
                await a.close()

    # ------------------------------------------------------------ helpers
    @property
    def broker(self) -> InProcBroker:
        assert self.b is not None, "UIWorld is not started"
        return self.b

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the seeded broker's web API as the signed-in human (as the UI does)."""
        assert self.web is not None, "UIWorld is not started"
        return ok(self.web.post(path, json=body, headers=self.broker.write_headers()))

    def command(self, room_slug: str, text: str) -> dict[str, Any]:
        """Run a slash command in a room as the human."""
        return self.post(f"/api/rooms/{room_slug}/command", {"text": text})

    def say(self, room_slug: str, text: str) -> int:
        """Post a message in a room as the human; returns its id."""
        return int(self.post(f"/api/rooms/{room_slug}/say", {"text": text})["id"])

    def create_room(self, name: str) -> dict[str, Any]:
        return self.post("/api/rooms", {"name": name})

    def add_agents(self, room: str, names: tuple[str, ...]) -> None:
        """Join more scripted test agents to ``room`` (which must exist), in order, for a test
        that kicks or loses members without touching #build's four. They run on the agents'
        loop and close with the others in ``stop()``. Joining a ``--harness test`` agent needs
        test mode, so the broker is in it only while they join (as during the seeding)."""
        from fakes.fake_agent import FakeAgent

        b = self.broker
        loop = self._loop
        assert loop is not None, "UIWorld is not started"

        def test_mode_on() -> None:
            b.state.test_mode = True
            b.state.info.test_mode = True

        async def join_all() -> None:
            for n in names:
                a = FakeAgent(self.home, f"extra-{room.lstrip('#')}-{n}")  # a name may recur per room
                self._extra.append(a)
                await a.start()
                joined = await a.join(room, n)
                assert joined["ok"], joined
                self._extra_named[(room, n)] = a

        b.on_loop(test_mode_on)
        try:
            asyncio.run_coroutine_threadsafe(join_all(), loop).result(60)
        finally:
            not_test_mode(b)

    def set_approval_modes(self, room: str, modes: dict[str, str]) -> None:
        """Set seeded agents' approval modes and publish the resulting Members frame."""
        b = self.broker

        def update() -> None:
            st = b.state
            row = st.store.get_room(room)
            for name, mode in modes.items():
                member = st.store.find_member(row.id, name)
                st.store.update_participant(member.participant_id, approval_mode=mode)
            st.hub.members_changed(room)

        b.on_loop(update)

    def agent_call(self, room: str, name: str, tool: str, **args: Any) -> dict[str, Any]:
        """Any tool call from an agent joined by ``add_agents`` (the review board's, #80)."""
        loop = self._loop
        assert loop is not None, "UIWorld is not started"
        agent = self._extra_named.get((room, name))
        assert agent is not None, f"{name} was not added to UIWorld"
        result = asyncio.run_coroutine_threadsafe(agent.call(tool, room=room, **args), loop).result(30)
        assert result.get("ok", True) is not False, result
        return result

    def seed_review(self, room: str, pr: str = "https://example.com/shop/pull/7") -> None:
        """A review board in ``room`` (joined first by ``add_agents(room, ("claude-1", "codex-1"))``):
        the made-up shop pull request #80's mockups use, with an item in every lane."""
        call = self.agent_call
        call(room, "codex-1", "review", action="open", url=pr, head="4f2c9e1a7b30")
        call(room, "codex-1", "review", action="raise", title="Discount is taken after tax",
             file="shop/cart.py", lines="40-58",
             detail="`total()` adds tax first, then subtracts the code's percentage, so a 10% code "
                    "saves 10% of the taxed price.")  # fmt: skip
        call(
            room,
            "codex-1",
            "review",
            action="raise",
            title="Expired codes still apply",
            file="shop/codes.py",
            lines="12-19",
            detail="`is_valid()` never checks `expires_at`.",
        )
        call(
            room,
            "codex-1",
            "review",
            action="raise",
            title="Rounding drops a cent on $19.99",
            file="shop/money.py",
            lines="8",
            detail="Half-up rounding, but the ledger rounds half-even.",
        )
        call(
            room,
            "codex-1",
            "review",
            action="raise",
            title="Codes are case-sensitive",
            file="shop/codes.py",
            lines="5",
        )
        call(room, "claude-1", "review", action="concede", item="F2")
        call(room, "claude-1", "review", action="fix", item="F2", commit="9a1b2c3d4e5f")
        call(room, "claude-1", "review", action="concede", item="F4", owner="codex-1")
        call(room, "claude-1", "review", action="contest", item="F3",
             reason="the ledger is the one to change: half-up is what the receipt shows")  # fmt: skip
        call(room, "codex-1", "review", action="drop", item="F1",
             reason="moved to Q1: it's a pricing decision, not a bug")  # fmt: skip
        call(room, "claude-1", "review", action="ask", title="Discount before or after tax?",
             detail="Before tax: $86.40 on the $96.00 order. After tax: $88.00. Finance's sheet says before.",
             options=["Before tax", "After tax"], recommend=0)  # fmt: skip

    def agent_say(self, room: str, name: str, text: str, reply_to: int | None = None) -> int:
        """Post from an agent joined by ``add_agents``; returns the posted message id."""
        loop = self._loop
        assert loop is not None, "UIWorld is not started"
        agent = self._extra_named.get((room, name))
        assert agent is not None, f"{name} was not added to UIWorld"
        result = asyncio.run_coroutine_threadsafe(agent.say(room, text, reply_to=reply_to), loop).result(30)
        assert result["ok"] and result["posted_id"], result
        return int(result["posted_id"])
