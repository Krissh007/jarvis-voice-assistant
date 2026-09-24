"""Gemini Live connection, resumption, reconnect (transport layer, swappable).

Everything the rest of the app knows about Gemini Live goes through `LiveSession`:
send audio/text/activity signals in, get callbacks out via `SessionEvents`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.genai import types

log = logging.getLogger(__name__)

AUDIO_MIME = "audio/pcm;rate=16000"
OUTGOING_MAX = 25  # ~2 s of mic audio; older chunks are dropped while reconnecting


class SessionEvents(Protocol):
    def on_audio(self, pcm: bytes) -> None: ...
    def on_interrupted(self) -> None: ...
    def on_input_transcript(self, text: str) -> None: ...
    def on_output_transcript(self, text: str) -> None: ...
    def on_turn_complete(self) -> None: ...
    def on_status(self, message: str, *, problem: bool = False) -> None: ...


def build_live_config(
    *,
    system_instruction: str,
    voice: str | None,
    push_to_talk: bool,
    resume_handle: str | None,
) -> types.LiveConnectConfig:
    """Session setup: audio out, both transcriptions, compression, resumption, VAD mode."""
    speech = None
    if voice:
        speech = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=system_instruction,
        speech_config=speech,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=push_to_talk),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
    )


@dataclass
class ReconnectPolicy:
    """Exponential backoff with jitter; after `max_failures` within `window_s`, a long back-off."""

    max_failures: int = 3
    window_s: float = 300.0
    long_backoff_s: float = 60.0
    base_delay_s: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _failures: deque[float] = field(default_factory=deque)

    def record_failure(self) -> tuple[float, bool]:
        """Returns (delay_s, give_notice). `give_notice` means: tell the user and back off."""
        now = self.clock()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.max_failures:
            self._failures.clear()
            return self.long_backoff_s, True
        delay = min(8 * self.base_delay_s, self.base_delay_s * 2 ** (len(self._failures) - 1))
        return delay + random.uniform(0, self.base_delay_s / 2), False


class LiveSession:
    """Keeps one Live session alive across GoAway, 10-minute connection limits and errors."""

    def __init__(
        self,
        client: Any,
        model: str,
        config_factory: Callable[[str | None], types.LiveConnectConfig],
        events: SessionEvents,
        policy: ReconnectPolicy | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._config_factory = config_factory
        self._events = events
        self._policy = policy or ReconnectPolicy()
        self._outgoing: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.resume_handle: str | None = None
        self.connected = asyncio.Event()

    # ---- outbound (non-blocking, safe to call from the event loop) ----

    def send_audio(self, pcm: bytes) -> None:
        if not self.connected.is_set():
            return  # don't let stale speech pile up while reconnecting
        if self._outgoing.qsize() >= OUTGOING_MAX:
            self._outgoing.get_nowait()
        self._outgoing.put_nowait(("audio", pcm))

    def send_text(self, text: str) -> None:
        self._outgoing.put_nowait(("text", text))

    def activity_start(self) -> None:
        self._outgoing.put_nowait(("activity_start", None))

    def activity_end(self) -> None:
        self._outgoing.put_nowait(("activity_end", None))

    # ---- lifecycle ----

    async def run(self) -> None:
        """Connect, and reconnect for ever (until cancelled)."""
        while True:
            try:
                await self._connect_once()
                log.info("connection ended cleanly; resuming")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected.clear()
                delay, notice = self._policy.record_failure()
                log.warning("live session error: %s: %s (retry in %.1fs)", type(exc).__name__, exc, delay)
                if notice:
                    self._events.on_status(
                        f"I keep losing the connection to Gemini, so I'm backing off for {delay:.0f} seconds.",
                        problem=True,
                    )
                await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        config = self._config_factory(self.resume_handle)
        async with self._client.aio.live.connect(model=self._model, config=config) as session:
            self.connected.set()
            log.info("connected (resumed=%s)", self.resume_handle is not None)
            sender = asyncio.create_task(self._send_loop(session))
            try:
                await self._receive_loop(session)
            finally:
                self.connected.clear()
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def _send_loop(self, session: Any) -> None:
        while True:
            kind, payload = await self._outgoing.get()
            if kind == "audio":
                await session.send_realtime_input(audio=types.Blob(data=payload, mime_type=AUDIO_MIME))
            elif kind == "text":
                await session.send_realtime_input(text=payload)
            elif kind == "activity_start":
                await session.send_realtime_input(activity_start=types.ActivityStart())
            elif kind == "activity_end":
                await session.send_realtime_input(activity_end=types.ActivityEnd())

    async def _receive_loop(self, session: Any) -> None:
        """Returns on GoAway (reconnect with the resumption handle); raises if the socket dies."""
        while True:
            got_any = False
            async for msg in session.receive():
                got_any = True
                if self._handle_message(msg):
                    return
            if not got_any:
                raise ConnectionError("live connection closed by server")

    def _handle_message(self, msg: types.LiveServerMessage) -> bool:
        """Dispatch one server message. Returns True when the connection should be recycled."""
        update = msg.session_resumption_update
        if update and update.resumable and update.new_handle:
            self.resume_handle = update.new_handle

        if msg.go_away:
            log.info("GoAway received (time left %s); reconnecting", msg.go_away.time_left)
            return True

        if msg.tool_call:
            log.warning("tool call received but no tools are registered yet: %s", msg.tool_call)

        content = msg.server_content
        if content is None:
            return False
        if content.interrupted:
            self._events.on_interrupted()
        if content.model_turn and content.model_turn.parts:
            for part in content.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    self._events.on_audio(part.inline_data.data)
        if content.input_transcription and content.input_transcription.text:
            self._events.on_input_transcript(content.input_transcription.text)
        if content.output_transcription and content.output_transcription.text:
            self._events.on_output_transcript(content.output_transcription.text)
        if content.turn_complete:
            self._events.on_turn_complete()
        return False
