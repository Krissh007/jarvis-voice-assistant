"""JARVIS's window for the web UI: a see-through Qt WebEngine window in a child process.

The window has no frame and no background: only what the page draws (the orb, the typing box, the
sections around the orb) is visible, with soft glows blending into the desktop (per-pixel alpha).
Everywhere else the window lets clicks through to whatever is behind it. The page reports its
clickable shapes, and a timer here switches the window between taking clicks and passing them on,
depending on whether the mouse is over one of them.

Qt has to own its process's main thread, and JARVIS's main thread runs the asyncio loop, so the window
lives in its own process (`python -m ui.window ...`) and only loads the local page. A crash in the
browser engine can't take the assistant down that way. The page talks to JARVIS over the WebSocket like
any view. To the window it sends only console lines starting "jarvis:":
- `jarvis:shape:[...]`: its clickable circles and boxes, in page pixels;
- `jarvis:drag`: start moving the window with the mouse (the orb was dragged);
- `jarvis:front`: come to the front (a second launch asked for the window).

The parent side, WindowProcess, starts the child, notices when it's closed, and stops it on quit.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x20, 0x80000
VK_LBUTTON = 0x01


# ---------------------------------------------------------------- helpers (no Qt needed; tested directly)


def _load_state(path: Path | None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}
        return {k: int(v) for k, v in data.items() if k in {"x", "y", "width", "height"}}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(path: Path | None, state: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def parse_shapes(text: str) -> list[dict]:
    """The page's clickable shapes: circles {"c": [x, y], "r": r} and boxes {"x", "y", "w", "h"}. Anything
    malformed is dropped, so a bad message can only make less of the window clickable."""
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    shapes = []
    for s in raw if isinstance(raw, list) else []:
        try:
            if "c" in s:
                shapes.append({"c": (float(s["c"][0]), float(s["c"][1])), "r": float(s["r"])})
            else:
                shapes.append({"x": float(s["x"]), "y": float(s["y"]), "w": float(s["w"]), "h": float(s["h"])})
        except (TypeError, KeyError, IndexError, ValueError):
            continue
    return shapes[:64]


def hit(shapes: list[dict], x: float, y: float) -> bool:
    """Is the point (page pixels) on one of the clickable shapes?"""
    for s in shapes:
        if "c" in s:
            if (x - s["c"][0]) ** 2 + (y - s["c"][1]) ** 2 <= s["r"] ** 2:
                return True
        elif s["x"] <= x <= s["x"] + s["w"] and s["y"] <= y <= s["y"] + s["h"]:
            return True
    return False


def set_click_through(hwnd: int, through: bool) -> None:
    """Let clicks pass through the whole window (or take them again)."""
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    new = (style | WS_EX_TRANSPARENT | WS_EX_LAYERED) if through else (style & ~WS_EX_TRANSPARENT)
    if new != style:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)


# ---------------------------------------------------------------- the child process


def run_window(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui.window")
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="JARVIS")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--width", type=int, default=1180)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--on-top", action="store_true")
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args(argv)
    if not args.url.startswith("http://127.0.0.1:"):
        print("ui.window only opens JARVIS's own local page.", file=sys.stderr)
        return 2
    origin = args.url.split("/#", 1)[0].rstrip("/")

    from core.taskbar import set_process_app_id

    set_process_app_id()  # groups this window with JARVIS's pinned icon

    from PySide6.QtCore import QTimer, QUrl, Qt
    from PySide6.QtGui import QColor, QCursor, QIcon
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication([sys.argv[0]])
    shapes: list[dict] = []
    state = _load_state(args.state)

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 (Qt's name)
            if not message.startswith("jarvis:") or not str(source).startswith(origin):
                return
            command, _, data = message[len("jarvis:"):].partition(":")
            if command == "shape":
                shapes[:] = parse_shapes(data)
            elif command == "drag":
                handle = view.windowHandle()
                if handle is not None:
                    handle.startSystemMove()
            elif command == "front":
                view.showNormal()
                view.raise_()
                view.activateWindow()

    class View(QWebEngineView):
        def moveEvent(self, event):  # noqa: N802
            super().moveEvent(event)
            state.update(x=self.x(), y=self.y())
            save_soon.start(500)

        def closeEvent(self, event):  # noqa: N802
            _save_state(args.state, state)
            super().closeEvent(event)

    profile = QWebEngineProfile(app)  # off the record: nothing is stored on disk
    view = View()
    page = Page(profile, view)
    page.setBackgroundColor(QColor(0, 0, 0, 0))
    view.setPage(page)
    view.setWindowTitle(args.title)
    if args.icon and args.icon.is_file():
        view.setWindowIcon(QIcon(str(args.icon)))
    flags = Qt.FramelessWindowHint | (Qt.WindowStaysOnTopHint if args.on_top else Qt.Widget)
    view.setWindowFlags(flags)
    view.setAttribute(Qt.WA_TranslucentBackground)
    view.setStyleSheet("background: transparent")
    view.setContextMenuPolicy(Qt.NoContextMenu)

    width, height = state.get("width", args.width), state.get("height", args.height)
    screen = app.primaryScreen().availableGeometry()
    x = state.get("x", screen.x() + (screen.width() - width) // 2)        # first time: top centre
    y = state.get("y", screen.y())
    view.setGeometry(x, y, width, height)
    state.update(x=x, y=y, width=width, height=height)
    save_soon = QTimer(view)
    save_soon.setSingleShot(True)
    save_soon.timeout.connect(lambda: _save_state(args.state, state))

    through = {"now": None}

    def follow_mouse() -> None:
        """Take clicks only while the mouse is over the orb, the typing box or a section."""
        pos = view.mapFromGlobal(QCursor.pos())
        held = bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        want = not hit(shapes, pos.x(), pos.y())
        if held and through["now"] is False:
            want = False  # keep a drag or a text selection going
        if want != through["now"]:
            through["now"] = want
            set_click_through(int(view.winId()), want)

    mouse = QTimer(view)
    mouse.timeout.connect(follow_mouse)
    mouse.start(30)

    view.load(QUrl(args.url))
    view.show()
    code = app.exec()
    _save_state(args.state, state)
    return code


# ---------------------------------------------------------------- the parent side


class WindowProcess:
    def __init__(self, url: str, *, state_path: Path, width: int = 1180, height: int = 860, on_top: bool = True,
                 icon: Path | None = None, on_closed: Callable[[], None] | None = None,
                 python: str | None = None, log_path: Path | None = None) -> None:
        self.url = url
        self.state_path = state_path
        self.width = width
        self.height = height
        self.on_top = on_top
        self.icon = icon
        self.on_closed = on_closed
        self.python = python or sys.executable
        self.log_path = log_path  # the window's own errors (Qt WebEngine)
        self._proc: subprocess.Popen | None = None
        self._stopping = False

    def command(self) -> list[str]:
        cmd = [self.python, "-m", "ui.window", "--url", self.url, "--state", str(self.state_path),
               "--width", str(self.width), "--height", str(self.height)]
        if self.on_top:
            cmd.append("--on-top")
        if self.icon is not None:
            cmd += ["--icon", str(self.icon)]
        return cmd

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.alive:
            return
        self._stopping = False
        # The token is in the URL: it goes to the child on its command line, never to a log.
        err = subprocess.DEVNULL
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            err = self.log_path.open("ab")
        try:
            self._proc = subprocess.Popen(self.command(), cwd=ROOT, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=err)
        finally:
            if err is not subprocess.DEVNULL:
                err.close()  # the child has its own handle now
        log.info("web UI window started (pid %d)", self._proc.pid)

    async def watch(self, interval_s: float = 0.5) -> None:
        """Runs for JARVIS's lifetime: tells `on_closed` when the window is closed."""
        while True:
            await asyncio.sleep(interval_s)
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._proc = None
                log.info("web UI window closed (exit code %s)", proc.returncode)
                if not self._stopping and self.on_closed is not None:
                    self.on_closed()

    def stop(self) -> None:
        """Close the window. The venv's python.exe is a launcher that runs the real interpreter as its child,
        so the whole process tree is stopped, or the window would stay open after JARVIS quits."""
        self._stopping = True
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        import psutil

        try:
            tree = [psutil.Process(proc.pid), *psutil.Process(proc.pid).children(recursive=True)]
        except psutil.Error:
            tree = []
        for p in reversed(tree):  # children first
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(tree, timeout=2)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass


if __name__ == "__main__":
    raise SystemExit(run_window())
