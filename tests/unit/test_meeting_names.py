"""Spoken names and who a call line addresses (DESIGN.md §52).

Speech-to-text writes "Claude one" or "claude 1", never ``@claude-1``. A line addresses
someone only when the name is at the start, after hey/hi/ok/okay, or followed by a comma.
"I asked Claude yesterday" is not a call-out. A bare "Claude" matches when exactly one
member's name starts with it, and asks which one when two do.
"""

from __future__ import annotations

from switchboard.meeting.names import hotwords, match, spoken_forms


def test_spoken_forms_of_a_numbered_name() -> None:
    """claude-1 is said "claude one", "claude 1" or "claude-1", and "number" in between,
    which is how the call's speech-to-text wrote "Codex number two". Digits go up to twenty.
    Fails if a hyphen stays the only form, or "21" is spelled out past the list."""
    forms = spoken_forms("claude-1")
    for want in ("claude one", "claude 1", "claude-1", "claude number one"):
        assert want in forms
    assert "codex twenty" in spoken_forms("codex-20")
    assert "claude twenty one" not in spoken_forms("claude-21")
    assert "claude 21" in spoken_forms("claude-21")
    assert spoken_forms("Claude-1") == spoken_forms("claude-1")


def test_hotwords_are_the_room_agents_spoken_names() -> None:
    """The broker passes these to the transcriber, so "Claude" is heard. One list, no duplicates.
    Fails if a second claude drops "claude two", or the bare name is missing."""
    words = [w.lower() for w in hotwords(["claude-1", "codex-1"])]
    assert "claude one" in words and "codex" in words and "claude" in words
    assert words.count("claude one") == 1
    assert hotwords([]) == []


def test_a_name_at_the_start_after_a_greeting_or_before_a_comma_is_an_address() -> None:
    """Those three are a call-out. The same name later in the sentence is not, so a story
    about Claude does not wake claude-1. Fails if mid-sentence matches, or a greeting doesn't."""
    members = ["claude-1", "codex-1"]
    assert match("Claude one, look at the upload limit.", members).name == "claude-1"
    assert match("hey Codex, what failed?", members).name == "codex-1"
    assert match("ok claude 1 the tests are red", members).name == "claude-1"
    assert match("hi claude-1", members).name == "claude-1"
    assert match("I asked Claude yesterday and he said no.", members).outcome == "none"
    assert match("the prices Claude mentioned", members).outcome == "none"


def test_exact_ambiguous_and_no_match() -> None:
    """One spoken form, one member. Two members for one phrase, no guess. A name that isn't
    in the room matches nothing. Fails if "claude one" wakes both, or a stranger wakes one."""
    both = ["claude-1", "claude-2", "codex-1"]
    one = match("claude one tell me how much", both)
    assert one.outcome == "one" and one.name == "claude-1"
    amb = match("Claude, say the number", both)
    assert amb.outcome == "ambiguous" and amb.names == ("claude-1", "claude-2")
    assert "Claude" in (amb.heard or "")
    assert match("Devin, are you there?", both).outcome == "none"
    assert match("claude-2, the log is empty", both).name == "claude-2"


def test_a_bare_base_name_matches_one_member_and_asks_when_two_share_it() -> None:
    """ "Claude" is claude-1 when claude-1 is the only Claude, and a question when claude-2
    is there too. "Codex" still means codex-1. Whole words only: "claudette" is not Claude.
    Fails if two Claudes collapse to claude-1, or one Claude is left unnamed."""
    assert match("Claude, look into the upload API.", ["claude-1", "codex-1"]).name == "claude-1"
    two = match("Claude look into the upload API.", ["claude-1", "claude-2"])
    assert two.outcome == "ambiguous" and set(two.names) == {"claude-1", "claude-2"}
    assert match("Codex the build failed", ["claude-1", "claude-2", "codex-1"]).name == "codex-1"
    assert match("Claudette, hello", ["claude-1"]).outcome == "none"
    assert match("claudeone, hello", ["claude-1"]).outcome == "none"


def test_the_transcriber_writing_out_its_hint_list_wakes_nobody() -> None:
    """Whisper wrote the hotwords back twice in the spike (#573): a run of hint words naming
    two members, or the participant's own name with one, is an echo, not a call-out."""
    room = ["claude-1", "codex-1", "alice"]
    assert match("Claude Claude one Codex switchboard", room).outcome == "none"
    assert match("One Codex Claude", room).outcome == "none"
    assert match("Codex switchboard transcribing", room).outcome == "none"
    # A real request that names two agents is still a call-out to the first one.
    assert match("Claude, ask Codex about the cache", room).name == "claude-1"
    assert match("Claude one, look into the upload API", room).name == "claude-1"
    assert match("Hey Codex, can you look into why?", room).name == "codex-1"


def test_a_real_call_out_behind_an_echoed_hint_list_still_wakes_the_agent() -> None:
    """#602, from a real Zoom call: the speaker said "Okay, Claude one, can you hear me?" and
    switchboard's bot transcribed "Claude-1 Claude switchboard Claude one can you hear me?". The run
    of hint words at the front made the whole line an echo, so nobody was woken (2 of 6 runs). Real
    words after the run mean real speech: the name right before them is the call-out. Fails if the
    line still counts as all echo."""
    heard = "Claude-1 Claude switchboard Claude one can you hear me?"
    assert match(heard, ["claude-1"]).name == "claude-1"
    assert match(heard, ["claude-1", "codex-1", "alice"]).name == "claude-1"
    # the spoken form is specific even with two Claudes
    assert match(heard, ["claude-1", "claude-2"]).name == "claude-1"
    # a bare base right before the words is ambiguous with two Claudes, as it is anywhere
    two = ["claude-1", "claude-2"]
    assert match("Claude-1 switchboard Claude can you hear me?", two).outcome == "ambiguous"


def test_an_echo_that_ends_in_no_name_or_ends_the_line_still_wakes_nobody() -> None:
    """The lead-in rule is narrow: only the name right before real words counts. A run that ends
    in the bot's own name, or a line that is nothing but hint words, addresses nobody, as before.
    Fails if an echo of names alone can wake an agent."""
    room = ["claude-1", "codex-1", "cursor-1"]
    assert match("Codex Cursor switchboard can you hear me", room).outcome == "none"
    assert match("Claude Claude one Codex Cursor switchboard", room).outcome == "none"
    assert match("Claude-1 Claude switchboard Claude one", ["claude-1"]).outcome == "none"
