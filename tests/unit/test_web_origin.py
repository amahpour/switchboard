"""`start --listen/--public-url` (issue #34, DESIGN.md §30): where browsers reach the web UI,
checked before a broker starts. The app's checks in public mode are tests/integration/test_public_url.py."""

from __future__ import annotations

import io
import os
import socket
from pathlib import Path

import pytest

from switchboard import cli
from switchboard.broker import daemon
from switchboard.broker.auth import WebOrigin, csp
from switchboard.config import ConfigError, from_dict
from switchboard.paths import Paths


# ------------------------------------------------------------------ WebOrigin
def test_the_default_is_switchboard_localhost_on_the_port() -> None:
    o = WebOrigin.local(7419)
    assert (o.scheme, o.host, o.origin, o.ws, o.secure) == (
        "http", "switchboard.localhost:7419", "http://switchboard.localhost:7419", "ws://switchboard.localhost:7419",
        False)
    assert "connect-src 'self' ws://switchboard.localhost:7419;" in csp(o)


@pytest.mark.parametrize("url,host", [
    ("https://sb.example.com", "sb.example.com"),
    ("https://sb.example.com/", "sb.example.com"),              # a trailing slash is still an origin
    ("https://SB.Example.COM:443", "sb.example.com"),           # browsers send neither the case nor :443
    ("https://sb.example.com:8443", "sb.example.com:8443"),
    ("  https://sb.example.com  ", "sb.example.com"),
    ("https://10.0.0.5", "10.0.0.5"),
    ("https://switchboard", "switchboard"),                      # a single-label name on a private network
])
def test_a_public_https_url(url: str, host: str) -> None:
    o = WebOrigin.parse(url)
    assert o.scheme == "https" and o.host == host and o.secure
    assert o.origin == f"https://{host}" and o.ws == f"wss://{host}"
    assert f"connect-src 'self' wss://{host};" in csp(o) and "ws://" not in csp(o)


@pytest.mark.parametrize("url,host", [
    ("http://switchboard.localhost:7419", "switchboard.localhost:7419"),   # `docker run -p 127.0.0.1:7419:7419`
    ("http://localhost:8080", "localhost:8080"),
    ("http://127.0.0.1:80", "127.0.0.1"),
    ("http://sb.test", "sb.test"),
])
def test_plain_http_only_for_a_local_test_host(url: str, host: str) -> None:
    o = WebOrigin.parse(url)
    assert (o.scheme, o.host, o.secure, o.ws) == ("http", host, False, f"ws://{host}")


@pytest.mark.parametrize("url,why", [
    ("http://sb.example.com", "plain http:// is only for a local test host"),
    ("http://10.0.0.5", "plain http:// is only for a local test host"),
    ("http://localhost.example.com", "plain http:// is only for a local test host"),
    ("sb.example.com", "must start with https://"),
    ("ftp://sb.example.com", "must start with https://"),
    ("wss://sb.example.com", "must start with https://"),
    ("https://sb.example.com/switchboard", "origin only"),
    ("https://sb.example.com/?x=1", "origin only"),
    ("https://sb.example.com#x", "origin only"),
    ("https://user:pw@sb.example.com", "origin only"),
    ("https://user@sb.example.com", "origin only"),
    ("https://", "DNS name or an IPv4 address"),
    ("https://[::1]", "DNS name or an IPv4 address"),
    ("https://sb_example.com", "DNS name or an IPv4 address"),
    ("https://-sb.example.com", "DNS name or an IPv4 address"),
    ("https://sb.example.com.", "DNS name or an IPv4 address"),
    ("https://sb example.com", "DNS name or an IPv4 address"),
    ("https://sb.example.com:99999", "not a URL"),
    ("https://sb.example.com:0", "port 0"),
    ("https://sb.example.com:port", "not a URL"),
])
def test_what_is_not_a_public_url(url: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        WebOrigin.parse(url)


# ------------------------------------------------------------ the two settings
def test_web_settings() -> None:
    assert daemon.web_settings("127.0.0.1", "") is None  # the default: switchboard.localhost
    assert daemon.web_settings("0.0.0.0", "https://sb.example.com") == WebOrigin("https", "sb.example.com", True)
    assert daemon.web_settings("10.1.2.3", "https://sb.example.com:8443") == WebOrigin("https", "sb.example.com:8443", True)
    # loopback behind a proxy on the same machine (Caddy, a tunnel)
    assert daemon.web_settings("127.0.0.1", "https://sb.example.com") == WebOrigin("https", "sb.example.com", True)


@pytest.mark.parametrize("listen,url,why", [
    ("0.0.0.0", "", "listening on 0.0.0.0 needs --public-url"),
    ("192.168.1.20", "", "listening on 192.168.1.20 needs --public-url"),
    # switchboard.localhost resolves to 127.0.0.1 only: any other loopback address needs a URL too
    ("127.0.0.2", "", "listening on 127.0.0.2 needs --public-url"),
    ("::", "https://sb.example.com", "--listen must be an IPv4 address"),
    ("localhost", "", "--listen must be an IPv4 address"),
    ("0.0.0.0:7419", "https://sb.example.com", "--listen must be an IPv4 address"),
    ("0.0.0.0", "http://sb.example.com", "--public-url 'http://sb.example.com': plain http://"),
    ("127.0.0.1", "https://sb.example.com/app", "--public-url 'https://sb.example.com/app': it is an origin only"),
])
def test_web_settings_refusals(listen: str, url: str, why: str) -> None:
    with pytest.raises(ValueError) as e:
        daemon.web_settings(listen, url)
    assert str(e.value).startswith(why), str(e.value)


def test_a_refused_setting_stops_the_broker_before_it_touches_the_home(tmp_path: Path,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    from switchboard.config import Config

    paths = Paths.from_home(tmp_path / "home")
    assert daemon.run_foreground(paths, Config(), port=0, listen="0.0.0.0", announce=False) == 1
    assert "needs --public-url" in capsys.readouterr().err
    assert not paths.home.exists()
    # config.toml's settings are checked the same way
    cfg = from_dict({"listen": "0.0.0.0", "public_url": "http://sb.example.com"})
    assert daemon.run_foreground(paths, cfg, port=0, announce=False) == 1
    assert "plain http://" in capsys.readouterr().err
    assert not paths.home.exists()


# ------------------------------------------------------------------ config.toml
def test_config_keys() -> None:
    cfg = from_dict({})
    assert (cfg.listen, cfg.public_url) == ("127.0.0.1", "")
    cfg = from_dict({"listen": "0.0.0.0", "public_url": "https://sb.example.com"})
    assert (cfg.listen, cfg.public_url) == ("0.0.0.0", "https://sb.example.com")
    for key, bad in (("listen", 0), ("public_url", ["https://x"]), ("listen", None)):
        with pytest.raises(ConfigError, match=f"{key} must be a string"):
            from_dict({key: bad})


# ------------------------------------------------------------------------ CLI
def test_start_flags_and_their_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    a = cli.build_parser().parse_args(["start", "--foreground"])
    assert (a.port, a.listen, a.public_url, a.log_stdout) == (None, None, None, False)
    a = cli.build_parser().parse_args(["start", "--foreground", "--port", "8080", "--listen", "0.0.0.0",
                                       "--public-url", "https://sb.example.com", "--log-stdout"])
    assert (a.port, a.listen, a.public_url, a.log_stdout) == (8080, "0.0.0.0", "https://sb.example.com", True)
    # a container sets them through its environment (docs/DEPLOY.md); a flag still wins
    monkeypatch.setenv("SWITCHBOARD_PORT", "7420")
    monkeypatch.setenv("SWITCHBOARD_LISTEN", "0.0.0.0")
    monkeypatch.setenv("SWITCHBOARD_PUBLIC_URL", "https://sb.example.com")
    a = cli.build_parser().parse_args(["start", "--foreground"])
    assert (a.port, a.listen, a.public_url) == (7420, "0.0.0.0", "https://sb.example.com")
    a = cli.build_parser().parse_args(["start", "--port", "0", "--listen", "127.0.0.1"])
    assert (a.port, a.listen) == (0, "127.0.0.1")
    # an empty variable is unset
    for k in ("SWITCHBOARD_PORT", "SWITCHBOARD_LISTEN", "SWITCHBOARD_PUBLIC_URL"):
        monkeypatch.setenv(k, "")
    a = cli.build_parser().parse_args(["start"])
    assert (a.port, a.listen, a.public_url) == (None, None, None)


@pytest.mark.parametrize("port", ["-1", "65536", "abc", "7419.0", ""])
def test_a_port_is_0_to_65535(port: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["start", "--port", port])
    assert "is not a port number (0..65535)" in capsys.readouterr().err
    if port:
        monkeypatch.setenv("SWITCHBOARD_PORT", port)
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["start"])
        assert "is not a port number" in capsys.readouterr().err


def test_start_checks_the_settings_before_it_starts_a_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """`switchboard start` (daemonizing) refuses a bad setting itself, from a flag or config.toml,
    rather than starting a broker that fails in its log."""
    started: list[dict[str, object]] = []
    monkeypatch.setattr(daemon, "start", lambda paths, **kw: started.append(kw) or 0)
    home = tmp_path / "home"
    assert cli.main(["start", "--home", str(home), "--listen", "0.0.0.0"]) == cli.EXIT_USAGE
    assert "listening on 0.0.0.0 needs --public-url" in capsys.readouterr().err
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text('listen = "0.0.0.0"\npublic_url = "http://sb.example.com"\n')
    assert cli.main(["start", "--home", str(home)]) == cli.EXIT_USAGE
    assert "plain http://" in capsys.readouterr().err
    assert started == []
    # a flag wins over config.toml: config's listen with the flag's URL is a good pair
    assert cli.main(["start", "--home", str(home), "--public-url", "https://sb.example.com"]) == 0
    assert len(started) == 1 and started[0]["public_url"] == "https://sb.example.com"


def test_log_stdout_needs_foreground(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start", "--home", str(tmp_path / "home"), "--log-stdout"]) == cli.EXIT_USAGE
    assert "--log-stdout needs --foreground" in capsys.readouterr().err
    assert not (tmp_path / "home").exists()


def test_start_hands_the_settings_to_the_broker_and_prints_its_url(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """`switchboard start` (daemonizing) passes --listen/--public-url to the broker it starts
    and prints the address the broker reports (ping's `url`), not switchboard.localhost."""
    pings = iter([None, {"pid": 4242, "port": 7419, "url": "https://sb.example.com/", "test_mode": False}])
    cmds: list[list[str]] = []

    class Child:
        def poll(self) -> None:
            return None

    def popen(cmd: list[str], **kw: object) -> Child:
        cmds.append(cmd)
        return Child()

    monkeypatch.setattr(daemon, "ping", lambda *a, **kw: next(pings))
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon, "call_sync", lambda *a, **kw: {"url": "https://sb.example.com/login?t=x"})
    out = io.StringIO()
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    try:
        assert daemon.start(Paths.from_home(tmp_path / "home"), port=7419, listen="0.0.0.0",
                            public_url="https://sb.example.com", out=out) == 0
    finally:
        os.umask(old_umask)  # start() sets 077
    cmd = cmds[0]
    assert cmd[cmd.index("--listen") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--public-url") + 1] == "https://sb.example.com"
    assert "switchboard is running (pid 4242) at https://sb.example.com/" in out.getvalue()


def test_start_says_where_a_running_broker_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "ping", lambda *a, **kw: {"pid": 7, "port": 7419, "url": "https://sb.example.com/"})
    out = io.StringIO()
    assert daemon.start(Paths.from_home(tmp_path / "home"), out=out) == 0
    assert "already running (pid 7) at https://sb.example.com/" in out.getvalue()
    # a broker from before #34 has no url in its ping
    monkeypatch.setattr(daemon, "ping", lambda *a, **kw: {"pid": 7, "port": 7419})
    out = io.StringIO()
    assert daemon.start(Paths.from_home(tmp_path / "home"), out=out) == 0
    assert "at http://switchboard.localhost:7419/" in out.getvalue()


def test_tcp_listener(tmp_path: Path) -> None:
    s = daemon.tcp_listener("127.0.0.1", 0)
    try:
        host, port = s.getsockname()
        assert host == "127.0.0.1" and port > 0
        assert s.proto == socket.IPPROTO_TCP  # or asyncio leaves Nagle on (tests/unit/test_tcp_listener.py)
        s.listen()  # Linux lets two SO_REUSEADDR sockets bind one port until one of them listens
        with pytest.raises(OSError):
            daemon.tcp_listener("127.0.0.1", port)
    finally:
        s.close()
    with pytest.raises(OSError):
        daemon.tcp_listener("192.0.2.1", 0)  # TEST-NET-1: never this machine's address
