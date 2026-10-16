"""JARVIS's windows for the web UI, each a Qt WebEngine window in a child process.

- **orb** (the control centre): no frame and no background, so only what the page draws is visible
  and its glow blends into the desktop (per-pixel alpha). Closed, it's just the orb; a double-tap opens
  it into the control centre around the orb (typing box, quick actions, sections), keeping the orb
  where it is on the screen, or moving it inward near an edge. It can be dragged anywhere. Everywhere
  the page draws nothing, clicks go through to whatever is behind: the page reports its clickable shapes,
  and a timer switches the window between taking clicks and passing them on as the mouse moves.
- **chat** (the complete chat page): an ordinary resizable window.

Qt has to own its process's main thread, and JARVIS's main thread runs the asyncio loop, so the windows
live in their own processes (`python -m ui.window --mode orb|chat ...`) and only load the local page. A
crash in the browser engine can't take the assistant down that way. The page talks to JARVIS over the
WebSocket like any view. To its window it sends only console lines starting "jarvis:":
- `jarvis:shape:[...]`: the orb page's clickable circles and boxes, in page pixels;
- `jarvis:mode:{"expanded": true|false}`: open or close the control centre;
- `jarvis:drag`: start moving the window with the mouse (the orb was dragged);
- `jarvis:front`: come to the front.
The orb window answers with `jarvisPlace({expanded, x, y, canvas})`: where the orb is in the page.

The parent side, WindowProcess, starts a child, notices when it's closed, and stops it on quit.
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
        return {k: int(v) for k, v in data.items() if k in {"x", "y", "width", "height", "cx", "cy"}}
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


ORB_SCALE = 0.68         # the orb's drawing (sphere and bands) fills this much of its canvas
EXPANDED = (780, 620)    # the orb window opened into the control centre (page pixels)
ORB_AT = (0.5, 0.45)     # where the orb sits in the opened window, as fractions of its size


def orb_canvas(size: int) -> int:
    """The orb canvas (and the closed window) for an orb that looks `size` pixels across."""
    return int(round(size / ORB_SCALE))


def orb_geometry(centre: tuple[int, int], expanded: bool, canvas: int,
                 screen: tuple[int, int, int, int]) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """Window rectangle (x, y, w, h) and the orb's position inside it, for an orb centred at `centre` on
    the screen (x, y, w, h). The window is kept on the screen: near an edge the orb moves inward."""
    w, h = EXPANDED if expanded else (canvas, canvas)
    ox, oy = (round(w * ORB_AT[0]), round(h * ORB_AT[1])) if expanded else (w // 2, h // 2)
    sx, sy, sw, sh = screen
    x = min(max(centre[0] - ox, sx), sx + sw - w)
    y = min(max(centre[1] - oy, sy), sy + sh - h)
    return (x, y, w, h), (ox, oy)


def run_window(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui.window")
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=["orb", "chat"], default="orb")
    parser.add_argument("--title", default="JARVIS")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--size", type=int, default=160, help="orb mode: how big the orb looks, in pixels")
    parser.add_argument("--width", type=int, default=1100, help="chat mode: first width")
    parser.add_argument("--height", type=int, default=760, help="chat mode: first height")
    parser.add_argument("--on-top", action="store_true")
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args(argv)
    if not args.url.startswith("http://127.0.0.1:"):
        print("ui.window only opens JARVIS's own local page.", file=sys.stderr)
        return 2
    from urllib.parse import urlsplit

    parts = urlsplit(args.url)
    origin = f"{parts.scheme}://{parts.netloc}"

    from core.taskbar import set_process_app_id

    set_process_app_id()  # groups these windows with JARVIS's pinned icon

    from PySide6.QtCore import QTimer, QUrl, Qt
    from PySide6.QtGui import QColor, QCursor, QIcon
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication([sys.argv[0]])
    orb_mode = args.mode == "orb"
    state = _load_state(args.state)
    avail = app.primaryScreen().availableGeometry()
    screen = (avail.x(), avail.y(), avail.width(), avail.height())
    canvas = orb_canvas(args.size)
    shapes: list[dict] = []
    orb = {"expanded": False, "inner": (canvas // 2, canvas // 2), "home": None, "placing": False}

    if orb_mode and not ("cx" in state and "cy" in state):  # first time: near the top right
        state.update(cx=screen[0] + screen[2] - canvas // 2 - 40, cy=screen[1] + canvas // 2 + 120)

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
            elif command == "mode" and orb_mode:
                try:
                    expanded = bool(json.loads(data).get("expanded"))
                except (ValueError, AttributeError):
                    return
                set_expanded(expanded)

    class View(QWebEngineView):
        def moveEvent(self, event):  # noqa: N802
            super().moveEvent(event)
            if orb_mode:
                if not orb["placing"]:  # the user dragged it: the orb lives where it was left
                    state.update(cx=self.x() + orb["inner"][0], cy=self.y() + orb["inner"][1])
                    orb["home"] = None
            else:
                state.update(x=self.x(), y=self.y())
            save_soon.start(500)

        def resizeEvent(self, event):  # noqa: N802
            super().resizeEvent(event)
            if not orb_mode:
                state.update(width=self.width(), height=self.height())
                save_soon.start(500)

        def closeEvent(self, event):  # noqa: N802
            _save_state(args.state, state)
            super().closeEvent(event)

    profile = QWebEngineProfile(app)  # off the record: nothing is stored on disk
    view = View()
    page = Page(profile, view)
    view.setPage(page)
    view.setWindowTitle(args.title if orb_mode else f"{args.title} · Chat")
    if args.icon and args.icon.is_file():
        view.setWindowIcon(QIcon(str(args.icon)))
    view.setContextMenuPolicy(Qt.NoContextMenu)
    save_soon = QTimer(view)
    save_soon.setSingleShot(True)
    save_soon.timeout.connect(lambda: _save_state(args.state, state))

    def place() -> None:
        """Tell the page where its orb is (it lays everything else out around it)."""
        ox, oy = orb["inner"]
        page.runJavaScript(f"window.jarvisPlace && window.jarvisPlace({json.dumps({'expanded': orb['expanded'], 'x': ox, 'y': oy, 'canvas': canvas})})")

    def set_expanded(expanded: bool) -> None:
        """Open the control centre around the orb, or close back to just the orb, keeping the orb where it
        is on the screen (near an edge it moves inward while open, and goes back when closed)."""
        if expanded == orb["expanded"]:
            place()
            return
        centre = (int(state["cx"]), int(state["cy"]))
        if not expanded and orb["home"] is not None:
            centre = orb["home"]  # opened near an edge and not moved since: back to where it was
        rect, inner = orb_geometry(centre, expanded, canvas, screen)
        orb["placing"] = True
        orb["expanded"], orb["inner"] = expanded, inner
        view.setGeometry(*rect)
        orb["placing"] = False
        if expanded:
            orb["home"] = centre
        state.update(cx=rect[0] + inner[0], cy=rect[1] + inner[1])
        place()

    if orb_mode:
        page.setBackgroundColor(QColor(0, 0, 0, 0))
        view.setWindowFlags(Qt.FramelessWindowHint | (Qt.WindowStaysOnTopHint if args.on_top else Qt.Widget))
        view.setAttribute(Qt.WA_TranslucentBackground)
        view.setStyleSheet("background: transparent")
        rect, orb["inner"] = orb_geometry((int(state["cx"]), int(state["cy"])), False, canvas, screen)
        orb["placing"] = True
        view.setGeometry(*rect)
        orb["placing"] = False
        page.loadFinished.connect(lambda ok: place())

        through = {"now": None}

        def follow_mouse() -> None:
            """Take clicks only while the mouse is over the orb or the open control centre's parts."""
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
    else:
        page.setBackgroundColor(QColor("#070b14"))
        w, h = state.get("width", args.width), state.get("height", args.height)
        x = state.get("x", screen[0] + (screen[2] - w) // 2)
        y = state.get("y", screen[1] + (screen[3] - h) // 2)
        view.setGeometry(x, y, w, h)
        view.setMinimumSize(420, 480)

    view.load(QUrl(args.url))
    view.show()
    if not orb_mode:
        view.raise_()
        view.activateWindow()
    code = app.exec()
    _save_state(args.state, state)
    return code


# ---------------------------------------------------------------- the parent side


class WindowProcess:
    def __init__(self, url: str, *, state_path: Path, mode: str = "orb", size: int = 160, width: int = 1100,
                 height: int = 760, on_top: bool = True, icon: Path | None = None,
                 on_closed: Callable[[], None] | None = None, python: str | None = None,
                 log_path: Path | None = None) -> None:
        self.url = url
        self.mode = mode
        self.size = size
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
        cmd = [self.python, "-m", "ui.window", "--url", self.url, "--mode", self.mode, "--state", str(self.state_path),
               "--size", str(self.size), "--width", str(self.width), "--height", str(self.height)]
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
