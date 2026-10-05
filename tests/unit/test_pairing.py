"""Pairing a remote (DESIGN.md §27.5.8, §27.8): the token, the exact ``authorized_keys`` line,
``--from``, the refusals, backups, the round trip, the host-key pin and ``ssh -G``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import shutil
import tomllib
from pathlib import Path

import pytest

from switchboard.paths import Paths
from switchboard.remote import pairing
from switchboard.remote.config import load_remotes, read_satellite_conf
from switchboard.remote.pairing import (
    PairingError,
    Token,
    authorized_line,
    check_from,
    find_pin,
    parse_ak_line,
    parse_ssh_g,
    parse_token,
    plan_accept,
    plan_remove,
    remove_table,
    satellite_command,
)

KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIMCGkdYxdHrN6N8Lhzn9oRL0Rj6qu5M3QZQpqk2hVg8C"
KEY2 = "AAAAC3NzaC1lZDI1NTE5AAAAIK3gW0ACRSHMIY6Sp+H5S+gnzwyCkGeAdA9cRZwQvAhS"
KEY3 = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(1, 33))).decode()
TOKEN = f"switchboard-link v1 fpga-pi desk ssh-ed25519 {KEY}"
PY = "/home/alice/.local/share/uv/tools/switchboard/bin/python"
HOME = "/home/alice/.switchboard"
HAVE_SSH = all(os.access(b, os.X_OK) for b in ("/usr/bin/ssh", "/usr/bin/ssh-keygen"))
needs_ssh = pytest.mark.skipif(not HAVE_SSH, reason="needs /usr/bin/ssh and /usr/bin/ssh-keygen")


@pytest.fixture(autouse=True)
def installed_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests run from the repo's editable venv; ``accept`` sees an installed copy
    unless a test says otherwise (``test_refuses_editable_install``)."""
    monkeypatch.setattr(pairing, "editable_install", lambda: False)


ORIGINAL = (
    "# my keys\n"
    f"ssh-ed25519 {KEY2} alice@laptop\n"
    "\n"
    f'restrict,command="rrsync -wo /home/alice/fpga/in" ssh-ed25519 {KEY2} fpga-push\n'
)


def test_token_round_trip() -> None:
    t = parse_token(TOKEN)
    assert t == Token(name="fpga-pi", desktop="desk", key=KEY)
    assert t.text() == TOKEN and parse_token(f"  {TOKEN}\n") == t
    assert t.fingerprint.startswith("SHA256:")
    rsa = "AAAAB3NzaC1yc2EAAAADAQABAAABAQC7"
    for bad, why in [
        ("", "not a switchboard link token"),
        (TOKEN.replace("v1", "v2"), "version"),
        (TOKEN + " extra", "six words"),
        (TOKEN.replace("fpga-pi", "Fpga_Pi"), "remote name"),
        (TOKEN.replace(" desk ", " de$k "), "desktop label"),
        (TOKEN.replace("ssh-ed25519", "ssh-rsa"), "key type"),
        (TOKEN.replace(KEY, "!!!!"), "base64"),
        (TOKEN.replace(KEY, rsa), "not an ssh-ed25519"),
        (
            TOKEN.replace(
                KEY, base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 31).decode()
            ),
            "not an ssh-ed25519",
        ),
    ]:
        with pytest.raises(PairingError, match=why):
            parse_token(bad)


def test_authorized_keys_line_exact() -> None:
    t = parse_token(TOKEN)
    assert authorized_line(t, PY, HOME) == (
        f'restrict,command="{PY} -I -m switchboard satellite --home {HOME} --name fpga-pi"'
        f" ssh-ed25519 {KEY} switchboard-link fpga-pi"
    )
    # a path with a blank is quoted for the login shell; quotes and $ are refused outright
    spaced = authorized_line(t, "/opt/my tools/python", HOME)
    assert "command=\"'/opt/my tools/python' -I -m switchboard satellite" in spaced
    for bad in ('/opt/x"y/python', "/opt/$x/python", "/opt/x'y/python", "relative/python", "/opt/x\\y"):
        with pytest.raises(PairingError):
            authorized_line(t, bad, HOME)
    with pytest.raises(PairingError):
        authorized_line(t, PY, "/home/alice/$HOME")
    # what we wrote, we read back
    e = parse_ak_line(authorized_line(t, PY, HOME))
    assert e is not None and e.key == KEY and e.option("restrict") == "" and e.has_command
    assert satellite_command(e) == (PY, HOME, "fpga-pi")


def test_from_option() -> None:
    assert check_from("192.0.2.10") == "192.0.2.10"
    assert check_from("192.0.2.0/24") == "192.0.2.0/24"
    assert check_from("2001:db8::1") == "2001:db8::1"
    assert check_from("192.0.2.10, 198.51.100.7") == "192.0.2.10,198.51.100.7"
    assert check_from(None) is None
    for bad in ("desk.local", "*", "192.0.2.10;x", "", "192.0.2.10,"):
        with pytest.raises(PairingError):
            check_from(bad)
    line = authorized_line(parse_token(TOKEN), PY, HOME, check_from("192.0.2.10"))
    assert line.startswith('restrict,from="192.0.2.10",command="')
    e = parse_ak_line(line)
    assert (
        e is not None and e.option("from") == "192.0.2.10" and satellite_command(e) == (PY, HOME, "fpga-pi")
    )


def test_refuses_unrestricted_duplicate(tmp_path: Path) -> None:
    t = parse_token(TOKEN)
    shell = f"ssh-ed25519 {KEY} somebody\n"
    with pytest.raises(PairingError, match="without command="):
        plan_accept(ORIGINAL + shell, t, authorized_line(t, PY, HOME), HOME)
    with pytest.raises(PairingError, match="without command="):
        plan_accept(f"no-pty ssh-ed25519 {KEY} x\n", t, authorized_line(t, PY, HOME), HOME)
    # through `accept`: nothing is written
    ak = tmp_path / "authorized_keys"
    ak.write_text(ORIGINAL + shell)
    home = tmp_path / "home"
    with pytest.raises(PairingError):
        _accept(home, ak)
    assert ak.read_text() == ORIGINAL + shell and not (home / "satellite.toml").exists()


def _accept(home: Path, ak: Path, token: str = TOKEN, **kw: object) -> str:
    out = io.StringIO()
    kw.setdefault("allow_editable", True)
    rc = pairing.accept(
        Paths.from_home(home), token, ak_path=ak, yes=True, python=PY, out=out, ping=lambda _p: None, **kw
    )  # type: ignore[arg-type]
    assert rc == 0, out.getvalue()
    return out.getvalue()


def test_backup_0600(tmp_path: Path) -> None:
    ak = tmp_path / "authorized_keys"
    ak.write_text(ORIGINAL)
    ak.chmod(0o644)
    home = tmp_path / "home"
    out = _accept(home, ak)
    backups = sorted(tmp_path.glob("authorized_keys.bak-switchboard-*"))
    assert len(backups) == 1 and backups[0].read_text() == ORIGINAL
    assert os.stat(backups[0]).st_mode & 0o777 == 0o600
    assert f"backup: {backups[0]}" in out and f"+ {authorized_line(parse_token(TOKEN), PY, str(home))}" in out
    assert os.stat(ak).st_mode & 0o777 == 0o644  # the file keeps its mode
    conf = read_satellite_conf(Paths.from_home(home))
    assert (conf.name, conf.desktop, conf.key_fingerprint) == (
        "fpga-pi",
        "desk",
        parse_token(TOKEN).fingerprint,
    )
    # a new file is created 0600, and a second accept changes nothing
    ak2 = tmp_path / "new" / "authorized_keys"
    _accept(tmp_path / "home2", ak2)
    assert os.stat(ak2).st_mode & 0o777 == 0o600
    before = ak2.read_text()
    assert "no changes" in _accept(tmp_path / "home2", ak2)
    assert ak2.read_text() == before and not list(ak2.parent.glob("*.bak-switchboard-*"))


def test_remove_round_trip_byte_for_byte(tmp_path: Path) -> None:
    ak = tmp_path / "authorized_keys"
    original = ORIGINAL + "# a comment after\r\nssh-rsa AAAAB3NzaC1yc2E alice@old\r\n"
    ak.write_bytes(original.encode())
    home = tmp_path / "home"
    _accept(home, ak, from_="192.0.2.10")
    assert ak.read_text().count("switchboard-link fpga-pi") == 1
    out = io.StringIO()
    assert pairing.remove_remote(Paths.from_home(home), "fpga-pi", ak_path=ak, yes=True, out=out) == 0
    assert ak.read_bytes() == original.encode()
    assert not (home / "satellite.toml").exists()
    # a re-accept after a rotated key replaces the old line where it stands
    t2 = f"switchboard-link v1 fpga-pi desk ssh-ed25519 {KEY3}"
    _accept(home, ak)
    first = ak.read_text()
    after, replaced = plan_accept(
        first, parse_token(t2), authorized_line(parse_token(t2), PY, str(home)), home
    )
    assert len(replaced) == 1 and KEY in replaced[0] and after.count("switchboard-link fpga-pi") == 1
    assert KEY3 in after and after.count("\n") == first.count("\n")
    # plan_remove never touches another remote's line
    both = after + authorized_line(Token("other-pi", "desk", KEY), PY, "/tmp/o") + "\n"
    kept, removed = plan_remove(both, "fpga-pi", home)
    assert len(removed) == 1 and "switchboard-link other-pi" in kept


def test_two_homes_with_one_remote_name_stay_apart(tmp_path: Path) -> None:
    """Two desktops can each call their remote ``fpga-pi`` on one account of the Pi, in two
    satellite homes: accept and remove in one home never touch the other's line."""
    ak = tmp_path / "authorized_keys"
    ha, hb = tmp_path / "home-a", tmp_path / "home-b"
    _accept(ha, ak)
    tb = f"switchboard-link v1 fpga-pi desk2 ssh-ed25519 {KEY3}"
    _accept(hb, ak, token=tb)
    text = ak.read_text()
    assert text.count("switchboard-link fpga-pi") == 2 and KEY in text and KEY3 in text
    assert pairing.remove_remote(Paths.from_home(ha), "fpga-pi", ak_path=ak, yes=True, out=io.StringIO()) == 0
    left = ak.read_text()
    assert KEY not in left and KEY3 in left and f"--home {hb}" in left
    kept, removed = plan_remove(left, "fpga-pi", ha)
    assert removed == [] and kept == left
    # a line with no satellite command is still recognized by its comment
    legacy = f'restrict,command="/bin/true" ssh-ed25519 {KEY2} switchboard-link fpga-pi\n'
    assert plan_remove(legacy, "fpga-pi", ha)[1] == [legacy.strip()]


def test_default_authorized_keys_is_the_passwd_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    """sshd reads authorized_keys under the password database's home: with ``$HOME``
    elsewhere (here the tests' temp HOME) there is no guessing, the file must be named."""
    with pytest.raises(PairingError, match="--authorized-keys"):
        pairing.default_authorized_keys()
    monkeypatch.setattr(pairing, "_passwd_home", lambda: os.path.expanduser("~"))
    assert pairing.default_authorized_keys() == Path(os.path.expanduser("~")) / ".ssh" / "authorized_keys"


def test_refuses_editable_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "editable_install", lambda: True)
    ak = tmp_path / "authorized_keys"
    ak.write_text(ORIGINAL)
    with pytest.raises(PairingError, match="editable"):
        pairing.accept(
            Paths.from_home(tmp_path / "home"),
            TOKEN,
            ak_path=ak,
            yes=True,
            python=PY,
            out=io.StringIO(),
            ping=lambda _p: None,
        )
    assert ak.read_text() == ORIGINAL and not (tmp_path / "home").exists()
    # --allow-editable is switchboard's own tests' override: never for a real home
    with pytest.raises(PairingError, match="only for switchboard's own tests"):
        _accept(tmp_path / "home", ak, allow_editable=True)
    monkeypatch.setenv("SWITCHBOARD_TEST", "0")
    (tmp_path / "home").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "home" / ".switchboard-test").touch()
    with pytest.raises(PairingError, match="only for switchboard's own tests"):
        _accept(tmp_path / "home", ak, allow_editable=True)
    assert ak.read_text() == ORIGINAL
    monkeypatch.setenv("SWITCHBOARD_TEST", "1")  # a test home: the override holds
    _accept(tmp_path / "home", ak, allow_editable=True)


def test_accept_refuses_a_desktop_or_foreign_home(tmp_path: Path) -> None:
    ak = tmp_path / "authorized_keys"
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "remotes.toml").write_text('[remote.x]\nhost = "h"\nuser = "u"\nrooms = ["#a"]\n')
    with pytest.raises(PairingError, match="desktop home"):
        _accept(home, ak)
    (home / "remotes.toml").unlink()
    with pytest.raises(PairingError, match="broker runs"):
        pairing.accept(
            Paths.from_home(home),
            TOKEN,
            ak_path=ak,
            yes=True,
            python=PY,
            out=io.StringIO(),
            allow_editable=True,
            ping=lambda _p: {"role": None, "pid": 1},
        )
    _accept(home, ak)
    with pytest.raises(PairingError, match="already fpga-pi's satellite home"):
        _accept(home, ak, token=TOKEN.replace("fpga-pi", "other-pi"))
    assert not ak.exists() or "other-pi" not in ak.read_text()


def test_accept_needs_a_yes(tmp_path: Path) -> None:
    ak = tmp_path / "authorized_keys"
    out = io.StringIO()
    rc = pairing.accept(
        Paths.from_home(tmp_path / "home"),
        TOKEN,
        ak_path=ak,
        yes=False,
        python=PY,
        allow_editable=True,
        stdin=io.StringIO("y\n"),
        out=out,
        ping=lambda _p: None,
    )
    assert rc == 1 and "not applied" in out.getvalue() and not ak.exists()  # no terminal: never applied


# --------------------------------------------------------------------- add
def test_ssh_g_parsing() -> None:
    r = parse_ssh_g(
        "user alice\nhostname fpga-pi.local\nport 2222\nproxyjump none\nhostkeyalias none\n"
        "userknownhostsfile /h/.ssh/known_hosts /h/.ssh/known_hosts2\n"
        "globalknownhostsfile /etc/ssh/ssh_known_hosts\n"
    )
    assert (r.hostname, r.user, r.port, r.proxy, r.hostkeyalias) == (
        "fpga-pi.local",
        "alice",
        2222,
        None,
        None,
    )
    assert r.known_hosts == ["/h/.ssh/known_hosts", "/h/.ssh/known_hosts2", "/etc/ssh/ssh_known_hosts"]
    assert parse_ssh_g("hostname x\nproxycommand nc %h %p\n").proxy == "proxycommand nc %h %p"
    assert pairing.known_hosts_name(r) == "[fpga-pi.local]:2222"
    r.port = 22
    assert pairing.known_hosts_name(r) == "fpga-pi.local"
    r.hostkeyalias = "pi-alias"
    assert pairing.known_hosts_name(r) == "pi-alias"


@needs_ssh
def test_refuses_proxyjump(tmp_path: Path) -> None:
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host jumpy\n  ProxyJump bob@bastion\nHost cmdy\n  ProxyCommand nc %h %p\n")
    for host in ("jumpy", "cmdy"):
        with pytest.raises(PairingError, match="jump host or proxy command"):
            pairing.resolve(host, None, None, str(cfg))
    r = pairing.resolve("fpga-pi.local", "alice", 2222, str(cfg))
    assert (r.hostname, r.user, r.port) == ("fpga-pi.local", "alice", 2222)
    with pytest.raises(PairingError):
        pairing.split_dest("-oProxyCommand=x")
    with pytest.raises(PairingError):
        pairing.split_dest("Alice@host")


def _hashed(host: str, keytype: str, key: str) -> str:
    salt = os.urandom(20)
    mac = hmac.new(salt, host.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(mac).decode()} {keytype} {key}\n"


@needs_ssh
def test_pin_from_hashed_known_hosts(tmp_path: Path) -> None:
    kh = tmp_path / "known_hosts"
    ecdsa = (
        "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBCpAEVchclT81q64CFZjE5HYEyQ2Wurc9jNSh2DUbBaPWCv"
        "/r16hVQGZEftMgtBvbYtldlVO0qsZlr1+Y57MZko="
    )
    kh.write_text(
        _hashed("other.host", "ssh-ed25519", KEY2)
        + _hashed("[192.0.2.10]:2222", "ecdsa-sha2-nistp256", ecdsa)
        + _hashed("[192.0.2.10]:2222", "ssh-ed25519", KEY)
        + f"@cert-authority *.example ssh-ed25519 {KEY2}\n"
    )
    assert find_pin("[192.0.2.10]:2222", [str(tmp_path / "missing"), str(kh)]) == (
        "ssh-ed25519",
        KEY,
        str(kh),
    )
    assert find_pin("192.0.2.10", [str(kh)]) is None  # port 22 is another entry
    # a revoked key is never pinned
    kh.write_text(_hashed("[192.0.2.10]:2222", "ssh-ed25519", KEY) + f"@revoked * ssh-ed25519 {KEY}\n")
    assert find_pin("[192.0.2.10]:2222", [str(kh)]) is None


def _add(home: Path, tmp: Path, kh_text: str, name: str = "fpga-pi", **kw: object) -> str:
    cfg = tmp / "ssh_config"
    cfg.write_text("")
    kh = tmp / "known_hosts"
    kh.write_text(kh_text)
    out = io.StringIO()
    kw.setdefault("rooms", ["#fpga"])
    rc = pairing.add(
        Paths.from_home(home),
        name,
        "alice@192.0.2.10",
        port=2222,
        ssh_config=str(cfg),
        known_hosts=str(kh),
        authorized_keys=tmp / "desk_ak",
        label="desk",
        out=out,
        **kw,
    )  # type: ignore[arg-type]
    assert rc == 0
    return out.getvalue()


@needs_ssh
def test_add_writes_key_pin_and_table(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "remotes.toml").write_text("# my remotes\n")
    (tmp_path / "desk_ak").write_text(ORIGINAL)
    out = _add(
        home,
        tmp_path,
        f"[192.0.2.10]:2222 ssh-ed25519 {KEY}\n",
        rooms=["#fpga,#lab"],
        harnesses=["claude,codex"],
    )
    d = home / "remotes" / "fpga-pi"
    assert (d / "known_hosts").read_text() == f"switchboard-fpga-pi ssh-ed25519 {KEY}\n"
    for f in (d / "id_ed25519", d / "id_ed25519.pub", d / "known_hosts", home / "remotes.toml"):
        assert os.stat(f).st_mode & 0o777 == 0o600, f
    assert os.stat(d).st_mode & 0o777 == 0o700
    text = (home / "remotes.toml").read_text()
    assert text.startswith("# my remotes\n")
    table = tomllib.loads(text)["remote"]["fpga-pi"]
    assert table == {
        "host": "192.0.2.10",
        "user": "alice",
        "port": 2222,
        "rooms": ["#fpga", "#lab"],
        "harnesses": ["claude", "codex"],
    }
    e = load_remotes(Paths.from_home(home), test_mode=False)["fpga-pi"]
    assert e.transport == "ssh" and e.rooms == ("#fpga", "#lab")
    pub = (d / "id_ed25519.pub").read_text().split()
    assert pub[2:] == ["switchboard-link", "fpga-pi@desk"]
    token = f"switchboard-link v1 fpga-pi desk ssh-ed25519 {pub[1]}"
    assert f"switchboard remote accept '{token}'" in out
    assert "opens a shell" in out or "open a shell" in out  # the desktop's shell key is pointed out
    # a second add of the same name is refused, and changes nothing
    with pytest.raises(PairingError, match="already in remotes.toml"):
        _add(home, tmp_path, f"[192.0.2.10]:2222 ssh-ed25519 {KEY}\n")


@needs_ssh
def test_no_pin_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(PairingError, match="no host key for \\[192.0.2.10\\]:2222"):
        _add(home, tmp_path, f"192.0.2.10 ssh-ed25519 {KEY}\n")  # port 22's key is not port 2222's
    assert not (home / "remotes").exists() and not (home / "remotes.toml").exists()


def test_add_without_rooms_allows_any_room(tmp_path: Path) -> None:
    home = tmp_path / "home"
    out = _add(home, tmp_path, f"[192.0.2.10]:2222 ssh-ed25519 {KEY}\n", rooms=None)
    assert "any room" in out
    table = tomllib.loads((home / "remotes.toml").read_text())["remote"]["fpga-pi"]
    assert table["rooms"] == ["*"]
    assert load_remotes(Paths.from_home(home), test_mode=False)["fpga-pi"].allows_room("#anything")


def test_remove_table_keeps_the_rest() -> None:
    text = (
        '# remotes\n[remote.a]\nhost = "a.local"\nuser = "alice"\nrooms = [\n  "#x",\n]\n\n'
        '[remote.fpga-pi] # the bench\nhost = "p.local"\nuser = "alice"\nrooms = ["#fpga"]\n\n'
        '[remote."b"]\nhost = "b.local"\nuser = "alice"\nrooms = ["#y"]\n'
    )
    new = remove_table(text, "fpga-pi")
    assert "p.local" not in new and new.startswith("# remotes\n[remote.a]") and '[remote."b"]' in new
    assert set(tomllib.loads(new)["remote"]) == {"a", "b"}
    inline = (
        '[remote]\nfpga-pi = { host = "p.local", user = "u", rooms = ["#x"] }\n\n'
        '[remote.a]\nhost = "a.local"\nuser = "u"\nrooms = ["#x"]\n'
    )
    with pytest.raises(PairingError, match="by hand"):
        remove_table(inline, "fpga-pi")


@needs_ssh
def test_remove_on_the_desktop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _add(home, tmp_path, f"[192.0.2.10]:2222 ssh-ed25519 {KEY}\n")
    _add(home, tmp_path, f"[192.0.2.10]:2222 ssh-ed25519 {KEY}\n", name="other-pi")
    calls: list[tuple[str, dict[str, object]]] = []

    def call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"name": params["name"], "ended": 2, "had_row": True}

    out = io.StringIO()
    assert pairing.remove_desktop(Paths.from_home(home), "fpga-pi", yes=True, call=call, out=out) == 0
    assert calls == [("remote.remove", {"name": "fpga-pi"})]
    assert not (home / "remotes" / "fpga-pi").exists() and (home / "remotes" / "other-pi").exists()
    assert set(load_remotes(Paths.from_home(home), test_mode=False)) == {"other-pi"}
    assert "2 member(s) ended" in out.getvalue() and "switchboard remote remove fpga-pi" in out.getvalue()
    # a refusal from the broker (not the human) changes nothing
    from switchboard.mcp.client import RpcError

    def refuse(method: str, params: dict[str, object]) -> dict[str, object]:
        raise RpcError("forbidden", "human commands must come from a terminal on this machine")

    with pytest.raises(RpcError):
        pairing.remove_desktop(Paths.from_home(home), "other-pi", yes=True, call=refuse, out=io.StringIO())
    assert (home / "remotes" / "other-pi").exists() and "other-pi" in load_remotes(
        Paths.from_home(home), test_mode=False
    )
    shutil.rmtree(home, ignore_errors=True)
