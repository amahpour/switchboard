"""An account's guessing budget and common-password checks (#181)."""

from __future__ import annotations

from conftest import FakeClock

from switchboard.broker.passwords import SignInLimiter, password_problem


def test_backoff_does_not_restart_its_free_attempts_after_each_wait() -> None:
    clock = FakeClock()
    limiter = SignInLimiter(clock)
    accepted = 0
    for _ in range(24 * 60 * 60):
        if limiter.wait_s("owner") == 0:
            limiter.failed("owner")
            accepted += 1
        clock.advance(1)
    assert accepted <= 110, accepted  # the 15-minute steady wait permits about 96/day
    clock.advance(24 * 60 * 60 + 1)
    for _ in range(4):
        assert limiter.wait_s("owner") == 0
        limiter.failed("owner")
    assert limiter.wait_s("owner") == 0
    limiter.failed("owner")
    assert limiter.wait_s("owner") > 0  # a full day quiet resets the free attempts


def test_common_passwords_are_refused_case_insensitively() -> None:
    for password in ("password123", "QWERTYUIOP", "1234567890", "Summer2026!"):
        assert "common" in (password_problem(password, "alice") or ""), password
    assert password_problem("correct horse battery", "alice") is None
