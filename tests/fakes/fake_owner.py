"""The owner of a hosted broker, in tests (DESIGN.md §31): a browser at the public URL and a
passkey in software.

``Browser`` sends what a browser sends through the platform's proxy: plain http to the broker's
port with the public Host (and, for writes, the public Origin and ``X-Switchboard: 1``), and it
keeps every cookie by hand, since they are Secure and an httpx jar would drop them over plain
http. ``claim`` and ``sign_in`` run the ceremonies as the pages do, with
``fakes.fake_authenticator.SoftAuthenticator``.
"""

from __future__ import annotations

from typing import Any

import httpx

from fakes.fake_authenticator import SoftAuthenticator


class Browser:
    def __init__(self, b: Any, host: str, origin: str):
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{b.port}", timeout=10.0, follow_redirects=False,
                              limits=httpx.Limits(keepalive_expiry=1.0))
        self.host, self.origin = host, origin
        self.cookies: dict[str, str] = {}
        self.set_cookies: list[str] = []  # every Set-Cookie seen, raw

    def close(self) -> None:
        self.c.close()

    def headers(self, write: bool) -> dict[str, str]:
        h = {"Host": self.host}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if write:
            h.update({"Origin": self.origin, "X-Switchboard": "1", "Content-Type": "application/json"})
        return h

    def take(self, r: httpx.Response) -> None:
        for sc in r.headers.get_list("set-cookie"):
            self.set_cookies.append(sc)
            name, _, value = sc.split(";")[0].partition("=")
            if "max-age=0" in sc.lower() or value in ("", '""'):
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def get(self, path: str) -> httpx.Response:
        r = self.c.get(path, headers=self.headers(False))
        self.take(r)
        return r

    def post(self, path: str, body: dict[str, Any] | None = None, **extra: str | None) -> httpx.Response:
        h = self.headers(True)
        for k, v in extra.items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        r = self.c.post(path, json=body if body is not None else {}, headers=h)
        self.take(r)
        return r

    def cookie_attrs(self, name: str) -> list[str]:
        """The attributes of the last Set-Cookie for ``name``, lowercased."""
        for sc in reversed(self.set_cookies):
            if sc.split("=", 1)[0] == name:
                return [p.strip().lower() for p in sc.split(";")[1:]]
        raise AssertionError(f"no Set-Cookie for {name}")


def claim_link(b: Any) -> str:
    return b.paths.test_claim_link.read_text().strip()


def claim(b: Any, br: Browser, auth: SoftAuthenticator, token: str, name: str = "MacBook Pro") -> dict[str, Any]:
    """The whole claim, as the page does it: begin with the link's token, create, finish."""
    r = br.post("/api/setup/begin", {"token": token})
    assert r.status_code == 200, r.text
    cred = auth.register(r.json()["options"])
    r = br.post("/api/setup/finish", {"credential": cred, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def sign_in(br: Browser, auth: SoftAuthenticator, **kw: Any) -> httpx.Response:
    """The passkey ceremony: a new session, or, from a signed-in browser, a fresh passkey check."""
    r = br.post("/api/passkey/begin")
    assert r.status_code == 200, r.text
    return br.post("/api/passkey/finish", {"credential": auth.get(r.json()["options"], **kw)})
