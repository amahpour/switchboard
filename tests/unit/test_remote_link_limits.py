"""The broker's end of a link, M8c review fixes (DESIGN.md §27.4.5, §27.16): the flood limit
counts bytes as well as frames, and a request frame is no larger than a local request."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from switchboard.broker.remote import FRAME_BYTES_RATE, FRAME_WINDOW_S, Attempt, LinkClosed, RemoteLink
from switchboard.remote import proto
from switchboard.remote.config import RemoteEntry


def stub_link() -> tuple[RemoteLink, list[str]]:
    posted: list[str] = []
    state = SimpleNamespace(
        hosts=SimpleNamespace(add_remote=lambda name: SimpleNamespace(link_down=lambda: None)),
        store=SimpleNamespace(get_room=lambda name: name),
        service=SimpleNamespace(post_notice=lambda room, text, level="info": posted.append(text)),
    )
    mgr = SimpleNamespace(state=state, now=lambda: 0.0, changed=lambda link: None)
    entry = RemoteEntry(name="fpga-pi", host="", user="", rooms=("#fpga",), transport="exec", home="/tmp/x")
    return RemoteLink(mgr, entry, "hash"), posted  # type: ignore[arg-type]


def test_bytes_count_as_well_as_frames() -> None:
    """1 MiB frames, far under 300 a second, still close the link past 8 MiB a second: each is
    JSON-parsed on the broker's one event loop, where local members are served too."""
    link, _ = stub_link()
    a = Attempt(n=1, link_id="0123456789abcdef")
    mib = 1024 * 1024
    for _ in range(int(FRAME_BYTES_RATE * FRAME_WINDOW_S) // mib):
        link._count(a, mib)
    with pytest.raises(LinkClosed) as ei:
        link._count(a, mib)
    assert (ei.value.state, ei.value.reason) == ("down", "flood")
    assert "MiB a second" in (ei.value.notice or "")
    assert len(a.frames) < 300


def test_small_frames_stay_under_the_byte_budget() -> None:
    link, _ = stub_link()
    a = Attempt(n=1, link_id="0123456789abcdef")
    for _ in range(1400):
        link._count(a, 200)
    assert a.window_bytes == 1400 * 200


async def test_request_frame_over_the_local_line_limit_closes_the_link() -> None:
    link, _ = stub_link()
    a = Attempt(n=1, link_id="0123456789abcdef")
    link.attempt = a
    big = proto.encode(proto.req(1, {"id": 1, "method": "agent.say",
                                     "params": {"cred": "c", "text": "x" * (proto.MAX_LINE + 70_000)}}))
    assert proto.MAX_REQ_FRAME < len(big) <= proto.MAX_FRAME + 1
    reader = asyncio.StreamReader(limit=proto.MAX_FRAME + 1)
    reader.feed_data(big)
    reader.feed_eof()
    with pytest.raises(LinkClosed) as ei:
        await link._frames(a, reader)
    assert (ei.value.state, ei.value.reason) == ("down", "malformed")
    assert "a request over" in (ei.value.notice or "")


async def test_frames_of_an_abandoned_attempt_end_it_as_down() -> None:
    """An abandoned attempt's frames never reach the broker, and its end is no clean end: the
    supervisor must not take it for a link that is still up."""
    link, _ = stub_link()
    a = Attempt(n=1, link_id="0123456789abcdef")
    link.attempt = Attempt(n=2, link_id="fedcba9876543210")
    reader = asyncio.StreamReader()
    reader.feed_data(proto.encode(proto.pong(1)))
    with pytest.raises(LinkClosed) as ei:
        await link._frames(a, reader)
    assert (ei.value.state, ei.value.reason) == ("down", "abandoned")


def test_a_repeating_failure_is_noticed_once_until_up() -> None:
    link, posted = stub_link()
    bad = LinkClosed("down", "malformed", "fpga-pi: a malformed hello from the satellite (unknown_field)")
    for _ in range(5):
        link._closed(bad)
    assert posted == [bad.notice]
    link._closed(LinkClosed("down", "flood", "fpga-pi: link closed: flood"))  # another reason: told
    assert len(posted) == 2
    link._down_noted = None  # what a link-up does
    link._closed(bad)
    assert len(posted) == 3
