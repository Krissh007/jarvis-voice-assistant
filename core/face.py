"""JARVIS's window (feature flag `window`): an animated orb, the conversation, a box to type to JARVIS,
and Talk / Sleep / Mute / Stop.

A borderless always-on-top panel, resizable from its corner. The orb's rings spin and its core pulses with your voice
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
FIELD = "#13293a"
PLACEHOLDER = "Type a message to JARVIS…"
MAX_TYPED = 2000  # characters per typed message
SIZE = 200  # orb canvas, in 96-dpi pixels
WIDTH = 240
FPS = 30
CAPTION_CHARS = 140
STATE_LABELS = {
    "starting": "Starting up…",
    "asleep": 'Asleep · say "hey jarvis"',
    "listening": "Listening…",
    "speaking": "Speaking",
    "working": "Working on a task…",
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
    speed = {"starting": 25, "asleep": 12, "listening": 40, "speaking": 90, "working": 60, "muted": 6}[state]
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
    """The window: a normal Windows window (dark title bar, minimise to the taskbar, resize, snap).

    Default 360 x 600; size and position are remembered. The X button quits JARVIS (or hides the
    window to the tray, with window.close_button: hide).
    """

    DEFAULT_W, DEFAULT_H = 360, 600
    MIN_W, MIN_H = 300, 420
    ORB = 170

    def __init__(self, *, status: Callable[[], dict[str, Any]], on_talk: Callable[[], None],
                 on_stop: Callable[[], None], on_mute: Callable[[], None], on_quit: Callable[[], None],
                 state_path: Path, always_on_top: bool = True, opacity: float = 1.0,
                 log_path: Path | None = None, on_sleep: Callable[[], None] | None = None,
                 icon_path: Path | None = None, close_hides: bool = False,
                 on_send: Callable[[str, bool], None] | None = None, speak_typed: bool = True) -> None:
        self._on_send = on_send  # (text, speak the reply aloud)
        self._speak_typed = speak_typed
        self._history: list[str] = []  # sent messages, for the Up key
        self._icon_path = icon_path
        self._close_hides = close_hides
        self._status = status
        self._on_talk, self._on_stop, self._on_mute, self._on_quit = on_talk, on_stop, on_mute, on_quit
        self._on_sleep = on_sleep
        self._log_path = log_path
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

    @property
    def started(self) -> bool:
        return self._thread is not None

    def show(self) -> None:
        self._visible = True

    def bring_to_front(self) -> None:
        self._visible = True
        self._raise = True

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
            import tkinter as tk

            for name, value in list(vars(self).items()):
                if isinstance(value, (tk.Misc, tk.Variable, dict)) and name != "_status":
                    setattr(self, name, None)  # widgets, Tk variables, the buttons dict
            gc.collect()

    def _px(self, v: float) -> int:
        return round(v * self._scale)

    def _build(self) -> None:
        import tkinter as tk

        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp on high-DPI screens
        except Exception:
            pass
        root = self._root = tk.Tk()
        root.title("JARVIS")
        root.configure(bg=BG)
        if self._icon_path is not None and Path(self._icon_path).exists():
            try:
                root.iconbitmap(default=str(self._icon_path))  # title bar and taskbar button
            except tk.TclError:
                log.warning("couldn't load the window icon")
        root.protocol("WM_DELETE_WINDOW", self.hide if self._close_hides else self._on_quit)
        root.attributes("-topmost", self._topmost)
        root.attributes("-alpha", self._opacity)
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96)
        px = self._px

        # bottom: the buttons (packed before the chat so they never get squeezed out)
        bottom = self._buttons_frame = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", side="bottom")
        buttons = tk.Frame(bottom, bg=BG)
        buttons.pack(pady=(px(6), px(8)))
        self._buttons = {}
        actions = [("Talk", self._on_talk), ("Sleep", self._on_sleep), ("Mute", self._on_mute), ("Stop", self._on_stop)]
        for label, action in actions:
            if action is None:
                continue
            b = tk.Label(buttons, text=label, bg=PANEL, fg=TEXT, font=("Segoe UI", 10), cursor="hand2",
                         padx=px(14), pady=px(5))
            b.pack(side="left", padx=px(4))
            b.bind("<Button-1>", lambda e, a=action, b=b: self._click(b, a))
            b.bind("<Enter>", lambda e, b=b: b.configure(bg=blend(PANEL, "#00b4ff", 0.35)))
            b.bind("<Leave>", lambda e, b=b: b.configure(bg=PANEL))
            self._buttons[label] = b

        # typing box, just above the buttons
        self._input_frame = None
        if self._on_send is not None:
            self._build_input(bottom)

        # orb and state
        self._canvas = tk.Canvas(root, width=px(self.ORB), height=px(self.ORB), bg=BG, highlightthickness=0,
                                 cursor="hand2")
        self._canvas.pack(side="top", pady=(px(8), 0))
        self._state_label = tk.Label(root, text="", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 11))
        self._state_label.pack(side="top", pady=(px(2), px(6)))

        # conversation: the whole exchange, scrollable, newest at the bottom
        chat_frame = self._chat_frame = tk.Frame(root, bg=PANEL)
        chat_frame.pack(side="top", fill="both", expand=True, padx=px(10))
        scroll = tk.Scrollbar(chat_frame, orient="vertical", width=px(10))
        scroll.pack(side="right", fill="y")
        chat = self._chat = tk.Text(chat_frame, wrap="word", bg=PANEL, fg=TEXT, relief="flat", bd=0,
                                    font=("Segoe UI", 10), padx=px(8), pady=px(6), cursor="arrow",
                                    yscrollcommand=scroll.set, spacing3=px(4), height=8)
        chat.pack(side="left", fill="both", expand=True)
        scroll.configure(command=chat.yview)
        chat.tag_configure("you", foreground="#7fd4ff", font=("Segoe UI Semibold", 10))
        chat.tag_configure("jarvis", foreground="#00e0ff", font=("Segoe UI Semibold", 10))
        chat.tag_configure("live", foreground=DIM)
        hint = 'Say "hey jarvis", click the orb or Talk' + (", or type below." if self._on_send else ".")
        chat.insert("end", hint, "live")
        chat.configure(state="disabled")
        self._chat_version = -1

        # menu
        self._menu = tk.Menu(root, tearoff=0)
        self._topmost_var = tk.BooleanVar(value=self._topmost)
        self._compact_var = tk.BooleanVar(value=False)
        self._speak_var = tk.BooleanVar(value=self._speak_typed)
        self._menu.add_command(label="Talk to JARVIS", command=self._on_talk)
        if self._on_sleep is not None:
            self._menu.add_command(label="Go to sleep", command=self._on_sleep)
        self._menu.add_checkbutton(label="Always on top", variable=self._topmost_var, command=self._toggle_topmost)
        if self._on_send is not None:
            self._menu.add_checkbutton(label="Speak replies to typed messages", variable=self._speak_var)
        self._menu.add_checkbutton(label="Compact (orb only)", variable=self._compact_var, command=self._toggle_compact)
        self._menu.add_command(label="Minimise to the taskbar", command=lambda: self._root.iconify())
        self._menu.add_command(label="Hide window (reopen from the tray)", command=self.hide)
        if self._log_path is not None:
            self._menu.add_command(label="Open log", command=self._open_log)
        self._menu.add_separator()
        self._menu.add_command(label="Quit JARVIS", command=self._on_quit)

        self._canvas.bind("<Button-1>", lambda e: self._on_talk())  # click the orb = talk
        for widget in (root, self._canvas, self._state_label, chat):
            widget.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

        root.minsize(px(self.MIN_W), px(self.MIN_H))
        x, y, w, h = self._initial_geometry()
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.bind("<Configure>", self._on_configure)
        root.after(50, self._style_title_bar)
        self._save_pending = None
        self._shown = True
        self._t0 = time.monotonic()
        root.after(1000 // FPS, self._tick)

    def _build_input(self, bottom) -> None:
        import tkinter as tk

        px = self._px
        frame = self._input_frame = tk.Frame(self._root, bg=BG)
        frame.pack(side="bottom", fill="x", padx=px(10), pady=(px(8), 0), after=bottom)
        box = tk.Frame(frame, bg=FIELD, highlightthickness=1, highlightbackground=FIELD, highlightcolor="#00b4ff")
        box.pack(fill="x")
        entry = self._entry = tk.Entry(box, bg=FIELD, fg=DIM, insertbackground=TEXT, relief="flat", bd=0,
                                       font=("Segoe UI", 10), disabledbackground=FIELD)
        entry.pack(side="left", fill="x", expand=True, padx=(px(8), px(4)), pady=px(6))
        send = tk.Label(box, text="Send", bg=FIELD, fg="#00e0ff", font=("Segoe UI Semibold", 10), cursor="hand2",
                        padx=px(10))
        send.pack(side="right", fill="y")
        send.bind("<Button-1>", lambda e: self._send())
        send.bind("<Enter>", lambda e: send.configure(bg=blend(FIELD, "#00b4ff", 0.3)))
        send.bind("<Leave>", lambda e: send.configure(bg=FIELD))
        entry.bind("<Return>", lambda e: self._send())
        entry.bind("<KP_Enter>", lambda e: self._send())
        entry.bind("<Escape>", lambda e: self._set_entry(""))
        entry.bind("<Up>", lambda e: self._recall(-1))
        entry.bind("<Down>", lambda e: self._recall(1))
        entry.bind("<FocusIn>", lambda e: self._placeholder(False))
        entry.bind("<FocusOut>", lambda e: self._placeholder(True))
        self._history_pos = 0
        self._placeholder(True)

    def _typed_text(self) -> str:
        return "" if getattr(self, "_showing_placeholder", False) else self._entry.get()

    def _set_entry(self, text: str) -> None:
        self._placeholder(False)
        self._entry.delete(0, "end")
        self._entry.insert(0, text)

    def _placeholder(self, show: bool) -> None:
        entry = self._entry
        if show and not entry.get() and self._root.focus_get() is not entry:
            entry.insert(0, PLACEHOLDER)
            entry.configure(fg=DIM)
            self._showing_placeholder = True
        elif not show and getattr(self, "_showing_placeholder", False):
            entry.delete(0, "end")
            entry.configure(fg=TEXT)
            self._showing_placeholder = False

    def _send(self) -> str:
        text = " ".join(self._typed_text().split())[:MAX_TYPED]
        if text and str(self._entry.cget("state")) == "normal":
            self._set_entry("")
            if not self._history or self._history[-1] != text:
                self._history.append(text)
            self._history_pos = len(self._history)
            self._on_send(text, bool(self._speak_var.get()))
        return "break"

    def _recall(self, step: int) -> str:
        """Up/Down: earlier messages, like a terminal."""
        if self._history:
            self._history_pos = max(0, min(len(self._history), self._history_pos + step))
            self._set_entry(self._history[self._history_pos] if self._history_pos < len(self._history) else "")
            self._entry.icursor("end")
        return "break"

    # ---- geometry: position and size are remembered ----

    def _initial_geometry(self) -> tuple[int, int, int, int]:
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        w, h = self._px(self.DEFAULT_W), min(self._px(self.DEFAULT_H), sh - self._px(80))
        try:
            saved = json.loads(self._state_path.read_text(encoding="utf-8"))
            w = max(self._px(self.MIN_W), int(saved.get("w", w)))
            h = max(self._px(self.MIN_H), int(saved.get("h", h)))
            x, y = clamp_position(int(saved["x"]), int(saved["y"]), w, h, sw, sh)
        except (OSError, ValueError, KeyError, TypeError):
            x, y = clamp_position(sw - w - self._px(24), sh - h - self._px(96), w, h, sw, sh)
        return x, y, w, h

    def _save_geometry(self) -> None:
        self._save_pending = None
        r = self._root
        if r is None or r.state() != "normal" or getattr(self, "_compact_var", None) is not None and self._compact_var.get():
            return  # minimised, maximised or compact: keep the normal size
        try:
            size, x, y = r.geometry().split("+")[0], *r.geometry().split("+")[1:3]
            w, h = size.split("x")
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"x": int(x), "y": int(y), "w": int(w), "h": int(h)}),
                                        encoding="utf-8")
        except (OSError, ValueError):
            log.exception("couldn't save the window position")

    def _on_configure(self, event) -> None:
        if event.widget is self._root:  # moved or resized: save once it settles
            if self._save_pending is not None:
                self._root.after_cancel(self._save_pending)
            self._save_pending = self._root.after(600, self._save_geometry)

    def _style_title_bar(self) -> None:
        """Dark title bar in JARVIS's colours (Windows 11; Windows 10 just gets the dark mode flag)."""
        try:
            import ctypes

            def colorref(hex_color: str) -> int:
                r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
                return r | g << 8 | b << 16

            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            dwm = ctypes.windll.dwmapi
            for attribute, value in [(20, 1), (35, colorref(PANEL)), (36, colorref(TEXT)), (34, colorref(PANEL))]:
                v = ctypes.c_int(value)  # 20 dark mode, 35 caption colour, 36 caption text, 34 border
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            log.debug("title bar styling not available")

    # ---- menu actions ----

    def _click(self, button, action: Callable[[], None]) -> None:
        button.configure(bg=blend(PANEL, "#00b4ff", 0.7))  # visible feedback that the click registered
        self._root.after(180, lambda: button.configure(bg=PANEL))
        action()

    def _open_log(self) -> None:
        import os

        try:
            os.startfile(self._log_path)  # opens in the default text editor
        except OSError:
            log.exception("couldn't open the log")

    def _toggle_topmost(self) -> None:
        self._topmost = bool(self._topmost_var.get())
        self._root.attributes("-topmost", self._topmost)

    def _toggle_compact(self) -> None:
        r = self._root
        if self._compact_var.get():
            self._full_size = (r.winfo_width(), r.winfo_height())
            self._chat_frame.pack_forget()
            if self._input_frame is not None:
                self._input_frame.pack_forget()
            r.geometry(f"{self._px(self.MIN_W)}x{self._px(330)}")
        else:
            if self._input_frame is not None:
                self._input_frame.pack(side="bottom", fill="x", padx=self._px(10), pady=(self._px(8), 0),
                                       after=self._buttons_frame)
            self._chat_frame.pack(side="top", fill="both", expand=True, padx=self._px(10))
            w, h = getattr(self, "_full_size", (self._px(self.DEFAULT_W), self._px(self.DEFAULT_H)))
            r.geometry(f"{w}x{h}")

    # ---- animation ----

    def _tick(self) -> None:
        if self._stopping:
            self._root.destroy()
            return
        try:
            if self._visible != self._shown:
                (self._root.deiconify if self._visible else self._root.withdraw)()
                self._shown = self._visible
            if getattr(self, "_raise", False):
                self._raise = False
                if self._root.state() == "iconic":
                    self._root.deiconify()  # restore from the taskbar
                self._root.lift()
                self._root.attributes("-topmost", True)
                if not self._topmost:
                    self._root.after(400, lambda: self._root.attributes("-topmost", False))
                self._root.focus_force()
            if self._shown and self._root.state() != "iconic":  # nothing to draw while minimised
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
        for kind, coords, opts in orb_shapes(state, time.monotonic() - self._t0, self._level, self._px(self.ORB)):
            {"oval": cv.create_oval, "arc": cv.create_arc, "line": cv.create_line}[kind](*coords, **opts)
        label = STATE_LABELS[state]
        if self._state_label.cget("text") != label:
            self._state_label.configure(text=label, fg=STATES[state][0] if state != "asleep" else DIM)
        entry = getattr(self, "_entry", None)
        if entry is not None:  # nothing to send to until JARVIS is connected
            wanted = "disabled" if state == "starting" else "normal"
            if str(entry.cget("state")) != wanted:
                entry.configure(state=wanted)
        mute = self._buttons.get("Mute")
        if mute is not None:
            text = "Unmute" if state == "muted" else "Mute"
            if mute.cget("text") != text:
                mute.configure(text=text)
        if st.get("version") is not None and st["version"] != self._chat_version:
            self._chat_version = st["version"]
            self._render_chat(st.get("lines") or [])

    def _render_chat(self, lines: list[tuple[str, str]]) -> None:
        chat = self._chat
        at_bottom = chat.yview()[1] > 0.98  # don't yank the view if the user scrolled up to read
        chat.configure(state="normal")
        chat.delete("1.0", "end")
        for i, (who, text) in enumerate(lines):
            if i:
                chat.insert("end", "\n")
            chat.insert("end", f"{who}  ", "jarvis" if who == "JARVIS" else "you")
            chat.insert("end", text)
        chat.configure(state="disabled")
        if at_bottom:
            chat.see("end")
