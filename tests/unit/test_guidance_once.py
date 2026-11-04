"""The standing guidance goes out in full, and then once more only when a session may have lost it (#543,
DESIGN.md §8.6).

Claude's SessionStart reminder used to repeat the same instructions every time (how messages arrive,
read() before say() or pass(), pass() as a default, what again=yes means). Now it carries them in full
after a compaction or a resume, after anything the broker cannot vouch for, and when a room's custom rules
(§35) have changed; after a fresh start or /clear it carries one pointer line. The line that peer text is
untrusted is in every reminder, whole: it is a guard (§11) and no choice here touches it.

Each test names the mistake that would make it fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock
from engine_agents import session
from engine_world import World

from switchboard import envelope
from switchboard.adapters import build_adapters
from switchboard.broker import hook_replies
from switchboard.config import Config
from switchboard.delivery import rules
from switchboard.delivery.engine import Engine
from switchboard.delivery.sinks import SinkRegistry
from switchboard.models import Participant

PAYLOADS = Path(__file__).resolve().parent.parent / "fixtures" / "payloads"
ROOMS = [("#build", "claude-1")]
FULL_ONLY = (envelope.PASS_ADVICE, "Call read() for any marked", "again=yes and reminder=yes", "authority")


@pytest.fixture
def w(tmp_path: Path, clock: FakeClock) -> World:
    return World(tmp_path, clock, Config().with_delivery(quiet_s=0.0, max_hold_s=0.0))


def joined(w: World, name: str = "claude-1", harness: str = "claude") -> Participant:
    """A session as a join() leaves it: it has been shown the room's rules (``rules_seen``) and the broker
    knows it gave it the standing guidance in full."""
    p, m = session(w, name, harness, status="idle")
    w.store.mark_rules_seen(m.id, w.room.rules_version)
    w.engine.note_guidance_given(p.id)
    return p


def reminder(w: World, p: Participant, source: str | None) -> str:
    out = w.hook(p, "SessionStart", source=source)
    assert out is not None and out.kind == "context" and out.batch_id is None
    return str(out.text)


def is_pointer(text: str) -> bool:
    return envelope.GUIDANCE_POINTER in text and not any(x in text for x in FULL_ONLY)


def is_full(text: str) -> bool:
    return all(x in text for x in FULL_ONLY) and envelope.GUIDANCE_POINTER not in text


# --------------------------------------------------------------------------- the pure rule
@pytest.mark.parametrize("source", ["startup", "clear"])
def test_only_a_fresh_start_or_clear_of_a_session_that_has_it_gets_the_pointer(source: str) -> None:
    """The one case of the pointer, with everything that must hold for it. Fails if the rule is more
    generous than its four conditions, or if the two sources the issue leaves out of 'full' stop
    getting it."""
    assert not rules.reminder_is_full("claude", source, given_in_full=True, rules_behind=False)


@pytest.mark.parametrize(
    "harness, source, given, behind",
    [
        ("claude", "compact", True, False),  # the context was rebuilt from a summary
        ("claude", "resume", True, False),  # from an old transcript
        ("claude", None, True, False),  # no sign of what began: fail closed
        ("claude", "", True, False),
        ("claude", "Clear", True, False),  # not a word this code knows (spelled differently)
        ("claude", "fork", True, False),  # a source a later Claude Code may add
        ("claude", "startup", False, False),  # the broker cannot tell it ever gave the full text
        ("claude", "clear", False, False),
        ("claude", "startup", True, True),  # a room's rules changed and the session lacks them
        ("claude", "clear", True, True),
        ("codex", "startup", True, False),  # no reminder exists for these: never a pointer
        ("devin", "startup", True, False),
        ("cursor", None, True, False),
        ("opencode", "clear", True, False),
        ("test", "clear", True, False),
        ("", "clear", True, False),
    ],
)
def test_anything_short_of_every_condition_is_full(
    harness: str, source: str | None, given: bool, behind: bool
) -> None:
    """Fail closed (#543): a compaction, a resume, a source or harness this code does not know, a session
    the broker cannot vouch for and a session behind on a room's rules all get the guidance in full. Fails
    if any of them is given the pointer."""
    assert rules.reminder_is_full(harness, source, given_in_full=given, rules_behind=behind)


def test_recorded_session_start_payloads_are_decided_as_the_issue_says() -> None:
    """What a harness sends is recorded, not remembered (CLAUDE.md): the `source` of Claude Code's four
    SessionStart payloads decides the reminder as the maintainer's comment on #543 says (full after a
    compaction or a resume, a pointer otherwise), and the other harnesses' SessionStart payloads (Codex and
    Devin say `startup`, Cursor says nothing) never get a pointer. Fails if a source is mapped the wrong way
    round, or a payload no longer carries the field the plugin relays."""
    want = {"startup": False, "clear": False, "compact": True, "resume": True}
    for source, full in want.items():
        payload = json.loads((PAYLOADS / "claude" / f"SessionStart_{source}.json").read_text())
        assert payload["hook_event_name"] == "SessionStart" and payload["source"] == source
        got = rules.reminder_is_full("claude", payload["source"], given_in_full=True, rules_behind=False)
        assert got is full, source
    for harness, name in (("codex", "codex/SessionStart"), ("devin", "devin/SessionStart")):
        payload = json.loads((PAYLOADS / f"{name}.json").read_text())
        assert payload["source"] == "startup"
        assert rules.reminder_is_full(harness, payload["source"], given_in_full=True, rules_behind=False)
    cursor = json.loads((PAYLOADS / "cursor" / "sessionStart.json").read_text())
    assert "source" not in cursor
    assert rules.reminder_is_full("cursor", cursor.get("source"), given_in_full=True, rules_behind=False)


def test_only_claude_code_has_a_reminder_to_shorten() -> None:
    """The harnesses that print nothing at session start have no reminder, so there is nothing to shorten
    and no harness to hand a pointer to: their guidance is the join() text, in full every time. Fails if
    one of them starts printing context at SessionStart (§7.3) without this rule being looked at again."""
    for harness, event in (("codex", "SessionStart"), ("devin", "SessionStart"), ("cursor", "sessionStart")):
        assert hook_replies.render(harness, event, {"kind": "context", "text": "x"}) is None
    assert "SessionStart" in hook_replies.CONTEXT_EVENTS["claude"]
    assert rules.REMINDER_POINTER_HARNESSES == frozenset({"claude"})


# ----------------------------------------------------------------------------- the envelope
def test_every_reminder_carries_the_untrusted_line_whole() -> None:
    """The line that peer text is untrusted is in the full reminder and in the pointer, word for word, for one
    person and for several, with and without the lists that follow it. Fails if the pointer drops it, shortens
    it, or the line is edited in one variant only."""
    owed = [("#build", _owed_item(5))]
    waiting = [("#build", _owed_item(7), "bob")]
    for people in ("alice", ["alice", "bob"]):
        for full in (True, False):
            for extra in ({}, {"still_on": owed, "waiting": waiting, "now": 1_000_600.0}):
                text = envelope.render_reminder(ROOMS, people, {"#build": "ab" * 8}, full=full, **extra)
                assert envelope.PEER_WARNING in text, (people, full, extra)
                assert text.startswith("[switchboard] Reminder: you are in #build as claude-1")


def test_the_pointer_keeps_what_is_not_standing_guidance() -> None:
    """The pointer drops only the standing guidance. The rooms and the people, the fresh join code that
    replaces the earlier one (#304) and the two short lists of what is open (#542) stay, because they differ
    every time. Fails if the pointer also drops any of them, or keeps any of the standing guidance."""
    owed = [("#build", _owed_item(5))]
    waiting = [("#build", _owed_item(7), "bob")]
    codes = {"#build": "ab" * 8}
    text = envelope.render_reminder(ROOMS, ["alice", "bob"], codes, owed, 1_000_600.0, waiting, full=False)
    assert (
        "you are in #build as claude-1 (switchboard chat with your users alice and bob and other agents)"
        in text
    )
    assert f"join again with resume=<code> to keep your name: #build yk:j{'ab' * 8}." in text
    assert "Still on you: #build 5 from alice 10 min ago." in text
    assert "Waiting on others: #build 7 for bob 10 min ago." in text
    assert is_pointer(text)


def test_the_full_reminder_is_what_it_was_and_is_the_default() -> None:
    """A caller that says nothing gets the full reminder (fail closed), and it is the text it always was.
    Fails if the default becomes the pointer, or the full text loses any of the guidance it carried."""
    default = envelope.render_reminder(ROOMS, "alice")
    assert default == envelope.render_reminder(ROOMS, "alice", full=True)
    assert is_full(default)
    assert default.index('"not shown here"') < default.index("pass()")
    assert default.endswith(envelope.PEER_WARNING + " " + envelope.PASS_ADVICE)


def test_a_typical_reminder_is_a_third_shorter_as_a_pointer() -> None:
    """The point of #543: one room, one person, the fresh join code (what a session in one room gets after
    /clear). Pins the measured saving so the pointer cannot quietly grow back into the guidance; the issue's
    PR quotes these numbers."""
    codes = {"#build": "ab" * 8}
    full = envelope.render_reminder(ROOMS, "alice", codes)
    pointer = envelope.render_reminder(ROOMS, "alice", codes, full=False)
    assert (len(full), len(pointer)) == (706, 445)
    assert len(full) - len(pointer) == 261
    assert len(pointer) <= len(full) * 0.65


def _owed_item(mid: int) -> Any:
    class Item:
        message_id = mid
        sender_name = "alice"
        ts = 1_000_000.0

    return Item()


# ------------------------------------------------------------------------------ the engine
def test_a_session_the_broker_cannot_vouch_for_gets_the_full_reminder_and_then_the_pointer(w: World) -> None:
    """Fail closed on what the broker does not know (a session it never gave the text, a restart): the
    first reminder is full whatever the source, and being full it is remembered, so the next /clear gets
    the pointer. Fails if /clear is pointed at a session the broker has no record for, or if a full
    reminder is not remembered (the saving would then never start)."""
    p, m = session(w, "claude-1", "claude", status="idle")
    w.store.mark_rules_seen(m.id, w.room.rules_version)  # the rules are not what is unknown here
    first = reminder(w, p, "clear")
    assert is_full(first) and envelope.PEER_WARNING in first
    second = reminder(w, p, "clear")
    assert is_pointer(second) and envelope.PEER_WARNING in second
    assert len(second) < len(first)


def test_a_join_is_what_lets_the_next_clear_get_the_pointer(w: World) -> None:
    """join() is the standing guidance in full, so a session that has joined may be pointed at once /clear
    empties its context, with no full reminder before it. Fails if the join is not remembered."""
    p = joined(w)
    text = reminder(w, p, "clear")
    assert is_pointer(text) and envelope.PEER_WARNING in text
    assert "#build as claude-1" in text and "resume=<code>" in text  # the rest is as it was


@pytest.mark.parametrize("source", ["compact", "resume"])
def test_after_a_compaction_or_a_resume_the_guidance_is_in_full_every_time(w: World, source: str) -> None:
    """The context was rebuilt from a summary or an old transcript, so the maintainer's scope is the full
    text each time, however often it came before. Fails if a compaction or a resume is ever pointed, or if
    one of them makes the broker forget the session has the text (the /clear after it is a pointer)."""
    p = joined(w)
    for _ in range(3):
        assert is_full(reminder(w, p, source))
    assert is_pointer(reminder(w, p, "clear"))


@pytest.mark.parametrize("source", [None, "", "fork", "COMPACT", "x" * 40])
def test_a_source_nobody_recognises_is_full(w: World, source: str | None) -> None:
    """Claude Code gave no sign (an older plugin sends none) or a sign this code does not know: the guidance
    goes out in full. Fails if an unknown source is read as one of the two that point."""
    p = joined(w)
    assert is_full(reminder(w, p, source))


def test_changed_room_rules_make_the_next_reminder_full_until_the_session_has_them(w: World) -> None:
    """The maintainer's third condition: the pointer never stands for rules the session has not been shown.
    A person edits the room's rules (§35); until the delivery that carries them reaches the session
    (rules_seen catches up), a /clear is answered in full, and after it the pointer is allowed again. Fails if
    the pointer is sent while the session is behind on the room's rules, or never comes back."""
    p, m = w.agent("claude-1", harness="claude", status="busy", hooks=True)
    w.store.mark_rules_seen(m.id, w.room.rules_version)
    w.engine.note_guidance_given(p.id)
    assert is_pointer(reminder(w, p, "clear"))
    w.store.set_room_rules(w.room.id, "Keep answers under three lines.")
    assert w.m(m).rules_seen < w.store.room_by_id(w.room.id).rules_version
    assert is_full(reminder(w, p, "clear"))
    assert is_full(reminder(w, p, "startup"))
    w.human("@claude-1 look at this", mentions=("claude-1",))
    delivered = w.hook(p, "PostToolUse", ok=True)
    assert delivered is not None and envelope.CUSTOM_RULES_HEADING in delivered.text
    assert w.m(m).rules_seen == w.store.room_by_id(w.room.id).rules_version
    assert is_pointer(reminder(w, p, "clear"))


def test_the_rules_of_any_room_of_the_session_count(w: World) -> None:
    """A session in two rooms is behind when either has rules it has not been shown, the first or the last.
    Fails if only one of the rooms is looked at."""
    other = w.store.create_room("#docs", "alice", 60, 5)
    p = joined(w)
    first = w.store.active_membership(w.room.id, p.id)
    assert first is not None
    second = w.store.create_membership(other.id, p.id, "claude-1")
    w.store.mark_rules_seen(second.id, other.rules_version)
    assert is_pointer(reminder(w, p, "clear"))
    w.store.set_room_rules(w.room.id, "Build rooms are for builds.")
    assert is_full(reminder(w, p, "clear"))
    w.store.mark_rules_seen(first.id, w.store.room_by_id(w.room.id).rules_version)  # delivered
    assert is_pointer(reminder(w, p, "clear"))
    w.store.set_room_rules(other.id, "Docs rooms are for docs.")
    assert is_full(reminder(w, p, "clear"))


def test_a_restarted_broker_starts_with_the_full_reminder(w: World, clock: FakeClock) -> None:
    """What the broker knows about a session is memory only, so a restart (every upgrade) makes it unknown
    and the first reminder after it is full, whatever the source. Fails if the record outlives the engine,
    or if a fresh engine assumes the session has the text."""
    p = joined(w)
    assert is_pointer(reminder(w, p, "clear"))
    restarted = Engine(
        w.store, clock, w.cfg, build_adapters(w.cfg), SinkRegistry(), key=b"k" * 32, test_mode=True
    )
    w.engine = restarted
    assert is_full(reminder(w, p, "clear"))
    assert is_pointer(reminder(w, p, "clear"))


def test_a_session_that_ended_is_not_remembered_but_a_clear_does_not_end_it(w: World) -> None:
    """A SessionEnd with a reason other than /clear ends the session, and the same id may start again with an
    empty context, so its next reminder is full. /clear keeps the session (§6.3) and the pointer stays on.
    Fails if an ended session keeps its record, or if /clear throws it away."""
    p = joined(w)
    w.hook(p, "SessionEnd", reason="clear")
    assert is_pointer(reminder(w, p, "clear"))
    w.hook(p, "SessionEnd", reason="logout")
    assert w.p(p).status == "offline"
    assert is_full(reminder(w, p, "startup"))


def test_a_reminder_too_long_to_print_is_not_remembered_as_given(tmp_path: Path, clock: FakeClock) -> None:
    """A context longer than the harness prints answers nothing (`hook_replies.render`), so the session
    never saw that full text. Fails if the broker counts it as given and points the session at text it
    does not have."""
    cfg = Config().with_delivery(quiet_s=0.0, max_hold_s=0.0, batch_max_chars=200)
    w = World(tmp_path, clock, cfg)
    p, m = session(w, "claude-1", "claude", status="idle")
    w.store.mark_rules_seen(m.id, w.room.rules_version)
    assert len(reminder(w, p, "clear")) > 200
    assert is_full(reminder(w, p, "clear"))  # still not remembered


def test_no_other_harness_is_answered_at_session_start(w: World) -> None:
    """Codex, Devin and Cursor print nothing at SessionStart, given the guidance or not: their reminder does
    not exist, so none can be given a pointer in its place. Fails if one starts answering."""
    for harness in ("codex", "devin", "cursor"):
        p = joined(w, f"{harness}-1", harness)
        assert w.hook(p, "SessionStart", source="startup") is None
        assert w.hook(p, "SessionStart", source="clear") is None
