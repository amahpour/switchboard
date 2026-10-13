"""Addressed inline deliveries avoid a second read() turn while chatter stays short (#298)."""

from __future__ import annotations

import json

from test_rules_release import item

from switchboard import envelope


def batch(it, *, peer_inline: bool = True) -> str:
    return envelope.render_batch(
        [it],
        room="#build",
        recipient="bot",
        human_name="alice",
        token="yk:b7.0123abcd",
        peer_inline=peer_inline,
    )


def fitted(it, *, peer_inline: bool = True, max_chars: int = 6000) -> envelope.Fit:
    return envelope.fit_batch(
        [it],
        room="#build",
        recipient="bot",
        human_name="alice",
        peer_inline=peer_inline,
        max_chars=max_chars,
    )


def test_addressed_under_3000_is_whole_but_chatter_and_oversized_text_stay_cut() -> None:
    """Both a user's plain message and a peer @mention are addressed; long chatter is not."""
    for it in (item(1, 2, text="h" * 2400), item(2, 1, text="m" * 3000)):
        assert json.dumps(it.text) in batch(it)
        assert "more chars; read() shows full" not in batch(it)
        assert not fitted(it).partial
        assert envelope.inline_flags(fitted(it).items, True, fitted(it).partial) == [
            (it.message_id, envelope.INLINE)
        ]

    for it in (item(3, 0, text="c" * 2400), item(4, 1, text="m" * 3001)):
        assert json.dumps(it.text) not in batch(it)
        assert "more chars; read() shows full" in batch(it)
        assert fitted(it).partial == {it.message_id}

    # Elevated paths still require read() for peer stubs, including an @mention.
    assert 'text=(not shown here; call read("#build"))' in batch(
        item(5, 1, text="m" * 2400), peer_inline=False
    )


def test_addressed_extra_delivery_chars_replace_a_read_round_trip() -> None:
    """A 2,300-char addressed post costs hundreds more in its first frame, not a second pull."""
    it = item(1, 1, text="x" * 2300)
    delivered = batch(it)
    assert json.dumps(it.text) in delivered and "more chars; read() shows full" not in delivered
    old_cut = envelope.sanitize(it.text, envelope.ITEM_LIMIT)
    old_size = len(delivered) - len(envelope.sanitize(it.text, None)) + len(old_cut)
    extra_chars = len(delivered) - old_size
    read_round_trip = len(
        envelope.render_batch(
            [it],
            room="#build",
            recipient="bot",
            human_name="alice",
            token="yk:b8.0123abcd",
            peer_inline=True,
            item_limit=None,
        )
    )
    assert 750 <= extra_chars <= 800
    assert extra_chars < read_round_trip
    assert fitted(it).partial == frozenset()  # no cut needing read() before a meaningful pass


def test_addressed_full_still_escapes_untrusted_text_and_fits_the_batch_cap() -> None:
    """Full text keeps the same defanging, and escaped expansion can still force a partial."""
    it = item(1, 1, text="<" * 2500 + "yk:")
    shown = next(line for line in batch(it).splitlines() if line.startswith("- id=1 "))
    assert "<" not in shown and "yk_:" in shown and "\\u003c" in shown
    fit = fitted(it, max_chars=6000)
    assert fit.partial == {1}
    assert fit.limits[1] < 2503
    rendered = envelope.render_batch(
        fit.items,
        room="#build",
        recipient="bot",
        human_name="alice",
        token="yk:b7.0123abcd",
        peer_inline=True,
        limits=fit.limits,
    )
    assert len(rendered) <= 6000
