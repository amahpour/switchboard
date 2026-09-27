"""config: defaults from DESIGN.md §2, TOML loading, validation."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import switchboard.config
from switchboard.config import Config, ConfigError, default_human_name, from_dict, load
from switchboard.paths import Paths


def test_defaults_match_design() -> None:
    c = Config()
    assert (c.human_name, c.port) == ("alice", 7419)  # the login name, which conftest pins to alice
    d = c.delivery
    assert (d.quiet_s, d.max_hold_s, d.batch_max_msgs, d.batch_max_chars) == (3.0, 60.0, 20, 6000)
    assert (d.rate_limit_s, d.budget_per_hour, d.hop_limit, d.watchdog_s, d.watchdog_max) == (10.0, 60, 6, 120, 2)
    assert (d.catchup_n, d.max_msg_chars, d.offer_backstop_s, d.hook_ack_s, d.pull_ack_s) == (30, 4000, 1800, 5.0, 10.0)
    assert (c.claude.inbox_hold_s, c.claude.inbox_idle_expire_s, c.claude.wait_cap_s) == (0.3, 5.0, 110)
    assert (c.codex.queue_fallback, c.codex.require_thread_proof, c.codex.wait_cap_s, c.codex.ctx_max_chars) == (True, True, 240, 5000)
    assert (c.cursor.stop_park_s, c.cursor.wait_cap_s, c.cursor.ctx_max_chars, c.cursor.max_unconfirmed_followups) == (600, 50, 8000, 2)
    assert (c.devin.wait_cap_s, c.devin.rearm, c.devin.rearm_max_per_prompt, c.devin.ctx_max_chars) == (600, True, 2, 6000)
    assert c.devin.rearm_max_per_hour == 12


def login(monkeypatch: pytest.MonkeyPatch, name: str | None = None, exc: Exception | None = None) -> None:
    def getuser() -> str:
        if exc is not None:
            raise exc
        assert name is not None
        return name

    monkeypatch.setattr(switchboard.config, "getpass", types.SimpleNamespace(getuser=getuser))


@pytest.mark.parametrize("name,want", [
    ("bob", "bob"),
    ("Bob", "bob"),            # lowercased
    ("sam_2-x", "sam_2-x"),
    ("john.doe", "me"),        # not a screen name
    ("9lives", "me"),
    ("x" * 25, "me"),          # too long
    ("", "me"),
    ("root", "me"),            # reserved
    ("admin", "me"),
    ("switchboard-svc", "me"), # switchboard* is reserved
    ("dev", "me"),             # agents can't take names starting with the human's: devin-1
    ("c", "me"),
    ("claude", "me"),
])
def test_default_human_name_is_a_valid_login_name_else_me(monkeypatch: pytest.MonkeyPatch, name: str,
                                                          want: str) -> None:
    login(monkeypatch, name)
    assert default_human_name() == want and Config().human_name == want


@pytest.mark.parametrize("exc", [OSError("no login name"), KeyError("getpwuid(): uid not found: 4242")])
def test_default_human_name_without_a_login_name(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    login(monkeypatch, exc=exc)
    assert Config().human_name == "me"


def test_a_configured_human_name_wins_over_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    login(monkeypatch, "bob")
    assert from_dict({"human_name": "sam"}).human_name == "sam" and from_dict({}).human_name == "bob"


def test_load_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load(Paths.from_home(tmp_path)) == Config()


def test_load_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'human_name = "sam"\nport = 8000\n[delivery]\nquiet_s = 1\nbudget_per_hour = 10\n[devin]\nrearm = false\n'
    )
    c = load(Paths.from_home(tmp_path))
    assert c.human_name == "sam" and c.port == 8000
    assert c.delivery.quiet_s == 1.0 and isinstance(c.delivery.quiet_s, float)
    assert c.delivery.budget_per_hour == 10 and c.devin.rearm is False
    assert c.delivery.hop_limit == 6


@pytest.mark.parametrize(
    "data",
    [
        {"nope": 1},
        {"delivery": {"quiet": 1}},
        {"delivery": {"hop_limit": "6"}},
        {"delivery": {"hop_limit": 1.5}},
        {"delivery": {"hop_limit": -1}},
        {"delivery": {"quiet_s": -0.5}},
        {"delivery": {"watchdog_s": "90"}},
        {"devin": {"rearm": 1}},
        {"human_name": "Alice!"},
        {"port": 70000},
        {"port": True},
        {"claude": []},
    ],
)
def test_invalid(data: dict) -> None:
    with pytest.raises(ConfigError):
        from_dict(data)


def test_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("port = = 1\n")
    with pytest.raises(ConfigError):
        load(Paths.from_home(tmp_path))


def test_float_fields_take_fractions_and_ints() -> None:
    d = from_dict({"delivery": {"watchdog_s": 90.5, "offer_backstop_s": 900, "quiet_s": 1}}).delivery
    assert d.watchdog_s == 90.5 and d.offer_backstop_s == 900.0 and isinstance(d.quiet_s, float)
    assert isinstance(Config().delivery.watchdog_s, float)


def test_with_delivery() -> None:
    c = Config().with_delivery(quiet_s=0.0, max_hold_s=0.0)
    assert c.delivery.quiet_s == 0.0 and c.delivery.max_hold_s == 0.0 and Config().delivery.quiet_s == 3.0
