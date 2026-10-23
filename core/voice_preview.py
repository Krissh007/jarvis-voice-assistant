"""Voice previews: hear one of Gemini Live's voices before choosing it.

A preview is a short Live session in that voice that reads one fixed sentence; nothing else is sent, so
no conversation, memory or screen content leaves the PC. The audio (24 kHz, 16-bit mono, as Live sends
it) is kept in data/voice_previews/<voice>.pcm, so the next preview of that voice plays at once and uses
no quota.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from google.genai import types

from core.settings_store import LIVE_VOICES, VOICE_STYLE  # noqa: F401 (VOICE_STYLE re-exported)

log = logging.getLogger(__name__)

SAMPLE = "Hello, I'm JARVIS. This is how I'll sound when I talk to you."
READER = "You read sample sentences aloud. Say the user's sentence exactly once, word for word, and nothing else."
TIMEOUT_S = 25.0



def find_voice(name: str) -> str | None:
    """The voice's proper name for what the user said ("kore", "Kore."), or None."""
    wanted = "".join(ch for ch in str(name or "").lower() if ch.isalpha())
    return next((v for v in LIVE_VOICES if v.lower() == wanted), None)


class VoicePreviews:
    def __init__(self, client: Any, model: str, cache_dir: Path) -> None:
        self._client = client
        self._model = model
        self.cache_dir = cache_dir
        self._lock = asyncio.Lock()   # one preview session at a time (the free tier allows few)

    def cached(self, voice: str) -> bytes | None:
        path = self.cache_dir / f"{voice}.pcm"
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data or None

    async def sample(self, name: str) -> bytes:
        """The sample sentence in that voice; from the cache when it's there. Raises ValueError for an
        unknown voice and RuntimeError when Gemini gives no audio."""
        voice = find_voice(name)
        if voice is None:
            raise ValueError(f"{name!r} isn't one of Gemini Live's voices.")
        if (data := self.cached(voice)) is not None:
            return data
        async with self._lock:
            if (data := self.cached(voice)) is not None:   # made while this call waited
                return data
            data = await asyncio.wait_for(self._generate(voice), TIMEOUT_S)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / f"{voice}.tmp"
            tmp.write_bytes(data)
            tmp.replace(self.cache_dir / f"{voice}.pcm")
            log.info("voice preview: made a %.1f s sample of %s", len(data) / 48_000, voice)
            return data

    async def _generate(self, voice: str) -> bytes:
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=READER,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        )
        chunks: list[bytes] = []
        async with self._client.aio.live.connect(model=self._model, config=config) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=SAMPLE)]), turn_complete=True)
            async for msg in session.receive():
                content = msg.server_content
                if content and content.model_turn and content.model_turn.parts:
                    for part in content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            chunks.append(part.inline_data.data)
                if content and content.turn_complete:
                    break
        data = b"".join(chunks)
        if not data:
            raise RuntimeError("Gemini sent no audio for the sample.")
        return data
