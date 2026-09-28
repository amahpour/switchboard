"""``remotes.toml`` and ``satellite.toml`` (DESIGN.md §27.3, §27.5.8, §27.8)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from switchboard.paths import Paths
from switchboard.remote.config import (
    RemoteConfigError,
    RemoteEntry,
    config_hash,
    entry_hash,
    key_fingerprint,
    link_material,
    parse_remotes,
    read_satellite_conf,
    write_satellite_conf,
)

GOOD = """
[remote.fpga-pi]
host = "fpga-pi.local"
user = "alice"
port = 2222
rooms = ["#FPGA", "bench", "#fpga"]
harnesses = ["devin", "claude"]
max_members = 4
end_after_s = 600
"""
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMCGkdYxdHrN6N8Lhzn9oRL0Rj6qu5M3QZQpqk2hVg8C switchboard-link fpga-pi"


def test_parse_and_validate() -> None:
    [(name, e)] = parse_remotes(GOOD, test_mode=False).items()
    assert name == "fpga-pi"
    assert e == RemoteEntry(name="fpga-pi", host="fpga-pi.local", user="alice", port=2222,
                            rooms=("#fpga", "#bench"), harnesses=("claude", "devin"), max_members=4,
                            end_after_s=600, transport="ssh", home="")
    d = parse_remotes('[remote.b]\nhost = "192.0.2.10"\nuser = "alice"\nrooms = ["#x"]\n', test_mode=False)["b"]
    assert (d.port, d.harnesses, d.max_members, d.end_after_s) == (22, ("claude", "codex", "cursor", "devin"), 8, 900)
    assert parse_remotes('[remote.c]\nhost = "::1"\nuser = "alice"\nrooms = ["#x"]\n', test_mode=False)["c"].host == "::1"
    assert parse_remotes("", test_mode=False) == {}
    bad = [
        ('[remote.Bad_Name]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\n', "remote names"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\nbogus = 1\n', "unknown key"),
        ('[remote.a]\nhost = "h;rm"\nuser = "alice"\nrooms = ["#x"]\n', "host"),
        ('[remote.a]\nhost = "-oProxyCommand=x"\nuser = "alice"\nrooms = ["#x"]\n', "host"),
        ('[remote.a]\nhost = "h"\nuser = "Alice Smith"\nrooms = ["#x"]\n', "user"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nport = 0\nrooms = ["#x"]\n', "port"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nport = true\nrooms = ["#x"]\n', "port"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\nharnesses = ["test"]\n', "harness"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\nmax_members = 33\n', "max_members"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\nend_after_s = 0\n', "end_after_s"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["not a room!"]\n', "rooms"),
        ('[remote.a]\nhost = "h"\nuser = "alice"\nrooms = ["#x"]\nhome = "/tmp/x"\n', "home"),
        ('top = 1\n', "top-level"),
        ('[remote.a\n', "remotes.toml"),
    ]
    for text, word in bad:
        with pytest.raises(RemoteConfigError) as ei:
            parse_remotes(text, test_mode=True)
        assert word in str(ei.value), (text, ei.value)


def test_rooms_required() -> None:
    for rooms in ("", "rooms = []\n", 'rooms = "#x"\n'):
        with pytest.raises(RemoteConfigError) as ei:
            parse_remotes(f'[remote.a]\nhost = "h"\nuser = "alice"\n{rooms}', test_mode=False)
        assert "rooms is required" in str(ei.value)


def test_exec_transport_needs_test_mode() -> None:
    text = '[remote.a]\ntransport = "exec"\nhome = "/tmp/yk-pi-x"\nrooms = ["#x"]\n'
    with pytest.raises(RemoteConfigError) as ei:
        parse_remotes(text, test_mode=False)
    assert "test-mode" in str(ei.value)
    e = parse_remotes(text, test_mode=True)["a"]
    assert (e.transport, e.home, e.host, e.user) == ("exec", "/tmp/yk-pi-x", "", "")
    with pytest.raises(RemoteConfigError):
        parse_remotes('[remote.a]\ntransport = "exec"\nhome = "rel"\nrooms = ["#x"]\n', test_mode=True)
    with pytest.raises(RemoteConfigError):
        parse_remotes('[remote.a]\ntransport = "carrier-pigeon"\nrooms = ["#x"]\n', test_mode=True)


def test_config_hash_changes_on_any_field_key_or_pin(tmp_path: Path) -> None:
    e = parse_remotes(GOOD, test_mode=False)["fpga-pi"]
    base = config_hash(e, "SHA256:k", "switchboard-fpga-pi ssh-ed25519 AAAA")
    assert base == config_hash(e, "SHA256:k", "switchboard-fpga-pi ssh-ed25519 AAAA")
    import dataclasses

    changes = [dict(host="other.local"), dict(user="bob"), dict(port=22), dict(rooms=("#fpga",)),
               dict(harnesses=("claude",)), dict(max_members=5), dict(end_after_s=601),
               dict(transport="exec", home="/tmp/x")]
    seen = {base}
    for ch in changes:
        h = config_hash(dataclasses.replace(e, **ch), "SHA256:k", "switchboard-fpga-pi ssh-ed25519 AAAA")
        assert h not in seen, ch
        seen.add(h)
    assert config_hash(e, "SHA256:other", "switchboard-fpga-pi ssh-ed25519 AAAA") not in seen
    assert config_hash(e, "SHA256:k", "switchboard-fpga-pi ssh-ed25519 BBBB") not in seen
    # the key material comes from remotes/<name>/: an edit of the key or the pin changes the hash
    paths = Paths.from_home(tmp_path)
    assert link_material(paths, "fpga-pi") == ("", "")
    h0 = entry_hash(paths, e)
    d = tmp_path / "remotes" / "fpga-pi"
    d.mkdir(parents=True)
    (d / "id_ed25519.pub").write_text(PUB + "\n")
    fp, pin = link_material(paths, "fpga-pi")
    assert fp == key_fingerprint(PUB) and fp.startswith("SHA256:") and pin == ""
    h1 = entry_hash(paths, e)
    (d / "known_hosts").write_text("# pinned\nswitchboard-fpga-pi ssh-ed25519 AAAA\n")
    h2 = entry_hash(paths, e)
    assert len({h0, h1, h2}) == 3
    assert key_fingerprint("garbage") == "" and key_fingerprint("ssh-ed25519 !!!") == ""


def test_satellite_toml_round_trip(tmp_path: Path) -> None:
    paths = Paths.from_home(tmp_path)
    with pytest.raises(FileNotFoundError):
        read_satellite_conf(paths)
    write_satellite_conf(paths, "fpga-pi", desktop="desk", key_fp="SHA256:abc", accepted_at=12.5)
    assert stat.S_IMODE(paths.satellite_conf.stat().st_mode) == 0o600
    c = read_satellite_conf(paths)
    assert (c.name, c.desktop, c.key_fingerprint, c.accepted_at) == ("fpga-pi", "desk", "SHA256:abc", 12.5)
    for text in ('name = "Bad Name"\n', 'name = "a"\nextra = 1\n', "not toml [", 'name = "a"\ndesktop = "x y"\n'):
        paths.satellite_conf.write_text(text)
        with pytest.raises(RemoteConfigError):
            read_satellite_conf(paths)
    with pytest.raises(RemoteConfigError):
        write_satellite_conf(paths, "Bad")


def test_host_is_a_name_or_an_address_nothing_else() -> None:
    """The host becomes ssh's destination argument (M8e): an IPv6 zone must look like an
    interface name, and nothing may start with '-'."""
    def host_ok(h: str) -> bool:
        try:
            parse_remotes(f'[remote.a]\nhost = "{h}"\nuser = "alice"\nrooms = ["#x"]\n', test_mode=False)
            return True
        except RemoteConfigError:
            return False

    for h in ("fpga-pi.local", "10.0.0.7", "::1", "fe80::1%eth0", "fe80::1%2"):
        assert host_ok(h), h
    for h in ("fe80::1%-oProxyCommand=sh -c x", "fe80::1% x", "fe80::1%a=b", "-oProxyCommand=x", "a b",
              "fe80::1%" + "x" * 16):
        assert not host_ok(h), h


def test_remotes_toml_must_be_yours_and_not_writable_by_others(tmp_path: Path) -> None:
    """M8c left this for M8e: ``remotes.toml`` is read only when it is a regular file of
    this user that nobody else can write (``remote add`` writes it 0600)."""
    import os

    from switchboard.remote.config import load_remotes

    paths = Paths(tmp_path)
    assert load_remotes(paths, test_mode=False) == {}
    f = tmp_path / "remotes.toml"
    f.write_text(GOOD)
    for mode, ok in ((0o600, True), (0o644, True), (0o664, False), (0o646, False)):
        f.chmod(mode)
        if ok:
            assert set(load_remotes(paths, test_mode=False)) == {"fpga-pi"}
        else:
            with pytest.raises(RemoteConfigError, match="writable only by you"):
                load_remotes(paths, test_mode=False)
    f.chmod(0o600)
    real = tmp_path / "real.toml"
    f.rename(real)
    os.symlink(real, f)
    with pytest.raises(RemoteConfigError):
        load_remotes(paths, test_mode=False)
