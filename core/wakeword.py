"""Optional openWakeWord listener (feature flag). Detection runs fully offline.

In wake_word mode no mic audio reaches Gemini until "hey jarvis" is heard. After that,
audio streams until `active_window_s` passes with no activity (speech in either
direction), then it goes back to sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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


def amplify(samples: np.ndarray, gain: float) -> np.ndarray:
    """Scale int16 audio, clipping instead of wrapping around."""
    if gain == 1.0:
        return samples
    return np.clip(samples.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


NEAR_MISS = 0.1  # a score this high means "heard something like the wake word"
NEAR_MISS_QUIET_CHUNKS = 8  # ~0.6 s below NEAR_MISS ends one attempt
VERIFIER_GATE = 0.05  # base score at which the personal verifier is consulted


def verifier_path(data_dir: Path, model_name: str) -> Path:
    return Path(data_dir) / "wake" / f"{model_name}_verifier.pkl"


class WakeWordDetector:
    """Wraps openWakeWord's pretrained ONNX model. Feed 16 kHz int16 chunks of 1280 samples.

    With a personal verifier (made by `--train-wake` from the user's own voice), the stock model
    only proposes candidates (score >= VERIFIER_GATE) and the verifier's probability is the score.
    """

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5, gain: float = 1.0,
                 verifier: Path | None = None) -> None:
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([model_name])
        extra = {}
        if verifier is not None:
            extra = {"custom_verifier_models": {model_name: str(verifier)}, "custom_verifier_threshold": VERIFIER_GATE}
        self._model = Model(wakeword_models=[model_name], inference_framework="onnx", **extra)
        self._name = model_name
        self.threshold = threshold
        self.gain = gain
        self.personal = verifier is not None
        self._peak = 0.0
        self._quiet = 0
        self.missed: float | None = None  # peak score of the last attempt that didn't wake

    @classmethod
    def from_config(cls, ww: dict, data_dir: Path, gain: float | None = None) -> "WakeWordDetector":
        """Use the personal verifier when it exists (and isn't turned off), with its own threshold."""
        path = verifier_path(data_dir, ww["model"])
        personal = ww.get("personal_verifier", True) and path.exists()
        threshold = float(ww.get("verifier_threshold", 0.5)) if personal else float(ww["threshold"])
        return cls(ww["model"], threshold, float(ww.get("gain", 1.0) if gain is None else gain),
                   path if personal else None)

    def score(self, samples: np.ndarray) -> float:
        """Wake score for one chunk. `gain` boosts a quiet voice (wake detection only, never sent anywhere)."""
        return float(max(self._model.predict(amplify(samples, self.gain)).values(), default=0.0))

    def detect(self, pcm: bytes) -> bool:
        return self.update(self.score(np.frombuffer(pcm, dtype=np.int16)))

    def update(self, score: float) -> bool:
        """Feed one chunk's score. True = wake. Sets `missed` when an attempt fades out without waking."""
        if score >= self.threshold:
            self._model.reset()  # avoid re-triggering on the same utterance
            self._peak, self._quiet = 0.0, 0
            return True
        if score >= NEAR_MISS:
            self._peak, self._quiet = max(self._peak, score), 0
        elif self._peak:
            self._quiet += 1
            if self._quiet >= NEAR_MISS_QUIET_CHUNKS:
                self.missed, self._peak, self._quiet = self._peak, 0.0, 0
        return False
