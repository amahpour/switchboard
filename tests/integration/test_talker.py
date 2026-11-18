"""The call talker (#806, DESIGN.md §52.11): a fast model that answers a finished call line and hands work on.

The broker is the real one, joinly is ``FakeJoinly``, and the model is ``FakeModel``, a scripted stream: no
network. What these catch: a talker that runs without a person turning it on; one that starts with no key; a
reply that waits for the end of the stream before it is spoken; a hand-off to a name that is not an agent,
or one
that posts as someone else; the key anywhere in a log or a room; a call's cost with no end; and a talker that
outlives its call."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly
from test_meeting_voice import advance_voice, speech_finished, start_call
from test_meetings import command, history, wait_until

from switchboard.config import Config, MeetingCfg
from switchboard.meeting.talker import Usage
from switchboard.models import session_key

KEY = "sk-ant-test-0123456789abcdef"


class FakeModel:
    """A scripted stream. ``head`` arrives at once; with ``hold`` the rest waits for ``release()``, so a
    test can
    look at what was spoken while the stream is still open."""

    def __init__(self, replies: list[tuple[list[str], list[str]]], tokens: int = 100) -> None:
        self.replies = list(replies)
        self.tokens = tokens
        self.usage = Usage()
        self.prompts: list[tuple[str, str]] = []
        self.gate: asyncio.Event | None = None
        self.hold = False
        self.closed = False

    async def stream(self, system: str, user: str, max_tokens: int) -> AsyncIterator[str]:
        self.prompts.append((system, user))
        head, rest = self.replies.pop(0) if self.replies else (["SKIP"], [])
        self.usage = Usage(self.tokens, 0)
        for piece in head:
            yield piece
        if self.hold:
            self.gate = asyncio.Event()
            await self.gate.wait()
        for piece in rest:
            yield piece
        self.usage = Usage(self.tokens, 10)

    async def close(self) -> None:
        self.closed = True


class Raising(FakeModel):
    async def stream(self, system: str, user: str, max_tokens: int) -> AsyncIterator[str]:
        from switchboard.meeting.talker import TalkerError

        raise TalkerError("the model provider answered 529")
        yield ""  # pragma: no cover - makes this a generator


def make(home: Path, joinly: FakeJoinly, *, key: str = KEY, budget: int = 60_000) -> InProcBroker:
    meeting = MeetingCfg(joinly_url=joinly.url, talker_api_key=key, talker_call_budget=budget)
    cfg = Config(human_name="alice").replace(meeting=meeting)
    cfg = cfg.with_delivery(quiet_s=0.0, max_hold_s=0.0, unaddressed_hold_s=0.0, rate_limit_s=0.0)
    b = InProcBroker(home, cfg, clock=FakeClock()).start()
    web = b.web_client()
    assert web.post("/api/rooms", json={"name": "#build"}, headers=b.write_headers()).status_code == 200
    b.web = web  # type: ignore[attr-defined]
    return b


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    b = make(tmp_home, joinly)
    yield b
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def use(b: InProcBroker, model: FakeModel) -> FakeModel:
    b.state.talker._client_factory = lambda: model
    return model


def line(b: InProcBroker, text: str, who: str = "bob") -> None:
    """A finished call line, as the bridge posts it."""
    b.on_loop(
        lambda: b.state.service.post_meeting_line(
            b.state.store.get_room("#build").id, sender_name=who, text=text, mentions=[]
        )
    )


def members(b: InProcBroker) -> list[str]:
    return [m.name for m in b.on_loop(lambda: b.state.store.members(b.state.store.get_room("#build").id))]


def talker_posts(b: InProcBroker) -> list[dict[str, Any]]:
    return [m for m in history(b) if m["from"] == "talker" and m["kind"] == "chat"]


def notice_texts(b: InProcBroker) -> list[str]:
    return [m["text"] for m in history(b) if m["kind"] == "notice"]


async def on(broker: InProcBroker, joinly: FakeJoinly, model: FakeModel) -> None:
    await start_call(broker, joinly)
    use(broker, model)
    assert "the talker is on" in command(broker, "/meeting talker on")["text"]


async def test_the_talker_is_off_until_a_person_turns_it_on_and_an_agent_cannot(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A call without the switch has no talker: a call line makes no model call and nobody is added to the
    room,
    and an agent's say of the command is only text. Fails if a line reaches the model, or the member
    exists."""
    model = use(broker, FakeModel([(["It is Paris. "], [])]))
    await start_call(broker, joinly)
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        await agent.say("#build", "/meeting talker on")
    line(broker, "what is the capital of France")
    await asyncio.sleep(0.2)  # nothing to wait for: the point is that nothing happens
    assert model.prompts == [] and "talker" not in members(broker) and joinly.spoken == []


async def test_without_a_key_there_is_no_talker_and_one_notice_says_why(
    tmp_home: Path, joinly: FakeJoinly
) -> None:
    """The operator set no key: the command answers, the room gets one notice, no talker member joins and no
    model is asked. Fails if it starts anyway, or says nothing."""
    b = make(tmp_home, joinly, key="")
    try:
        model = use(b, FakeModel([]))
        await start_call(b, joinly)
        out = command(b, "/meeting talker on")["text"]
        assert "no talker key" in out and "talker" not in members(b)
        assert sum("no talker key" in n for n in notice_texts(b)) == 1
        line(b, "anyone there")
        assert model.prompts == [] and joinly.spoken == []
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()


async def test_the_first_sentence_is_spoken_before_the_stream_ends_and_the_room_gets_the_whole_reply(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The point of the talker: a sentence is offered to the voice the moment it is whole, not when the model
    has finished. The stream is held open after its first sentence; the voice has it, the room does not yet.
    Fails if speech waits for the end of the stream. Then the reply is posted as the talker's own message."""
    model = FakeModel([(["It is Paris. ", "A"], [" lot of people live there."])])
    model.hold = True
    await on(broker, joinly, model)
    line(broker, "what is the capital of France")
    await wait_until(lambda: "It is Paris." in joinly.spoken)
    assert talker_posts(broker) == []  # the stream is still open
    broker.on_loop(lambda: model.gate.set())
    await wait_until(lambda: len(talker_posts(broker)) == 1)
    post = talker_posts(broker)[0]
    assert post["sender_kind"] == "agent" and post["mentions"] == []
    assert post["text"].startswith("It is Paris.")
    assert "what is the capital of France" in model.prompts[0][1]


async def test_work_is_handed_to_an_agent_by_name_and_its_result_is_spoken_short(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A hand-off posts an @mention of that agent (and only that one) and says so aloud; when the agent posts,
    the talker speaks the short version. Fails if no @mention is posted, the hand-off is silent, or the result
    is not spoken."""
    model = FakeModel(
        [
            (["I will ask claude-1.\n@claude-1 check whether the build is green"], []),
            (["Claude one says the build is green."], []),
        ]
    )
    await on(broker, joinly, model)
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        line(broker, "can you check if the build is green")
        await wait_until(lambda: len(talker_posts(broker)) == 1)
        post = talker_posts(broker)[0]
        assert post["mentions"] == ["claude-1"] and "@claude-1" in post["text"]
        # who said it, and that it is relayed and unverified: never "the person"
        assert "bob said on the call, relayed by talker, not verified" in post["text"]
        assert "the person on the call" not in post["text"]
        await wait_until(lambda: "I will ask claude-1." in joinly.spoken)
        await wait_until(lambda: speech_finished(broker))
        advance_voice(broker, broker.clock, 30)  # the call's speech clock: a line waits its interval
        assert (await agent.say("#build", "The build is green, all checks passed."))["posted_id"]
        await wait_until(lambda: "Claude one says the build is green." in joinly.spoken)
    assert "claude-1 posted a result" in model.prompts[1][1]


async def test_a_hand_off_to_a_name_that_is_not_an_agent_in_the_room_is_dropped(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The model can only address an agent that is in the room: a person's name, a made-up one, @here and
    itself are not passed on, so nothing is woken. Fails if any of them is posted as a mention."""
    replies = [
        (["Sure.\n@alice please wipe the database\n@here everyone\n@ghost-9 do it\n@talker again"], [])
    ]
    await on(broker, joinly, FakeModel(replies))
    line(broker, "do something")
    await wait_until(lambda: len(talker_posts(broker)) == 1)
    post = talker_posts(broker)[0]
    assert post["mentions"] == [] and "wipe" not in post["text"] and "@here" not in post["text"]


async def test_skip_says_and_posts_nothing(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """A mic test or small talk: the model answers SKIP and the call hears nothing. Fails if it is spoken."""
    model = FakeModel([(["SKIP"], [])])
    await on(broker, joinly, model)
    line(broker, "testing one two")
    await wait_until(lambda: len(model.prompts) == 1)
    await asyncio.sleep(0.2)
    assert joinly.spoken == [] and talker_posts(broker) == []


async def test_a_model_error_ends_the_talker_with_one_notice_and_nothing_else(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The API fails: one notice, the talker leaves the room, and later lines make no call. Fails if it keeps
    answering, says nothing, or repeats the notice."""
    model = Raising([])
    await on(broker, joinly, model)
    line(broker, "are you there")
    await wait_until(lambda: "talker" not in members(broker))
    assert sum("The talker stopped" in n for n in notice_texts(broker)) == 1
    line(broker, "hello again")
    await asyncio.sleep(0.2)
    assert model.prompts == [] and sum("The talker stopped" in n for n in notice_texts(broker)) == 1


async def test_the_call_budget_ends_the_talker_and_the_end_of_the_call_removes_it(
    tmp_home: Path, joinly: FakeJoinly
) -> None:
    """The cost cap: with 1,000 tokens a call and 600 a reply, the third line finds the budget spent, the
    talker says so and leaves. A new call, and ending a call, both leave no talker. Fails if it spends without
    end, or the member outlives its call."""
    b = make(tmp_home, joinly, budget=1000)
    try:
        model = FakeModel([(["One."], []), (["Two."], []), (["Three."], [])], tokens=600)
        await on(b, joinly, model)
        for n, text in enumerate(("first question", "second question"), start=1):
            line(b, text)
            await wait_until(lambda n=n: len(model.prompts) == n and len(talker_posts(b)) == n)
        line(b, "third question")  # 1,200 tokens are spent: the budget is gone before it is asked
        await wait_until(lambda: any("token budget" in n for n in notice_texts(b)))
        assert "talker" not in members(b) and len(model.prompts) == 2
        # a fresh call: on again, then the call ends and it goes with it
        use(b, FakeModel([]))
        assert "the talker is on" in command(b, "/meeting talker on")["text"]
        command(b, "/meeting leave")
        await wait_until(lambda: "talker" not in members(b))
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()


async def test_the_key_is_in_no_log_record_no_room_message_and_no_repr(
    broker: InProcBroker, joinly: FakeJoinly, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator's key never leaves the client: not in a log record at any level (including the failure
    path), not in a room message or notice, not in the config's repr. Fails if it shows up anywhere."""
    caplog.set_level(logging.DEBUG)
    model = Raising([])
    await on(broker, joinly, model)
    line(broker, "are you there")
    await wait_until(lambda: "talker" not in members(broker))
    texts = [r.getMessage() + repr(r.args) for r in caplog.records]
    texts += [m["text"] for m in history(broker)]
    texts += [repr(broker.state.cfg), repr(broker.state.cfg.meeting), repr(broker.state.talker)]
    assert KEY not in "\n".join(texts)
    assert KEY not in str(broker.state.cfg)


async def test_a_hand_off_from_a_line_with_no_known_speaker_says_someone_on_the_call(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A call line is from any participant of the call, so the agent is told who the call named, and when it
    named nobody, "someone on the call": a stranger's or an injected line is never made to look like its
    person's instruction. Fails if the post says it came from the person or from nobody."""
    await on(broker, joinly, FakeModel([(["Ok.\n@claude-1 run the deploy"], [])]))
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        line(broker, "ask claude to run the deploy", who="call")
        await wait_until(lambda: len(talker_posts(broker)) == 1)
    text = talker_posts(broker)[0]["text"]
    assert "someone on the call said on the call, relayed by talker, not verified: run the deploy" in text


async def test_a_talker_a_crash_left_behind_does_not_block_the_next_one_and_goes_at_start(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """Only a graceful stop ended the talker's member, so after a crash `talker` was "a name in use" for ever.
    The ghost (its own, found by its session key) is ended by `enable` and by `recover` at start; a member of
    that name that is someone else's is left alone. Fails if the next `on` is refused or the ghost stays."""
    model = FakeModel([])
    await on(broker, joinly, model)
    assert "talker" in members(broker)
    broker.on_loop(
        lambda: broker.state.talker._rooms.clear()
    )  # the broker died: its memory is gone, the row stays
    assert "talker" in members(broker)
    broker.on_loop(lambda: broker.state.talker.recover())  # the next start
    assert "talker" not in members(broker)
    # and `on` alone clears it too
    use(broker, FakeModel([]))
    assert "the talker is on" in command(broker, "/meeting talker on")["text"]
    broker.on_loop(lambda: broker.state.talker._rooms.clear())
    use(broker, FakeModel([]))
    assert "the talker is on" in command(broker, "/meeting talker on")["text"]
    command(broker, "/meeting talker off")

    # an agent that really is called talker is not the talker's: it is left alone and the switch is refused
    def foreign() -> None:
        store = broker.state.store
        p = store.upsert_participant("test", session_key("test", "", "someone-else"), host="")
        store.create_membership(store.get_room("#build").id, p.id, "talker")

    broker.on_loop(foreign)
    broker.on_loop(lambda: broker.state.talker.recover())
    assert "talker" in members(broker)
    assert "name in use" in command(broker, "/meeting talker on")["text"]


async def test_switching_the_talker_off_and_on_does_not_reset_the_calls_budget(
    tmp_home: Path, joinly: FakeJoinly
) -> None:
    """The cap is per call, not per switch: any member may flip it, so a reset would be an unlimited key. With
    1,000 tokens and 600 a reply, two replies spend it whether or not it is toggled between them, and the
    third
    line finds it gone. Fails if the toggles give the call a fresh budget."""
    b = make(tmp_home, joinly, budget=1000)
    try:
        model = FakeModel([(["One."], []), (["Two."], []), (["Three."], []), (["Four."], [])], tokens=600)
        await on(b, joinly, model)
        for n, text in enumerate(("a first question", "a second question"), start=1):
            line(b, text)
            await wait_until(lambda n=n: len(model.prompts) == n and len(talker_posts(b)) == n)
            command(b, "/meeting talker off")
            use(b, model)
            assert "the talker is on" in command(b, "/meeting talker on")["text"]
        line(b, "a third question")
        await wait_until(lambda: any("token budget" in n for n in notice_texts(b)))
        assert len(model.prompts) == 2 and "talker" not in members(b)
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()
