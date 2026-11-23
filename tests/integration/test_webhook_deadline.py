"""One webhook send is one deadline and at most one POST (#927, DESIGN.md §62).

The guide promises a post waits ``TIMEOUT_S`` (5 seconds) at most and a person gets at most 12 posts an hour.
Before #927 the timeout was per socket operation (the resolver ran outside it, and every address of a name
started a fresh 5 seconds), and a POST whose response failed was posted again to the next address, uncounted.

The deadline is measured on ``webhook.monotonic``, which these tests replace with the real clock plus an
offset the fake resolver or receiver moves forward, so a slow resolver costs no real time and nothing
sleeps. The receivers are local sockets: one that never answers the TLS handshake (it listens and never
accepts), and ``tests/fakes/tls_hook.py``'s, told to drop or drip its answer.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakes.tls_hook import HOST, TlsHook

from switchboard import notify
from switchboard.broker import webhook

TIMEOUT = 1.0  # the deadline these tests use, so a send that keeps it is quick
SLACK = 0.5  # a timer that fires late on a loaded machine; far less than one more step's timeout


class Skewed:
    """The real monotonic clock plus ``ahead``: what a fake resolver or receiver adds is time it spent."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.ahead


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Skewed:
    c = Skewed()
    monkeypatch.setattr(webhook, "TIMEOUT_S", TIMEOUT)
    monkeypatch.setattr(webhook, "monotonic", c, raising=False)  # raising=False: main has no such hook
    return c


@pytest.fixture
def hook() -> Iterator[TlsHook]:
    h = TlsHook()
    yield h
    h.stop()


@pytest.fixture
def stalled() -> Iterator[int]:
    """A port on 127.0.0.1 that takes connections (the kernel's backlog) and never answers the handshake."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield s.getsockname()[1]
    s.close()


def test_a_slow_resolver_and_a_stalled_first_address_keep_the_one_deadline(
    clock: Skewed, stalled: int
) -> None:
    """The resolver takes most of the deadline and the name has two addresses that both stall: the whole
    send still ends within the deadline, as a timeout. Fails if the resolver's time is not counted or each
    address starts a fresh timeout (before #927: 0.8 + 1 + 1 = 2.8 s)."""

    def slow(host: str, port: int) -> list[str]:
        clock.ahead += 0.8 * TIMEOUT  # the lookup took 0.8 of the deadline
        return ["127.0.0.1", "127.0.0.1"]

    url = f"https://{HOST}:{stalled}/hook"
    t0 = clock()
    with pytest.raises(webhook.SendError) as e:
        webhook.post_blocking(url, b"{}", allow_private=True, resolve=slow)
    took = clock() - t0
    assert e.value.code == "timeout"
    assert took <= TIMEOUT + SLACK, took


def test_a_resolver_that_uses_the_deadline_up_connects_nowhere(clock: Skewed, stalled: int) -> None:
    """A lookup that took the whole deadline leaves no time to connect: the send is a timeout and no
    connection is made. Fails if a connection after the lookup is given a timeout of its own."""

    def slower(host: str, port: int) -> list[str]:
        clock.ahead += TIMEOUT + 0.1
        return ["127.0.0.1"]

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.setblocking(False)
    try:
        url = f"https://{HOST}:{listener.getsockname()[1]}/hook"
        with pytest.raises(webhook.SendError) as e:
            webhook.post_blocking(url, b"{}", allow_private=True, resolve=slower)
        assert e.value.code == "timeout"
        with pytest.raises(BlockingIOError):  # the backlog is empty: nothing connected
            listener.accept()
    finally:
        listener.close()


def test_a_slow_answer_cannot_outlast_the_deadline(clock: Skewed, hook: TlsHook) -> None:
    """The receiver takes past the deadline to answer, then answers in two writes: every read is given only
    the time left, so the read after the deadline is a timeout. Fails if a read waiting from before the
    deadline, or the next one, gets a fresh timeout (the answer then completes, a 200, after the deadline)."""

    def late() -> None:
        clock.ahead += 2 * TIMEOUT

    hook.drip = late
    with pytest.raises(webhook.SendError) as e:
        webhook.post_blocking(hook.url, b"{}", allow_private=True, resolve=hook.resolve, context=hook.context)
    assert e.value.code == "timeout"
    assert len(hook.posts) == 1


def test_an_answer_with_a_body_that_ends_the_connection_is_still_read(hook: TlsHook) -> None:
    """The receiver answers 200 with a body and closes the connection (HTTP/1.0), as many do: the body is read
    and the post went. Guards the deadline's socket against closing before the answer is read, which
    ``http.client`` does to an answer that ends the connection (the post would be a failure that went)."""
    hook.answer = b'{"ok": true}'
    webhook.post_blocking(hook.url, b"{}", allow_private=True, resolve=hook.resolve, context=hook.context)
    assert len(hook.posts) == 1


def test_a_post_whose_response_failed_is_not_posted_again_to_the_next_address(hook: TlsHook) -> None:
    """The request reached the receiver and the connection closed with no answer: the send fails and does
    not try the name's next address, which would be a second POST of one send. Fails if a failure after the
    request went out moves on to the next address (before #927: two POSTs)."""
    hook.drop = True
    with pytest.raises(webhook.SendError) as e:
        webhook.post_blocking(
            hook.url,
            b"{}",
            allow_private=True,
            resolve=lambda host, port: ["127.0.0.1", "127.0.0.1"],
            context=hook.context,
        )
    assert e.value.code == "connect"
    assert len(hook.posts) == 1


def test_a_connect_failure_still_moves_on_to_the_next_address(
    hook: TlsHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing of the request goes out when the first address refuses the connection, so the next address is
    tried and the post goes. Guards the fix against stopping at the first address whatever failed."""
    tried: list[str] = []
    real = webhook._Pinned.connect

    def connect(self: Any) -> None:
        tried.append(self._pinned_ip)
        if len(tried) == 1:
            raise ConnectionRefusedError  # the first address refuses, before any byte of the request
        real(self)

    monkeypatch.setattr(webhook._Pinned, "connect", connect)
    webhook.post_blocking(
        hook.url,
        b"{}",
        allow_private=True,
        resolve=lambda host, port: ["127.0.0.1", "127.0.0.1"],
        context=hook.context,
    )
    assert len(tried) == 2
    assert len(hook.posts) == 1


def test_each_send_counts_once_and_posts_once_against_the_hourly_cap(hook: TlsHook) -> None:
    """Twelve sends whose responses all fail, to a name with two addresses: the receiver gets exactly twelve
    POSTs, and the thirteenth send is refused by the cap without posting. Fails if a failed response is
    followed by an uncounted POST to the next address (before #927: 24 POSTs for 12 counted)."""
    hook.drop = True
    now = [1_000_000.0]
    statuses: list[bool] = []
    state = SimpleNamespace(
        clock=SimpleNamespace(now=lambda: now[0]),
        cfg=SimpleNamespace(notify=SimpleNamespace(allow_private_webhooks=True)),
        store=SimpleNamespace(webhook_status_set=lambda wid, lane, gen, at, ok, err: statuses.append(ok)),
    )
    two = lambda host, port: ["127.0.0.1", "127.0.0.1"]  # noqa: E731
    n = webhook.Notifier(state, resolve=two, context=hook.context)  # type: ignore[arg-type]
    w = {"id": 1, "first_gen": 1, "second_gen": 1, "url": hook.url, "second": None}

    async def run() -> list[str | None]:
        codes = []
        for _ in range(notify.MAX_PER_HOUR + 1):
            codes.append(await n._send(7, w, {"text": "x"}, False))
            now[0] += 1
        return codes

    codes = asyncio.run(run())
    assert codes[: notify.MAX_PER_HOUR] == ["connect"] * notify.MAX_PER_HOUR
    assert codes[-1] == "limit"
    assert len(hook.posts) == notify.MAX_PER_HOUR
