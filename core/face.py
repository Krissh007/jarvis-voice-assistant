"""JARVIS's window (feature flag `window`): an animated orb, live captions and Talk / Mute / Stop.

A small borderless always-on-top panel. The orb's rings spin and its core pulses with your voice
(listening) or JARVIS's voice (speaking); its colours match the tray icon. Click the orb to talk,
drag it anywhere, right-click for options. Tk runs in its own thread and only *reads* the
assistant's state through `status()`; buttons go back through the same callbacks as the tray.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.tray import STATES

log = logging.getLogger(__name__)

BG = "#0a1520"
PANEL = "#0f2230"
TEXT = "#cfe8f5"
DIM = "#6f8b9c"
SIZE = 200  # orb canvas, in 96-dpi pixels
WIDTH = 240
FPS = 30
CAPTION_CHARS = 140
STATE_LABELS = {
    "asleep": 'Asleep · say "hey jarvis"',
    "listening": "Listening…",
    "speaking": "Speaking",
    "muted": "Microphone muted",
}


def blend(a: str, b: str, t: float) -> str:
    """Mix two #rrggbb colours: t=0 gives a, t=1 gives b."""
    t = min(1.0, max(0.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def trim_caption(who: str, text: str, limit: int = CAPTION_CHARS) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) > limit:
        text = "…" + text[-(limit - 1):].lstrip()  # show the newest words of a long reply
    return f"{who}: {text}" if who else text


def clamp_position(x: int, y: int, w: int, h: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Keep the window on screen (e.g. after a monitor was unplugged)."""
    return max(0, min(x, screen_w - w)), max(0, min(y, screen_h - h))


def orb_shapes(state: str, t: float, level: float, size: int = SIZE) -> list[tuple]:
    """What to draw for one frame, as (kind, bbox or coords, options) tuples, back to front.

    Pure function of state, time and a 0..1 voice level, so it is testable without a screen.
    """
    ring, core, _ = STATES[state]
    c, s = size / 2, size / 200
    speed = {"asleep": 12, "listening": 40, "speaking": 90, "muted": 6}[state]
    breathe = 0.5 + 0.5 * math.sin(t * (1.2 if state == "asleep" else 2.5))
    energy = level if state in {"listening", "speaking"} else 0.15 * breathe
    shapes: list[tuple] = []

    def circle(r: float) -> tuple[float, float, float, float]:
        return (c - r * s, c - r * s, c + r * s, c + r * s)

    for i in range(6, 0, -1):  # soft halo, bigger and brighter with the voice
        r = 62 + i * 5 + energy * 8  # stays inside the canvas (radius 100)
        shapes.append(("oval", circle(r), {"fill": blend(BG, ring, (0.05 + 0.10 * energy) * (7 - i) / 6),
                                           "outline": ""}))
    shapes.append(("oval", circle(88), {"fill": "#0d1b26", "outline": ring, "width": max(1, round(4 * s))}))
    for k in range(72):  # fine tick marks
        if k % 2:
            continue
        a = math.radians(k * 5)
        r1, r2 = 80 * s, (76 if k % 6 else 72) * s
        shapes.append(("line", (c + r1 * math.cos(a), c + r1 * math.sin(a), c + r2 * math.cos(a), c + r2 * math.sin(a)),
                       {"fill": blend("#0d1b26", ring, 0.55), "width": 1}))
    spin = (t * speed) % 360
    for k in range(10):  # outer segments, clockwise
        shapes.append(("arc", circle(64), {"start": spin + k * 36, "extent": 22, "style": "arc",
                                           "outline": ring, "width": max(1, round(5 * s))}))
    for k in range(6):  # inner segments, counter-clockwise
        shapes.append(("arc", circle(46), {"start": -spin * 1.6 + k * 60, "extent": 34, "style": "arc",
                                           "outline": blend(ring, core, 0.4), "width": max(1, round(3 * s))}))
    r_core = 20 + energy * 12
    shapes.append(("oval", circle(r_core + 6), {"fill": blend("#0d1b26", ring, 0.35), "outline": ""}))
    shapes.append(("oval", circle(r_core), {"fill": core, "outline": ""}))
    shapes.append(("oval", circle(r_core * 0.45), {"fill": "#ffffff", "outline": ""}))
    if state == "muted":
        shapes.append(("line", (c - 55 * s, c + 55 * s, c + 55 * s, c - 55 * s), {"fill": "#ffffff", "width": round(6 * s)}))
    return shapes


class Face:
    def __init__(self, *, status: Callable[[], dict[str, Any]], on_talk: Callable[[], None],
                 on_stop: Callable[[], None], on_mute: Callable[[], None], on_quit: Callable[[], None],
                 state_path: Path, always_on_top: bool = True, opacity: float = 1.0) -> None:
        self._status = status
        self._on_talk, self._on_stop, self._on_mute, self._on_quit = on_talk, on_stop, on_mute, on_quit
        self._state_path = Path(state_path)
        self._topmost = always_on_top
        self._opacity = opacity
        self._visible = True
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._root = None

    # ---- called from other threads: only set flags, the Tk thread applies them ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="face", daemon=True)
        self._thread.start()

    def show(self) -> None:
        self._visible = True

    def hide(self) -> None:
        self._visible = False

    def stop(self) -> None:
        self._stopping = True
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ---- Tk thread ----

    def _run(self) -> None:
        try:
            self._build()
            self._root.mainloop()
        except Exception:
            log.exception("JARVIS window stopped")
        finally:
            # Tk objects must be freed by the thread that made them, or Tcl aborts at exit.
            import gc

            self._menu = self._canvas = self._state_label = self._caption = self._mute_button = None
            self._topmost_var = None
            self._root = None
            gc.collect()

    def _build(self) -> None:
        import tkinter as tk

        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp on high-DPI screens
        except Exception:
            pass
        root = self._root = tk.Tk()
        root.title("JARVIS")
        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes("-topmost", self._topmost)
        root.attributes("-alpha", self._opacity)
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96)
        k = self._scale
        w = round(WIDTH * k)

        bar = tk.Frame(root, bg=PANEL)
        bar.pack(fill="x")
        title = tk.Label(bar, text="J.A.R.V.I.S.", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 9))
        title.pack(side="left", padx=round(8 * k), pady=round(3 * k))
        hide = tk.Label(bar, text="–", bg=PANEL, fg=DIM, font=("Segoe UI", 11), cursor="hand2")
        hide.pack(side="right", padx=round(8 * k))
        hide.bind("<Button-1>", lambda e: self.hide())

        self._canvas = tk.Canvas(root, width=round(SIZE * k), height=round(SIZE * k), bg=BG,
                                 highlightthickness=0, cursor="hand2")
        self._canvas.pack(padx=round((WIDTH - SIZE) / 2 * k), pady=(round(6 * k), 0))
        self._state_label = tk.Label(root, text="", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 10))
        self._state_label.pack(pady=(round(2 * k), 0))
        self._caption = tk.Label(root, text="", bg=BG, fg=DIM, font=("Segoe UI", 9), wraplength=w - round(24 * k),
                                 justify="center", height=3)
        self._caption.pack(padx=round(10 * k))

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(pady=(round(4 * k), round(10 * k)))
        self._mute_button = None
        for label, action in [("Talk", self._on_talk), ("Mute", self._on_mute), ("Stop", self._on_stop)]:
            b = tk.Label(buttons, text=label, bg=PANEL, fg=TEXT, font=("Segoe UI", 9), cursor="hand2",
                         padx=round(12 * k), pady=round(3 * k))
            b.pack(side="left", padx=round(4 * k))
            b.bind("<Button-1>", lambda e, a=action: a())
            b.bind("<Enter>", lambda e, b=b: b.configure(bg=blend(PANEL, "#00b4ff", 0.35)))
            b.bind("<Leave>", lambda e, b=b: b.configure(bg=PANEL))
            if label == "Mute":
                self._mute_button = b

        self._menu = tk.Menu(root, tearoff=0)
        self._topmost_var = tk.BooleanVar(value=self._topmost)
        self._menu.add_command(label="Talk to JARVIS", command=self._on_talk)
        self._menu.add_checkbutton(label="Always on top", variable=self._topmost_var, command=self._toggle_topmost)
        self._menu.add_command(label="Hide window (reopen from the tray)", command=self.hide)
        self._menu.add_separator()
        self._menu.add_command(label="Quit JARVIS", command=self._on_quit)

        for widget in (bar, title, self._canvas):
            widget.bind("<ButtonPress-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._release)
        for widget in (root, bar, title, self._canvas, self._state_label, self._caption):
            widget.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

        root.update_idletasks()
        x, y = self._initial_position(root.winfo_reqwidth(), root.winfo_reqheight())
        root.geometry(f"+{x}+{y}")
        self._drag_from = None
        self._shown = True
        self._t0 = time.monotonic()
        root.after(1000 // FPS, self._tick)

    def _initial_position(self, w: int, h: int) -> tuple[int, int]:
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        try:
            saved = json.loads(self._state_path.read_text(encoding="utf-8"))
            return clamp_position(int(saved["x"]), int(saved["y"]), w, h, sw, sh)
        except (OSError, ValueError, KeyError, TypeError):
            return clamp_position(sw - w - 24, sh - h - 72, w, h, sw, sh)  # bottom-right, above the taskbar

    def _save_position(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"x": self._root.winfo_x(), "y": self._root.winfo_y()}),
                                        encoding="utf-8")
        except OSError:
            log.exception("couldn't save the window position")

    def _press(self, event) -> None:
        self._drag_from = (event.x_root, event.y_root, self._root.winfo_x(), self._root.winfo_y(), False)

    def _drag(self, event) -> None:
        if not self._drag_from:
            return
        x0, y0, wx, wy, _ = self._drag_from
        dx, dy = event.x_root - x0, event.y_root - y0
        if abs(dx) + abs(dy) > 4:
            self._drag_from = (x0, y0, wx, wy, True)
            self._root.geometry(f"+{wx + dx}+{wy + dy}")

    def _release(self, event) -> None:
        moved = bool(self._drag_from and self._drag_from[4])
        self._drag_from = None
        if moved:
            self._save_position()
        elif event.widget is self._canvas:
            self._on_talk()  # a click (not a drag) on the orb = talk

    def _toggle_topmost(self) -> None:
        self._topmost = bool(self._topmost_var.get())
        self._root.attributes("-topmost", self._topmost)

    def _tick(self) -> None:
        if self._stopping:
            self._root.destroy()
            return
        try:
            if self._visible != self._shown:
                (self._root.deiconify if self._visible else self._root.withdraw)()
                self._shown = self._visible
            if self._shown:
                self._draw(self._status())
        except Exception:
            log.exception("window frame failed")
        self._root.after(1000 // FPS, self._tick)

    def _draw(self, st: dict[str, Any]) -> None:
        state = st.get("state", "asleep")
        level = float(st.get("level", 0.0))
        self._level = getattr(self, "_level", 0.0) * 0.7 + level * 0.3  # smooth the pulse
        cv = self._canvas
        cv.delete("all")
        for kind, coords, opts in orb_shapes(state, time.monotonic() - self._t0, self._level, round(SIZE * self._scale)):
            {"oval": cv.create_oval, "arc": cv.create_arc, "line": cv.create_line}[kind](*coords, **opts)
        label = STATE_LABELS[state]
        if self._state_label.cget("text") != label:
            self._state_label.configure(text=label, fg=STATES[state][0] if state != "asleep" else DIM)
        caption = trim_caption(*st.get("caption", ("", "")))
        if self._caption.cget("text") != caption:
            self._caption.configure(text=caption)
        if self._mute_button is not None:
            text = "Unmute" if state == "muted" else "Mute"
            if self._mute_button.cget("text") != text:
                self._mute_button.configure(text=text)
