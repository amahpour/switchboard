"""The web UI behind a public URL (issue #34, DESIGN.md §30): every check of the default mode
(tests/integration/test_web_auth.py) with the public URL's host, origin and scheme instead of
switchboard.localhost, plus /healthz and a real broker's container settings.

The broker sees what a TLS-terminating proxy forwards: plain http to its own port, carrying the
browser's Host and Origin. So these requests go to 127.0.0.1:<port> with ``Host: sb.example.com``."""

from __future__ import annotations

import shutil
import signal
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import InProcBroker, SubprocBroker, cookie_of, make_tmp_home
from switchboard.broker.auth import SESSION_TTL_S, WebOrigin

PUBLIC = "https://sb.example.com"
HOST = "sb.example.com"


@pytest.fixture
def public() -> Iterator[InProcBroker]:
    """A broker behind PUBLIC, in a home of its own (a test may run the default `broker` too)."""
    home = make_tmp_home()
    b = InProcBroker(home, web_origin=WebOrigin.parse(PUBLIC)).start()
    try:
        yield b
    finally:
        b.stop()
        shutil.rmtree(home, ignore_errors=True)


def client(b: InProcBroker, host: str = HOST, cookie: str | None = None) -> httpx.Client:
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = f"switchboard_session={cookie}"
    return httpx.Client(base_url=f"http://127.0.0.1:{b.port}", headers=headers, timeout=10.0,
                        follow_redirects=False, limits=httpx.Limits(keepalive_expiry=1.0))


def cookie_attrs(set_cookie: str) -> tuple[str, list[str]]:
    first, *rest = [p.strip() for p in set_cookie.split(";")]
    name, _, value = first.partition("=")
    assert name == "switchboard_session"
    return value, [p.lower() for p in rest]


def sign_in(b: InProcBroker) -> str:
    """The session id, from following a login link as the browser would (through the proxy).
    The cookie is Secure, so it goes back by hand: a cookie jar keeps it off plain http."""
    url = b.login_url()
    assert url.startswith(f"{PUBLIC}/login?t=")
    with client(b) as c:
        r = c.get(url.removeprefix(PUBLIC))
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    sid, attrs = cookie_attrs(r.headers["set-cookie"])
    assert {"secure", "httponly", "samesite=strict", "path=/", f"max-age={SESSION_TTL_S}"} <= set(attrs)
    assert not any(a.startswith("domain") for a in attrs)
    return sid


def write_headers() -> dict[str, str]:
    return {"Origin": PUBLIC, "X-Switchboard": "1", "Content-Type": "application/json"}


# -------------------------------------------------------------------- host
def test_the_public_host_is_the_only_host(public: InProcBroker) -> None:
    with client(public) as c:
        r = c.get("/")
    assert r.status_code == 200 and "switchboard login" in r.text
    for host in [f"switchboard.localhost:{public.port}", f"127.0.0.1:{public.port}", "sb.example.com:443",
                 "SB.example.com", "sb.example.com.evil.com", "evil.com"]:
        with client(public, host=host) as c:
            r = c.get("/")
        assert r.status_code == 421, host
        assert r.text == f"open {PUBLIC}/\n" and "set-cookie" not in r.headers


def test_the_login_link_and_the_secure_cookie(public: InProcBroker) -> None:
    sid = sign_in(public)
    with client(public, cookie=sid) as c:
        r = c.get("/api/me")
        assert r.status_code == 200 and r.json()["human"] == "alice"
        # the sliding refresh keeps the cookie Secure (a refresh without it would downgrade it)
        _, attrs = cookie_attrs(r.headers["set-cookie"])
        assert "secure" in attrs and "httponly" in attrs and "samesite=strict" in attrs
        assert c.get("/").text.count("/static/app.js") == 1
    # a signed-in cookie from another host is still refused by the Host check
    with client(public, host=f"switchboard.localhost:{public.port}", cookie=sid) as c:
        assert c.get("/api/me").status_code == 421


def test_writes_need_the_public_origin(public: InProcBroker) -> None:
    sid = sign_in(public)
    with client(public, cookie=sid) as c:
        assert c.post("/api/rooms", json={"name": "#build"}, headers=write_headers()).status_code == 200
        for origin in ["http://sb.example.com", f"http://switchboard.localhost:{public.port}", "https://evil.com",
                       "https://sb.example.com:443", "null", None]:
            h = {"X-Switchboard": "1", "Content-Type": "application/json"}
            if origin:
                h["Origin"] = origin
            r = c.post("/api/rooms/build/say", json={"text": "x"}, headers=h)
            assert r.status_code == 403, origin
        r = c.post("/api/rooms/build/say", json={"text": "x"}, headers={"Origin": PUBLIC})
        assert r.status_code == 403  # no X-Switchboard: a form or a simple fetch from elsewhere
        assert c.post("/api/rooms/build/say", json={"text": "ok"}, headers=write_headers()).status_code == 200


def test_the_csp_allows_only_the_public_wss(public: InProcBroker) -> None:
    with client(public) as c:
        for path in ["/", "/static/app.js", "/healthz"]:
            csp = c.get(path).headers["content-security-policy"]
            assert f"connect-src 'self' wss://{HOST};" in csp, path
            assert "ws://" not in csp and "switchboard.localhost" not in csp


def ws(b: InProcBroker, cookie: str | None, *, host: str = HOST, origin: str | None = PUBLIC) -> object:
    """The WebSocket as the proxy forwards it: plain ws to the broker's port, the browser's Host."""
    sock = socket.create_connection(("127.0.0.1", b.port), timeout=5)
    headers = {"Cookie": f"switchboard_session={cookie}"} if cookie else {}
    try:
        return connect(f"ws://{host}/ws", sock=sock, origin=origin, additional_headers=headers,  # type: ignore[arg-type]
                       open_timeout=5, close_timeout=2, legacy=True)  # connect now; tests close explicitly
    except BaseException:
        sock.close()
        raise


def test_the_websocket_needs_the_public_host_and_origin(public: InProcBroker) -> None:
    sid = sign_in(public)
    conn = ws(public, sid)
    try:
        conn.send('{"t":"ping"}')  # type: ignore[attr-defined]
        assert conn.recv(timeout=5) == '{"t": "pong"}'  # type: ignore[attr-defined]
    finally:
        conn.close()  # type: ignore[attr-defined]
    refused = [dict(origin="http://sb.example.com"), dict(origin=f"http://switchboard.localhost:{public.port}"),
               dict(origin=None), dict(host=f"switchboard.localhost:{public.port}"), dict(host="evil.com")]
    for kw in refused:
        with pytest.raises(InvalidStatus):
            ws(public, sid, **kw)  # type: ignore[arg-type]
    with pytest.raises(InvalidStatus):
        ws(public, None)
    with pytest.raises(InvalidStatus):
        ws(public, "forged")


def test_logout_clears_the_secure_cookie(public: InProcBroker) -> None:
    sid = sign_in(public)
    with client(public, cookie=sid) as c:
        r = c.post("/logout", json={}, headers=write_headers())
        assert r.status_code == 200
        value, attrs = cookie_attrs(r.headers["set-cookie"])
        assert value in ("", '""') and "secure" in attrs and "max-age=0" in attrs
        assert c.get("/api/me").status_code == 401


def test_status_and_ping_give_the_public_url(public: InProcBroker, broker: InProcBroker) -> None:
    assert public.call("sys.ping")["url"] == f"{PUBLIC}/"
    assert public.call("sys.status")["url"] == f"{PUBLIC}/"
    assert broker.call("sys.ping")["url"] == f"http://switchboard.localhost:{broker.port}/"
    assert broker.call("sys.status")["url"] == f"http://switchboard.localhost:{broker.port}/"


# ------------------------------------------------------------------ /healthz
@pytest.mark.parametrize("mode", ["public", "default"])
def test_healthz_answers_any_host_and_nothing_else(mode: str, public: InProcBroker, broker: InProcBroker) -> None:
    """A platform's health check probes the container's own address, so /healthz skips the Host
    check; it needs no session and says nothing but "ok". Everything else keeps the check."""
    b = public if mode == "public" else broker
    for host in ["10.0.0.7:7419", "localhost", f"127.0.0.1:{b.port}", HOST]:
        with client(b, host=host) as c:
            r = c.get("/healthz")
            assert (r.status_code, r.text) == (200, "ok\n"), host
            assert r.headers["cache-control"] == "no-store" and "set-cookie" not in r.headers
            assert r.headers["x-content-type-options"] == "nosniff" and "content-security-policy" in r.headers
            r = c.head("/healthz")
            assert r.status_code == 200 and r.content == b""
            assert c.get("/healthz?verbose=1").text == "ok\n"
    with client(b, host="10.0.0.7:7419") as c:
        # only GET and HEAD of exactly /healthz
        assert c.post("/healthz", headers=write_headers()).status_code == 421
        for path in ["/healthz/", "/healthz/../api/me", "//healthz", "/HEALTHZ", "/api/me", "/", "/login?t=x"]:
            assert c.get(path).status_code == 421, path
    # no Host at all (HTTP/1.0): still ok
    s = socket.create_connection(("127.0.0.1", b.port), timeout=5)
    try:
        s.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    finally:
        s.close()
    assert out.split(b"\r\n", 1)[0].endswith(b" 200 OK") and out.endswith(b"\r\n\r\nok\n")


# --------------------------------------------------------- a real broker process
def test_a_real_broker_with_container_settings(tmp_home: Path) -> None:
    """`start --foreground --public-url … --log-stdout`: announces the public URL, logs to stdout
    (not logs/broker.log), and on SIGTERM (`docker stop`) shuts down and exits 0."""
    b = SubprocBroker(tmp_home, args=["--public-url", "http://sb.test", "--log-stdout"]).start()
    try:
        from switchboard.mcp.client import call_sync

        assert call_sync(b.paths.sock, "sys.ping", {})["url"] == "http://sb.test/"
        assert call_sync(b.paths.sock, "human.login_link", {})["url"].startswith("http://sb.test/login?t=")
        r = httpx.get(f"http://127.0.0.1:{b.port}/healthz")
        assert (r.status_code, r.text) == (200, "ok\n")
        assert httpx.get(f"http://127.0.0.1:{b.port}/", headers={"Host": "sb.test"}).status_code == 200
        assert b.paths.pidfile.exists()
        assert b.proc is not None
        b.proc.send_signal(signal.SIGTERM)
        assert b.proc.wait(15) == 0
    finally:
        b.kill()
    out = (tmp_home / "subproc.out").read_text(errors="replace")
    assert f"switchboard broker pid {b.pid} on http://sb.test/ (TEST MODE)" in out
    assert "broker up" in out  # the log, on stdout
    assert not b.paths.log.exists()
    assert not b.paths.pidfile.exists()  # the broker's own cleanup ran


def test_sigterm_stops_a_default_broker_cleanly_too(tmp_home: Path) -> None:
    b = SubprocBroker(tmp_home).start()
    try:
        assert b.proc is not None
        deadline = time.monotonic() + 10
        while not b.paths.log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        b.proc.send_signal(signal.SIGTERM)
        assert b.proc.wait(15) == 0
    finally:
        b.kill()
    assert "broker up" in b.paths.log.read_text()
    assert not b.paths.pidfile.exists()


def test_cookie_of_is_not_used_for_secure_cookies() -> None:
    """A reminder for this file's helpers: conftest's cookie_of reads a client's jar, which never
    holds a Secure cookie for a plain-http base URL; sign_in() above reads Set-Cookie instead."""
    c = httpx.Client(base_url="http://127.0.0.1:1")
    c.cookies.set("switchboard_session", "x", domain="127.0.0.1")
    assert cookie_of(c) == "x"
