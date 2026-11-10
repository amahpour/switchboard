"""Which Kokoro voice an agent speaks with on a call (DESIGN.md §52.6, #713).

Pure. Each agent gets a default from a short list, picked by its screen name, so no setting is needed; a
person can pin another with ``/meeting voice <name> <voice>``. The voice is data about a member, never a
setting an agent reaches: it is written only by that command (human role) and read when a line is made.
The shape is checked where it is set, in the database, and again where it is used; a value that fails
the check is no voice, and the line is spoken in the session's voice.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Kokoro v1.0 names of the shape ``<accent><sex>_<name>``. ``am_adam`` and ``bf_emma`` were heard on a real
# Zoom call (#713, the meeting self-test); the other two are Kokoro's own, and a name that Kokoro does not
# have falls back to the session's voice for that line, so a wrong guess is a missing voice, never a failure.
DEFAULT_VOICES = ("am_adam", "bf_emma", "af_sarah", "bm_lewis")
VOICE_RE = re.compile(r"[a-z0-9_]{1,32}")
# what the patched ``speak_text`` answers when Kokoro has no such voice (deploy/joinly/NOTICE)
NOT_FOUND = "voice was not found"
MISSING_NOTICE = "{name}'s voice {voice} was not found, so the call's own voice is used for its lines."


def valid(voice: object) -> bool:
    """Whether ``voice`` has the shape of a Kokoro voice name: ``a-z``, ``0-9`` and ``_``, 1 to 32 of them."""
    return isinstance(voice, str) and VOICE_RE.fullmatch(voice) is not None


def default_for(name: str) -> str:
    """The default voice for a screen name: the same name always gets the same voice, on every machine."""
    digest = hashlib.sha256(name.lower().encode("utf-8", "replace")).digest()
    return DEFAULT_VOICES[int.from_bytes(digest[:4], "big") % len(DEFAULT_VOICES)]


def voice_of(store: Any, membership_id: int | None) -> str | None:
    """The voice for a line by member ``membership_id`` (its pinned voice, else its default), or None when
    there is no such member: the line is then spoken in the session's voice."""
    member = store.get_membership(membership_id) if membership_id is not None else None
    return effective(member.call_voice, member.screen_name) if member is not None else None


def missing_notice(store: Any, membership_id: int | None, voice: str) -> str:
    """The sentence for the room when Kokoro had no ``voice``. Names the member by its screen name."""
    member = store.get_membership(membership_id) if membership_id is not None else None
    return MISSING_NOTICE.format(name=member.screen_name if member is not None else "an agent", voice=voice)


def effective(pinned: str | None, name: str) -> str:
    """The voice of the member called ``name``: the one a person pinned, else its default."""
    return pinned if valid(pinned) else default_for(name)  # type: ignore[return-value]
