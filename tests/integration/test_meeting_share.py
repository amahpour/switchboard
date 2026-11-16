"""A person shows a room's picture in the call (#763, DESIGN.md §52.9).

The broker is the real one and joinly is ``FakeJoinly``, which records the bytes its ``share_image`` was given
and counts the stops. Only a person's ``/meeting share`` starts a share, and it sends bytes, never a link.
Nothing waits on a sleep: a test waits for the event it expects.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import InProcBroker
from fakes.fake_agent import FakeAgent
from fakes.fake_joinly import FakeJoinly, tiny_png
from test_meeting_voice import call_state, command, make_broker, notice_texts, refused, start_call
from test_meetings import wait_until

ZOOM = "https://us02web.zoom.us/j/123456789"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


@pytest.fixture
def joinly() -> Iterator[FakeJoinly]:
    server = FakeJoinly().start()
    yield server
    server.stop()


@pytest.fixture
def broker(tmp_home: Path, joinly: FakeJoinly) -> Iterator[InProcBroker]:
    b = make_broker(tmp_home, joinly)
    yield b
    b.web.close()  # type: ignore[attr-defined]
    b.stop()


def upload(b: InProcBroker, name: str, data: bytes, room: str = "build") -> str:
    """A person's room file, as the page uploads it. Its id."""
    headers = {
        **b.write_headers(),
        "x-switchboard-filename": name,
        "content-type": "application/octet-stream",
    }
    r = b.web.post(f"/api/rooms/{room}/files", content=data, headers=headers)  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


def sharing(b: InProcBroker) -> bool:
    return bool((call_state(b) or {}).get("sharing"))


async def test_a_person_shares_a_room_picture_and_stops_it(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """``/meeting share <name>`` hands joinly the file's bytes (base64, no link), the room is told, and the
    page's frame says ``sharing`` until ``/meeting share stop`` reaches joinly. Fails if joinly gets anything
    but the stored bytes, the bar never learns, or the stop never arrives."""
    png = tiny_png()
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", png)
    assert "share" in command(broker, "/meeting share plan.png")["text"]
    await wait_until(lambda: sharing(broker))
    assert joinly.shared == [png]
    assert any("is sharing plan.png" in t for t in notice_texts(broker))
    assert call_state(broker) == {"state": "live", "platform": "zoom", "voice": False, "sharing": True}

    assert "stop" in command(broker, "/meeting share stop")["text"]
    await wait_until(lambda: not sharing(broker))
    assert joinly.share_stops == 1
    assert "sharing" not in (call_state(broker) or {})
    assert any("stopped sharing" in t for t in notice_texts(broker))


async def test_a_picture_is_found_by_its_id_and_by_a_name_with_spaces(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A file may be named by its id, ``switchboard:file/<id>``, or its name with spaces in it. Fails if
    the words after ``share`` are not put back together, or an id does not find the file."""
    png = tiny_png()
    await start_call(broker, joinly, ZOOM)
    file_id = upload(broker, "release plan.png", png)
    for ref in ("release plan.png", file_id, f"switchboard:file/{file_id}"):
        command(broker, f"/meeting share {ref}")
        await wait_until(lambda: sharing(broker))
        command(broker, "/meeting share stop")
        await wait_until(lambda: not sharing(broker))
    assert joinly.shared == [png, png, png]


async def test_a_jpeg_is_shared_too(broker: InProcBroker, joinly: FakeJoinly) -> None:
    await start_call(broker, joinly, ZOOM)
    upload(broker, "photo.jpg", JPEG)
    command(broker, "/meeting share photo.jpg")
    await wait_until(lambda: joinly.shared == [JPEG])


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("notes.png", b"this is text, whatever the name says"),  # the type is the bytes', not the name's
        ("pic.webp", WEBP),
        ("page.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        ("anim.gif", b"GIF89a" + b"\x00" * 32),
        ("empty.png", b""),
    ],
)
async def test_only_a_png_or_a_jpeg_by_its_bytes_is_shared(
    broker: InProcBroker, joinly: FakeJoinly, name: str, data: bytes
) -> None:
    """The bytes decide: a text file called ``.png`` is refused, and so are WebP, SVG, GIF and an empty
    file, with a sentence and nothing sent to joinly. Fails if a name or a stored type is trusted."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, name, data)
    r = refused(broker, f"/meeting share {name}")
    assert r.status_code == 400 and "PNG or JPEG" in r.text
    assert joinly.share_attempts == 0 and not sharing(broker)


async def test_a_picture_past_the_share_cap_is_refused_unread_by_joinly(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The cap on a share (8 MB) is lower than the room's file limit (25 MB here). Fails if the whole file
    is sent to joinly, which would refuse it itself."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "huge.png", tiny_png() + b"\x00" * (9 * 1024 * 1024))
    r = refused(broker, "/meeting share huge.png")
    assert r.status_code == 400 and "8 MB" in r.text
    assert joinly.share_attempts == 0


async def test_a_file_that_is_not_in_this_room_is_not_shared(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """A person names a file by id; only this room's stored files count, so another room's picture, a file
    that is not there and a made-up id are the same refusal. Fails if an id finds another room's file."""
    assert (
        broker.web.post("/api/rooms", json={"name": "#other"}, headers=broker.write_headers()).status_code
        == 200
    )  # type: ignore[attr-defined]
    await start_call(broker, joinly, ZOOM)
    other_id = upload(broker, "secret.png", tiny_png(), room="other")
    for ref in (other_id, "nothing.png", "0" * 32):
        r = refused(broker, f"/meeting share {ref}")
        assert r.status_code == 400 and "No file named" in r.text, ref
    assert joinly.share_attempts == 0


async def test_share_needs_a_call_a_file_and_the_bot_in_it(broker: InProcBroker, joinly: FakeJoinly) -> None:
    assert "no call" in command(broker, "/meeting share x.png")["text"]
    assert refused(broker, "/meeting share").status_code == 400  # no file named
    assert refused(broker, "/meeting share stop").status_code == 200  # no call: the same answer as the rest
    await start_call(broker, joinly, ZOOM)
    r = refused(broker, "/meeting share stop")
    assert r.status_code == 409 and "not sharing" in r.text
    assert joinly.share_stops == 0


async def test_one_share_at_a_time(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """While a share is starting, and while one is showing, a second is refused: nothing reaches joinly. A
    stop makes room for the next. Fails if two ``share_image`` calls are ever open at once."""
    png = tiny_png()
    await start_call(broker, joinly, ZOOM)
    upload(broker, "a.png", png)
    upload(broker, "b.png", JPEG)
    joinly.hold_share = True
    command(broker, "/meeting share a.png")
    await wait_until(lambda: joinly.share_attempts == 1)
    r = refused(broker, "/meeting share b.png")
    assert r.status_code == 409 and "already sharing" in r.text
    joinly.release_share()
    await wait_until(lambda: sharing(broker))
    r = refused(broker, "/meeting share b.png")
    assert r.status_code == 409 and "already sharing" in r.text
    command(broker, "/meeting share stop")
    await wait_until(lambda: not sharing(broker))
    command(broker, "/meeting share b.png")
    await wait_until(lambda: sharing(broker))
    assert joinly.shared == [png, JPEG] and joinly.share_attempts == 2


async def test_zoom_refusing_the_share_becomes_a_plain_notice(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """When the host has not let participants share, Zoom says so and the room is told what to change, in
    words of ours. joinly's sentence is never repeated, and the bar does not say sharing. Fails if the
    notice is a generic failure, or the bar says sharing for a share that never began."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    joinly.share_error = "Host disabled attendee screen sharing."
    command(broker, "/meeting share plan.png")
    await wait_until(
        lambda: any("host has not allowed participants to share" in t for t in notice_texts(broker))
    )
    assert any("Security" in t for t in notice_texts(broker))
    assert not any("Host disabled attendee" in t for t in notice_texts(broker))
    assert not sharing(broker)
    # the host allows it, and the next try works
    joinly.share_error = None
    command(broker, "/meeting share plan.png")
    await wait_until(lambda: sharing(broker))


async def test_another_failure_is_one_plain_sentence(broker: InProcBroker, joinly: FakeJoinly) -> None:
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    joinly.share_error = "Zoom did not start the share."
    command(broker, "/meeting share plan.png")
    await wait_until(lambda: any("Couldn't share the picture" in t for t in notice_texts(broker)))
    assert not sharing(broker)


async def test_a_joinly_without_the_patch_is_told_so(
    tmp_home: Path,
) -> None:
    """Stock joinly 0.5.4 has no ``share_image``. The room is told the patch is missing and nothing else is
    tried. Fails if the call is sent to a tool joinly does not have and the person sees a generic error."""
    stock = FakeJoinly()
    stock.share_tools = False
    stock.start()
    b = make_broker(tmp_home, stock)
    try:
        await start_call(b, stock, ZOOM)
        upload(b, "plan.png", tiny_png())
        command(b, "/meeting share plan.png")
        await wait_until(lambda: any("joinly patch" in t for t in notice_texts(b)))
        assert not sharing(b)
    finally:
        b.web.close()  # type: ignore[attr-defined]
        b.stop()
        stock.stop()


async def test_leaving_the_call_stops_the_share(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """``/meeting leave`` ends a share that is showing: joinly is asked to stop it before the bot leaves.
    Fails if a share outlives the bot's leave."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    command(broker, "/meeting share plan.png")
    await wait_until(lambda: sharing(broker))
    command(broker, "/meeting leave")
    await wait_until(lambda: call_state(broker) is None)
    assert joinly.share_stops == 1 and joinly.leaves == 1


async def test_a_call_that_ends_leaves_no_share_behind(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """The next call in the room starts with no share: it is a fact of one call's session. Fails if the
    bar says sharing in a new call."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    command(broker, "/meeting share plan.png")
    await wait_until(lambda: sharing(broker))
    command(broker, "/meeting leave")
    await wait_until(lambda: call_state(broker) is None)
    await start_call(broker, joinly, ZOOM)
    assert not sharing(broker)
    r = refused(broker, "/meeting share stop")
    assert r.status_code == 409


async def test_an_agent_never_starts_a_share(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """Only a person's slash command shares. An agent's ``/meeting share`` is stored as text, and an
    ``@call`` line is only ever spoken, with voice on or off. Fails if either reaches joinly's
    ``share_image``."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    async with FakeAgent(broker.home, "k1") as agent:
        await agent.join("#build", "claude-1")
        said = await agent.say("#build", "/meeting share plan.png")
        assert said.get("posted_id")
        await agent.say("#build", "@call /meeting share plan.png")
        command(broker, "/meeting voice on")
        await agent.say("#build", "@call please share plan.png on screen")
        await wait_until(lambda: len(joinly.spoken) == 1)
    assert joinly.share_attempts == 0 and joinly.shared == []
    assert not sharing(broker)


async def test_a_call_line_cannot_start_a_share(broker: InProcBroker, joinly: FakeJoinly) -> None:
    """What someone says in the call becomes a call line in the room; "share plan.png" there is only text.
    Fails if a call line reaches the share."""
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", tiny_png())
    joinly.push(
        [
            {
                "text": "/meeting share plan.png",
                "start": 1.0,
                "end": 2.0,
                "speaker": "Bob",
                "role": "participant",
            }
        ]
    )
    await wait_until(lambda: any("share plan.png" in m["text"] for m in _lines(broker)))
    assert joinly.share_attempts == 0


def _lines(b: InProcBroker) -> list[dict[str, Any]]:
    from test_meetings import meeting_lines

    return meeting_lines(b)


async def test_what_joinly_receives_is_bytes_in_base64_and_never_a_link(
    broker: InProcBroker, joinly: FakeJoinly
) -> None:
    """The one argument is the picture's base64. Fails if the broker sends a URL, such as a download link,
    which would make the bot's browser fetch it."""
    png = tiny_png()
    await start_call(broker, joinly, ZOOM)
    upload(broker, "plan.png", png)
    command(broker, "/meeting share plan.png")
    await wait_until(lambda: joinly.shared == [png])
    assert joinly.share_raw == [base64.b64encode(png).decode()]
    assert "://" not in joinly.share_raw[0]
