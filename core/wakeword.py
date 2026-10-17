"""Optional openWakeWord listener (feature flag). Detection runs fully offline.

In wake_word mode no mic audio reaches Gemini until "hey jarvis" is heard. After that,
audio streams until `active_window_s` passes with no activity (speech in either
direction), then it goes back to sleep.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


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
VERIFIER_GATE = 0.05  # stock score above which your voice model's score counts as it is
STRICT_MARGIN = 0.10  # below that, the stock model barely stirred: your voice model must be this much surer
# (wake_word.strict_margin; 0.15 until 17 Oct 2026, when genuine tries scoring 0.61 and 0.64 missed)


def verifier_path(data_dir: Path, model_name: str) -> Path:
    return Path(data_dir) / "wake" / f"{model_name}_verifier.pkl"


class WakeWordDetector:
    """Wraps openWakeWord's pretrained ONNX model. Feed 16 kHz int16 chunks of 1280 samples.

    With a personal verifier (made by `--train-wake` from the user's own voice), the verifier's
    probability is the score. openWakeWord only asks the verifier once the stock model scores 0.05,
    but for this user's accent the stock model scored about 1 in 3 of their own recorded
    "hey jarvis"es near 0, so those never woke JARVIS. Now the verifier is always asked, and must be
    STRICT_MARGIN surer when the stock model barely reacted: that caught 7 of 8 recordings instead of 5,
    with no extra false wakes in about 3 minutes of other speech.
    Both scores are kept, so a miss says which model said no.
    """

    strict_margin = STRICT_MARGIN

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5, gain: float = 1.0,
                 verifier: Path | None = None, strict_margin: float = STRICT_MARGIN) -> None:
        import pickle

        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([model_name])
        self._model = Model(wakeword_models=[model_name], inference_framework="onnx")
        self._verifier = None
        if verifier is not None:
            with open(verifier, "rb") as f:
                self._verifier = pickle.load(f)  # our own file, written by --train-wake
        self._name = model_name
        self.threshold = threshold
        self.gain = gain
        self.strict_margin = strict_margin
        self.personal = verifier is not None
        self.last_base = 0.0  # the stock model's score for the last chunk
        self.last_verifier: float | None = None  # the voice model's, when it was asked
        self._attempt = Attempt()
        self.missed: Attempt | None = None  # the last attempt that didn't wake (for "near miss" hints)

    @classmethod
    def from_config(cls, ww: dict, data_dir: Path, gain: float | None = None) -> "WakeWordDetector":
        """Use the personal verifier when it exists (and isn't turned off), with its own threshold."""
        path = verifier_path(data_dir, ww["model"])
        personal = ww.get("personal_verifier", True) and path.exists()
        threshold = float(ww.get("verifier_threshold", 0.5)) if personal else float(ww["threshold"])
        return cls(ww["model"], threshold, float(ww.get("gain", 1.0) if gain is None else gain),
                   path if personal else None, float(ww.get("strict_margin", STRICT_MARGIN)))

    def score(self, samples: np.ndarray) -> float:
        """Wake score for one chunk. `gain` boosts a quiet voice (wake detection only, never sent anywhere)."""
        base = float(max(self._model.predict(amplify(samples, self.gain)).values(), default=0.0))
        self.last_base, self.last_verifier = base, None
        if self._verifier is None or len(self._model.prediction_buffer[self._name]) < 5:
            return base  # (the first frames after a reset are warm-up; openWakeWord zeroes them too)
        features = self._model.preprocessor.get_features(self._model.model_inputs[self._name])
        self.last_verifier = float(self._verifier.predict_proba(features)[0][-1])
        if base >= VERIFIER_GATE:
            return self.last_verifier
        return max(0.0, self.last_verifier - self.strict_margin)

    def detect(self, pcm: bytes) -> bool:
        return self.update(self.score(np.frombuffer(pcm, dtype=np.int16)))

    def reset(self) -> None:
        self._model.reset()

    def needed(self, attempt: Attempt) -> float:
        """The voice-model score that attempt needed: higher when the stock model barely stirred
        (strict_margin), so a hint never says 0.61 failed against a bar of 0.50."""
        if self.personal and attempt.base < VERIFIER_GATE:
            return self.threshold + self.strict_margin
        return self.threshold

    def update(self, score: float) -> bool:
        """Feed one chunk's score. True = wake. Sets `missed` when an attempt fades out without waking."""
        if score >= self.threshold:
            self._model.reset()  # avoid re-triggering on the same utterance
            woke, self._attempt = self._attempt, Attempt()
            woke.add(score, self.last_base, self.last_verifier)
            log.info("wake word: woke (score %.2f, %s)", score, woke.describe(self.personal))
            return True
        if score >= NEAR_MISS or (self.personal and self.last_base >= FAINT):
            self._attempt.add(score, self.last_base, self.last_verifier)
        elif self._attempt.active:
            self._attempt.quiet += 1
            if self._attempt.quiet >= NEAR_MISS_QUIET_CHUNKS:
                ended, self._attempt = self._attempt, Attempt()
                log.info("wake word: no wake (need %.2f; %s)", self.needed(ended), ended.describe(self.personal))
                close = ended.score >= CLOSE or ended.base >= VERIFIER_GATE if self.personal else                     ended.score >= NEAR_MISS
                if close:
                    self.missed = ended  # close enough to tell the user
        return False


FAINT = 0.02  # the stock model stirring: logged, to diagnose "I had to say it several times"
CLOSE = 0.25  # a miss this close is shown in the console too


@dataclass
class Attempt:
    """One try at the wake word: the peak scores while it lasted."""

    score: float = 0.0  # what's compared with the threshold
    base: float = 0.0  # stock model
    verifier: float | None = None  # your voice model (None: not in use)
    quiet: int = 0

    @property
    def active(self) -> bool:
        return self.score > 0 or self.base > 0

    def add(self, score: float, base: float, verifier: float | None) -> None:
        self.score, self.base, self.quiet = max(self.score, score), max(self.base, base), 0
        if verifier is not None:
            self.verifier = max(self.verifier or 0.0, verifier)

    def describe(self, personal: bool) -> str:
        if not personal:
            return f"stock model {self.base:.2f}"
        return f"stock model {self.base:.2f}, your voice model {self.verifier or 0.0:.2f}"
