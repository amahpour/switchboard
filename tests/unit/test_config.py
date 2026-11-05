"""config: defaults from DESIGN.md §2, TOML loading, validation."""

from __future__ import annotations

import os
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
    assert (d.rate_limit_s, d.budget_per_hour, d.hop_limit, d.watchdog_s, d.watchdog_max) == (
        10.0,
        60,
        0,  # the loop guard is off unless a room has a limit (#406)
        120,
        2,
    )
    assert (d.catchup_n, d.max_msg_chars, d.offer_backstop_s, d.hook_ack_s, d.pull_ack_s) == (
        8,  # issue #272: a smaller join catch-up, within DESIGN.md's 6,000-character join budget
        4000,
        1800,
        5.0,
        10.0,
    )
    assert c.claude.wait_cap_s == 600
    assert (c.codex.wait_cap_s, c.codex.ctx_max_chars) == (280, 5000)
    assert (c.cursor.wait_cap_s, c.cursor.ctx_max_chars) == (55, 8000)
    assert c.opencode.wait_cap_s == 50  # #547: the one harness that had no key for it
    assert (c.devin.wait_cap_s, c.devin.rearm, c.devin.rearm_max_per_prompt, c.devin.ctx_max_chars) == (
        600,
        True,
        2,
        6000,
    )
    assert c.devin.rearm_max_per_hour == 12


def login(monkeypatch: pytest.MonkeyPatch, name: str | None = None, exc: Exception | None = None) -> None:
    def getuser() -> str:
        if exc is not None:
            raise exc
        assert name is not None
        return name

    monkeypatch.setattr(switchboard.config, "getpass", types.SimpleNamespace(getuser=getuser))


@pytest.mark.parametrize(
    "name,want",
    [
        ("bob", "bob"),
        ("Bob", "bob"),  # lowercased
        ("sam_2-x", "sam_2-x"),
        ("john.doe", "me"),  # not a screen name
        ("9lives", "me"),
        ("x" * 25, "me"),  # too long
        ("", "me"),
        ("root", "me"),  # reserved
        ("admin", "me"),
        ("switchboard-svc", "me"),  # switchboard* is reserved
        ("dev", "me"),  # agents can't take names starting with the human's: devin-1
        ("c", "me"),
        ("claude", "me"),
    ],
)
def test_default_human_name_is_a_valid_login_name_else_me(
    monkeypatch: pytest.MonkeyPatch, name: str, want: str
) -> None:
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
        'human_name = "sam"\nport = 8000\n[delivery]\nquiet_s = 1\n'
        "budget_per_hour = 10\n[devin]\nrearm = false\n"
    )
    c = load(Paths.from_home(tmp_path))
    assert c.human_name == "sam" and c.port == 8000
    assert c.delivery.quiet_s == 1.0 and isinstance(c.delivery.quiet_s, float)
    assert c.delivery.budget_per_hour == 10 and c.devin.rearm is False
    assert c.delivery.hop_limit == 0  # not in the file: the built-in default, the guard off (#406)


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


def test_the_keys_only_the_local_agent_path_read_are_accepted_and_reported() -> None:
    """A config.toml written for 0.37 still loads (#385): the keys of the local agent path (Claude's
    inbox and sessions directory, Codex's daemon, thread proof and queue, Cursor's park and follow-up
    limit) are ignored and named in ``Config.retired``, and what is still read is read. Fails if a
    release that retired them refuses the file, or reads them as settings."""
    c = from_dict(
        {
            "claude": {"sessions_dir": "/x", "inbox_hold_s": 0.3, "wait_cap_s": 90},
            "codex": {"control_socket": "/y", "require_thread_proof": False, "queue_fallback": True},
            "cursor": {"stop_park_s": 600, "max_unconfirmed_followups": 2, "wait_cap_s": 40},
        }
    )
    assert c.claude.wait_cap_s == 90 and c.cursor.wait_cap_s == 40
    assert from_dict({"opencode": {"wait_cap_s": 300}}).opencode.wait_cap_s == 300
    assert c.retired == (
        "claude.inbox_hold_s",
        "claude.sessions_dir",
        "codex.control_socket",
        "codex.queue_fallback",
        "codex.require_thread_proof",
        "cursor.max_unconfirmed_followups",
        "cursor.stop_park_s",
    )
    assert from_dict({"claude": {"wait_cap_s": 90}}).retired == ()
    for bad in ({"claude": {"nope": 1}}, {"cursor": {"stop_park_s": 600, "nope": 1}}):
        with pytest.raises(ConfigError):  # a key that was never one is still refused
            from_dict(bad)


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


def test_security_section(tmp_path: Path) -> None:
    """[security] (DESIGN.md §27.5.7): allow_ssh_cli, off by default, a strict bool."""
    assert Config().security.allow_ssh_cli is False
    (tmp_path / "config.toml").write_text("[security]\nallow_ssh_cli = true\n")
    assert load(Paths.from_home(tmp_path)).security.allow_ssh_cli is True
    assert from_dict({"security": {}}).security.allow_ssh_cli is False
    for bad in ("true", 1, 0):
        with pytest.raises(ConfigError, match="security.allow_ssh_cli must be true or false"):
            from_dict({"security": {"allow_ssh_cli": bad}})
    with pytest.raises(ConfigError, match=r"\[security\] must be a table"):
        from_dict({"security": True})


def test_security_unknown_key_refused() -> None:
    with pytest.raises(ConfigError, match=r"unknown key\(s\) in \[security\]: allow_ssh"):
        from_dict({"security": {"allow_ssh": True}})
    with pytest.raises(ConfigError, match="unknown top-level key"):
        from_dict({"allow_ssh_cli": True})  # only inside [security]


@pytest.mark.parametrize(
    "toml,want",
    [
        ("", False),
        ("[security]\nallow_ssh_cli = false\n", False),
        ("[security]\nallow_ssh_cli = true\n", True),
    ],
)
def test_security_reaches_the_broker_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str, want: bool
) -> None:
    """[security] allow_ssh_cli from config.toml reaches the production ProcessPeerPolicy,
    through create_app and through the daemon's run_foreground (the two places that build it)."""
    import socket as socket_mod

    from switchboard.broker import app as app_mod
    from switchboard.broker import daemon
    from switchboard.broker.peer import AllowAllHumans, ProcessPeerPolicy

    paths = Paths.from_home(tmp_path / "home")
    paths.home.mkdir(mode=0o700)
    paths.config.write_text(toml)
    cfg = load(paths)

    pol = app_mod.default_peer_policy(cfg)
    assert type(pol) is ProcessPeerPolicy and pol.allow_ssh_cli is want
    assert type(app_mod.default_peer_policy(cfg, test_trust_uds=True)) is AllowAllHumans
    built = app_mod.create_app(paths, cfg).state.broker.peer_policy
    assert type(built) is ProcessPeerPolicy and built.allow_ssh_cli is want

    # run_foreground, stopped right after it built its policy
    seen: list[object] = []

    class Built(Exception):
        pass

    def fake_create_app(paths: Paths, cfg: Config, policy: object, *a: object, **kw: object) -> None:
        seen.append(policy)
        raise Built

    listener = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
    monkeypatch.setattr(app_mod, "create_app", fake_create_app)
    monkeypatch.setattr(daemon, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "loopback_listener", lambda port: listener)
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    try:
        for trust in (False, True):
            with pytest.raises(Built):
                daemon.run_foreground(paths, cfg, port=0, test_trust_uds=trust, announce=False)
    finally:
        os.umask(old_umask)  # run_foreground sets 077
        listener.close()
    assert type(seen[0]) is ProcessPeerPolicy and seen[0].allow_ssh_cli is want
    assert type(seen[1]) is AllowAllHumans


def test_the_human_name_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment's manifest names its admin (DESIGN.md §32.4): SWITCHBOARD_HUMAN_NAME wins
    over config.toml and the login name; a bad one is refused like a bad human_name."""
    from switchboard.config import HUMAN_NAME_ENV, ConfigError, load
    from switchboard.paths import Paths

    paths = Paths.from_home(tmp_path)
    monkeypatch.setenv(HUMAN_NAME_ENV, "ari")
    assert load(paths).human_name == "ari" and load(paths).human_name_from_env  # Settings can't rename (#114)
    paths.config.write_text('human_name = "bob"\n')
    assert load(paths).human_name == "ari"
    monkeypatch.setenv(HUMAN_NAME_ENV, "")
    assert load(paths).human_name == "bob" and not load(paths).human_name_from_env
    monkeypatch.setenv(HUMAN_NAME_ENV, "Not A Name")
    with pytest.raises(ConfigError, match=HUMAN_NAME_ENV):
        load(paths)


# the name docs/DEPLOY.md and the Kubernetes patch use, spelled out so a rename in the code fails here
JOINLY_ENV = "SWITCHBOARD_JOINLY_URL"


def test_the_meeting_bot_address_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Kubernetes manifest points the broker at the joinly sidecar (#591, DESIGN.md §52): the
    variable sets ``[meeting] joinly_url`` with no config.toml, and wins over the file when both are
    set, as SWITCHBOARD_HUMAN_NAME does. Set to nothing (a templated value left blank) it is the
    same as unset: the file's address stays. Fails if ``load`` reads only the file, or if a blank
    variable blanks the address and so turns /meeting off."""
    paths = Paths.from_home(tmp_path)
    assert load(paths).meeting.joinly_url == ""
    monkeypatch.setenv(JOINLY_ENV, "http://127.0.0.1:8000/mcp/")
    assert load(paths).meeting.joinly_url == "http://127.0.0.1:8000/mcp/"  # no file at all
    paths.config.write_text('[meeting]\njoinly_url = "http://127.0.0.1:8010/mcp/"\n')
    assert load(paths).meeting.joinly_url == "http://127.0.0.1:8000/mcp/"  # the variable wins
    monkeypatch.setenv(JOINLY_ENV, " http://joinly:8000/mcp/ \n")
    assert load(paths).meeting.joinly_url == "http://joinly:8000/mcp/"  # padding is cut, as in the file
    for blank in ("", "   "):
        monkeypatch.setenv(JOINLY_ENV, blank)
        assert load(paths).meeting.joinly_url == "http://127.0.0.1:8010/mcp/"  # the file's again
    monkeypatch.delenv(JOINLY_ENV)
    assert load(paths).meeting.joinly_url == "http://127.0.0.1:8010/mcp/"
    paths.config.unlink()
    assert load(paths).meeting.joinly_url == ""


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1:8000/mcp/",  # no scheme
        "ftp://joinly:8000/mcp/",
        "file:///etc/passwd",
        "http://",  # no host
        "http://user:pass@joinly:8000/mcp/",  # userinfo
        "http://user@joinly:8000/mcp/",
        "http://[::1/mcp/",  # urlsplit itself refuses it
        "http://joinly:8000/" + "a" * 300,  # over 300 characters
    ],
)
def test_a_bad_meeting_bot_variable_is_a_startup_error_that_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The variable goes through the checks the config key has (http or https, 300 characters, a
    host, no user name or password) and a bad one stops the broker with a message naming it. It
    is never ignored: a typo would otherwise turn /meeting off with no word of why. Fails if a bad
    value is dropped silently, or if the error names the file's key instead of the variable."""
    paths = Paths.from_home(tmp_path)
    paths.config.write_text('[meeting]\njoinly_url = "http://127.0.0.1:8010/mcp/"\n')  # a good file: no help
    monkeypatch.setenv(JOINLY_ENV, value)
    with pytest.raises(ConfigError, match=JOINLY_ENV) as e:
        load(paths)
    assert "joinly_url" not in str(e.value)
    assert value not in str(e.value)  # an address with a password in it is not echoed to the log


def test_the_meeting_bot_variable_does_not_loosen_the_key(tmp_path: Path) -> None:
    """The config key still refuses what it did, and now refuses an address ``urlsplit`` cannot read
    with a ConfigError (it was a bare ValueError that skipped the CLI's ``config error:`` line)."""
    for bad in ("ftp://joinly/", "http://user:pw@joinly/", "http://[::1/mcp/"):
        with pytest.raises(ConfigError, match=r"\[meeting\] joinly_url"):
            from_dict({"meeting": {"joinly_url": bad}})


def test_start_stops_on_a_bad_meeting_bot_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``switchboard start --foreground`` (a container's command) prints ``config error:`` with the
    variable's name and exits 1, before it opens anything. Fails if the error is swallowed or the
    broker starts without a meeting server."""
    from switchboard import cli
    from switchboard.broker import daemon

    monkeypatch.setattr(daemon, "run_foreground", lambda *a, **k: pytest.fail("the broker started"))
    monkeypatch.setenv(JOINLY_ENV, "joinly:8000")
    assert cli.main(["start", "--foreground", "--home", str(tmp_path)]) == cli.EXIT_ERR
    assert "config error: SWITCHBOARD_JOINLY_URL must be an http(s) URL" in capsys.readouterr().err


# the names docs/DEPLOY.md and the Kubernetes patch use, spelled out so a rename in the code fails here
MAX_CALLS_ENV = "SWITCHBOARD_MEETING_MAX_CALLS"


def test_max_calls_defaults_to_one_and_takes_one_to_fifty() -> None:
    """``[meeting] max_calls`` (#607, DESIGN.md §52) guards the meeting bot's memory, which holds one
    live call at the default size: it is 1 unless the operator says more, and 1 through 50 are all
    accepted. Fails if the default is higher (a second call would then reach a bot sized for one)."""
    assert Config().meeting.max_calls == 1
    assert from_dict({}).meeting.max_calls == 1
    for ok in (1, 2, 3, 50):
        assert from_dict({"meeting": {"max_calls": ok}}).meeting.max_calls == ok


@pytest.mark.parametrize("bad", [0, -1, 51, 500, True, False, 1.5, 2.0, "2", None])
def test_a_bad_max_calls_key_is_a_config_error(bad: object) -> None:
    """The key is an integer from 1 to 50, as ``[files] max_mb`` is checked: 0 would turn /meeting off
    by refusing every call, a huge number defeats the guard, and a bool or a string is a typo that must
    not be believed (``True`` is an ``int`` in Python). Fails if any of these loads."""
    with pytest.raises(ConfigError, match="max_calls"):
        from_dict({"meeting": {"max_calls": bad}})
    with pytest.raises(ConfigError, match="max_calls"):
        switchboard.config.MeetingCfg(max_calls=bad)  # type: ignore[arg-type]


def test_max_calls_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Kubernetes manifest or a Helm chart sets the limit with no config.toml (#607): the variable
    sets ``[meeting] max_calls`` alone, wins over the file when both are set, and is trimmed. Empty or
    blank it is unset and the file's value stays, as for SWITCHBOARD_JOINLY_URL. Fails if ``load``
    reads only the file, if the file wins, or if a blank variable resets the limit to 1."""
    paths = Paths.from_home(tmp_path)
    assert load(paths).meeting.max_calls == 1
    monkeypatch.setenv(MAX_CALLS_ENV, "3")
    assert load(paths).meeting.max_calls == 3  # no file at all
    paths.config.write_text("[meeting]\nmax_calls = 2\n")
    assert load(paths).meeting.max_calls == 3  # the variable wins
    monkeypatch.setenv(MAX_CALLS_ENV, " 50\n")
    assert load(paths).meeting.max_calls == 50  # padding is cut
    for blank in ("", "   "):
        monkeypatch.setenv(MAX_CALLS_ENV, blank)
        assert load(paths).meeting.max_calls == 2  # the file's again
    monkeypatch.delenv(MAX_CALLS_ENV)
    assert load(paths).meeting.max_calls == 2
    paths.config.unlink()
    assert load(paths).meeting.max_calls == 1
    # the other two meeting keys are left alone by it
    monkeypatch.setenv(MAX_CALLS_ENV, "4")
    paths.config.write_text('[meeting]\njoinly_url = "http://127.0.0.1:8010/mcp/"\ntts_voice = "af_bella"\n')
    got = load(paths).meeting
    assert (got.joinly_url, got.tts_voice, got.max_calls) == ("http://127.0.0.1:8010/mcp/", "af_bella", 4)


@pytest.mark.parametrize(
    "value",
    ["0", "51", "-1", "100", "1000", "abc", "2.5", "1e1", "+2", "0x2", "1 2", "two", "٢"],
)
def test_a_bad_max_calls_variable_is_a_startup_error_that_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The variable is checked as the key is (a whole number, 1 to 50) and a bad one stops the broker
    with a message that names the variable. It is never ignored, even with a good file under it: a typo
    would otherwise leave the limit at the file's or at 1 with no word of why. Fails if a bad value
    is dropped silently, if "0" or "51" loads, or if the error names the file's key instead."""
    paths = Paths.from_home(tmp_path)
    paths.config.write_text("[meeting]\nmax_calls = 2\n")  # a good file: no help
    monkeypatch.setenv(MAX_CALLS_ENV, value)
    with pytest.raises(ConfigError, match=MAX_CALLS_ENV) as e:
        load(paths)
    assert "[meeting]" not in str(e.value)


def test_start_stops_on_a_bad_max_calls_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``switchboard start --foreground`` (a container's command) prints ``config error:`` with the
    variable's name and exits 1, before it opens anything. Fails if the broker starts with a limit
    the operator did not ask for."""
    from switchboard import cli
    from switchboard.broker import daemon

    monkeypatch.setattr(daemon, "run_foreground", lambda *a, **k: pytest.fail("the broker started"))
    monkeypatch.setenv(MAX_CALLS_ENV, "many")
    assert cli.main(["start", "--foreground", "--home", str(tmp_path)]) == cli.EXIT_ERR
    assert f"config error: {MAX_CALLS_ENV} must be a whole number from 1 to 50" in capsys.readouterr().err
