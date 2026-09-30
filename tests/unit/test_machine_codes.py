"""Pairing codes and what a machine may say about itself (``broker/machines.py``, DESIGN.md §31.7)."""

from __future__ import annotations

from conftest import FakeClock
from switchboard.broker.machines import PAIR_TTL_S, PairingCodes, clean_facts
from switchboard.remote import linkkey


def test_a_code_is_single_use_and_a_second_try_hears_used() -> None:
    codes = PairingCodes(FakeClock())
    code = codes.mint("work-laptop")
    assert linkkey.normalize_code(code) is not None and codes.live("work-laptop") is not None
    assert codes.use(code.lower(), "SHA256:a") == ("ok", "work-laptop")  # as typed, in any case
    assert codes.live("work-laptop") is None
    assert codes.use(code, "SHA256:b") == ("used", "work-laptop")
    assert codes.use("7KQ4-M2XD-9HVA", "SHA256:c") == ("bad", None)
    assert codes.use(None, "SHA256:c") == ("bad", None)


def test_a_code_lives_ten_minutes_and_one_per_name() -> None:
    clock = FakeClock()
    codes = PairingCodes(clock)
    old = codes.mint("work-laptop")
    new = codes.mint("work-laptop")  # a new code for the same name: the old one dies
    other = codes.mint("lab-pc")
    assert codes.use(old, "x") == ("bad", None)
    clock.advance(PAIR_TTL_S - 1)
    assert codes.use(new, "x") == ("ok", "work-laptop")
    clock.advance(1)
    assert codes.use(other, "x") == ("bad", None)  # expired
    assert codes.use(new, "y") == ("bad", None)  # a used code is forgotten once it would have expired
    assert len(codes) == 0
    code = codes.mint("box")
    codes.drop("box")
    assert codes.use(code, "x") == ("bad", None)


def test_facts_are_short_clean_claims() -> None:
    raw = {"hostname": "box‮\u0007  one", "os": "Linux " + "x" * 200, "arch": 5, "version": "0.7.0",
           "harnesses": ["claude", "codex", "evil", "test", 3, "claude"], "extra": "dropped"}
    assert clean_facts(raw) == {"hostname": "box one", "os": "Linux " + "x" * 74, "version": "0.7.0",
                                "harnesses": ["claude", "codex"]}
    assert clean_facts("not a dict") == {} and clean_facts({"harnesses": "claude"}) == {}
