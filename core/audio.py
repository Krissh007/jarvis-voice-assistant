"""Mic capture, playback, mute, push-to-talk, playback-gating for echo.

Formats follow the Live API docs: input is 16-bit PCM mono at 16 kHz,
output is 16-bit PCM mono at 24 kHz, both little-endian.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
MIC_BLOCK = 1_280  # 80 ms at 16 kHz; also the frame size openWakeWord expects


@dataclass
class PlaybackState:
    """Tracks whether the speaker is playing, plus the echo tail after it stops."""

    tail_s: float = 0.4
    clock: Callable[[], float] = time.monotonic
    _playing: bool = False
    _stopped_at: float = field(default=-1e9)

    def set_playing(self, playing: bool) -> None:
        if self._playing and not playing:
            self._stopped_at = self.clock()
        self._playing = playing

    @property
    def playing(self) -> bool:
        return self._playing

    def speaking_or_tail(self) -> bool:
        """True while audio plays and for `tail_s` afterwards (echo may still be in the room)."""
        return self._playing or (self.clock() - self._stopped_at) < self.tail_s


@dataclass
class MicGate:
    """Decides whether a mic chunk may be sent to the model."""

    playback: PlaybackState
    push_to_talk: bool = False
    mute_while_speaking: bool = False
    muted: bool = False
    ptt_held: bool = False

    def allows(self) -> bool:
        if self.muted:
            return False
        if self.push_to_talk and not self.ptt_held:
            return False
        if self.mute_while_speaking and self.playback.speaking_or_tail():
            return False
        return True


class Player:
    """Streams 24 kHz PCM to the default output device; `clear()` drops queued audio for barge-in."""

    def __init__(self, playback: PlaybackState) -> None:
        self.playback = playback
        self._chunks: deque[np.ndarray] = deque()
        self._current: np.ndarray | None = None
        self._pos = 0
        self._lock = threading.Lock()
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.OutputStream(
            samplerate=OUTPUT_RATE, channels=1, dtype="int16", callback=self._callback, blocksize=0
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def enqueue(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        with self._lock:
            self._chunks.append(samples)
            self.playback.set_playing(True)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._current = None
            self._pos = 0
            self.playback.set_playing(False)

    def _callback(self, outdata: np.ndarray, frames: int, _time, _status) -> None:
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            while filled < frames:
                if self._current is None or self._pos >= len(self._current):
                    if not self._chunks:
                        break
                    self._current = self._chunks.popleft()
                    self._pos = 0
                n = min(frames - filled, len(self._current) - self._pos)
                out[filled : filled + n] = self._current[self._pos : self._pos + n]
                filled += n
                self._pos += n
            out[filled:] = 0
            idle = filled < frames and not self._chunks
            if idle and self.playback.playing:
                self.playback.set_playing(False)


class Mic:
    """Captures 16 kHz mono PCM and hands each 80 ms chunk to the event loop."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, on_chunk: Callable[[bytes], None], device: int | None = None
    ) -> None:
        self._loop = loop
        self._on_chunk = on_chunk
        self._device = device
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=INPUT_RATE,
            channels=1,
            dtype="int16",
            blocksize=MIC_BLOCK,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _callback(self, indata: np.ndarray, _frames: int, _time, status) -> None:
        if status:
            log.debug("mic status: %s", status)
        self._loop.call_soon_threadsafe(self._on_chunk, indata[:, 0].tobytes())


def resolve_input_device(spec: int | str | None) -> int | None:
    """config `audio.input_device`: None = Windows default, an index, or part of a device name."""
    if spec is None or isinstance(spec, int):
        return spec
    import sounddevice as sd

    wanted = spec.lower()
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        if d["max_input_channels"] > 0 and api == "MME" and wanted in d["name"].lower():
            return i
    raise SystemExit(f"audio.input_device {spec!r} not found. Run with --mic-test to list devices.")


def chime(freq: float = 880.0, duration_s: float = 0.15, volume: float = 0.25) -> bytes:
    """A short sine tone at the output rate, with a fade to avoid clicks."""
    t = np.arange(int(OUTPUT_RATE * duration_s)) / OUTPUT_RATE
    wave = np.sin(2 * np.pi * freq * t) * np.minimum(1.0, np.minimum(t, t[::-1]) * 40)
    return (wave * volume * 32767).astype(np.int16).tobytes()
