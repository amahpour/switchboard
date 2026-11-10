"""The relay of a hosted call: what it reads, what it does and why it stops (#623, DESIGN.md §54).

``Relay`` runs here against a scripted broker and a scripted joinly, both in memory. The broker answers a
speech poll when the test puts an answer in its queue (a long poll is woken by an event, as the real one is),
and a joinly's transcript changes when the test says so. Time is a list the relay's ``sleep`` appends to and a
clock that moves by what it was asked to wait, so a backoff, a ``Retry-After`` and the broker's quiet limit
are read off numbers and nothing sleeps. The relay against the real broker and the real wire is in
``tests/integration/test_relay_and_broker.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from switchboard import cli
from switchboard.meeting import hosted, keys, relay
from switchboard.meeting.lines import Segment
from switchboard.meeting.relay import (
    Exit,
    Relay,
    RelayConfig,
    Reply,
    Secret,
    Unreachable,
    UsageError,
    read_secrets,
    server_target,
)

KEY = "sbc_" + "ab" * 32
LINK = "https://zoom.us/j/123456789?pwd=hunter2"
FP = keys.fingerprint(keys.key_hash(KEY))


def seg(
    text: str, start: float, end: float, speaker: str | None = "bob", role: str = "participant"
) -> Segment:
    return Segment(text=text, start=start, end=end, speaker=speaker, role=role)


def ok(**body: Any) -> Reply:
    return Reply(200, body)


# --------------------------------------------------------------------------------- the stand-ins
class Time:
    """The relay's clock and its sleeps: a sleep moves the clock by what it was asked to wait."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class Broker:
    """The broker's key routes. A push is answered 200 unless the test queued something else; a speech poll
    with ``wait`` 0 is answered at once, and any other waits for ``answer`` (a long poll), until the relay
    ``abort``s."""

    def __init__(self, hints: Sequence[str] = ("Claude", "Codex")) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pushed: list[dict[str, Any]] = []
        self.push_answers: list[Reply | Exception] = []
        self.first: Reply | Exception = ok(lines=[], hints=list(hints), voice=False, ended=False)
        self.polls: asyncio.Queue[Reply | Exception] = asyncio.Queue()
        self.aborted = False

    def answer(self, **body: Any) -> None:
        """A long poll's answer, now or when the next poll comes."""
        self.polls.put_nowait(ok(**{"lines": [], "hints": [], "voice": False, "ended": False, **body}))

    async def post(self, path: str, body: dict[str, Any], timeout: float) -> Reply:
        self.calls.append((path, body))
        if self.aborted:
            raise Unreachable("closed")
        if path == "/mcp/meeting/leave":
            return ok(ended=True)
        if path == relay.PUSH_PATH:
            got = self.push_answers.pop(0) if self.push_answers else ok(accepted=0, duplicate=0, ended=False)
            if isinstance(got, Exception):
                raise got
            if got.status == 200:
                self.pushed.extend(body["segments"])
            return got
        if "done" in body:
            assert body.get("wait") == 0
            return ok(lines=[], voice=True, ended=False, speech_id=None)
        if body.get("wait") == 0 and self.first is not None:
            first, self.first = self.first, None  # type: ignore[assignment]
            if isinstance(first, Exception):
                raise first
            return first
        got = await self.polls.get()
        if isinstance(got, Exception):
            raise got
        return got

    def abort(self) -> None:
        self.aborted = True
        self.polls.put_nowait(Unreachable("closed"))

    def pushes(self) -> list[list[dict[str, Any]]]:
        return [b["segments"] for p, b in self.calls if p == relay.PUSH_PATH]

    def texts(self) -> list[str]:
        return [s["text"] for s in self.pushed]


class Joinly:
    """A joinly: its transcript is what the test says; ``changed`` returns when the test says it changed."""

    def __init__(self) -> None:
        self.segs: list[Segment] = []
        self.opens = 0
        self.refuse_opens = 0
        self.joins: list[tuple[str, str]] = []
        self.fail_join = False
        self.hold_join = asyncio.Event()
        self.gate_join = False
        self.left = 0
        self.closed = 0
        self.spoken: list[str] = []
        self.voices: list[str | None] = []  # the voice each line was asked for
        self.speak_answers: list[str] = []  # answers to give in turn, before speak_answer
        self.speak_answer = "Finished speaking."
        self.fail_speak = False
        self.hold_speech = asyncio.Event()
        self.gate_speech = False
        self.dropped = False
        self._update = asyncio.Event()

    def say(self, *more: Segment) -> None:
        self.segs.extend(more)
        self._update.set()

    async def open(self) -> None:
        self.opens += 1
        if self.refuse_opens:
            self.refuse_opens -= 1
            raise ConnectionError("not up yet")

    async def close(self) -> None:
        self.closed += 1

    async def join(self, url: str, name: str, timeout: float) -> None:
        self.joins.append((url, name))
        if self.gate_join:
            await self.hold_join.wait()
        if self.fail_join:
            raise RuntimeError("could not join")

    async def segments(self) -> list[Segment]:
        if self.dropped:
            raise RuntimeError("the session dropped")
        return list(self.segs)

    async def changed(self, stop: asyncio.Event, timeout: float) -> None:
        waiting = asyncio.ensure_future(self._update.wait())
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
            stopping.cancel()
            await asyncio.gather(waiting, stopping, return_exceptions=True)
        self._update.clear()

    async def speak(self, text: str, voice: str | None = None) -> str:
        self.spoken.append(text)
        self.voices.append(voice)
        if self.gate_speech:
            await self.hold_speech.wait()
        if self.fail_speak:
            raise RuntimeError("could not speak")
        return self.speak_answers.pop(0) if self.speak_answers else self.speak_answer

    async def leave(self) -> None:
        self.left += 1


class Rig:
    """A relay with its stand-ins, run as a task."""

    def __init__(
        self, cfg: RelayConfig | None = None, link: str | None = LINK, broker: Broker | None = None
    ) -> None:
        self.time, self.broker, self.joinly = Time(), broker or Broker(), Joinly()
        self.said: list[str] = []
        self.hints: list[Sequence[str]] = []

        def open_joinly(hints: Sequence[str], _voice: str) -> Joinly:
            self.hints.append(list(hints))
            return self.joinly

        self.relay = Relay(
            cfg or RelayConfig(),
            Secret(KEY),
            Secret(link) if link else None,
            self.broker,
            open_joinly,
            sleep=self.time.sleep,
            now=self.time.now,
            say=self.said.append,
        )
        self.task: asyncio.Task[Exit] | None = None

    def start(self) -> Rig:
        self.task = asyncio.create_task(self.relay.run())
        return self

    async def until(self, cond: Callable[[], bool], what: str = "the event") -> None:
        """Let the relay's tasks run until ``cond`` holds. It yields to the loop, never sleeps: a bound on the
        number of turns stands in for a timeout: a relay that does not get there fails, it does not hang."""
        for _ in range(20000):
            if cond():
                return
            await asyncio.sleep(0)
        raise AssertionError(f"waited for {what}; relay said {self.said}")

    async def code(self) -> Exit:
        assert self.task is not None
        return await asyncio.wait_for(self.task, 30)

    def heard(self) -> str:
        return "\n".join(self.said)


async def joined(rig: Rig) -> Rig:
    rig.start()
    await rig.until(lambda: bool(rig.broker.pushes()), "the first push")
    return rig


# ----------------------------------------------------------------- guards on the numbers and the words
def test_the_relays_numbers_are_the_brokers() -> None:
    """The relay holds copies of what the broker holds a source to, so it need not import the broker. They
    must not drift: a batch bigger than the broker takes would be refused whole, a poll longer than it
    allows would be cut, and a relay that tries longer than the broker waits tries for nothing."""
    assert relay.BATCH_MAX == hosted.BATCH_MAX
    assert relay.SPEECH_WAIT_S == hosted.SPEECH_WAIT_S <= hosted.SPEECH_WAIT_MAX_S
    assert relay.QUIET_S == hosted.QUIET_S
    assert relay.JOIN_TIMEOUT_S < hosted.JOIN_WINDOW_S  # it gives up joining before the broker gives up on it


def test_the_commands_defaults_are_the_relays() -> None:
    """``cli.py`` writes the defaults out (the relay imports fastmcp, and the command line stays light)."""
    assert cli.RELAY_JOINLY == relay.DEFAULT_JOINLY
    assert cli.RELAY_VOICE == relay.DEFAULT_VOICE


def test_the_command_takes_its_switches_from_the_environment_a_container_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Compose file gives the relay its non-secret settings by environment (the server, the model and two
    switches), because a command list cannot leave a flag out. Fails if one is not read, or if a blank one
    counts as on."""
    for name in ("SWITCHBOARD_URL", "SWITCHBOARD_MEETING_STT_MODEL", "SWITCHBOARD_RELAY_NO_JOIN",
                 "SWITCHBOARD_RELAY_ALLOW_HTTP"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    plain = cli.build_parser().parse_args(["meeting", "relay"])
    assert (plain.server, plain.model, plain.no_join, plain.allow_http) == (None, "", False, False)
    monkeypatch.setenv("SWITCHBOARD_URL", "https://sb.example.com")
    monkeypatch.setenv("SWITCHBOARD_MEETING_STT_MODEL", "large-v3-turbo")
    monkeypatch.setenv("SWITCHBOARD_RELAY_NO_JOIN", "1")
    monkeypatch.setenv("SWITCHBOARD_RELAY_ALLOW_HTTP", "true")
    got = cli.build_parser().parse_args(["meeting", "relay"])
    assert (got.server, got.model, got.no_join, got.allow_http) == (
        "https://sb.example.com",
        "large-v3-turbo",
        True,
        True,
    )
    monkeypatch.setenv("SWITCHBOARD_RELAY_NO_JOIN", "")  # a blank variable (Compose's default) is off
    monkeypatch.setenv("SWITCHBOARD_RELAY_ALLOW_HTTP", "0")
    off = cli.build_parser().parse_args(["meeting", "relay"])
    assert (off.no_join, off.allow_http) == (False, False)


def test_a_relay_that_should_join_but_has_no_link_is_a_usage_error_before_it_touches_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--link-file`` is missing and ``--no-join`` was not said: a mistake, not a call to join none. The
    relay stops with exit 2 and a sentence, and has read nothing and connected to nothing. Fails if it
    starts a relay that joins no call without being told to."""
    key = write(tmp_path / "key", KEY)
    code = relay.main(
        server="https://sb.example.com", joinly_url="http://127.0.0.1:1/", key_file=key, link_file=None
    )
    assert code == Exit.USAGE
    err = capsys.readouterr().err
    assert "--link-file" in err and KEY not in err


def test_a_key_file_that_cannot_be_used_stops_the_relay_with_exit_2_and_repeats_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = write(tmp_path / "key", KEY, 0o644)
    link = write(tmp_path / "link", LINK)
    code = relay.main(
        server="https://sb.example.com", joinly_url="http://127.0.0.1:1/", key_file=key, link_file=link
    )
    assert code == Exit.USAGE
    err = capsys.readouterr().err
    assert "644" in err and KEY not in err and "hunter2" not in err


def test_every_exit_code_has_a_distinct_number_and_a_sentence() -> None:
    assert len({int(e) for e in Exit}) == len(Exit)
    assert set(relay.WHY) == set(Exit)
    assert Exit.ENDED == 0  # a call that ended is a clean exit: the container did what it was for


# --------------------------------------------------------------------------------------- what is read
def test_a_secret_shows_nothing_of_itself() -> None:
    """The key and the link are held as ``Secret``s so that a stray f-string, ``%s`` or traceback prints
    nothing of them. ``reveal`` is the one way to the text. Fails if any of the four spellings leaks."""
    s = Secret(KEY)
    assert KEY not in f"{s} {s!r} {s!s}" + "%s" % s + str([s]) + format(s, ">80")
    assert s.reveal() == KEY and type(s.reveal()) is str


def write(path: Path, text: str, mode: int = 0o600) -> str:
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def test_the_key_and_the_link_are_read_from_files_only_their_owner_reads(tmp_path: Path) -> None:
    key, link = read_secrets(write(tmp_path / "key", KEY + "\n"), write(tmp_path / "link", LINK + "\n"))
    assert (key.reveal(), link.reveal() if link else None) == (KEY, LINK)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_file_others_can_read_is_refused_unless_trusted(tmp_path: Path, mode: int) -> None:
    """The link has a passcode in it and the key is a bearer: a file group or world can read is refused, with
    the mode in the message, and the message holds neither. ``mode_check=False`` (a container shows the host's
    modes, and the host script made the files) takes it."""
    path = write(tmp_path / "key", KEY, mode)
    with pytest.raises(UsageError) as e:
        read_secrets(path, None)
    assert f"{mode:03o}" in str(e.value) and KEY not in str(e.value)
    assert read_secrets(path, None, mode_check=False)[0].reveal() == KEY


def test_a_link_to_a_file_is_not_followed(tmp_path: Path) -> None:
    """``O_NOFOLLOW``: a link planted at the name must not lead the relay to read somewhere else."""
    target = write(tmp_path / "elsewhere", KEY)
    (tmp_path / "key").symlink_to(target)
    with pytest.raises(UsageError):
        read_secrets(str(tmp_path / "key"), None)


def test_what_cannot_be_read_is_an_error_that_repeats_nothing(tmp_path: Path) -> None:
    cases = [
        (write(tmp_path / "a", "sbl_" + "ab" * 32), None),  # a listener key is not a call key
        (write(tmp_path / "b", "sbc_" + "zz" * 32), None),
        (write(tmp_path / "c", ""), None),
        (str(tmp_path / "missing"), None),
        (str(tmp_path), None),  # a directory
        (write(tmp_path / "d", KEY), write(tmp_path / "e", "http://zoom.us/j/1?pwd=hunter2")),  # not https
        (write(tmp_path / "f", KEY), write(tmp_path / "g", "https://example.com/j/1?pwd=hunter2")),
    ]
    for key_file, link_file in cases:
        with pytest.raises(UsageError) as e:
            read_secrets(key_file, link_file)
        text = str(e.value)
        assert "hunter2" not in text and "ab" * 8 not in text and "zz" * 8 not in text, text


def test_both_come_from_stdin_key_first_and_a_link_that_is_not_one_is_refused() -> None:
    import io

    key, link = read_secrets("-", "-", stdin=io.StringIO(KEY + "\n" + LINK + "\n"))
    assert (key.reveal(), link.reveal() if link else None) == (KEY, LINK)
    with pytest.raises(UsageError):
        read_secrets("-", "-", stdin=io.StringIO(KEY + "\n"))  # no second line: nothing to read
    with pytest.raises(UsageError):
        read_secrets("-", "-", stdin=io.StringIO(KEY + "\nhttps://example.com/j/1\n"))


def test_a_relay_with_no_link_wants_only_a_key(tmp_path: Path) -> None:
    key, link = read_secrets(write(tmp_path / "key", KEY), None)
    assert link is None and key.reveal() == KEY


def test_the_brokers_address_is_https_or_this_machines_own() -> None:
    """The key is a bearer: it must not cross a network in the clear unless the person says that network is
    theirs. ``localhost`` is this machine; anything else needs ``allow_http``."""
    assert server_target("https://sb.example.com") == ("https", "sb.example.com", 443, "")
    assert server_target("https://sb.example.com:8443/sb/") == ("https", "sb.example.com", 8443, "/sb")
    assert server_target("http://localhost:7419")[:3] == ("http", "localhost", 7419)
    assert server_target("http://switchboard.localhost:7419")[1] == "switchboard.localhost"
    for bad in (
        "http://sb.example.com",
        "http://192.0.2.5:7419",
        "ftp://sb.example.com",
        "sb.example.com",
        "",
    ):
        with pytest.raises(UsageError):
            server_target(bad)
    assert server_target("http://192.0.2.5:7419", allow_http=True)[:3] == ("http", "192.0.2.5", 7419)
    with pytest.raises(UsageError):
        server_target("https://user:pw@sb.example.com")  # a password in an address ends up in logs
    with pytest.raises(UsageError):
        server_target("https://sb.example.com:notaport")


# ------------------------------------------------------------------------------- the relay's run
async def test_it_joins_with_the_rooms_names_and_marks_the_call_live() -> None:
    """The first thing it asks is the room's names (answered at once, ``wait`` 0), which joinly is given as
    hints before it joins; it prints the key's fingerprint; it joins with the link, under the bot's name; and
    its first push, empty, is what marks the broker's call live. Fails if it joined before it had the names,
    never marked the call live, or showed another fingerprint than the call bar's."""
    rig = await joined(Rig())
    assert rig.hints == [["Claude", "Codex"]]
    assert rig.joinly.joins == [(LINK, "switchboard (transcribing)")]
    assert rig.broker.calls[0] == (relay.SPEECH_PATH, {"wait": 0, "turns": True})  # the names, first
    assert rig.broker.pushes()[0] == []  # and the first push is empty
    assert rig.said[0] == f"relay: key {FP} (the room's call bar shows the same)"
    rig.relay.finish(Exit.ENDED)
    assert await rig.code() == Exit.ENDED


async def test_the_first_push_is_asked_again_until_the_broker_has_it() -> None:
    """The empty first push is what marks the call live, and a call nobody speaks in sends nothing else. An
    error or a 429 on that one push must not leave the call "joining" for good. Fails if it is tried once."""
    rig = Rig()
    rig.broker.push_answers = [Unreachable("ConnectionResetError"), Reply(429, {}, 2.0), ok()]
    await joined(rig)
    await rig.until(lambda: len(rig.broker.pushes()) == 3, "the third try")
    assert rig.broker.pushes() == [[], [], []]
    assert rig.relay.exit is None  # and nothing ended
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_it_pushes_each_new_segment_once_and_not_the_bots_own() -> None:
    """The transcript is the whole call every time it is read: only what is new goes. The bot's own speech
    (another ``role``) and empty text are not what the call said. Fails if a segment goes twice, or the
    bot's own words are pushed back into the room."""
    rig = await joined(Rig())
    rig.joinly.say(
        seg("Hello there.", 0, 2), seg("I can hear you.", 3, 4, speaker="switchboard", role="assistant")
    )
    await rig.until(lambda: rig.broker.texts() == ["Hello there."], "the first segment")
    rig.joinly.say(seg("   ", 5, 6), seg("Claude, can you check?", 7, 9))
    await rig.until(lambda: len(rig.broker.pushed) == 2, "the second push")
    assert rig.broker.texts() == ["Hello there.", "Claude, can you check?"]
    assert rig.broker.pushed[0] == {"speaker": "bob", "text": "Hello there.", "start": 0.0, "end": 2.0}
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_a_long_backlog_goes_in_batches_the_broker_takes() -> None:
    """250 waiting segments are 100, 100 and 50: a batch past the broker's limit is refused whole."""
    rig = await joined(Rig())
    rig.joinly.say(*[seg(f"line {i}", i, i + 0.5) for i in range(250)])
    await rig.until(lambda: len(rig.broker.pushed) == 250, "all of them")
    assert [len(b) for b in rig.broker.pushes()][1:] == [100, 100, 50]
    assert [s["text"] for s in rig.broker.pushed] == [f"line {i}" for i in range(250)]
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_a_429_is_waited_out_for_as_long_as_retry_after_says_and_nothing_is_lost() -> None:
    """The same batch is sent again after the wait, and the broker's own skipping of a segment it had taken
    makes that safe. A Retry-After of minutes is waited in pieces of at most a minute. Fails if the batch is
    dropped, resent at once, or the wait is ignored."""
    rig = Rig()
    rig.broker.push_answers = [
        ok(accepted=0, duplicate=0, ended=False),
        Reply(429, {"error": "rate_limited"}, 7.0),
    ]
    await joined(rig)
    rig.joinly.say(seg("one", 0, 1))
    await rig.until(lambda: rig.broker.texts() == ["one"], "the segment, after the wait")
    assert 7.0 in rig.time.slept
    sent = [b for b in rig.broker.pushes() if b]
    assert (
        sent == [[{"speaker": "bob", "text": "one", "start": 0.0, "end": 1.0}]] * 2
    )  # asked twice, taken once
    rig.broker.push_answers = [Reply(429, {}, 500.0)]
    rig.joinly.say(seg("two", 2, 3))
    await rig.until(lambda: rig.broker.texts() == ["one", "two"], "the second segment")
    assert relay.RETRY_AFTER_MAX_S in rig.time.slept and 500.0 not in rig.time.slept
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_an_error_is_retried_with_a_growing_pause_and_a_good_answer_starts_it_over() -> None:
    """1, 2, 4, 8 seconds while the broker does not answer, never past 30, and the first good answer puts the
    pause back to 1. Fails if it hammers the broker, grows without a bound, or never forgives."""
    rig = Rig()
    boom = Unreachable("ConnectionRefusedError")
    rig.broker.push_answers = [ok(), boom, boom, boom, boom, ok(), Reply(503, {}), ok()]
    await joined(rig)
    rig.joinly.say(seg("one", 0, 1))
    await rig.until(lambda: rig.broker.texts() == ["one"], "the segment, after four errors")
    pauses = [s for s in rig.time.slept if s != relay.SPEECH_MIN_GAP_S]
    assert pauses[:4] == [1.0, 2.0, 4.0, 8.0]
    rig.joinly.say(seg("two", 2, 3))
    await rig.until(lambda: rig.broker.texts() == ["one", "two"], "the segment, after a 503")
    assert [s for s in rig.time.slept if s != relay.SPEECH_MIN_GAP_S][4] == 1.0  # started over
    assert (
        sum("trying again" in line for line in rig.said) == 2
    )  # said once a time it went down, not each try
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_pauses_never_pass_thirty_seconds() -> None:
    rig = Rig()
    rig.broker.push_answers = [ok()] + [Unreachable("x")] * 8 + [ok()]
    await joined(rig)
    rig.joinly.say(seg("one", 0, 1))
    await rig.until(lambda: rig.broker.texts() == ["one"], "the segment")
    assert max(rig.time.slept) <= relay.BACKOFF_MAX_S
    assert relay.BACKOFF_MAX_S in rig.time.slept
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_a_broker_that_stays_away_as_long_as_it_would_wait_ends_the_relay() -> None:
    """Past ``QUIET_S`` the broker has ended the call itself, so trying longer gains nothing: exit 6, and it
    still leaves the call. Fails if it retries for ever."""
    rig = Rig()
    rig.broker.push_answers = [ok()] + [Unreachable("x")] * 100
    await joined(rig)
    rig.joinly.say(seg("one", 0, 1))
    assert await rig.code() == Exit.SERVER_LOST
    assert rig.time.t - 1000.0 >= relay.QUIET_S
    assert rig.joinly.left == 1 and rig.joinly.closed == 1


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (Reply(410, {"error": "ended"}), Exit.ENDED),
        (Reply(401, {"error": "revoked"}), Exit.REFUSED),
        (Reply(401, {"error": "unknown_key"}), Exit.REFUSED),
    ],
)
async def test_a_410_or_401_on_a_push_leaves_the_call_and_exits_with_the_reason(
    reply: Reply, code: Exit
) -> None:
    """410 is the call ended (a person's End call, the room closed, the broker stopped), 401 a key that is no
    longer accepted. Either way the relay leaves the call and joinly's session is closed. Fails if it keeps
    pushing, stays in the call, or exits 0 for a refusal."""
    rig = Rig()
    rig.broker.push_answers = [ok(), reply]
    await joined(rig)
    rig.joinly.say(seg("one", 0, 1))
    assert await rig.code() == code
    assert (rig.joinly.left, rig.joinly.closed) == (1, 1)
    assert rig.broker.aborted
    if code == Exit.REFUSED:
        assert reply.body["error"] in rig.heard()  # the broker's word, so the owner learns which


async def test_ended_in_a_poll_leaves_the_call_and_exits_0() -> None:
    rig = await joined(Rig())
    rig.broker.answer(ended=True)
    assert await rig.code() == Exit.ENDED
    assert (rig.joinly.left, rig.joinly.closed) == (1, 1)
    assert "the call has ended" in rig.heard()


async def test_a_call_over_before_it_joined_never_opens_joinly() -> None:
    """The first answer says ended: nothing to start. Fails if it opens a joinly session for a dead call."""
    rig = Rig(broker=Broker())
    rig.broker.first = ok(lines=[], hints=[], voice=False, ended=True)
    rig.start()
    assert await rig.code() == Exit.ENDED
    assert rig.joinly.opens == 0 and rig.hints == []


@pytest.mark.parametrize(("status", "code"), [(401, Exit.REFUSED), (410, Exit.ENDED), (404, Exit.USAGE)])
async def test_the_first_request_decides_early(status: int, code: Exit) -> None:
    """A refused key, an ended call and a broker that has no such route (an older switchboard) each stop the
    relay before joinly is touched, with the exit code that says which."""
    rig = Rig(broker=Broker())
    rig.broker.first = Reply(status, {"error": "x"})
    rig.start()
    assert await rig.code() == code
    assert rig.joinly.opens == 0


async def test_the_call_is_ended_while_the_relay_is_still_joining() -> None:
    """The join can take minutes (a first start loads a model, the host admits the bot). A person who ends the
    call meanwhile is heard, the join is dropped, and the relay leaves: nothing is left running. Fails if the
    relay only looks at the broker after the join returns."""
    rig = Rig()
    rig.joinly.gate_join = True
    rig.start()
    await rig.until(lambda: rig.joinly.joins != [], "the join to begin")
    rig.broker.answer(ended=True)
    assert await rig.code() == Exit.ENDED
    assert (
        rig.joinly.left == 1 and rig.joinly.closed == 1
    )  # it asked joinly to leave a join it did not finish
    assert rig.broker.pushes() == []  # never marked live


async def test_a_join_that_fails_exits_4_after_leaving() -> None:
    rig = Rig()
    rig.joinly.fail_join = True
    rig.start()
    assert await rig.code() == Exit.JOIN_FAILED
    assert (rig.joinly.left, rig.joinly.closed) == (1, 1)
    assert rig.broker.pushes() == []
    assert "hunter2" not in rig.heard() and LINK not in rig.heard()


async def test_joinly_still_starting_is_waited_for_and_one_that_never_comes_exits_5() -> None:
    """The relay and joinly start together, and joinly's image takes a while: connecting is retried every two
    seconds, up to ``JOINLY_START_S``. Fails if the first refused connection ends the relay, or it waits for
    ever."""
    rig = Rig()
    rig.joinly.refuse_opens = 3
    await joined(rig)
    assert rig.joinly.opens == 4 and rig.time.slept.count(2.0) == 3
    rig.relay.finish(Exit.ENDED)
    await rig.code()
    gone = Rig()
    gone.joinly.refuse_opens = 10**6
    gone.start()
    assert await gone.code() == Exit.JOINLY_LOST
    assert gone.time.t - 1000.0 >= relay.JOINLY_START_S
    assert gone.joinly.joins == []


async def test_joinly_dropping_in_the_call_exits_5() -> None:
    rig = await joined(Rig())
    rig.joinly.dropped = True
    rig.joinly.say(seg("x", 0, 1))
    assert await rig.code() == Exit.JOINLY_LOST
    assert (rig.joinly.left, rig.joinly.closed) == (1, 1)


async def test_told_to_stop_it_pushes_the_last_words_then_leaves_and_exits_7() -> None:
    """SIGTERM is ``docker stop``. What was said since the last read is pushed first (the broker would not
    have it otherwise), then it leaves. Fails if the end of the call is dropped, or it does not leave."""
    rig = await joined(Rig())
    rig.joinly.say(seg("last words", 0, 1))
    rig.relay.finish(Exit.STOPPED)
    assert await rig.code() == Exit.STOPPED
    assert "last words" in rig.broker.texts()
    assert (rig.joinly.left, rig.joinly.closed) == (1, 1)


async def test_a_no_join_relay_starts_everything_and_joins_no_call() -> None:
    """``--no-join``: a check of the setup. joinly is reached, the key is heard from (the poll), nothing is
    joined or pushed, and the end is still obeyed."""
    rig = Rig(RelayConfig(join=False), link=None)
    rig.start()
    await rig.until(lambda: rig.joinly.opens == 1, "joinly to be opened")
    rig.broker.answer(ended=True)
    assert await rig.code() == Exit.ENDED
    assert rig.joinly.joins == [] and rig.broker.pushes() == []


# ------------------------------------------------------------------------------------------ speaking
async def test_the_rooms_call_lines_are_spoken_one_after_another() -> None:
    rig = await joined(Rig())
    rig.broker.answer(lines=["First line.", "Second line."], voice=True)
    await rig.until(lambda: rig.joinly.spoken == ["First line.", "Second line."], "both lines spoken")
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_turning_voice_off_drops_the_lines_waiting_but_not_the_one_being_said() -> None:
    """As the broker's own bot does (§52). Fails if a line the person switched off for is still spoken."""
    rig = await joined(Rig())
    rig.joinly.gate_speech = True
    rig.broker.answer(lines=["One.", "Two.", "Three."], voice=True)
    await rig.until(lambda: rig.joinly.spoken == ["One."], "the first line to begin")
    rig.broker.answer(lines=[], voice=False)
    await rig.until(lambda: not rig.relay._speech, "the waiting lines to be dropped")
    rig.joinly.hold_speech.set()
    rig.relay.finish(Exit.ENDED)
    await rig.code()
    assert rig.joinly.spoken == ["One."]


async def test_lines_are_not_spoken_when_the_answer_says_voice_is_off() -> None:
    rig = await joined(Rig())
    rig.broker.answer(lines=["Never said."], voice=False)
    rig.broker.answer(lines=["Said."], voice=True)
    await rig.until(lambda: rig.joinly.spoken == ["Said."], "the voiced line")
    rig.relay.finish(Exit.ENDED)
    await rig.code()
    assert rig.joinly.spoken == ["Said."]


async def test_a_failed_speech_is_said_and_the_rest_dropped_and_an_interruption_is_noted() -> None:
    rig = await joined(Rig())
    rig.joinly.fail_speak = True
    rig.broker.answer(lines=["One.", "Two."], voice=True)
    await rig.until(lambda: rig.joinly.spoken == ["One."], "the failed line")
    await rig.until(lambda: any("could not speak" in s for s in rig.said), "the failure to be said")
    rig.joinly.fail_speak = False
    rig.joinly.speak_answer = 'Interrupted by detected speech. Spoken until now: "One..."'
    rig.broker.answer(lines=["Three."], voice=True)
    await rig.until(lambda: any("spoke over" in s for s in rig.said), "the interruption to be said")
    assert rig.joinly.spoken == ["One.", "Three."]  # "Two." was dropped with the failure
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_polls_never_come_faster_than_every_half_second() -> None:
    """A broker that answers at once must not be asked in a tight loop. Fails if the gap is not kept."""
    rig = await joined(Rig())
    for _ in range(3):
        rig.broker.answer()
    await rig.until(lambda: rig.time.slept.count(relay.SPEECH_MIN_GAP_S) >= 3, "three polls")
    rig.relay.finish(Exit.ENDED)
    await rig.code()


# ------------------------------------------------------------------------------ secrets, and files
async def test_nothing_the_relay_says_or_sends_holds_the_key_or_the_link() -> None:
    """Across a whole call with every kind of ending: what it prints, and every body it sends. (The key is in
    the Authorization header only, which the real client sets and a test of the wire checks.) Fails if a log
    line or a body carries either."""
    for finish in (Exit.ENDED, Exit.STOPPED):
        rig = await joined(Rig())
        rig.joinly.say(seg("something said", 0, 1))
        await rig.until(lambda r=rig: "something said" in r.broker.texts(), "the push")
        rig.relay.finish(finish)
        await rig.code()
        everything = rig.heard() + json.dumps(rig.broker.calls)
        for secret in (KEY, KEY[4:], LINK, "hunter2", "123456789"):
            assert secret not in everything, secret


async def test_the_link_file_goes_once_the_call_is_joined_and_the_key_file_at_the_end(tmp_path: Path) -> None:
    """``--burn-files``: the link's passcode is not kept on disk longer than the join needs it, and the key
    of a call that is over is gone. Without it a person's own files are left alone."""
    key_file, link_file = write(tmp_path / "key", KEY), write(tmp_path / "link", LINK)
    rig = Rig(RelayConfig(burn=True, key_path=key_file, link_path=link_file))
    rig.joinly.gate_join = True
    rig.start()
    await rig.until(lambda: rig.joinly.joins != [], "the join to begin")
    assert os.path.exists(link_file)  # not before it has joined
    rig.joinly.hold_join.set()
    await rig.until(lambda: bool(rig.broker.pushes()), "the call to be live")
    assert not os.path.exists(link_file) and os.path.exists(key_file)
    rig.relay.finish(Exit.ENDED)
    await rig.code()
    assert not os.path.exists(key_file)
    keep_key, keep_link = write(tmp_path / "k2", KEY), write(tmp_path / "l2", LINK)
    plain = await joined(Rig(RelayConfig(key_path=keep_key, link_path=keep_link)))
    plain.relay.finish(Exit.ENDED)
    await plain.code()
    assert os.path.exists(keep_key) and os.path.exists(keep_link)


# --------------------------------------------------------------------- the real HTTP client, on a socket
class Server:
    """A throwaway HTTP server that records what it is sent and answers as it is told."""

    def __init__(
        self, status: int = 200, body: bytes = b'{"ok": true}', headers: dict[str, str] | None = None
    ):
        self.seen: list[dict[str, Any]] = []
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.hang = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.seen.append({"path": self.path, "auth": self.headers.get("authorization"), "body": raw})
                outer.arrived.set()
                if outer.hang:
                    outer.release.wait(10)
                    return
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(10)


async def test_the_client_sends_the_key_as_a_bearer_and_reads_the_answer() -> None:
    srv = Server(429, b'{"error": "rate_limited"}', {"Retry-After": "3"})
    try:
        client = relay.BrokerClient(srv.url, Secret(KEY), allow_http=True)
        got = await client.post("/mcp/meeting/push", {"segments": []}, 5)
        assert (got.status, got.body, got.retry_after) == (429, {"error": "rate_limited"}, 3.0)
        assert srv.seen[0]["path"] == "/mcp/meeting/push"
        assert srv.seen[0]["auth"] == f"Bearer {KEY}"
        assert KEY.encode() not in srv.seen[0]["body"]
    finally:
        srv.close()


async def test_an_answer_that_is_not_a_json_object_is_an_empty_one() -> None:
    for body in (b"<html>a proxy's page</html>", b"[1, 2]", b""):
        srv = Server(502, body)
        try:
            got = await relay.BrokerClient(srv.url, Secret(KEY), allow_http=True).post("/x", {}, 5)
            assert (got.status, got.body) == (502, {})
        finally:
            srv.close()


async def test_an_address_nothing_listens_at_is_unreachable_and_says_only_the_class() -> None:
    srv = Server()
    url = srv.url
    srv.close()
    with pytest.raises(Unreachable) as e:
        await relay.BrokerClient(url, Secret(KEY), allow_http=True).post("/x", {}, 5)
    assert KEY not in str(e.value) and "127.0.0.1" not in str(e.value)


async def test_abort_ends_a_poll_that_is_waiting_at_once() -> None:
    """A relay told to stop must not sit out a 25 second poll (``docker stop`` would kill it at ten). Fails if
    ``abort`` leaves the request blocked."""
    srv = Server()
    srv.hang = True
    try:
        client = relay.BrokerClient(srv.url, Secret(KEY), allow_http=True)
        poll = asyncio.create_task(client.post("/mcp/meeting/speech", {"wait": 25}, 60))
        assert await asyncio.to_thread(srv.arrived.wait, 10), "the server never got the request"
        client.abort()
        with pytest.raises(Unreachable):
            await asyncio.wait_for(poll, 5)
        with pytest.raises(Unreachable):
            await client.post("/x", {}, 5)  # and a stopped client takes no new request
    finally:
        srv.close()


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("af_bella", "af_bella"),
        (None, "bm_lewis"),
        ("", "bm_lewis"),
        ("x" * 33, "bm_lewis"),
        ("../voice", "bm_lewis"),
        (True, "bm_lewis"),
    ],
)
async def test_the_first_poll_selects_a_checked_server_voice_or_the_older_servers_fallback(
    given: Any, expected: str
) -> None:
    """A valid server voice wins before joinly opens. An older server or malformed field preserves
    --voice, so one bad setting cannot make joinly ignore the entire settings header. The original
    configuration stays unchanged when a connection selects the server's voice.
    """
    cfg = RelayConfig(voice="bm_lewis")
    rig = Rig(cfg=cfg)
    rig.broker.first = ok(hints=["Codex"], **({"tts_voice": given} if given is not None else {}))
    hints, voice = await rig.relay._first_poll()
    assert cfg.voice == "bm_lewis"  # the server setting belongs to this connection, not the config
    client = relay.real_joinly("http://localhost/mcp/", cfg)(hints, voice)
    assert isinstance(client, relay.JoinlyClient)
    header = json.loads(client.header)
    assert header["tts_args"]["voice"] == expected
    assert header["stt_args"]["hotwords"] == ["Codex"]


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (Exit.STOPPED, "stopped"),
        (Exit.JOIN_FAILED, "join_failed"),
        (Exit.JOINLY_LOST, "joinly_lost"),
        (Exit.ENDED, "ended"),
        (Exit.REFUSED, "left"),
        (Exit.SERVER_LOST, "left"),
    ],
)
async def test_every_exit_reports_departure_once(code: Exit, reason: str) -> None:
    """After registration the relay reports every exit, including an already ended call. This must
    happen after the last push and before aborting its broker connection, without exposing secrets."""
    r = await joined(Rig())
    r.relay.finish(code)
    assert await r.code() == code
    assert [(p, b) for p, b in r.broker.calls if p == "/mcp/meeting/leave"] == [
        ("/mcp/meeting/leave", {"reason": reason})
    ]
    assert r.broker.aborted
    assert KEY not in r.heard() and LINK not in r.heard()


async def test_failed_join_reports_departure_and_keeps_the_exit_code() -> None:
    """A real join failure reports its fixed reason, rather than leaving a ten-minute joining bar."""
    r = Rig()
    r.joinly.fail_join = True
    r.start()
    assert await r.code() == Exit.JOIN_FAILED
    assert ("/mcp/meeting/leave", {"reason": "join_failed"}) in r.broker.calls
