# joinly v0.5.4, kokoro.py, changed by switchboard (#713): see deploy/joinly/NOTICE for what.
# Copyright (c) 2025 Dominik Brockmann, Johannes Dittrich, Fynn Aurand. MIT License:
#
# Copyright (c) 2025 Dominik Brockmann, Johannes Dittrich, Fynn Aurand
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
import contextvars
import logging
import os
import pathlib
import re
from collections.abc import AsyncIterator
from typing import Self

from joinly.core import TTS
from joinly.settings import get_settings
from joinly.types import AudioFormat
from kokoro_onnx import Kokoro

logger = logging.getLogger(__name__)

_CHUNK_RE = re.compile(r"(?<=[.!?])\s+")

# switchboard patch (#713): the voice for the line being spoken. ``server.speak_text`` sets it for the length
# of one call; the speech tasks it starts copy the context, so each line is made in its own voice. None: the
# session's voice (``tts_args`` at connect). A name Kokoro does not know falls back to the session's voice.
LINE_VOICE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "switchboard_line_voice", default=None
)
# set by ``_tts`` when the line's voice was not known, so the tool's answer can say so
VOICE_FELL_BACK: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "switchboard_voice_fell_back", default=None
)


class KokoroTTS(TTS):
    """Text-to-Speech (TTS) service for converting text to speech."""

    def __init__(self, *, voice: str = "af_bella") -> None:
        """Initialize the TTS service.

        Args:
            voice: The voice to use for TTS (default is "af_bella" for English).
        """
        default_voices = {
            "en": "af_bella",
            "es": "ef_dora",
            "fr": "ff_siwis",
            "it": "if_sara",
        }
        if get_settings().language not in default_voices:
            logger.warning(
                "Unsupported language %s for Kokoro TTS, falling back to English.",
                get_settings().language,
            )
        self._voice = voice or default_voices.get(get_settings().language, default_voices["en"])
        self._model: Kokoro | None = None
        self._sem = asyncio.BoundedSemaphore(1)
        self.audio_format = AudioFormat(sample_rate=24000, byte_depth=4)

    async def __aenter__(self) -> Self:
        """Load the TTS model."""
        cache_dir = pathlib.Path(os.getenv("XDG_CACHE_HOME", "~/.cache")).expanduser() / "kokoro"
        if not cache_dir.exists():
            msg = (
                f"Kokoro TTS cache directory {cache_dir} does not exist. "
                "Make sure to download the model first "
                "(uv run scripts/download_assets.py)."
            )
            raise RuntimeError(msg)

        logger.info("Loading TTS model from %s", cache_dir)
        self._model = await asyncio.to_thread(
            Kokoro,
            model_path=str(cache_dir / "kokoro-v1.0.onnx"),
            voices_path=str(cache_dir / "voices-v1.0.bin"),
        )
        logger.debug("Loaded TTS model")

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Clean up resources."""
        if self._model is not None:
            del self._model
            self._model = None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Convert text to speech and stream the audio data.

        Args:
            text: The text to convert to speech.

        Yields:
            bytes: The audio data for each text segment.
        """
        # further chunk down to speed up response time
        chunks = _CHUNK_RE.split(text)
        for chunk in chunks:
            audio_data = await self._tts(chunk)
            yield audio_data

    async def _tts(self, text: str) -> bytes:
        """Convert text to speech."""
        if self._model is None:
            msg = "Model not initialized"
            raise RuntimeError(msg)

        voice = self._line_voice()
        async with self._sem:
            return await asyncio.to_thread(
                lambda text: self._model.create(text, voice=voice)[0].tobytes(),  # type: ignore[attr-defined]
                text,
            )

    def _line_voice(self) -> str:
        """The voice for this line: the one ``speak_text`` asked for, else the session's."""
        wanted = LINE_VOICE.get()
        if not wanted or wanted == self._voice:
            return self._voice
        try:
            known = wanted in self._model.get_voices()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            known = False
        if known:
            return wanted
        logger.warning("Kokoro TTS has no voice %r, using %r for this line", wanted, self._voice)
        fell = VOICE_FELL_BACK.get()
        if fell is not None:
            fell.append(wanted)
        return self._voice
