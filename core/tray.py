"""System tray icon (feature flag `tray`): shows JARVIS's state, and Talk / Stop / Mute / Quit.

Left-click = Talk to JARVIS (wakes it without saying "hey jarvis"). pystray runs its own
Windows message loop in a daemon thread; every menu action is handed to the asyncio loop
through the callbacks, so nothing here touches the assistant directly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# ring colour, core colour, label
STATES: dict[str, tuple[str, str, str]] = {
    "starting": ("#3a5568", "#6f8b9c", "starting up"),
    "asleep": ("#4a6b82", "#9fb4c4", 'asleep - say "hey jarvis" or click'),
    "listening": ("#00b4ff", "#e6f7ff", "listening"),
    "speaking": ("#00e0ff", "#ffffff", "speaking"),
    "muted": ("#d93a3a", "#f3c2c2", "microphone muted"),
}


def draw_icon(state: str, size: int = 64) -> Image.Image:
    """An arc-reactor style ring in the state's colours (transparent background)."""
    ring, core, _ = STATES[state]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill="#0d1b26")
    d.ellipse((6 * s, 6 * s, 58 * s, 58 * s), outline=ring, width=round(6 * s))
    for k in range(8):  # segmented inner ring
        start = k * 45 + 8
        d.arc((16 * s, 16 * s, 48 * s, 48 * s), start, start + 29, fill=ring, width=round(4 * s))
    d.ellipse((25 * s, 25 * s, 39 * s, 39 * s), fill=core)
    if state == "muted":
        d.line((14 * s, 50 * s, 50 * s, 14 * s), fill="#ffffff", width=round(5 * s))
    return img


def save_ico(path: Path) -> Path:
    """The desktop-shortcut icon (listening colours), with the sizes Windows asks for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    draw_icon("listening", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return path


class Tray:
    def __init__(self, *, on_talk: Callable[[], None], on_stop: Callable[[], None], on_mute: Callable[[], None],
                 on_quit: Callable[[], None], is_muted: Callable[[], bool],
                 on_show: Callable[[], None] | None = None, on_sleep: Callable[[], None] | None = None) -> None:
        self._callbacks = (on_talk, on_stop, on_mute, on_quit)
        self._on_sleep = on_sleep
        self._on_show = on_show
        self._is_muted = is_muted
        self._images = {name: draw_icon(name) for name in STATES}
        self._icon = None
        self.state = "asleep"

    def start(self) -> None:
        import pystray

        on_talk, on_stop, on_mute, on_quit = self._callbacks
        menu = pystray.Menu(
            pystray.MenuItem("Talk to JARVIS", lambda: on_talk(), default=True),  # left-click
            *([pystray.MenuItem("Go to sleep", lambda: self._on_sleep())] if self._on_sleep else []),
            pystray.MenuItem("Stop (JARVIS and the music)", lambda: on_stop()),
            pystray.MenuItem("Mute microphone", lambda: on_mute(), checked=lambda item: self._is_muted()),
            *([pystray.MenuItem("Show JARVIS window", lambda: self._on_show())] if self._on_show else []),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit JARVIS", lambda: on_quit()),
        )
        self._icon = pystray.Icon("JARVIS", self._images[self.state], self._title(self.state), menu)
        threading.Thread(target=self._run, name="tray", daemon=True).start()

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            log.exception("tray icon stopped")

    @staticmethod
    def _title(state: str) -> str:
        return f"JARVIS: {STATES[state][2]}"

    def set_state(self, state: str) -> None:
        """Called often (every tick); only touches the icon when the state changes."""
        if state == self.state or self._icon is None:
            return
        self.state = state
        try:
            self._icon.icon = self._images[state]
            self._icon.title = self._title(state)
            self._icon.update_menu()  # refresh the Mute tick
        except Exception:
            log.exception("tray update failed")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.exception("tray stop failed")
            self._icon = None
