"""A real call, end to end: every meetings feature against a live Zoom, Meet or Teams call (§12.4, §52).

Opt-in: ``SWITCHBOARD_LIVE=meeting SWITCHBOARD_MEETING_URL_FILE=<file> uv run pytest -m live
tests/live/test_meeting_selftest.py -s -q``. ``all`` does not include it: it needs a call, and a call needs
an account. The skill ``.claude/skills/meeting-self-test`` makes a Zoom meeting, runs this, and deletes it.

Two joinly containers are started (published on a free loopback port each, since joinly has no sign-in):
switchboard's, which the throwaway broker's ``[meeting] joinly_url`` names, and a test speaker named
"switchboard self-test speaker" that this test drives as an MCP client the way the broker drives its own. The
broker runs in this process in a temp home, with a room and a scripted agent, ``claude-1``. Both bots join
the call named in the file, and seven steps run in order, each with what it expected, what it saw and how long
it took (``harness/selftest.py`` writes them as JSON and a Markdown table):

1. ``/meeting <link>`` from a person: the bar goes live.
2. The speaker joins and says a known sentence: a call line with most of its words reaches the room.
3. The speaker says "Claude one, can you hear me?": claude-1 is woken by that line.
4. ``/meeting voice on``, and claude-1 says ``@call I can hear you loud and clear``: the speaker's transcript
   has most of those words.
5. ``/meeting snapshot``: a stored file whose bytes are a JPEG, and its card in the room.
6. Two voices (#713): the speaker, whose joinly has the voice patch, says one line in each of two Kokoro
   voices, and the broker's bot hears both (each reaches the room as a call line). A third line, in a voice
   Kokoro does not have, is answered with the patch's "voice was not found" and still spoken. Not run with
   stock joinly. Whether the two voices *sound* different is for a person to hear on the call.
7. ``/meeting leave`` and the speaker leaves: the bar ends and no bot is left in the call.

The link has the call's passcode in it. It is read from the file named by ``SWITCHBOARD_MEETING_URL_FILE``
(mode 0600, refused otherwise), never from an argument; it is held as a ``SecretLink``; every error that
might quote it is scrubbed first; and at the end the broker's database, its messages and its log are checked
for any piece of it. Whatever happens, both bots leave and both containers are removed. A failed step keeps
both containers' logs in the output folder, scrubbed of the link, and the report names the files; delete
them after reading. A step that can't start because an earlier one failed is recorded as failed ("not
attempted"), so a run is green only if all seven passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from harness import selftest
from harness.relay import JOINLY_IMAGE as IMAGE
from harness.selftest import SecretLink, StepResult
from harness.tmuxdrv import REAL_HOME
from harness.world import REAL_PATH, live_set

from switchboard.config import Config, MeetingCfg
from switchboard.meeting.bridge import PARTICIPANT as BOT_NAME
from switchboard.meeting.links import PLATFORM_LABEL

pytestmark = pytest.mark.live

# Opt-in by name only: ``SWITCHBOARD_LIVE=all`` is the harnesses, and this joins a real call.
if "meeting" not in live_set():
    pytest.skip(
        "set SWITCHBOARD_LIVE=meeting and SWITCHBOARD_MEETING_URL_FILE=<file with a call's link, mode 0600> "
        "to run the live meeting self-test (the meeting-self-test skill does it for you)",
        allow_module_level=True,
    )

LINK_ENV = "SWITCHBOARD_MEETING_URL_FILE"
OUT_ENV = "SWITCHBOARD_SELFTEST_OUT"
# On Zoom, both bots run with our speaker-name fix (joinly-ai/joinly#187), as DEPLOY.md says to: stock 0.5.4
# can't open Zoom's participant list, so the speaker could neither see the bot arrive (and spoke before its
# audio was up) nor see it leave. The file is the one DEPLOY.md pins, checked by its sha256.
# On any platform both bots also run with the first-word fix (#620), the same way: joinly's voice detection
# flags speech late, and the hotwords turn a clipped first word into "Claude". That file is pinned too.
# SWITCHBOARD_SELFTEST_STOCK_JOINLY=1 runs stock joinly instead, without either fix.
ZOOM_FIX_URL = (
    "https://raw.githubusercontent.com/amahpour/joinly/9fbd07ceccbbfc358d697378c3cb1d7b911b17a5"
    "/joinly/providers/browser/platforms/zoom.py"
)
ZOOM_FIX_SHA256 = "9bf581c20a9a7911afb9f2ac1447a5d0800cdca24485dd215099d5d82afad6ff"
ZOOM_PY = "/app/.venv/lib/python3.12/site-packages/joinly/providers/browser/platforms/zoom.py"
PREROLL_FIX_COMMIT = "1fe384d9deba32f38fabd5bfc77fc5975adef6a7"
PREROLL_FIX_URL = (
    f"https://raw.githubusercontent.com/amahpour/joinly/{PREROLL_FIX_COMMIT}"
    "/joinly/controllers/transcription/default.py"
)
PREROLL_FIX_SHA256 = "6053ca7c6b100952c91e14f2552b82eaf92bcde6a809d627f040479dc6d39d97"
CONTROLLER_PY = "/app/.venv/lib/python3.12/site-packages/joinly/controllers/transcription/default.py"
# The voice patch (#713, deploy/joinly/NOTICE): `speak_text` takes a voice for one line. Two files of this
# repository, mounted into every container on any platform, pinned by sha256 like the others. Step 6 uses it.
VOICE_PATCH = {
    "server.py": (
        "b168efc06793f6ee1873f1f9d28bd8e01692a4577408a1637968ad2b2d6a38c0",
        "/app/.venv/lib/python3.12/site-packages/joinly/server.py",
    ),
    "kokoro.py": (
        "d64834fc3146261df194c8da6e3c392f058b317fa7cc56953a2bb4b553c065b3",
        "/app/.venv/lib/python3.12/site-packages/joinly/services/tts/kokoro.py",
    ),
}
VOICE_PATCH_DIR = Path(__file__).resolve().parents[2] / "deploy" / "joinly"
STOCK_ENV = "SWITCHBOARD_SELFTEST_STOCK_JOINLY"
# SWITCHBOARD_SELFTEST_LOGS=1 keeps each container's log, scrubbed of the link, in the output folder (0600),
# on a pass as well. A failed step keeps them either way (#642): the run that failed is the one to read.
# Off otherwise: a log holds the call's participants. Delete the files after reading them.
LOGS_ENV = "SWITCHBOARD_SELFTEST_LOGS"
SPEAKER_NAME = "switchboard self-test speaker"
# SWITCHBOARD_SELFTEST_OBSERVER=1 adds a third joinly that only listens (#602). When a call-out doesn't wake
# claude-1, the report says what this independent listener heard at the same moment: if it heard "Claude
# one" and the bot didn't, the loss is in the bot's session; if neither did, the audio lost it.
OBSERVER = os.environ.get("SWITCHBOARD_SELFTEST_OBSERVER", "") == "1"
OBSERVER_NAME = "switchboard self-test observer"
ROOM = "#build"
SLUG = "build"
LABEL = "switchboard-selftest"  # on both containers, so `docker ps --filter label=...` finds a leftover

# What is said, and what the agent answers. None of them says "screen": a call line that addresses an agent
# and says it takes a snapshot of its own (§52), and step 5 asks for the only one.
SENTENCE = "The purple elephant is reading the morning newspaper"
# Said first, and not measured. On Zoom a bot's first sentence, begun the moment it unmutes, was lost in 2
# of 3 runs (its log: "unmute" and the speech in the same second; nothing reached the other bot), while every
# later one arrived. A person has been unmuted a moment before they speak; this line is that moment. Whether
# it was heard is in step 2's report, so the first-speech loss stays visible.
WARMUP = "Testing, testing, the microphone is on."
# A lead word first (#602): on Zoom the first half-second after a silence can be cut before anyone hears
# it. With "Claude one, can you hear me?" both switchboard's bot and an independent observer heard only
# "can you hear me?", so the name never reached the transcript. "Okay" is an address switchboard already
# takes ("ok", "okay", "hey", "hi" before a name), and if it is the part cut, the name survives.
CALL_OUT = "Okay, Claude one, can you hear me?"
REPLY = "I can hear you loud and clear"

# Every wait is a poll with a deadline, in seconds.
HEALTH_S = 120.0  # a container answers /health
JOIN_S = 90.0  # the bar goes live (the bridge itself gives up on a join at 45)
SPEAKER_JOIN_S = 90.0
SEE_BOT_S = 20.0  # the speaker sees the bot in the participant list
SPEAK_S = 120.0  # speak_text returns when the speech has ended
HEARD_S = 60.0  # from the end of a sentence to its words arriving (transcripts come in batches of ~3 s)
SNAPSHOT_S = 30.0
LEAVE_S = 30.0
POLL_S = 1.0
SLOW_POLL_S = 3.0  # a call to the speaker (its transcript, its participants) is not cheap
# SWITCHBOARD_SELFTEST_CALLOUTS=N says step 3's call-out N times (1 to 30; 1 by default), each waiting
# REPEAT_HEARD_S, so one call measures how often a call-out is missed (#602).
CALLOUTS = max(1, min(30, int(os.environ.get("SWITCHBOARD_SELFTEST_CALLOUTS") or "1")))
REPEAT_HEARD_S = 20.0
WARMUP_HEARD_S = 20.0  # the warm-up line arrives, or the speaker goes on without it
TAIL_S = 4.0  # after a pass, one more transcript batch (they come every ~3 s), only for what the report shows

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

STEPS = {
    1: (
        "the bar goes live",
        "/meeting <link> from a person: the room's call bar is live on the {platform} call",
    ),
    2: ("a spoken line reaches the room", f"a call line with most of the words of: {SENTENCE}"),
    3: ("a spoken name wakes the agent", f"claude-1 is woken by a call line addressed to it: {CALL_OUT}"),
    4: ("an @call line is heard in the call", f"the speaker's transcript has most of the words of: {REPLY}"),
    5: (
        "a snapshot is a room file and a card",
        "a stored file whose bytes are a JPEG, and its card in the room",
    ),
    6: (
        "two voices are heard",
        "the speaker says a line in each of two voices and the bot hears both; an unknown voice falls back",
    ),
    7: ("the bots leave", "the bar ends and the speaker no longer sees the bot; the speaker leaves"),
}
# Step 6's words: two lines to be heard, in two voices Kokoro v1.0 has (an American man and a British woman),
# neither of which is switchboard's default (bm_george) or joinly's (af_bella); and a voice no one has.
VOICE_LINES = (
    "The green lighthouse keeps the quiet harbour company",
    "A small orange kite is circling above the old bridge",
)
VOICES = ("am_adam", "bf_emma")
UNKNOWN_VOICE = "zz_nobody"
UNKNOWN_LINE = "Nothing here has that voice"
NOT_FOUND = "voice was not found"  # what the patched speak_text answers when Kokoro lacks the voice
HEARD_BOTH_S = 90.0


# ---------------------------------------------------------------------- docker
def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], env=DOCKER_ENV, capture_output=True, text=True, timeout=timeout, check=False
    )


class Joinly:
    """One joinly container, published on a free loopback port and nowhere else. ``--publish 127.0.0.1::8000``
    lets docker pick the port, so there is no gap between finding a free one and binding it."""

    def __init__(self, label: str):
        self.label = label
        self.name = f"sb-selftest-{secrets.token_hex(4)}-{label}"
        self.port = 0
        self.mounts: list[tuple[Path, str]] = []  # files mounted read-only over the image's own copies

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/"

    def start(self) -> None:
        mount = [arg for path, target in self.mounts for arg in ("-v", f"{path}:{target}:ro")]
        run = docker(
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            "--label",
            f"{LABEL}=1",
            "-p",
            "127.0.0.1::8000",
            *mount,
            IMAGE,
        )
        if run.returncode != 0:
            raise RuntimeError(f"docker could not start {self.name}: {run.stderr.strip()[:300]}")
        found = re.search(r"127\.0\.0\.1:(\d+)", docker("port", self.name, "8000/tcp").stdout)
        if found is None:
            raise RuntimeError(f"docker did not say which port {self.name} is on")
        self.port = int(found.group(1))

    def wait_healthy(self) -> None:
        deadline = time.monotonic() + HEALTH_S
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{self.name} did not answer /health within {HEALTH_S:.0f} s")
            time.sleep(0.5)

    def save_log(self, out_dir: Path, pieces: list[str]) -> Path:
        """The container's log, every piece of the link scrubbed, into ``out_dir`` (mode 0600)."""
        got = docker("logs", self.name, timeout=60.0)
        text = (got.stdout or "") + (got.stderr or "")
        return selftest.write_container_log(out_dir, f"joinly-{self.label}.log", text, pieces)

    def remove(self) -> str | None:
        """Remove the container, running or not. None when it is gone, else why not."""
        docker("rm", "-f", self.name, timeout=60.0)
        left = docker("ps", "-aq", "--filter", f"name=^{self.name}$")
        return (
            None
            if left.returncode == 0 and not left.stdout.strip()
            else f"container {self.name} is still there"
        )


def kept_logs(
    containers: dict[str, Joinly], out_dir: Path, pieces: list[str], steps: list[StepResult]
) -> list[str]:
    """The log files written for this run. A failed step keeps them; ``SWITCHBOARD_SELFTEST_LOGS=1``
    keeps them on a pass too. Each name is what the report lists. Empty when nothing is kept."""
    if not selftest.keep_container_logs(steps, os.environ.get(LOGS_ENV, "") == "1"):
        return []
    saved: list[str] = []
    for box in containers.values():
        with contextlib.suppress(Exception):
            saved.append(box.save_log(out_dir, pieces).name)
    return saved


# ------------------------------------------------------------------- the speaker
def reply_text(result: Any) -> str:
    """The text of a tool result, whatever shape the client library gave it."""
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


class Speaker:
    """The test speaker: the second joinly, driven over MCP the way the broker drives its own. It takes its
    name in the ``joinly-settings`` header at connect (§52) and says what it is asked with ``speak_text``."""

    def __init__(self, url: str, name: str = SPEAKER_NAME):
        self.name = name
        header = json.dumps({"name": name}, ensure_ascii=True)
        self.client = Client(StreamableHttpTransport(url, headers={"joinly-settings": header}), mode="legacy")
        self.joined = False

    async def __aenter__(self) -> Speaker:
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.__aexit__(*exc)

    async def call(self, tool: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        return await self.client.call_tool(tool, args or {}, timeout=timeout)

    async def join(self, link: SecretLink) -> None:
        await self.call(
            "join_meeting",
            {"meeting_url": link.reveal(), "participant_name": self.name},
            timeout=SPEAKER_JOIN_S,
        )
        self.joined = True

    async def say(self, text: str, voice: str | None = None) -> str:
        """Speak ``text``; with ``voice``, in that Kokoro voice (the voice patch, #713)."""
        args = {"text": text, **({"voice": voice} if voice else {})}
        return reply_text(await self.call("speak_text", args, timeout=SPEAK_S))

    async def segments(self) -> list[selftest.Segment]:
        return selftest.segments_of(await self.call("get_transcript", {"mode": "full"}))

    async def names(self) -> list[str]:
        return selftest.names_of(await self.call("get_participants"))

    async def leave(self) -> None:
        """Leave the call. A speaker that never joined has nothing to leave."""
        if self.joined:
            await self.call("leave_meeting", timeout=15.0)
            self.joined = False


# ---------------------------------------------------------------------- the world
@dataclass
class Outcome:
    ok: bool
    seen: str
    ratio: float | None = None


async def until(check: Callable[[], Any], seconds: float, every: float = POLL_S) -> Any:
    """Poll ``check`` (a function, or a coroutine function) until it returns something truthy or ``seconds``
    pass, and return the last value. It is looked at once even when ``seconds`` is 0. There is no fixed
    sleep anywhere in this file: a wait is this, or an agent's own ``wait()``."""
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if inspect.isawaitable(value):
            value = await value
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(every)


class World:
    """The broker, its room, its scripted agent, the speaker and the seven steps."""

    voice_patch = os.environ.get(STOCK_ENV, "") != "1"  # step 6 needs the patched joinly; stock has none

    def __init__(
        self,
        broker: InProcBroker,
        web: httpx.Client,
        agent: FakeAgent,
        speaker: Speaker,
        link: SecretLink,
        steps: list[StepResult],
        observer: Speaker | None = None,
    ):
        self.broker, self.web, self.agent, self.speaker, self.link = broker, web, agent, speaker, link
        self.observer = observer  # an independent listener (#602), or None
        self.pieces = link.parts()
        self.steps = steps  # the test's own list, so a step that finished is kept if a later one blows up
        self.bot_live = False
        self.bot_seen = False  # the speaker has seen the bot in the participant list

    # -- reading the room as the person's page does
    def headers(self) -> dict[str, str]:
        return self.broker.write_headers()

    def describe(self, e: BaseException) -> str:
        """An error as one scrubbed line: joinly's and the client library's messages can quote the link."""
        return selftest.scrub(f"{type(e).__name__}: {e}", self.pieces)[:300]

    def command(self, text: str) -> httpx.Response:
        return self.web.post(f"/api/rooms/{SLUG}/command", json={"text": text}, headers=self.headers())

    def messages(self, after: int = 0) -> list[dict[str, Any]]:
        r = self.web.get(
            f"/api/rooms/{SLUG}/messages", params={"after": after, "limit": 1000}, headers=self.headers()
        )
        r.raise_for_status()
        return list(r.json()["messages"])

    def last_id(self) -> int:
        return max((m["id"] for m in self.messages()), default=0)

    def call_state(self) -> dict[str, Any] | None:
        """The room frame's ``settings.meeting``, as the page reads it: None once the call has ended."""
        r = self.web.get(f"/api/rooms/{SLUG}/members", headers=self.headers())
        r.raise_for_status()
        state = r.json()["settings"]["meeting"]
        return dict(state) if state else None

    def notices(self, last: int = 2) -> str:
        texts = [m["text"] for m in self.messages() if m["kind"] == "notice"]
        return selftest.scrub(" / ".join(texts[-last:]), self.pieces)

    def meeting_row(self) -> tuple[str, str | None] | None:
        con = sqlite3.connect(self.broker.paths.db)
        try:
            row = con.execute("SELECT state, error FROM meetings ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        return (row[0], row[1]) if row else None

    # -- running a step
    async def step(
        self, n: int, run: Callable[[], Awaitable[Outcome]], needs: Sequence[Callable[[], str | None]] = ()
    ) -> None:
        name, expected = STEPS[n]
        expected = expected.format(platform=PLATFORM_LABEL[self.link.platform])
        why = next((w for w in (need() for need in needs) if w), None)
        if why:
            result = StepResult.not_attempted(n, name, expected, why)
        else:
            began = time.monotonic()
            try:
                out = await run()
            except Exception as e:  # a step that raises has failed; it does not stop the next one
                out = Outcome(False, self.describe(e))
            seen = selftest.scrub(out.seen, self.pieces)
            result = StepResult(n, name, out.ok, expected, seen, time.monotonic() - began, out.ratio)
        self.steps.append(result)
        print(
            f"[{n}/{len(STEPS)}] {'PASS' if result.ok else 'FAIL'} {name} ({result.seconds:.1f} s)"
            f" {result.seen[:160]}"
        )

    def needs_bot(self) -> str | None:
        return None if self.bot_live else "the bot is not in the call (step 1)"

    def needs_speaker(self) -> str | None:
        return None if self.speaker.joined else "the speaker is not in the call (step 2)"

    # -- 1: the bar goes live
    async def join(self) -> Outcome:
        r = self.command(f"/meeting {self.link.reveal()}")
        if r.status_code != 200:
            refused = selftest.scrub(f"the command was refused (HTTP {r.status_code}): {r.text}", self.pieces)
            return Outcome(False, refused[:300])

        def settled() -> str | None:
            state = self.call_state()
            if state is None:
                return "ended"
            return "live" if state["state"] == "live" else None

        got = await until(settled, JOIN_S)
        if got == "live":
            self.bot_live = True
            return Outcome(True, f"state=live platform={self.link.platform}")
        if got == "ended":
            return Outcome(False, f"the call ended before it went live: {self.notices()}")
        return Outcome(False, f"still joining after {JOIN_S:.0f} s: {self.notices()}")

    async def bot_listed(self) -> bool | None:
        """Whether the speaker's participant list has the bot. None when the list could not be read."""
        try:
            return selftest.has_name(await self.speaker.names(), "transcribing")
        except Exception:
            return None

    # -- 2: the speaker says a sentence and it arrives as a call line
    async def speak(self) -> Outcome:
        try:
            await self.speaker.join(self.link)
        except Exception as e:
            return Outcome(False, f"the speaker could not join: {self.describe(e)}")
        if self.observer is not None:
            try:
                await self.observer.join(self.link)
            except Exception as e:  # the observer only adds evidence: its failure fails no step
                self.observer = None
                print(f"the observer could not join, and is left out: {self.describe(e)}")
        if not self.bot_live:
            return Outcome(False, "the speaker joined, but the bot is not in the call (step 1)")

        async def listed() -> bool:
            return await self.bot_listed() is True

        # a wait for the bot to show in the list, so the sentence is not said before the audio is up
        self.bot_seen = bool(await until(listed, SEE_BOT_S, SLOW_POLL_S))
        before = self.last_id()
        await self.speaker.say(WARMUP)

        def warm() -> bool:
            return any(m.get("sender_kind") == "meeting" for m in self.messages(before))

        warmed = bool(await until(warm, WARMUP_HEARD_S))
        cursor = self.last_id()
        said = await self.speaker.say(SENTENCE)
        seen = ""

        def arrived() -> bool:
            nonlocal seen
            seen = " ".join(m["text"] for m in self.messages(cursor) if m.get("sender_kind") == "meeting")
            return selftest.matches(SENTENCE, seen)

        ok = await until(arrived, HEARD_S)
        note = " (the speaker was talked over)" if said.startswith("Interrupted") else ""
        note += " (the warm-up line was heard)" if warmed else " (the warm-up line was lost)"
        if not self.bot_seen:
            note += (
                " (the speaker never saw the bot in its participant list, so it may have spoken too early)"
            )
        return Outcome(
            bool(ok), (seen or "no call line reached the room") + note, selftest.match_ratio(SENTENCE, seen)
        )

    # -- 3: a name wakes claude-1
    async def wake(self) -> Outcome:
        """Step 3, said ``CALLOUTS`` times (one by default): each must wake claude-1. Several make one call
        measure how often a call-out is missed (#602), with each miss's heard text in the report."""
        hits, misses, last_seen = 0, [], ""
        for _ in range(CALLOUTS):
            ok, seen, heard = await self.one_wake(HEARD_S if CALLOUTS == 1 else REPEAT_HEARD_S)
            if ok:
                hits, last_seen = hits + 1, seen
            else:
                misses.append(heard or "nothing")
        if CALLOUTS == 1:
            if hits:
                return Outcome(True, last_seen, selftest.match_ratio(CALL_OUT, last_seen))
            why = (
                f"claude-1 was not woken; call lines heard instead: {misses[0]}"
                if misses[0] != "nothing"
                else ("claude-1 was not woken, and no call line reached the room")
            )
            return Outcome(False, why[:400], 0.0)
        shown = f"{hits} of {CALLOUTS} call-outs woke claude-1"
        if misses:
            shown += "; missed ones heard as: " + " / ".join(misses)
        return Outcome(hits == CALLOUTS, shown[:600], hits / CALLOUTS)

    async def one_wake(self, heard_s: float) -> tuple[bool, str, str]:
        """Say the call-out once. ``(woken, the line that woke it, the call lines heard)``."""
        seen = ""
        spoken_at: list[float] = []  # when the speaker finished saying it
        cursor = self.last_id()

        async def woken() -> bool:
            nonlocal seen
            began = time.monotonic()
            while True:
                limit = (spoken_at[0] if spoken_at else began + SPEAK_S) + heard_s
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    return False
                got = await self.agent.wait(ROOM, timeout_s=max(1, min(30, int(remaining))))
                lines = selftest.batch_items(str(got.get("text") or ""))
                seen = " ".join(i.text for i in lines if i.kind == "meeting" and i.to_you)
                if selftest.matches(CALL_OUT, seen):
                    return True

        observed_from = selftest.last_end(await self.observer.segments()) if self.observer else 0.0
        waiting = asyncio.ensure_future(woken())
        try:
            await self.speaker.say(CALL_OUT)
            spoken_at.append(time.monotonic())
            ok = await waiting
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        # What did arrive, so a miss can be told apart: a misheard name (a call line that addresses nobody),
        # or nothing heard at all.
        heard = " / ".join(m["text"] for m in self.messages(cursor) if m.get("sender_kind") == "meeting")
        if not ok and self.observer is not None:
            observed = selftest.heard_since(await self.observer.segments(), observed_from)
            heard = f"{heard or 'nothing'} (the observer heard: {observed or 'nothing'})"
        return ok, seen, heard

    # -- 4: an @call line is heard by the other bot
    async def hear(self) -> Outcome:
        r = self.command("/meeting voice on")
        if r.status_code != 200 or "voice is on" not in r.text:
            return Outcome(
                False, selftest.scrub(f"/meeting voice on: HTTP {r.status_code} {r.text}", self.pieces)[:300]
            )
        began = selftest.last_end(await self.speaker.segments())
        for attempt in range(2):
            said = await self.agent.say(ROOM, f"@call {REPLY}")
            if said.get("posted_id"):
                break
            if attempt == 0:
                await self.agent.read(ROOM)  # say() asks for a read() first when something is unread
        else:
            return Outcome(False, f"claude-1's say was refused: {str(said.get('code') or said)[:200]}")
        heard = ""

        async def arrived() -> bool:
            nonlocal heard
            heard = selftest.heard_since(await self.speaker.segments(), began)
            return selftest.matches(REPLY, heard)

        ok = await until(arrived, HEARD_S, SLOW_POLL_S)
        if ok:
            # The rest of the sentence comes in the next transcript batch (~3 s): wait for it, so the report
            # shows what was heard, not the part that was enough to pass (which read as a clipped line).
            await asyncio.sleep(TAIL_S)
            heard = selftest.heard_since(await self.speaker.segments(), began)
        return Outcome(
            bool(ok), heard or "the speaker heard nothing from the bot", selftest.match_ratio(REPLY, heard)
        )

    # -- 5: a snapshot is a room file, a JPEG, and a card
    async def snapshot(self) -> Outcome:
        cursor = self.last_id()
        r = self.command("/meeting snapshot")
        if r.status_code != 200:
            return Outcome(
                False, selftest.scrub(f"/meeting snapshot: HTTP {r.status_code} {r.text}", self.pieces)[:300]
            )

        def card() -> dict[str, Any] | None:
            return next((m for m in self.messages(cursor) if m.get("files")), None)

        found = await until(card, SNAPSHOT_S)
        if not found:
            return Outcome(False, f"no card reached the room: {self.notices()}")
        shape = re.search(
            r"artifact: (screen-\d{6}\.(\w+)) \| (\d+) \| sha256:([0-9a-f]{64}) "
            r"\| switchboard:file/([0-9a-f]{32})",
            found["text"],
        )
        if shape is None:
            return Outcome(False, "the card does not have the shape of a room file's card")
        name, ext, size, digest, file_id = shape.groups()
        got = self.web.get(f"/api/files/{file_id}", headers=self.headers())
        data = got.content
        problems = []
        if got.status_code != 200:
            problems.append(f"download answered HTTP {got.status_code}")
        if not data.startswith(b"\xff\xd8\xff"):
            problems.append("the bytes are not a JPEG")
        if ext != "jpg":
            problems.append(f"the card names a .{ext} file")
        if len(data) != int(size) or hashlib.sha256(data).hexdigest() != digest:
            problems.append("the stored file does not match its card")
        seen = f"{name}, {len(data)} bytes" + (
            f"; {'; '.join(problems)}" if problems else ", a JPEG that matches its card"
        )
        return Outcome(not problems, seen)

    # -- 6: two voices (the voice patch, #713)
    async def voices(self) -> Outcome:
        """The speaker says two lines in two voices and an unknown voice's line. Both lines must reach the
        room
        as call lines (the bot heard them), neither answer may say the voice was not found (Kokoro has both),
        and the third must (the patch falls back and still speaks)."""
        cursor = self.last_id()
        answers = []
        for line, voice in zip(VOICE_LINES, VOICES, strict=True):
            answers.append(await self.speaker.say(line, voice=voice))
        unknown = await self.speaker.say(UNKNOWN_LINE, voice=UNKNOWN_VOICE)
        if any(NOT_FOUND in a for a in answers):
            return Outcome(False, f"Kokoro has no such voice among {VOICES}: {' / '.join(answers)}"[:300])
        if NOT_FOUND not in unknown:
            return Outcome(False, f"an unknown voice was not reported as such: {unknown[:200]}")
        heard = ""

        def arrived() -> bool:
            nonlocal heard
            heard = " / ".join(m["text"] for m in self.messages(cursor) if m.get("sender_kind") == "meeting")
            return all(selftest.matches(line, heard) for line in VOICE_LINES)

        ok = await until(arrived, HEARD_BOTH_S)
        shown = f"voices {', '.join(VOICES)} accepted; unknown voice fell back; heard: {heard or 'nothing'}"
        return Outcome(bool(ok), shown[:400])

    # -- 7: both bots leave
    async def leave(self) -> Outcome:
        if not self.bot_seen:  # "the bot is gone" means nothing if the bot was never seen
            self.bot_seen = await self.bot_listed() is True
        began = time.monotonic()
        r = self.command("/meeting leave")
        if r.status_code != 200:
            return Outcome(
                False, selftest.scrub(f"/meeting leave: HTTP {r.status_code} {r.text}", self.pieces)[:300]
            )

        def ended() -> bool:
            return self.call_state() is None and (self.meeting_row() or ("", None))[0] == "ended"

        if not await until(ended, LEAVE_S):
            return Outcome(False, f"the bar did not end within {LEAVE_S:.0f} s: {self.notices()}")
        bar = time.monotonic() - began
        error = (self.meeting_row() or ("", None))[1]

        async def gone() -> bool:
            return await self.bot_listed() is False

        if not await until(gone, LEAVE_S, SLOW_POLL_S):
            last = await self.bot_listed()
            why = "the speaker can't read its participant list" if last is None else "the bot is still listed"
            return Outcome(False, f"the bar ended after {bar:.1f} s, but {why}")
        await self.speaker.leave()
        seen = (
            f"the bar ended after {bar:.1f} s"
            + (f" with the error {error!r}" if error else "")
            + "; the speaker left"
        )
        if not self.bot_seen:
            return Outcome(
                False,
                seen + "; the speaker never saw the bot in its participant list, so it can't tell it left",
            )
        return Outcome(error is None, seen)

    async def run(self) -> None:
        await self.step(1, self.join)
        await self.step(2, self.speak)
        await self.step(3, self.wake, [self.needs_bot, self.needs_speaker])
        await self.step(4, self.hear, [self.needs_bot, self.needs_speaker])
        await self.step(5, self.snapshot, [self.needs_bot])
        if self.voice_patch:
            await self.step(6, self.voices, [self.needs_bot, self.needs_speaker])
        await self.step(7, self.leave, [self.needs_bot, self.needs_speaker])
        if self.observer is not None:
            with contextlib.suppress(Exception):
                await self.observer.leave()

    async def leave_everything(self, problems: list[str]) -> None:
        """Whatever the steps did: the bar's call ends, and the speaker leaves. Each is tried alone."""
        try:
            if self.call_state() is not None:
                self.command("/meeting leave")
                await until(lambda: self.call_state() is None, LEAVE_S)
        except Exception as e:
            problems.append(f"the bot's call could not be ended: {self.describe(e)}")
        try:
            await self.speaker.leave()
        except Exception as e:
            problems.append(f"the speaker could not leave the call: {self.describe(e)}")
        if self.observer is not None:
            try:
                await self.observer.leave()
            except Exception as e:
                problems.append(f"the observer could not leave the call: {self.describe(e)}")


# ---------------------------------------------------------------------- the test
def refuse(reason: str) -> None:
    pytest.fail(reason, pytrace=False)


def read_link() -> SecretLink:
    where = os.environ.get(LINK_ENV, "").strip()
    if not where:
        refuse(
            f"{LINK_ENV} names no file. Put the call's join link in a file of mode 0600 and name it there."
        )
    problem = ""
    try:
        return selftest.read_link_file(where)
    except selftest.LinkFileError as e:
        problem = str(e)
    return pytest.fail(f"{LINK_ENV}: {problem}", pytrace=False)  # outside the except: no chained traceback


def check_docker() -> None:
    if shutil.which("docker", path=REAL_PATH) is None:
        refuse("docker is not on PATH")
    if docker("version", "--format", "{{.Server.Version}}", timeout=30.0).returncode != 0:
        refuse("docker is installed, but its daemon does not answer")
    if docker("image", "inspect", IMAGE, timeout=30.0).returncode != 0:
        refuse(f"the joinly image is not here: docker pull {IMAGE}")


@contextlib.contextmanager
def sigterm_is_an_interrupt() -> Iterator[None]:
    """A suite runner stops a run with SIGTERM, which ends Python at once and runs no ``finally``. As an
    interrupt it runs them, and they remove the containers (the bots are in a real call until they go)."""

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, stop)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


async def test_every_meetings_feature_against_a_real_call(
    tmp_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both bots join one real call and every meetings feature is used in it. Fails (and names the steps) if
    any of the seven steps fails, if a step could not start, if a bot or a container is left behind, or if any
    piece of the call's link is found in the broker's database, messages or log. Each step's expected and
    seen strings and timings are in the report."""
    link = read_link()
    check_docker()
    pieces = link.parts()
    out_dir = Path(os.environ.get(OUT_ENV) or tempfile.mkdtemp(prefix="sb-selftest-"))
    containers = {"switchboard": Joinly("switchboard"), "speaker": Joinly("speaker")}
    if OBSERVER:
        containers["observer"] = Joinly("observer")
    steps: list[StepResult] = []
    problems: list[str] = []
    saved: list[str] = []
    stock = os.environ.get(STOCK_ENV, "") == "1"
    zoom_fix = link.platform == "zoom" and not stock
    preroll_fix = not stock
    fixes: list[tuple[str, str, str, str, str]] = []  # url, sha256, file name, where it goes, what it is
    if zoom_fix:
        fixes.append((ZOOM_FIX_URL, ZOOM_FIX_SHA256, "zoom.py", ZOOM_PY, "the Zoom fix"))
    if preroll_fix:
        fixes.append(
            (PREROLL_FIX_URL, PREROLL_FIX_SHA256, "transcription.py", CONTROLLER_PY, "the first-word fix")
        )
    voice_files: list[tuple[Path, str]] = []
    if not stock:  # the voice patch (#713) is this repository's own files, pinned by sha256 all the same
        for name, (voice_sha256, target) in VOICE_PATCH.items():
            copy = Path(tempfile.mkdtemp(prefix="sb-selftest-fix-")) / name
            copy.write_bytes(selftest.check_pinned((VOICE_PATCH_DIR / name).read_bytes(), voice_sha256))
            copy.chmod(0o644)
            voice_files.append((copy, target))
    for fix_url, fix_sha256, name, target, what in fixes:
        fix = Path(tempfile.mkdtemp(prefix="sb-selftest-fix-")) / name
        try:
            fix.write_bytes(selftest.check_pinned(httpx.get(fix_url, timeout=30.0).content, fix_sha256))
        except (httpx.HTTPError, selftest.PinnedFileError) as e:
            refuse(f"{what} could not be fetched ({type(e).__name__}: {e}); {STOCK_ENV}=1 runs stock joinly")
        fix.chmod(0o644)  # the image's user (uid 1001) reads it through the mount
        for box in containers.values():
            box.mounts.append((fix, target))
    for box in containers.values():
        box.mounts.extend(voice_files)
    try:
        with sigterm_is_an_interrupt():
            try:
                for box in containers.values():
                    box.start()
                for box in containers.values():
                    box.wait_healthy()
                await run_in_a_broker(tmp_home, containers, link, steps, problems, caplog)
            except Exception as e:
                problems.append(selftest.scrub(f"{type(e).__name__}: {e}", pieces)[:300])
    finally:
        try:
            # Missing steps count as failed, and a failed step is what keeps the logs. Fill them
            # before the containers go: docker logs needs the container, and --rm drops it on remove.
            specs = selftest.step_specs(STEPS, PLATFORM_LABEL[link.platform])
            if stock:
                del specs[6]  # the voices step needs the patch
            selftest.fill_unattempted(steps, specs)
            saved = kept_logs(containers, out_dir, pieces, steps)
        finally:
            # sync, so it also runs after an interrupt: no bot stays in the call once its container is gone
            removed = [box.remove() for box in containers.values()]
    problems += [why for why in removed if why]
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": link.platform,
        "joinly_image": IMAGE,
        "zoom_fix": "mounted (joinly-ai/joinly#187)" if zoom_fix else "none",
        "preroll_fix": f"mounted (#620, {PREROLL_FIX_COMMIT[:7]})" if preroll_fix else "none",
        "voice_patch": "mounted (#713, deploy/joinly)" if not stock else "none",
        "callouts": CALLOUTS,
        "bot_name": BOT_NAME,
        "speaker_name": SPEAKER_NAME,
        "containers_removed": not any(removed),
        "problems": problems,
    }
    if saved:
        meta["container_logs"] = ", ".join(saved)
    try:
        _, md = selftest.write_report(out_dir, steps, pieces, meta)
        print("\n" + md.read_text(encoding="utf-8"))
        print(f"report: {out_dir / selftest.REPORT_JSON}")
    except (selftest.ReportLeak, OSError) as e:
        problems.append(f"the report was not written: {type(e).__name__}")
    failed = selftest.failed_steps(steps)
    if failed or problems:
        refuse(
            "meeting self-test failed"
            + (f"; steps: {', '.join(failed)}" if failed else "")
            + (f"; problems: {' | '.join(problems)}" if problems else "")
            + f"; report in {out_dir}"
        )


async def run_in_a_broker(
    home: Path,
    containers: dict[str, Joinly],
    link: SecretLink,
    steps: list[StepResult],
    problems: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The throwaway broker, its room, its scripted agent and the speaker; the seven steps; and, last, the
    check that nothing the broker kept holds the link."""
    cfg = Config(human_name="alice").replace(meeting=MeetingCfg(joinly_url=containers["switchboard"].url))
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0, rate_limit_s=0.0)
    broker = InProcBroker(home, cfg).start()
    web = broker.web_client()
    kept_messages = ""
    try:
        assert web.post("/api/rooms", json={"name": ROOM}, headers=broker.write_headers()).status_code == 200
        observing = (
            Speaker(containers["observer"].url, OBSERVER_NAME)
            if "observer" in containers
            else contextlib.nullcontext()
        )
        async with (
            FakeAgent(broker.home, "selftest-claude") as agent,
            Speaker(containers["speaker"].url) as speaker,
            observing as observer,
        ):
            joined = await agent.join(
                ROOM, "claude-1"
            )  # before the call: its spoken names are the bot's hints
            if not joined.get("ok"):
                raise RuntimeError(f"the scripted agent could not join: {joined.get('code')}")
            world = World(broker, web, agent, speaker, link, steps, observer)
            try:
                await world.run()
            finally:
                await world.leave_everything(problems)
                with contextlib.suppress(Exception):
                    kept_messages = json.dumps(world.messages())
    finally:
        web.close()
        broker.stop()
    blobs: dict[str, str | bytes] = {"the broker's log": caplog.text, "the room's messages": kept_messages}
    for suffix, label in (("", "the broker's database"), ("-wal", "the broker's write-ahead log")):
        path = Path(str(broker.paths.db) + suffix)
        if path.exists():
            blobs[label] = path.read_bytes()
    problems += [f"a piece of the link is in {name}" for name in selftest.leaks(link.parts(), blobs)]
