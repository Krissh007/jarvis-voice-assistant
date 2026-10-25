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
    confirmation_pending: Callable[[], bool] = lambda: False

    def allows(self) -> bool:
        if self.muted:
            return False
        if self.confirmation_pending() and self.playback.speaking_or_tail():
            return False  # §0.3b: while an action awaits confirmation, never send audio that may be echo
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

    def clear(self, reason: str = "") -> None:
        """Stop talking now and drop everything queued. Logged when it cut something off."""
        with self._lock:
            left = sum(len(c) for c in self._chunks)
            if self._current is not None:
                left += max(0, len(self._current) - self._pos)
            self._chunks.clear()
            self._current = None
            self._pos = 0
            self.playback.set_playing(False)
        if left:
            log.info("playback stopped%s: dropped %.1f s of queued speech",
                     f" ({reason})" if reason else "", left / OUTPUT_RATE)

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


_HEADPHONE_WORDS = ("headphone", "headset", "earphone", "earbud", "buds", "airpods", "hands-free", "handsfree")


def is_headphones(output_name: str) -> bool:
    """Guess from the Windows device name whether sound goes to the ears rather than the room."""
    name = output_name.lower()
    return any(word in name for word in _HEADPHONE_WORDS)


def resolve_echo_mode(setting: str | None, output_name: str) -> str:
    """'headphones' (full duplex, barge-in by voice) or 'speakers' (mic ignored while JARVIS talks).

    On laptop speakers JARVIS's own voice reaches the mic about as loud as the user's
    (measured: echo peaks ~5400 vs speech 4000-8000), so no level threshold can separate them.
    """
    if setting in {"headphones", "speakers"}:
        return setting
    return "headphones" if is_headphones(output_name) else "speakers"


def default_output_name() -> str:
    try:
        import sounddevice as sd

        return str(sd.query_devices(kind="output")["name"])
    except Exception:
        return ""


def windows_audio_state() -> tuple[str, frozenset[str]]:
    """(default output name, names of all active devices) as Windows sees them *right now*.
    PortAudio only knows the list from startup, so this asks Core Audio directly.
    Safe to call from a worker thread."""
    import comtypes
    from pycaw.constants import AudioDeviceState
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    try:
        default = str(getattr(AudioUtilities.GetSpeakers(), "FriendlyName", "") or "")
        active = frozenset(str(d.FriendlyName) for d in AudioUtilities.GetAllDevices()
                           if d.state == AudioDeviceState.Active and d.FriendlyName)
        return default, active
    except Exception:
        return "", frozenset()
    finally:
        comtypes.CoUninitialize()


def refresh_portaudio() -> None:
    """Re-read the device list so newly connected/disconnected devices are seen. Close all streams first."""
    import sounddevice as sd

    sd._terminate()
    sd._initialize()


def _inside(name: str) -> str:
    """The device part of a Windows endpoint name: "Headset (OnePlus Nord Buds 3 Pro)" -> "oneplus nord buds 3 pro"."""
    name = name.strip()
    if "(" not in name:
        return ""
    return name[name.index("(") + 1:].rstrip(")").strip().lower()


def is_headset_mic_of(input_name: str, output_name: str) -> bool:
    """True when the mic is the hands-free microphone of the same Bluetooth earbuds JARVIS plays to.
    Listening on it switches them into call mode, and their stereo "Headphones" output goes silent:
    JARVIS talks and nothing is heard. MME cuts names at 31 characters, so the names are compared by
    prefix."""
    i, o = input_name.lower(), output_name.lower()
    if not (i.startswith(("headset", "hands-free", "handsfree")) or "hands-free" in i) or not is_headphones(output_name):
        return False
    a, b = _inside(input_name), _inside(output_name)
    return len(a) >= 6 and len(b) >= 6 and (a.startswith(b) or b.startswith(a))


def mic_beside_earbuds(output_name: str) -> tuple[int | None, str]:
    """For `audio.input_device: null`: Windows' default mic, unless it's the hands-free mic of the earbuds
    JARVIS plays to; then another microphone (the laptop's), so the earbuds keep playing in stereo.
    Returns (device index or None for the default, its name)."""
    import sounddevice as sd

    try:
        default = sd.query_devices(kind="input")
    except Exception:
        return None, ""
    name = str(default["name"])
    if not is_headset_mic_of(name, output_name):
        return None, name
    choices = []
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        dname = str(d["name"])
        if (d["max_input_channels"] > 0 and api == "MME" and "sound mapper" not in dname.lower()
                and not is_headset_mic_of(dname, output_name) and not dname.lower().startswith(("headset", "hands-free"))):
            choices.append((0 if ("microphone" in dname.lower() or "array" in dname.lower()) else 1, i, dname))
    if not choices:
        return None, name
    _, index, dname = min(choices)
    log.warning("audio: Windows' default mic is %s, the earbuds' own hands-free mic: listening on it would "
                "silence their stereo sound, so JARVIS listens on %s instead", name, dname)
    return index, dname


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
