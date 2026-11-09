"""The board document alone gets its approved sandbox exceptions (DESIGN.md §56.8, #657)."""

from __future__ import annotations

import httpx
from conftest import InProcBroker

from switchboard.broker.auth import app_csp, csp

POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data: blob:; worker-src 'self' blob:; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'none'"
)


def test_board_document_has_exactly_the_approved_policy_and_no_credentials(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """A public, inert shell may be framed by this broker only; it never embeds room data or a cookie."""
    response = web.get("/board")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == POLICY
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    assert '<script type="module" src="/board-assets/board.js"></script>' in response.text
    assert "switchboard_session" not in response.text


def test_board_exceptions_do_not_leak_to_other_responses_or_static_assets(
    broker: InProcBroker, web: httpx.Client
) -> None:
    """CSP, framing and CORS stay byte-for-byte unchanged on existing pages and on near-miss paths."""
    assert web.get("/").headers["content-security-policy"] == app_csp(broker.state.web_origin)
    for path in (
        "/healthz",
        "/api/me",
        "/static/app.js",
        "/static/style.css",
        "/static/favicon.svg",
        "/static/vendor/mermaid/mermaid.min.js",
        "/board-not-a-route",
        "/board-assets/missing.js",
        "/board-assets/../app.js",
        "/static/vendor/board/board.js",
    ):
        response = web.get(path)
        assert response.headers["content-security-policy"] == csp(broker.state.web_origin), path
        assert response.headers["x-frame-options"] == "DENY", path
        assert response.headers["cross-origin-resource-policy"] == "same-origin", path
        assert "access-control-allow-origin" not in response.headers, path
    for path in ("/board-assets/board.js", "/board-assets/board.css", "/board-assets/mermaid.min.js"):
        response = web.get(path, headers={"Origin": "null"})
        assert response.status_code == 200, path
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["content-security-policy"] == csp(broker.state.web_origin)
        assert response.headers["x-frame-options"] == "DENY"
        assert "access-control-allow-credentials" not in response.headers
