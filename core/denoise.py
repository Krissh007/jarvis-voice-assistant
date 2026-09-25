"""Background noise suppression for the mic stream (RNNoise, runs locally).

RNNoise is a small neural denoiser from Xiph/Mozilla that works on 10 ms frames at 48 kHz. The
mic runs at 16 kHz, so each chunk is resampled up (soxr, streaming), denoised frame by frame,
and resampled back. Output chunks have exactly the input's length (about 10-20 ms of added
latency), so everything downstream sees the same 80 ms blocks as before.

The DLL comes from the `pyrnnoise` wheel; its Python package is not imported (it pulls in
file-decoding libraries JARVIS doesn't need).
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RNNOISE_RATE = 48000
FRAME = 480  # 10 ms at 48 kHz


def _load_library() -> ctypes.CDLL:
    spec = importlib.util.find_spec("pyrnnoise")
    if spec is None or not spec.submodule_search_locations:
        raise OSError("pyrnnoise is not installed")
    path = Path(list(spec.submodule_search_locations)[0]) / "rnnoise.dll"
    lib = ctypes.CDLL(str(path))
    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                          ctypes.POINTER(ctypes.c_float)]
    lib.rnnoise_process_frame.restype = ctypes.c_float
    lib.rnnoise_get_frame_size.restype = ctypes.c_int
    if lib.rnnoise_get_frame_size() != FRAME:
        raise OSError("unexpected RNNoise frame size")
    return lib


class Denoiser:
    """Streaming 16 kHz int16 -> denoised 16 kHz int16. Not thread-safe; use from one thread."""

    def __init__(self, rate: int = 16000, strength: float = 1.0) -> None:
        import soxr

        self._lib = _load_library()
        self._state = self._lib.rnnoise_create(None)
        self.strength = min(1.0, max(0.0, float(strength)))
        self._up = soxr.ResampleStream(rate, RNNOISE_RATE, 1, dtype="float32")
        self._down = soxr.ResampleStream(RNNOISE_RATE, rate, 1, dtype="float32")
        self._in48 = np.zeros(0, dtype=np.float32)
        self._out = np.zeros(0, dtype=np.float32)
        self.speech_prob = 0.0  # RNNoise's own voice-activity estimate for the last chunk

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if not len(x):
            return pcm
        buf = np.concatenate([self._in48, self._up.resample_chunk(x)])
        n = len(buf) // FRAME * FRAME
        frames, self._in48 = buf[:n].reshape(-1, FRAME).copy(), buf[n:]
        dry = frames.copy()
        probs = []
        ptr_type = ctypes.POINTER(ctypes.c_float)
        for frame in frames:  # rows of a C-contiguous array are contiguous: denoise in place
            ptr = frame.ctypes.data_as(ptr_type)
            probs.append(self._lib.rnnoise_process_frame(self._state, ptr, ptr))
        if probs:
            self.speech_prob = float(max(probs))
        wet = frames if self.strength >= 1.0 else dry + (frames - dry) * self.strength
        self._out = np.concatenate([self._out, self._down.resample_chunk(wet.reshape(-1))])
        if len(self._out) >= len(x):
            y, self._out = self._out[: len(x)], self._out[len(x):]
        else:  # warming up: the resamplers and frame buffer hold a few ms back
            y = np.concatenate([np.zeros(len(x) - len(self._out), dtype=np.float32), self._out])
            self._out = np.zeros(0, dtype=np.float32)
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()

    def close(self) -> None:
        if self._state:
            self._lib.rnnoise_destroy(self._state)
            self._state = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def make_denoiser(config: dict) -> Denoiser | None:
    """The configured denoiser, or None if it's off or can't load (JARVIS then runs without it)."""
    ns = config.get("noise_suppression") or {}
    if not ns.get("enabled"):
        return None
    try:
        return Denoiser(strength=float(ns.get("strength", 1.0)))
    except Exception as exc:
        log.warning("noise suppression unavailable (%s); continuing without it", exc)
        return None


class DenoiseWorker:
    """Runs a Denoiser on its own thread (RNNoise's C code releases the GIL), in order.

    `deliver(clean_pcm)` is called from the worker thread; the runtime hands it to the event loop.
    If the worker falls behind (CPU starved), chunks go through raw rather than piling up delay.
    """

    MAX_BACKLOG = 25  # 2 s of 80 ms chunks

    def __init__(self, denoiser: Denoiser, deliver) -> None:
        import queue
        import threading

        self.denoiser = denoiser
        self._deliver = deliver
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="denoise", daemon=True)
        self._thread.start()
        self.passed_raw = 0

    def submit(self, pcm: bytes) -> None:
        if self._queue.qsize() >= self.MAX_BACKLOG:
            self.passed_raw += 1
            self._deliver(pcm)
            return
        self._queue.put(pcm)

    def _run(self) -> None:
        while True:
            pcm = self._queue.get()
            if pcm is None:
                return
            try:
                clean = self.denoiser.process(pcm)
            except Exception:
                log.exception("denoise failed; sending raw audio")
                clean = pcm
            self._deliver(clean)

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2)
        self.denoiser.close()
