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
UNARMED_GRACE_S = 90  # extra time allowed for reading a long summary before the timeout starts


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


# Words that may surround an accept phrase without changing its meaning ("Yeah, go ahead please").
FILLER_WORDS = {"yeah", "yep", "yup", "ok", "okay", "sure", "please", "jarvis", "now", "right", "alright", "fine"}


# Words that clearly mean "no" or "not yet" ("don't" normalises to "don t").
NEGATIVE_WORDS = {"no", "nope", "nah", "cancel", "stop", "don", "dont", "wait", "not", "never", "later", "hold"}
UNCLEAR_MAX_WORDS = 4   # a reply this short that is neither yes nor no is asked again, not taken as a "no"


def summary_phrases(summary: str) -> list[str]:
    """Echoing the action counts as yes: for "Open www.figma.com ..." that's "open it" / "open that"."""
    words = normalize(summary).split()
    if not words or not words[0].isalpha():
        return []
    return [f"{words[0]} {w}" for w in ("it", "that", "this", "please")]


# "Yes, and don't send it": a yes followed by one thing not to do. Seen live, that cancelled the action
# because of the "don't". It is still a yes when the thing not to do isn't part of the action.
NEGATIONS = {"don", "dont", "not", "never"}
JOINERS = {"and", "but", "just", "do"}
# Words that name the same step: the summary says "press Enter" where the user says "send".
SAME_STEP = {"send": {"send", "enter", "submit"}, "submit": {"send", "enter", "submit"},
             "enter": {"send", "enter", "submit"}, "press": {"press", "enter"}}
MAX_NEGATED_WORDS = 4   # "don't send it yet"


def yes_but_not(text: str, phrases: list[str], summary: str) -> str:
    """For "yes, but don't X": 'yes' if X isn't a step of the action, 'no' if it is, else ''."""
    words = normalize(text).split()
    at = next((i for i, w in enumerate(words) if w in NEGATIONS), -1)
    if at <= 0:
        return ""
    head = words[:at]
    while head and head[-1] in JOINERS:
        head.pop()
    tail = words[at + 1:]
    if tail[:1] == ["t"]:            # "don't" normalises to "don t"
        tail = tail[1:]
    if not head or not tail or len(tail) > MAX_NEGATED_WORDS or any(w in NEGATIVE_WORDS for w in tail):
        return ""
    if not is_accept(" ".join(head), phrases):
        return ""
    verb = tail[0]
    steps = set(normalize(summary).split())
    return "no" if SAME_STEP.get(verb, {verb}) & steps else "yes"


def answer_kind(text: str, phrases: list[str], summary: str = "") -> str:
    """'yes', 'no' or 'unclear' for a reply to a pending action. Only a clear yes confirms; a clear
    no or a longer sentence (a new request) cancels; a short reply that is neither (often a
    mis-transcribed "yeah") is asked again. "Yes, and don't send it" is a yes unless sending is part
    of the action."""
    if is_accept(text, phrases + summary_phrases(summary)):
        return "yes"
    if kind := yes_but_not(text, phrases, summary):
        return kind
    words = normalize(text).split()
    if not words or any(w in NEGATIVE_WORDS for w in words) or len(text.split()) > UNCLEAR_MAX_WORDS:
        return "no"   # (length counts the words as said: normalising splits Hindi words at their vowel marks)
    return "unclear"


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
    unclear: int = 0  # short replies that were neither yes nor no (asked again, up to twice)


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
        if p.armed_at is not None:
            return now - p.armed_at >= self.timeout_s  # 20 s from the end of the spoken summary
        # Still being read out (a long plan can take 30 s+): only a safety cap, in case the model
        # never finishes its turn.
        return now - p.created_at >= self.timeout_s + UNARMED_GRACE_S

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
