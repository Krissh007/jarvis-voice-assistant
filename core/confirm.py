"""Accept-phrase matching, pending-action timing, and echo-safe utterance collection.

Only the user's own input can confirm: a transcript of *mic* audio heard while the speaker
is silent, typed text in --text mode, or the confirm hotkey. Nothing the model says or
returns from a tool is ever looked at here.
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.audio import PlaybackState

_ids = itertools.count(1)


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


# Words that may surround an accept phrase without changing its meaning ("Yeah, go ahead please").
FILLER_WORDS = {"yeah", "yep", "yup", "ok", "okay", "sure", "please", "jarvis", "now", "right", "alright", "fine"}


def is_accept(text: str, phrases: list[str]) -> bool:
    """True if the utterance contains an accept phrase and every other word is harmless filler.

    'Yes.' and 'Yeah, go ahead' match; 'yes but change it', "don't go ahead" and 'no' do not,
    because 'but', 'change', "don't" and 'no' are neither part of an accept phrase nor filler.
    """
    words = normalize(text).split()
    accepted = [normalize(p).split() for p in phrases]
    allowed = FILLER_WORDS | {w for p in accepted for w in p}
    if not words or any(w not in allowed for w in words):
        return False
    return any(words[i : i + len(p)] == p for p in accepted for i in range(len(words) - len(p) + 1))


@dataclass
class PendingAction:
    tool: str
    args: dict[str, Any]
    summary: str
    created_at: float
    call_id: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    turn_done: bool = False  # the model turn that read the summary has completed
    armed_at: float | None = None  # summary finished playing; confirmations accepted from here


class Confirmer:
    """Owns the single pending action and decides when it arms and times out."""

    def __init__(
        self,
        playback: PlaybackState,
        timeout_s: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.playback = playback
        self.timeout_s = timeout_s
        self.clock = clock
        self.pending: PendingAction | None = None

    def create(self, tool: str, args: dict[str, Any], summary: str, call_id: str | None) -> PendingAction:
        self.pending = PendingAction(tool, dict(args), summary, self.clock(), call_id)
        return self.pending

    def take(self) -> PendingAction | None:
        """Remove and return the pending action."""
        action, self.pending = self.pending, None
        return action

    def on_turn_complete(self) -> None:
        if self.pending:
            self.pending.turn_done = True

    def poll(self) -> bool:
        """Arm when the summary has finished playing. Returns True if the pending action timed out."""
        p = self.pending
        if p is None:
            return False
        now = self.clock()
        if p.armed_at is None and p.turn_done and not self.playback.speaking_or_tail():
            p.armed_at = now
        start = p.armed_at if p.armed_at is not None else p.created_at
        hard_cap = p.created_at + self.timeout_s + 30  # if the model never finishes its turn
        return now - start >= self.timeout_s or now >= hard_cap

    @property
    def armed(self) -> bool:
        return self.pending is not None and self.pending.armed_at is not None


class UtteranceTracker:
    """Joins input-transcription fragments into utterances, dropping anything heard during playback."""

    def __init__(
        self, playback: PlaybackState, idle_s: float = 1.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.playback = playback
        self.idle_s = idle_s
        self.clock = clock
        self._buf = ""
        self._last = 0.0

    def add(self, fragment: str) -> bool:
        """Returns False if the fragment was dropped as possible echo."""
        if self.playback.speaking_or_tail():
            return False
        self._buf += fragment
        self._last = self.clock()
        return True

    def finalize(self, force: bool = True) -> str | None:
        """Return the finished utterance. With force=False, only if the user has gone quiet."""
        if not self._buf.strip():
            return None
        if not force and self.clock() - self._last < self.idle_s:
            return None
        text, self._buf = " ".join(self._buf.split()), ""
        return text
