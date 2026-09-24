"""Optional openWakeWord listener (feature flag). Detection runs fully offline.

In wake_word mode no mic audio reaches Gemini until "hey jarvis" is heard. After that,
audio streams until `active_window_s` passes with no activity (speech in either
direction), then it goes back to sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np


@dataclass
class WakeState:
    """Asleep/awake state machine, clock injectable for tests."""

    active_window_s: float = 15.0
    clock: Callable[[], float] = time.monotonic
    awake: bool = False
    _last_activity: float = field(default=0.0)

    def wake(self) -> None:
        self.awake = True
        self.touch()

    def touch(self) -> None:
        """Record activity (user or model speech) that keeps the window open."""
        self._last_activity = self.clock()

    def check_sleep(self, playing: bool) -> bool:
        """Go back to sleep after a silent window. Returns True if it just fell asleep."""
        if not self.awake:
            return False
        if playing:
            self.touch()
            return False
        if self.clock() - self._last_activity >= self.active_window_s:
            self.awake = False
            return True
        return False


class WakeWordDetector:
    """Wraps openWakeWord's pretrained ONNX model. Feed 16 kHz int16 chunks of 1280 samples."""

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5) -> None:
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([model_name])
        self._model = Model(wakeword_models=[model_name], inference_framework="onnx")
        self._name = model_name
        self.threshold = threshold

    def detect(self, pcm: bytes) -> bool:
        scores = self._model.predict(np.frombuffer(pcm, dtype=np.int16))
        if max(scores.values(), default=0.0) >= self.threshold:
            self._model.reset()  # avoid re-triggering on the same utterance
            return True
        return False
