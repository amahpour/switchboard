"""A hosted relay completes only a bounded, identified spoken turn (DESIGN.md §52.2)."""

from __future__ import annotations

import pytest
from test_meeting_relay import Rig, joined

from switchboard.meeting.relay import SPEECH_PATH, Exit


async def test_completion_is_sent_only_after_speech_finishes() -> None:
    """A long speak must keep the broker's slot reserved, then promptly complete its exact id."""
    rig = await joined(Rig())
    rig.joinly.gate_speech = True
    rig.broker.answer(lines=["First."], voice=True, speech_id=7)
    await rig.until(lambda: rig.joinly.spoken == ["First."], "speech to begin")
    assert not any("done" in body for _, body in rig.broker.calls)
    rig.joinly.hold_speech.set()
    await rig.until(
        lambda: any(p == SPEECH_PATH and b.get("done") == 7 for p, b in rig.broker.calls), "completion"
    )
    rig.relay.finish(Exit.ENDED)
    await rig.code()


@pytest.mark.parametrize("line_id", [True, "7", 0, -1, 2**63, None])
async def test_malformed_speech_ids_never_mean_legacy_permission_to_speak(line_id: object) -> None:
    """An old broker has no speech_id field; an invalid claimed id must fail closed instead."""
    rig = await joined(Rig())
    rig.broker.answer(lines=["Never."], voice=True, speech_id=line_id)
    rig.broker.answer(lines=["Valid."], voice=True, speech_id=9)
    await rig.until(lambda: "Valid." in rig.joinly.spoken, "the valid line")
    assert rig.joinly.spoken == ["Valid."]
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_one_id_never_allows_a_batch_of_spoken_lines() -> None:
    rig = await joined(Rig())
    rig.broker.answer(lines=["First.", "Not a second turn."], voice=True, speech_id=7)
    await rig.until(lambda: any(b.get("done") == 7 for _, b in rig.broker.calls), "completion")
    assert rig.joinly.spoken == ["First."]
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_voice_off_completes_a_claimed_line_dropped_before_it_started() -> None:
    """A line can be handed to the relay just before Voice goes off; dropping it must free its id."""
    rig = Rig()
    rig.relay._heard({"lines": ["Not started."], "voice": True, "speech_id": 7})
    rig.relay._heard({"lines": [], "voice": False, "speech_id": None})
    await joined(rig)
    await rig.until(
        lambda: any(b.get("done") == 7 for _, b in rig.broker.calls), "the dropped turn completed"
    )
    assert rig.joinly.spoken == []
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_a_lost_completion_is_retried_without_speaking_the_line_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary network loss must not leave a successfully spoken line occupying the call forever."""
    from switchboard.meeting.relay import Unreachable

    rig = Rig()
    post = rig.broker.post
    attempts = []

    async def flaky(path, body, timeout):
        if body.get("done") == 7:
            attempts.append(body["done"])
            if len(attempts) == 1:
                raise Unreachable("temporary loss")
        return await post(path, body, timeout)

    monkeypatch.setattr(rig.broker, "post", flaky)
    await joined(rig)
    rig.broker.answer(lines=["Once."], voice=True, speech_id=7)
    await rig.until(lambda: bool(rig.relay._speech_done), "completion retained for retry")
    rig.broker.answer(lines=[], voice=True, speech_id=None)
    await rig.until(lambda: len(attempts) == 2, "completion retried")
    assert rig.joinly.spoken == ["Once."]
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_every_speech_poll_says_this_relay_reports_when_a_line_ends() -> None:
    """The broker frees an older relay's slot at hand-off, because it never reports an end. So this relay
    has to say it does, on every poll, or two of its lines could overlap at the broker. Fails if any poll
    leaves the claim out."""
    rig = await joined(Rig())
    rig.broker.answer(lines=["First."], voice=True, speech_id=7)
    await rig.until(
        lambda: any(p == SPEECH_PATH and b.get("done") == 7 for p, b in rig.broker.calls), "completion"
    )
    polls = [b for p, b in rig.broker.calls if p == SPEECH_PATH]
    assert len(polls) >= 3 and all(b.get("turns") is True for b in polls)
    rig.relay.finish(Exit.ENDED)
    await rig.code()


# ------------------------------------------------------------------------------ a voice per line (#713)
async def test_a_lines_voice_goes_to_speak_text_and_is_checked_here_again() -> None:
    """The broker names a Kokoro voice for each line (``line_voice``). The relay passes a well-shaped one on
    and drops anything else, so joinly never gets an argument it would refuse and the line is still said in
    the call's voice. Older brokers send none. Fails if a bad shape reaches joinly or a good one is lost."""
    rig = await joined(Rig())
    rig.broker.answer(lines=["One."], voice=True, speech_id=1, line_voice="am_adam")
    await rig.until(lambda: rig.joinly.spoken == ["One."], "the first line")
    rig.broker.answer(lines=["Two."], voice=True, speech_id=2, line_voice="../etc/passwd")
    await rig.until(lambda: rig.joinly.spoken == ["One.", "Two."], "the second line")
    rig.broker.answer(lines=["Three."], voice=True, speech_id=3, line_voice="A" * 40)
    await rig.until(lambda: len(rig.joinly.spoken) == 3, "the third line")
    rig.broker.answer(lines=["Four."], voice=True, speech_id=4)
    await rig.until(lambda: len(rig.joinly.spoken) == 4, "the fourth line")
    rig.broker.answer(lines=["Five."], voice=True, speech_id=5, line_voice=7)
    await rig.until(lambda: len(rig.joinly.spoken) == 5, "the fifth line")
    assert rig.joinly.voices == ["am_adam", None, None, None, None]
    rig.relay.finish(Exit.ENDED)
    await rig.code()


async def test_a_voice_kokoro_lacks_is_reported_with_the_receipt_and_nothing_else_is() -> None:
    """When joinly says the voice was not found, the receipt for that line says so (``voice_missing``), so the
    broker can tell the room once; a line that was found, or had no voice, reports nothing."""
    rig = await joined(Rig())
    rig.joinly.speak_answers = [
        "Finished speaking.",
        "Finished speaking. The voice was not found, so the session's voice was used.",
    ]
    rig.broker.answer(lines=["Found."], voice=True, speech_id=1, line_voice="am_adam")
    await rig.until(lambda: any(b.get("done") == 1 for _, b in rig.broker.calls), "the first receipt")
    rig.broker.answer(lines=["Lost."], voice=True, speech_id=2, line_voice="zz_nobody")
    await rig.until(lambda: any(b.get("done") == 2 for _, b in rig.broker.calls), "the second receipt")
    receipts = {b["done"]: b for _, b in rig.broker.calls if "done" in b}
    assert "voice_missing" not in receipts[1]
    assert receipts[2]["voice_missing"] is True
    rig.relay.finish(Exit.ENDED)
    await rig.code()
