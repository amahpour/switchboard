"""Customizing the agents' avatars over the web API (#498, DESIGN.md §29.11): the admin uploads a PNG or
WebP for a harness's light and dark avatar, every signed-in person is served it, and nobody else can
change one. The files are checked by their bytes and refused with a message the page shows; they are
served from the page's own origin with the right type, nosniff and a cache key that changes on upload;
each change is an event; the page's CSP is the one it had.

The pictures are generated solid colours (``tests/fakes/pictures.py``). The hosted broker and its people
are ``test_people``'s."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from conftest import InProcBroker, cookie_of, ws_connect
from fakes import pictures
from test_people import Browser, join_as_bob, set_up

from switchboard.broker.auth import app_csp

pytest_plugins = ("test_people",)  # makes its hosted fixture available to this module

PNG = pictures.png(64, 48, (200, 30, 30, 255))
PNG_DARK = pictures.png(32, 32, (240, 240, 40, 255))
WEBP = pictures.webp(40, 40, (30, 30, 200, 255))


def put(
    web: httpx.Client, broker: InProcBroker, harness: str, scheme: str, data: bytes, ctype: str | None
) -> Any:
    headers = {"Origin": broker.origin, "X-Switchboard": "1"}
    if ctype is not None:
        headers["Content-Type"] = ctype
    return web.put(f"/api/agent-icons/{harness}/{scheme}", content=data, headers=headers)


def listing(web: httpx.Client) -> dict[str, Any]:
    r = web.get("/api/agent-icons")
    assert r.status_code == 200, r.text
    return r.json()


def stored(web: httpx.Client) -> list[tuple[str, str]]:
    return [(i["harness"], i["scheme"]) for i in listing(web)["icons"]]


def icon_events(b: InProcBroker) -> list[dict[str, Any]]:
    evs = b.on_loop(lambda: b.state.store.recent_events(kinds=["icons"], limit=200))
    return [e.data for e in reversed(evs)]


# ------------------------------------------------------------------ the owner, on a desktop broker
def test_the_owner_uploads_an_icon_and_it_is_served_back_as_what_it_is(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """The picture is stored whole and served from the page's own origin with its own type, nosniff and a
    cache that lasts as long as its key does (the hash in ``?v=``), with a CSP of ``default-src 'none'``
    so a response opened on its own is inert. Fails if it comes back with another type, without
    nosniff, cached forever under a URL a new upload doesn't change, or scriptable."""
    r = put(web, broker, "codex", "light", PNG, "image/png")
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["harness"], got["scheme"], got["type"]) == ("codex", "light", "image/png")
    assert (got["width"], got["height"], got["bytes"]) == (64, 48, len(PNG)) and len(got["v"]) == 12
    image = web.get(f"/api/agent-icons/codex/light?v={got['v']}")
    assert image.status_code == 200 and image.content == PNG
    h = image.headers
    assert h["content-type"] == "image/png"
    assert h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "private, max-age=31536000, immutable"
    assert h["content-security-policy"] == "default-src 'none'; sandbox"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert "content-disposition" not in h or "attachment" not in h["content-disposition"]
    # a request that doesn't carry the current key (an old URL, or none) is never cached for long
    for url in ("/api/agent-icons/codex/light", "/api/agent-icons/codex/light?v=000000000000"):
        stale = web.get(url)
        assert stale.status_code == 200 and stale.content == PNG
        assert stale.headers["cache-control"] == "private, no-cache"
    put(web, broker, "codex", "dark", WEBP, "image/webp")
    dark = web.get("/api/agent-icons/codex/dark")
    assert dark.headers["content-type"] == "image/webp" and dark.content == WEBP


def test_a_new_upload_changes_the_cache_key_and_the_same_file_does_not(
    broker: InProcBroker, web: httpx.Client
) -> None:
    first = put(web, broker, "cursor", "light", PNG, "image/png").json()["v"]
    same = put(web, broker, "cursor", "light", PNG, "image/png").json()["v"]
    other = put(web, broker, "cursor", "light", PNG_DARK, "image/png").json()["v"]
    assert first == same and other != first
    assert web.get(f"/api/agent-icons/cursor/light?v={other}").content == PNG_DARK
    assert web.get(f"/api/agent-icons/cursor/light?v={other}").headers["cache-control"].endswith("immutable")
    assert web.get(f"/api/agent-icons/cursor/light?v={first}").headers["cache-control"] == "private, no-cache"


def test_the_list_says_what_is_stored_and_what_may_be_uploaded(
    broker: InProcBroker, web: httpx.Client
) -> None:
    empty = listing(web)
    assert empty["icons"] == [] and empty["can_change"] is True
    assert empty["harnesses"] == ["claude", "codex", "cursor", "devin", "opencode"]
    assert empty["limits"] == {"bytes": 256 * 1024, "side": 512, "types": ["image/png", "image/webp"]}
    put(web, broker, "devin", "dark", PNG, "image/png")
    put(web, broker, "claude", "light", WEBP, "image/webp")
    icons = listing(web)["icons"]
    assert [(i["harness"], i["scheme"], i["type"]) for i in icons] == [
        ("claude", "light", "image/webp"),
        ("devin", "dark", "image/png"),
    ]
    assert icons[1]["width"] == 64 and icons[1]["bytes"] == len(PNG) and len(icons[1]["v"]) == 12
    assert "data" not in icons[1]


def test_an_admin_can_upload_and_reset_opencode_s_avatar(broker: InProcBroker, web: httpx.Client) -> None:
    """OpenCode (#518) takes a light and a dark upload like the other four: each is served back, listed
    after the others, written into the page's stylesheet for `.avatar.h-opencode` (which hides the built-in
    glyph), and a reset removes both and the stylesheet's rules. Fails with a 404 on the PUT if
    `opencode` is not in the harness list, and on the schema's CHECK if the migration is missing."""
    light = put(web, broker, "opencode", "light", PNG, "image/png")
    assert light.status_code == 200 and light.json()["harness"] == "opencode"
    dark = put(web, broker, "opencode", "dark", PNG_DARK, "image/png")
    assert web.get(f"/api/agent-icons/opencode/dark?v={dark.json()['v']}").content == PNG_DARK
    assert "opencode" in listing(web)["harnesses"]
    assert stored(web) == [("opencode", "light"), ("opencode", "dark")]
    css = web.get("/api/agent-icons.css").text
    assert ".avatar.h-opencode > .ic, .avatar.h-opencode > .av-word { display: none; }" in css
    assert f"/api/agent-icons/opencode/light?v={light.json()['v']}" in css
    assert web.delete("/api/agent-icons/opencode", headers=broker.write_headers()).json() == {"removed": 2}
    assert stored(web) == [] and "opencode" not in web.get("/api/agent-icons.css").text
    assert web.get("/api/agent-icons/opencode/light").status_code == 404


def test_the_stylesheet_follows_the_uploads_and_a_reset(broker: InProcBroker, web: httpx.Client) -> None:
    """The page's own stylesheet for the icons is empty until one is uploaded, names each image by its
    cache key, and goes back to empty when the harness is reset. It is never cached without asking."""
    css = web.get("/api/agent-icons.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert css.headers["cache-control"] == "no-cache" and "url(" not in css.text
    v = put(web, broker, "codex", "light", PNG, "image/png").json()["v"]
    text = web.get("/api/agent-icons.css").text
    assert f"url('/api/agent-icons/codex/light?v={v}')" in text and ".avatar.h-codex" in text
    assert web.delete("/api/agent-icons/codex", headers=broker.write_headers()).json() == {"removed": 1}
    assert "url(" not in web.get("/api/agent-icons.css").text


def test_a_reset_returns_the_built_in_avatar_for_both_schemes(
    broker: InProcBroker, web: httpx.Client
) -> None:
    put(web, broker, "codex", "light", PNG, "image/png")
    put(web, broker, "codex", "dark", PNG_DARK, "image/png")
    put(web, broker, "devin", "light", PNG, "image/png")
    assert web.delete("/api/agent-icons/codex", headers=broker.write_headers()).json() == {"removed": 2}
    assert stored(web) == [("devin", "light")]
    assert web.get("/api/agent-icons/codex/light").status_code == 404
    assert web.delete("/api/agent-icons/codex", headers=broker.write_headers()).json() == {"removed": 0}


def test_every_change_is_an_event_with_who_and_how_big_and_never_the_picture(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """The log says who set which icon and when, with the size and the start of its hash, so an admin
    can see what changed; the image itself is not in it. Fails if a change leaves no event or the event
    holds the bytes."""
    got = put(web, broker, "claude", "dark", PNG, "image/png").json()
    web.delete("/api/agent-icons/claude", headers=broker.write_headers())
    events = icon_events(broker)
    assert [e["what"] for e in events] == ["set", "reset"]
    assert events[0] == {
        "what": "set",
        "harness": "claude",
        "scheme": "dark",
        "by": "alice",
        "type": "image/png",
        "bytes": len(PNG),
        "sha": got["v"],
    }
    assert events[1] == {"what": "reset", "harness": "claude", "by": "alice", "removed": 1}
    assert PNG.hex()[:40] not in json.dumps(events).lower()


def test_an_unknown_harness_or_scheme_is_not_found(broker: InProcBroker, web: httpx.Client) -> None:
    for harness, scheme in (("test", "light"), ("codex", "sepia"), ("unknown", "dark"), ("CODEX", "light")):
        assert put(web, broker, harness, scheme, PNG, "image/png").status_code == 404
        assert web.get(f"/api/agent-icons/{harness}/{scheme}").status_code == 404
    assert web.delete("/api/agent-icons/test", headers=broker.write_headers()).status_code == 404
    assert stored(web) == [] and icon_events(broker) == []


REFUSED = [
    pytest.param(pictures.SVG, "image/svg+xml", 415, "svg", ("SVG", "script"), id="svg-called-svg"),
    pytest.param(pictures.SVG, "image/png", 415, "svg", ("SVG",), id="svg-called-png"),
    pytest.param(PNG, "image/webp", 400, "mismatch", ("PNG", "image/webp"), id="png-called-webp"),
    pytest.param(WEBP, "image/png", 400, "mismatch", ("WebP", "image/png"), id="webp-called-png"),
    pytest.param(PNG, "image/jpeg", 415, "type", ("PNG", "WebP"), id="other-type"),
    pytest.param(PNG, None, 415, "type", ("PNG", "WebP"), id="no-type"),
    pytest.param(b"", "image/png", 400, "empty", ("empty",), id="empty"),
    pytest.param(b"not a picture at all", "image/png", 415, "not_an_image", ("PNG", "WebP"), id="text"),
    pytest.param(
        pictures.png_with_size(513, 100), "image/png", 400, "dimensions", ("513", "512"), id="513-wide"
    ),
    pytest.param(PNG[:-9], "image/png", 400, "bad_png", ("PNG",), id="cut-short"),
    pytest.param(
        PNG + b"<script>alert(1)</script>", "image/png", 400, "bad_png", ("PNG",), id="markup-appended"
    ),
    pytest.param(
        pictures.png(64, 64, pad=300_000), "image/png", 413, "too_large", ("256 KB",), id="over-256-kb"
    ),
]


@pytest.mark.parametrize(("data", "ctype", "status", "code", "words"), REFUSED)
def test_a_refused_file_says_why_and_changes_nothing(
    broker: InProcBroker,
    web: httpx.Client,
    data: bytes,
    ctype: str | None,
    status: int,
    code: str,
    words: tuple[str, ...],
) -> None:
    """SVG, a file labelled as something else, a wrong type, a damaged or oversized picture are each
    refused with their own message, and the icon that was stored before is still the one served. Fails
    if any reaches the store, or the answer is a bare status."""
    before = put(web, broker, "codex", "light", PNG_DARK, "image/png").json()["v"]
    r = put(web, broker, "codex", "light", data, ctype)
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"] == code and all(w in body["message"] for w in words), body
    assert stored(web) == [("codex", "light")] and listing(web)["icons"][0]["v"] == before
    assert web.get("/api/agent-icons/codex/light").content == PNG_DARK
    assert [e["what"] for e in icon_events(broker)] == ["set"]  # the setup's own, and nothing after


def test_a_body_with_no_length_is_cut_off_at_the_cap_while_it_is_read(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """A chunked upload has no Content-Length to check first: the broker stops reading at the cap rather
    than hold what it is sent. Fails if a streamed body is read whole."""

    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b"\x00" * 16_384  # 640 KB in all

    headers = {"Origin": broker.origin, "X-Switchboard": "1", "Content-Type": "image/png"}
    r = web.put("/api/agent-icons/codex/light", content=chunks(), headers=headers)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert stored(web) == []


class FakeRequest:
    """A request whose body counts how much of it was read."""

    def __init__(self, declared: str | None, chunks: list[bytes]) -> None:
        self.headers = {} if declared is None else {"content-length": declared}
        self.chunks = chunks
        self.read = 0

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += len(chunk)
            yield chunk


def test_the_body_reader_never_reads_past_the_cap(
    broker: InProcBroker, web: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What holds the upload to 256 KB in memory: a declared length over the cap is refused without reading
    a byte, a body that doesn't declare one is cut off as the chunk that crosses the cap arrives (not
    after the whole of it), and the upload route asks for exactly that cap. Fails if the route reads the
    body whole before judging it, so a long upload is held in memory first."""
    from switchboard.broker import web as web_module

    def read(request: FakeRequest, limit: int) -> bytes | None:
        return asyncio.run(web_module._capped_body(request, limit))  # type: ignore[arg-type]

    declared = FakeRequest("10000000", [b"x" * 10])
    assert read(declared, 1000) is None and declared.read == 0
    assert read(FakeRequest("many", [b"x"]), 1000) is None
    streamed = FakeRequest(None, [b"x" * 400] * 100)
    assert read(streamed, 1000) is None and streamed.read == 1200  # stopped at the third chunk
    assert read(FakeRequest("5", [b"hello"]), 1000) == b"hello"
    limits: list[int] = []
    real = web_module._capped_body

    async def spy(request: Any, limit: int) -> bytes | None:
        limits.append(limit)
        return await real(request, limit)

    monkeypatch.setattr(web_module, "_capped_body", spy)
    assert put(web, broker, "codex", "light", PNG, "image/png").status_code == 200
    assert limits == [256 * 1024]


def test_the_largest_file_the_cap_allows_is_accepted(broker: InProcBroker, web: httpx.Client) -> None:
    exact = pictures.png(64, 64, pad=256 * 1024 - len(pictures.png(64, 64, pad=1)) + 1)
    assert len(exact) == 256 * 1024
    assert put(web, broker, "codex", "light", exact, "image/png").status_code == 200
    assert put(web, broker, "codex", "dark", exact + b"\x00", "image/png").status_code == 413


def test_the_stylesheet_uses_one_image_for_both_schemes_until_there_are_two(
    broker: InProcBroker, web: httpx.Client
) -> None:
    v_light = put(web, broker, "devin", "light", PNG, "image/png").json()["v"]
    css = web.get("/api/agent-icons.css").text
    assert css.count(f"/api/agent-icons/devin/light?v={v_light}") >= 3 and "devin/dark" not in css
    v_dark = put(web, broker, "devin", "dark", PNG_DARK, "image/png").json()["v"]
    css = web.get("/api/agent-icons.css").text
    assert f"/api/agent-icons/devin/dark?v={v_dark}" in css


def test_the_page_links_the_stylesheet_and_its_csp_is_the_one_it_had(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """The icons need no change to the policy: the images and the sheet come from the page's own origin
    (``img-src 'self'`` and ``style-src 'self'``). Fails if the page's CSP differs from the app page's
    or the sheet isn't linked after the main one (so it would lose to it)."""
    page = web.get("/")
    assert page.headers["content-security-policy"] == app_csp(broker.state.web_origin)
    link = '<link rel="stylesheet" href="/api/agent-icons.css" id="agent-icons-css">'
    assert link in page.text and page.text.index("/static/style.css") < page.text.index(link)
    put(web, broker, "codex", "light", PNG, "image/png")
    sheet = web.get("/api/agent-icons.css")
    assert "img-src 'self'" in sheet.headers["content-security-policy"]
    assert "unsafe-inline" not in sheet.headers["content-security-policy"]


def test_me_says_whether_this_person_may_change_the_icons(broker: InProcBroker, web: httpx.Client) -> None:
    assert web.get("/api/me").json()["can_set_icons"] is True


def test_a_page_hears_of_a_change_on_its_socket(broker: InProcBroker, web: httpx.Client) -> None:
    """Every open page refetches the stylesheet when an icon changes, so a viewer sees it without
    reloading: an ``icons`` frame follows an upload and a reset, and carries nothing else."""
    sock = ws_connect(broker, cookie_of(web))
    try:
        sock.send(json.dumps({"t": "hello", "rooms": [], "after": {}}))
        put(web, broker, "codex", "light", PNG, "image/png")
        assert next_frame(sock, "icons") == {"t": "icons"}
        web.delete("/api/agent-icons/codex", headers=broker.write_headers())
        assert next_frame(sock, "icons") == {"t": "icons"}
    finally:
        sock.close()


def next_frame(sock: Any, kind: str) -> dict[str, Any]:
    while True:
        frame = json.loads(sock.recv(timeout=10))
        if frame.get("t") == kind:
            return frame


# ------------------------------------------------------------------ nobody signed in, and not the admin
def test_nobody_signed_out_reads_or_changes_an_icon(broker: InProcBroker, web: httpx.Client) -> None:
    put(web, broker, "codex", "light", PNG, "image/png")
    out = httpx.Client(base_url=broker.base)
    headers = {"Origin": broker.origin, "X-Switchboard": "1"}
    try:
        for path in ("/api/agent-icons", "/api/agent-icons.css", "/api/agent-icons/codex/light"):
            assert out.get(path).status_code == 401, path
        assert out.put("/api/agent-icons/codex/light", content=PNG, headers=headers).status_code == 401
        assert out.delete("/api/agent-icons/codex", headers=headers).status_code == 401
        # a write from a page that isn't ours (no Origin, no header) never reaches the route
        assert web.put("/api/agent-icons/codex/light", content=PNG).status_code == 403
        assert web.delete("/api/agent-icons/codex").status_code == 403
    finally:
        out.close()
    assert stored(web) == [("codex", "light")]


def test_a_person_who_is_not_the_admin_sees_the_icons_and_cannot_change_them(hosted: InProcBroker) -> None:
    """On a hosted broker everyone is served the admin's icons, and only the admin changes them: bob's
    upload and reset are refused with 403 whatever the file is, and nothing is stored or logged. Fails if
    a person can replace the avatars every other viewer sees."""
    admin = set_up(hosted)
    bob = join_as_bob(hosted, admin)

    def write(
        who: Browser, method: str, path: str, body: bytes | None = None, ctype: str = "image/png"
    ) -> Any:
        h = who.headers(True)
        h["Content-Type"] = ctype
        r = who.c.request(method, path, content=body, headers=h)
        who.take(r)
        return r

    try:
        assert admin.get("/api/me").json()["can_set_icons"] is True
        assert bob.get("/api/me").json()["can_set_icons"] is False
        assert write(admin, "PUT", "/api/agent-icons/codex/dark", PNG).status_code == 200
        listed = bob.get("/api/agent-icons").json()
        assert listed["can_change"] is False and [(i["harness"], i["scheme"]) for i in listed["icons"]] == [
            ("codex", "dark")
        ]
        assert bob.get("/api/agent-icons/codex/dark").content == PNG
        assert "codex/dark" in bob.get("/api/agent-icons.css").text
        for method, path, body in (
            ("PUT", "/api/agent-icons/codex/dark", PNG_DARK),
            ("PUT", "/api/agent-icons/cursor/light", PNG),
            ("PUT", "/api/agent-icons/cursor/light", pictures.SVG),  # refused as bob before it is judged
            ("DELETE", "/api/agent-icons/codex", None),
        ):
            r = write(bob, method, path, body)
            assert r.status_code == 403 and r.json()["error"] == "forbidden", (method, path, r.text)
        assert [(i["harness"], i["scheme"]) for i in admin.get("/api/agent-icons").json()["icons"]] == [
            ("codex", "dark")
        ]
        assert admin.get("/api/agent-icons/codex/dark").content == PNG
        evs = hosted.on_loop(lambda: hosted.state.store.recent_events(kinds=["icons"], limit=50))
        assert [e.data["by"] for e in evs] == ["alice"]
    finally:
        bob.close()
        admin.close()
