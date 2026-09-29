"""Web auth on the real app (DESIGN.md §5.4, §12.2)."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from conftest import FakeClock, InProcBroker, SubprocBroker, cookie_of, ws_connect
from switchboard.broker.auth import SESSION_TTL_S


def raw_http(port: int, request: bytes) -> str:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(request)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
            if b"\r\n\r\n" in out and (b" 101 " in out.split(b"\r\n", 1)[0] or out.endswith(b"\n")):
                break
    finally:
        s.close()
    return out.decode(errors="replace")


# -------------------------------------------------------------------- host
@pytest.mark.parametrize("host", ["127.0.0.1:{p}", "localhost:{p}", "switchboard.localhost", "switchboard.localhost:1",
                                  "evil.com", "switchboard.localhost.evil.com:{p}", "SWITCHBOARD.localhost:{p}"])
def test_421_on_other_hosts(broker: InProcBroker, host: str) -> None:
    h = host.format(p=broker.port)
    r = httpx.get(f"http://127.0.0.1:{broker.port}/", headers={"Host": h})
    assert r.status_code == 421
    assert f"open http://switchboard.localhost:{broker.port}/" in r.text
    assert "set-cookie" not in r.headers


def test_no_host_header_is_421(broker: InProcBroker) -> None:
    out = raw_http(broker.port, b"GET / HTTP/1.0\r\n\r\n")
    assert out.startswith("HTTP/1.1 421") or out.startswith("HTTP/1.0 421"), out[:80]


def test_websocket_on_other_host_is_refused_even_with_cookie(broker: InProcBroker, web: httpx.Client) -> None:
    ck = cookie_of(web)
    with pytest.raises(InvalidStatus) as e:
        ws_connect(broker, ck, host=f"127.0.0.1:{broker.port}", origin=f"http://127.0.0.1:{broker.port}")
    assert e.value.response.status_code in (403, 421)
    with pytest.raises(InvalidStatus):
        ws_connect(broker, ck, host=f"127.0.0.1:{broker.port}")


# ------------------------------------------------------------------- login
def test_login_is_one_time_and_sets_strict_cookie(broker: InProcBroker) -> None:
    url = broker.login_url()
    path = url.removeprefix(broker.base)
    c = httpx.Client(base_url=broker.base)
    r = c.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/"
    sc = r.headers["set-cookie"]
    parts = [p.strip().lower() for p in sc.split(";")]
    assert parts[0].startswith("switchboard_session=")
    assert "httponly" in parts and "samesite=strict" in parts and "path=/" in parts
    assert f"max-age={SESSION_TTL_S}" in parts
    assert not any(p.startswith("domain") for p in parts)
    assert c.get("/api/me").status_code == 200
    # the same link again: refused
    c2 = httpx.Client(base_url=broker.base)
    r2 = c2.get(path)
    assert r2.status_code == 403 and "set-cookie" not in r2.headers
    assert c2.get("/api/me").status_code == 401
    assert httpx.get(broker.base + "/login?t=nope").status_code == 403
    assert httpx.get(broker.base + "/login").status_code == 403


def test_bad_login_tokens_do_not_grow_the_database(broker: InProcBroker) -> None:
    """GET /login needs no session, so bad-token hits are rate-limited before any write."""
    for i in range(25):
        assert httpx.get(f"{broker.base}/login?t=bad{i}").status_code == 403
    evs = broker.on_loop(lambda: broker.state.store.recent_events(kinds=["login"], limit=100))
    bad = [e for e in evs if e.data.get("what") == "bad_token"]
    assert len(bad) == 1 and bad[0].data["count"] == 1


def test_test_mode_token_file(broker: InProcBroker) -> None:
    p = broker.paths.test_login_token
    assert (os.stat(p).st_mode & 0o777) == 0o600
    tok = p.read_text().strip()
    r = httpx.get(f"{broker.base}/login?t={tok}")
    assert r.status_code == 303


def test_index_without_session_is_the_login_page(broker: InProcBroker, web: httpx.Client) -> None:
    r = httpx.get(broker.base + "/")
    assert r.status_code == 200 and "switchboard login" in r.text and "app.js" not in r.text
    assert "login?t=" not in r.text
    r = web.get("/")
    assert r.status_code == 200 and "/static/app.js" in r.text
    assert "switchboard_session" not in r.text and "login?t=" not in r.text


def test_sliding_expiry(tmp_home: Path) -> None:
    clock = FakeClock()
    b = InProcBroker(tmp_home, clock=clock).start()
    try:
        c = b.web_client()
        clock.advance(SESSION_TTL_S - 60)
        assert c.get("/api/rooms").status_code == 200  # slides the session
        clock.advance(SESSION_TTL_S - 60)
        assert c.get("/api/rooms").status_code == 200
        clock.advance(SESSION_TTL_S + 1)
        assert c.get("/api/rooms").status_code == 401
    finally:
        b.stop()


def test_me_refreshes_the_cookie(broker: InProcBroker, web: httpx.Client) -> None:
    r = web.get("/api/me")
    assert r.status_code == 200
    assert r.json()["human"] == "alice" and r.json()["test_mode"] is True
    assert f"Max-Age={SESSION_TTL_S}" in r.headers["set-cookie"]


# ---------------------------------------------------------------- writes
def test_writes_need_origin_and_header(broker: InProcBroker, web: httpx.Client) -> None:
    web.post("/api/rooms", json={"name": "#build"}, headers=broker.write_headers())
    good = broker.write_headers()
    cases = {
        "no origin": {"X-Switchboard": "1"},
        "null origin": {"Origin": "null", "X-Switchboard": "1"},
        "other port origin": {"Origin": "http://switchboard.localhost:1", "X-Switchboard": "1"},
        "localhost origin": {"Origin": f"http://localhost:{broker.port}", "X-Switchboard": "1"},
        "lookalike origin": {"Origin": f"http://switchboard.localhost:{broker.port}.evil.com", "X-Switchboard": "1"},
        "https origin": {"Origin": f"https://switchboard.localhost:{broker.port}", "X-Switchboard": "1"},
        "no x-switchboard": {"Origin": broker.origin},
        "x-switchboard 0": {"Origin": broker.origin, "X-Switchboard": "0"},
    }
    for label, h in cases.items():
        r = web.post("/api/rooms/build/say", json={"text": "x"}, headers=h)
        assert r.status_code == 403, label
    r = web.post("/api/rooms/build/say", content='{"text":"x"}',
                 headers={"Origin": broker.origin, "Content-Type": "text/plain"})
    assert r.status_code == 403  # a simple (no-preflight) cross-site form post
    assert web.post("/api/rooms/build/say", json={"text": "ok"}, headers=good).status_code == 200


def test_unauthenticated_api_is_401(broker: InProcBroker) -> None:
    c = httpx.Client(base_url=broker.base)
    for path in ["/api/me", "/api/rooms", "/api/rooms/build/messages", "/api/rooms/build/members"]:
        assert c.get(path).status_code == 401, path
    for path in ["/api/rooms", "/api/rooms/build/say", "/api/rooms/build/command", "/logout"]:
        assert c.post(path, json={}, headers=broker.write_headers()).status_code == 401, path


def test_no_unauthenticated_write_routes(broker: InProcBroker) -> None:
    """Enumerate every route: each unsafe method refuses a cookie-less request."""
    from starlette.routing import Mount, Route, WebSocketRoute

    c = httpx.Client(base_url=broker.base)
    checked = 0
    for route in broker.app.routes:
        if isinstance(route, Route):
            for m in route.methods or ():
                if m in {"GET", "HEAD", "OPTIONS"}:
                    continue
                path = route.path.replace("{slug}", "build")
                r = c.request(m, path, json={}, headers=broker.write_headers())
                assert r.status_code in (401, 403, 405), (m, path, r.status_code)
                checked += 1
        elif isinstance(route, WebSocketRoute):
            with pytest.raises(InvalidStatus):
                ws_connect(broker, None)
        elif isinstance(route, Mount):
            r = c.post(route.path + "/app.js", headers=broker.write_headers())
            assert r.status_code in (401, 403, 405)
    assert checked >= 4  # POST /logout, /api/rooms, say, command


def test_docs_are_off_and_headers_present(broker: InProcBroker, web: httpx.Client) -> None:
    for path in ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]:
        assert web.get(path).status_code == 404, path
    for r in [web.get("/"), web.get("/api/rooms"), httpx.get(f"http://127.0.0.1:{broker.port}/"),
              web.get("/static/app.js")]:
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert f"ws://switchboard.localhost:{broker.port}" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_markdown_script_is_served_under_the_csp(broker: InProcBroker, web: httpx.Client) -> None:
    """The native UI (DESIGN.md §29): md.js is a same-origin static script under the same CSP,
    loaded by the signed-in page only; styles come from style.css alone (no inline style)."""
    r = web.get("/static/md.js")
    assert r.status_code == 200 and "SBMarkdown" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    page = web.get("/").text
    assert "/static/md.js" in page and page.index("/static/md.js") < page.index("/static/app.js")
    login = httpx.get(broker.base + "/").text
    assert "md.js" not in login and "app.js" not in login
    for r in (web.get("/"), httpx.get(broker.base + "/")):
        assert "unsafe-inline" not in r.headers["content-security-policy"]


def test_member_detail_needs_a_session(broker: InProcBroker) -> None:
    c = httpx.Client(base_url=broker.base)
    assert c.get("/api/rooms/build/members/claude-1").status_code == 401


# ----------------------------------------------------------------- logout
def test_logout_and_logout_all(broker: InProcBroker) -> None:
    a, b, c = broker.web_client(), broker.web_client(), broker.web_client()
    h = broker.write_headers()
    assert a.post("/logout", json={}, headers=h).status_code == 200
    assert a.get("/api/rooms").status_code == 401
    assert b.get("/api/rooms").status_code == 200
    ws = ws_connect(broker, cookie_of(c))
    r = b.post("/logout", json={"all": True}, headers=h)
    assert r.status_code == 200 and r.json()["revoked"] == 2
    assert b.get("/api/rooms").status_code == 401 and c.get("/api/rooms").status_code == 401
    with pytest.raises(ConnectionClosed):
        for _ in range(10):
            ws.recv(timeout=2)
    # CLI logout --all (human_cli)
    d = broker.web_client()
    assert broker.call("human.logout_all")["revoked"] == 1
    assert d.get("/api/rooms").status_code == 401


# -------------------------------------------------------------- websocket
def test_ws_requires_cookie_and_exact_origin(broker: InProcBroker, web: httpx.Client) -> None:
    ck = cookie_of(web)
    for origin in [None, "null", "http://switchboard.localhost:1", f"http://localhost:{broker.port}"]:
        with pytest.raises(InvalidStatus):
            ws_connect(broker, ck, origin=origin)
    with pytest.raises(InvalidStatus):
        ws_connect(broker, None)
    with pytest.raises(InvalidStatus):
        ws_connect(broker, "forged-cookie")
    ws = ws_connect(broker, ck)
    ws.close()


def test_ws_is_read_only(broker: InProcBroker, web: httpx.Client) -> None:
    ws = ws_connect(broker, cookie_of(web))
    ws.send('{"t":"ping"}')
    assert ws.recv(timeout=5) == '{"t": "pong"}'
    ws.send('{"t":"say","room":"#build","text":"sneaky"}')
    with pytest.raises(ConnectionClosed) as e:
        ws.recv(timeout=5)
    assert e.value.rcvd is not None and e.value.rcvd.code == 1008


@pytest.mark.parametrize("frame", ["not json", "[]", '{"t":"hello","rooms":"#build"}', b"\x00binary"])
def test_ws_bad_frames_close(broker: InProcBroker, web: httpx.Client, frame: str | bytes) -> None:
    ws = ws_connect(broker, cookie_of(web))
    ws.send(frame)
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=5)


# --------------------------------------------------------- secrets in logs
def test_login_token_never_logged(tmp_home: Path) -> None:
    """Real daemon logging: the token is in no log, even after a 421 or a failed login."""
    b = SubprocBroker(tmp_home).start()
    try:
        from switchboard.mcp.client import call_sync

        url = call_sync(b.paths.sock, "human.login_link")["url"]
        tok = url.split("t=", 1)[1]
        test_tok = b.paths.test_login_token.read_text().strip()
        httpx.get(f"http://127.0.0.1:{b.port}/login?t={tok}")  # 421 (wrong host)
        httpx.get(f"{b.base}/login?t={tok}x")  # bad token
        assert httpx.get(f"{b.base}/login?t={tok}").status_code == 303
        assert httpx.get(f"{b.base}/login?t={tok}").status_code == 403
        raw_http(b.port, f"GET /login?t={tok} HTTP/1.1\r\nHost: bogus\r\n\r\n".encode())
    finally:
        b.kill()
    logs = b.paths.logs_dir
    blobs = [p.read_text(errors="replace") for p in logs.iterdir() if p.is_file()]
    blobs.append((tmp_home / "subproc.out").read_text(errors="replace"))
    assert blobs and any("broker up" in x for x in blobs)
    for text in blobs:
        assert tok not in text and test_tok not in text
        assert "login?t=" not in text
