"""Music ducking: other apps get quieter while JARVIS is awake, JARVIS's own voice doesn't.

Windows gives every app its own volume ("audio session"). While JARVIS is awake, each other
app's session volume is lowered to `level` x its normal volume; when JARVIS goes back to sleep
the saved volumes are put back. JARVIS's own session (this process) is never touched, and the
PC's master volume isn't either, so you can still hear JARVIS clearly.

`set_music_volume` changes the *normal* volume of the other apps (what "turn the music down"
means) without touching JARVIS. While ducking, the saved volumes are also written to
data/ducking.json, so a crash can't leave your music stuck quiet: the next start restores them.
All COM calls happen on this module's own thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

AUDIO_SESSION_ACTIVE = 1
NEVER_DUCK = {"audiodg.exe"}  # Windows' own audio engine
POLL_S = 1.0  # while ducked, look for apps that started playing since (e.g. the next song's window)


def session_key(pid: int, name: str) -> str:
    return f"{name.lower()}:{pid}"


class DuckPlan:
    """The pure bookkeeping (testable without Windows audio).

    `saved` maps a session key to that app's normal volume (0..1).
    """

    def __init__(self, level: float) -> None:
        self.level = min(1.0, max(0.0, level))
        self.saved: dict[str, float] = {}

    def duck(self, sessions: Iterable[tuple[str, float]]) -> dict[str, float]:
        """sessions: (key, current volume). Returns {key: new volume} for sessions not ducked yet."""
        changes = {}
        for key, volume in sessions:
            if key not in self.saved:
                self.saved[key] = volume
                changes[key] = volume * self.level
        return changes

    def restore(self) -> dict[str, float]:
        changes, self.saved = dict(self.saved), {}
        return changes

    def set_base(self, sessions: Iterable[str], volume: float, ducked: bool) -> dict[str, float]:
        """'Turn the music down to 30%': the normal volume becomes `volume`; if ducked, apply the dip on top."""
        volume = min(1.0, max(0.0, volume))
        changes = {}
        for key in sessions:
            if ducked:
                self.saved[key] = volume
                changes[key] = volume * self.level
            else:
                changes[key] = volume
        return changes


class Ducker:
    def __init__(self, level: float = 0.3, state_path: Path | None = None,
                 own_pids: Iterable[int] = ()) -> None:
        self.plan = DuckPlan(level)
        self.state_path = state_path
        self.own_pids = {os.getpid(), *own_pids}
        self._want = False
        self._ducked = False
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ---- called from the asyncio thread ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ducking", daemon=True)
        self._thread.start()

    def set_ducked(self, want: bool) -> None:
        """Called every tick with 'is JARVIS awake?'; only transitions do any work."""
        if want != self._want:
            self._want = want
            self._jobs.put(("state", None, None))

    def set_music_volume(self, percent: float, done: Callable[[str], None] | None = None) -> None:
        self._jobs.put(("base", percent / 100.0, done))

    def stop(self) -> None:
        self._stopping = True
        self._want = False
        self._jobs.put(("state", None, None))
        self._jobs.put(("quit", None, None))
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ---- ducking thread ----

    def _sessions(self, active_only: bool = False) -> list[tuple[str, Any]]:
        """(key, ISimpleAudioVolume) for every app except JARVIS itself and system sounds."""
        from pycaw.pycaw import AudioUtilities

        out = []
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            if proc is None or proc.pid in self.own_pids:
                continue
            if active_only and s.State != AUDIO_SESSION_ACTIVE:
                continue  # silent apps are left alone; the poll catches them if they start playing
            try:
                name = proc.name()
                if name.lower() in NEVER_DUCK:
                    continue
                out.append((session_key(proc.pid, name), s.SimpleAudioVolume))
            except Exception:
                continue
        return out

    def _apply(self, changes: dict[str, float], volumes: dict[str, Any]) -> None:
        for key, level in changes.items():
            vol = volumes.get(key)
            if vol is None:
                continue  # the app has closed
            try:
                vol.SetMasterVolume(float(level), None)
            except Exception:
                log.debug("couldn't set the volume of %s", key)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            if self.plan.saved:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(self.plan.saved), encoding="utf-8")
            elif self.state_path.exists():
                self.state_path.unlink()
        except OSError:
            log.exception("couldn't save the ducking state")

    def _recover(self, volumes: dict[str, Any]) -> None:
        """A previous run crashed while ducked: put those apps' volumes back."""
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._apply({k: float(v) for k, v in saved.items()}, volumes)
            log.info("restored %d app volumes left lowered by a previous run", len(saved))
        except (OSError, ValueError):
            log.exception("couldn't read the ducking state")
        self.state_path.unlink(missing_ok=True)

    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            log.exception("ducking unavailable")
            return
        try:
            self._recover(dict(self._sessions()))
            while True:
                try:
                    kind, value, done = self._jobs.get(timeout=POLL_S)
                except queue.Empty:
                    kind, value, done = "poll", None, None
                if kind == "quit":
                    return
                try:
                    self._handle(kind, value, done)
                except Exception:
                    log.exception("ducking step failed")
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _handle(self, kind: str, value: float | None, done: Callable[[str], None] | None) -> None:
        sessions = self._sessions()
        volumes = dict(sessions)
        if kind == "base":
            self._apply(self.plan.set_base(volumes, value, self._ducked), volumes)
            self._save_state()
            if done:
                done(f"Set the music and other apps to {round(value * 100)}% (my voice is unchanged).")
            return
        if self._want and (kind == "state" or self._ducked):
            current = []
            for key, vol in self._sessions(active_only=True):
                try:
                    current.append((key, float(vol.GetMasterVolume())))
                except Exception:
                    continue
            changes = self.plan.duck(current)
            if changes:
                self._apply(changes, volumes)
                self._save_state()
                log.info("ducked %d app(s) to %d%%", len(changes), round(self.plan.level * 100))
            self._ducked = True
        elif not self._want and self._ducked:
            self._apply(self.plan.restore(), volumes)
            self._ducked = False
            self._save_state()
            log.info("app volumes restored")
