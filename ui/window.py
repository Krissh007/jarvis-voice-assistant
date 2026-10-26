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
- `jarvis:front`: come to the front (un-minimized, but still maximized if it was);
- `jarvis:size:{"size": px}`: the orb's new size (the Customize slider), in pixels;
- `jarvis:hide` / `jarvis:show`: the orb's show/hide hotkey (the page keeps running while hidden);
- `jarvis:dock:{"docked": true, "x", "y", "size", "hwnd"}` / `{"docked": false}`: the orb sits in the
  full-screen app's dot (screen pixels; it shrinks to `size`) or floats again where it was. Docked, the app's
  window (`hwnd`) owns the orb's: it stays just above the app and goes behind other apps with it, instead of
  staying on top of everything (it would cover their buttons);
- `jarvis:dock_slot:{"x", "y", "size"}`: the app page's dot for the orb, in page pixels. While the app's
  window is maximized it tells the page where that is on the screen (window.jarvisDock), and the page
  tells JARVIS, which tells the orb.
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
GWLP_HWNDPARENT = -8  # SetWindowLongPtr: a window's owner
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10


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
EXPANDED = (420, 720)    # the orb window opened into the control centre (page pixels): the orb on top,
                         # everything else stacked under it
RING = 480               # the orb window while its menu blooms: a square around the orb, room for the
                         # ring and the Dashboards ring outside it


def orb_canvas(size: int) -> int:
    """The orb canvas (and the closed window) for an orb that looks `size` pixels across."""
    return int(round(size / ORB_SCALE))


def orb_geometry(centre: tuple[int, int], expanded: bool, canvas: int,
                 screen: tuple[int, int, int, int], ring: bool = False) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """Window rectangle (x, y, w, h) and the orb's position inside it, for an orb centred at `centre` on
    the screen (x, y, w, h). The window is kept on the screen: near an edge the orb moves inward. Except
    for the menu (ring): the orb stays where it is, off the window's centre near an edge, and the page fans
    the menu out on the side with room."""
    if ring:
        w = h = RING
    else:
        w, h = EXPANDED if expanded else (canvas, canvas)
    ox, oy = (w // 2, canvas // 2 + 6) if expanded and not ring else (w // 2, h // 2)
    sx, sy, sw, sh = screen
    x = min(max(centre[0] - ox, sx), sx + sw - w)
    y = min(max(centre[1] - oy, sy), sy + sh - h)
    if ring:
        ox, oy = centre[0] - x, centre[1] - y
    return (x, y, w, h), (ox, oy)


def run_window(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui.window")
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=["orb", "chat"], default="orb")
    parser.add_argument("--title", default="JARVIS")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--size", type=int, default=112, help="orb mode: how big the orb looks, in pixels")
    parser.add_argument("--width", type=int, default=1100, help="chat mode: first width")
    parser.add_argument("--height", type=int, default=760, help="chat mode: first height")
    parser.add_argument("--maximized", action="store_true", help="chat mode: open maximized")
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

    from PySide6.QtCore import QEvent, QPoint, QTimer, QUrl, Qt
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
    orb = {"expanded": False, "ring": False, "inner": (canvas // 2, canvas // 2), "home": None, "placing": False, "canvas": canvas,
           "normal": canvas, "dock": None}   # normal: the floating orb's canvas; dock: (x, y) while docked in the app
    slot: dict = {"at": None, "sent": None}   # chat mode: the page's dot for the orb, and what was last reported

    if orb_mode and not ("cx" in state and "cy" in state):  # first time: near the top right
        state.update(cx=screen[0] + screen[2] - canvas // 2 - 40, cy=screen[1] + canvas // 2 + 120)

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 (Qt's name)
            if not message.startswith("jarvis:") or not str(source).startswith(origin):
                return
            command, _, data = message[len("jarvis:"):].partition(":")
            if command == "shape":
                shapes[:] = parse_shapes(data)
            elif command == "drag" and orb["dock"] is None:   # docked, the orb stays in the app's dot
                try:   # a drag out of the menu: first under the mouse again (it moved dx, dy since the press)
                    d = json.loads(data) if data else {}
                    dx, dy = (max(-4000, min(4000, int(d.get("dx", 0)))), max(-4000, min(4000, int(d.get("dy", 0)))))
                except (ValueError, AttributeError, TypeError):
                    dx = dy = 0
                if dx or dy:
                    view.move(view.x() + dx, view.y() + dy)
                handle = view.windowHandle()
                if handle is not None:
                    handle.startSystemMove()
            elif command == "front":
                if view.isMinimized():   # back as it was before (maximized stays maximized)
                    view.setWindowState((view.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
                view.show()
                view.raise_()
                view.activateWindow()
            elif command == "hide" and orb_mode:  # the show/hide hotkey (the page keeps running, hidden)
                view.hide()
            elif command == "show" and orb_mode:
                view.show()
                view.raise_()
            elif command == "mode" and orb_mode:
                try:
                    d = json.loads(data)
                    expanded, ring = bool(d.get("expanded")), bool(d.get("ring"))
                except (ValueError, AttributeError):
                    return
                set_expanded(expanded, ring)
            elif command == "dock" and orb_mode:
                try:
                    d = json.loads(data)
                    where = ((int(d["x"]), int(d["y"]), int(d.get("size") or 40), int(d.get("hwnd") or 0))
                             if d.get("docked") else None)
                except (ValueError, AttributeError, TypeError, KeyError):
                    return
                dock(where)
            elif command == "dock_slot" and not orb_mode:
                try:
                    d = json.loads(data)
                    slot["at"] = (float(d["x"]), float(d["y"]), int(d.get("size") or 40))
                except (ValueError, AttributeError, TypeError, KeyError):
                    slot["at"] = None
                slot["sent"] = None   # the page (re)loaded or moved its dot: tell it again
                report_dock()
            elif command == "size" and orb_mode:
                try:
                    size = int(json.loads(data).get("size"))
                except (ValueError, AttributeError, TypeError):
                    return
                resize(min(max(size, 70), 260))

    class View(QWebEngineView):
        def moveEvent(self, event):  # noqa: N802
            super().moveEvent(event)
            if orb_mode:
                if not orb["placing"] and orb["dock"] is None:  # the user dragged it: the orb lives where it was left
                    state.update(cx=self.x() + orb["inner"][0], cy=self.y() + orb["inner"][1])
                    orb["home"] = None
            elif not (self.isMaximized() or self.isMinimized()):   # keep the normal place, for un-maximizing
                state.update(x=self.x(), y=self.y())
            if not orb_mode:
                dock_soon.start(150)
            save_soon.start(500)

        def resizeEvent(self, event):  # noqa: N802
            super().resizeEvent(event)
            if not orb_mode and not (self.isMaximized() or self.isMinimized()):
                state.update(width=self.width(), height=self.height())
                save_soon.start(500)
            if not orb_mode:
                dock_soon.start(150)

        def changeEvent(self, event):  # noqa: N802
            super().changeEvent(event)
            if not orb_mode and event.type() == QEvent.WindowStateChange:   # maximized, minimized, restored
                dock_soon.start(150)

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
    dock_soon = QTimer(view)
    dock_soon.setSingleShot(True)

    def report_dock() -> None:
        """Chat mode: while the window is maximized (or full screen) and showing, where the page's dot is on the
        screen, for the orb to sit in; otherwise that the orb should float. Sent only when it changes, and only
        once the page has said where its dot is (before that it can't hear it)."""
        if slot["at"] is None:
            return
        full = (view.isMaximized() or view.isFullScreen()) and not view.isMinimized() and view.isVisible()
        if full and slot["at"] is not None:
            x, y, size = slot["at"]
            g = view.mapToGlobal(QPoint(int(round(x)), int(round(y))))
            msg = {"docked": True, "x": g.x(), "y": g.y(), "size": size, "hwnd": int(view.winId())}
        else:
            msg = {"docked": False}
        if msg != slot["sent"]:
            slot["sent"] = msg
            page.runJavaScript(f"window.jarvisDock && window.jarvisDock({json.dumps(msg)})")

    dock_soon.timeout.connect(report_dock)

    def place() -> None:
        """Tell the page where its orb is (it lays everything else out around it)."""
        ox, oy = orb["inner"]
        where = {"expanded": orb["expanded"], "ring": orb["ring"], "x": ox, "y": oy, "canvas": orb["canvas"]}
        page.runJavaScript(f"window.jarvisPlace && window.jarvisPlace({json.dumps(where)})")

    def set_expanded(expanded: bool, ring: bool = False) -> None:
        """Open the control centre (or, ring, the menu) around the orb, or close back to just the orb,
        keeping the orb where it is on the screen (near an edge the control centre moves it inward while
        open, and it goes back when closed)."""
        ring = bool(expanded and ring)
        if expanded == orb["expanded"] and ring == orb["ring"]:
            place()
            return
        centre = orb["dock"] or (int(state["cx"]), int(state["cy"]))
        if orb["home"] is not None and (orb["expanded"] or not expanded):
            centre = orb["home"]  # opened near an edge and not moved since: back to where it was
        rect, inner = orb_geometry(centre, expanded, orb["canvas"], screen, ring)
        orb["placing"] = True
        was_open = orb["expanded"]
        orb["expanded"], orb["ring"], orb["inner"] = expanded, ring, inner
        view.setGeometry(*rect)
        orb["placing"] = False
        if expanded and not was_open:
            orb["home"] = centre
        if orb["dock"] is None:   # docked, the floating place is kept for later
            state.update(cx=rect[0] + inner[0], cy=rect[1] + inner[1])
        place()

    def resize(size: int) -> None:
        """A new orb size: the same centre on the screen, a new canvas (and, closed, a new window size). While
        docked it's kept for when the orb floats again."""
        orb["normal"] = orb_canvas(size)
        if orb["dock"] is not None:
            return
        orb["canvas"] = orb["normal"]
        rect, inner = orb_geometry((int(state["cx"]), int(state["cy"])), orb["expanded"], orb["canvas"], screen)
        orb["placing"] = True
        orb["inner"] = inner
        view.setGeometry(*rect)
        orb["placing"] = False
        if not orb["expanded"]:
            shapes[:] = [{"c": inner, "r": orb["canvas"] * 0.36}]
        place()

    def dock(where: tuple[int, int, int, int] | None) -> None:
        """Sit in the full-screen app's dot (x, y: its centre on the screen; size: how big the orb looks), the
        same orb, smaller; or float again where it was, at its own size. The floating place isn't changed."""
        own_by(where[3] if where is not None else 0)
        if where is not None:
            orb["dock"] = (where[0], where[1])
            orb["canvas"] = orb_canvas(min(max(where[2], 28), 80))
            centre = orb["dock"]
        else:
            if orb["dock"] is None:
                return
            orb["dock"] = None
            orb["canvas"] = orb["normal"]
            centre = (int(state["cx"]), int(state["cy"]))
        orb["expanded"], orb["ring"], orb["home"] = False, False, None
        rect, inner = orb_geometry(centre, False, orb["canvas"], screen)
        orb["placing"] = True
        orb["inner"] = inner
        view.setGeometry(*rect)
        orb["placing"] = False
        shapes[:] = [{"c": inner, "r": orb["canvas"] * 0.36}]
        place()

    def own_by(owner: int) -> None:
        """Docked: the app's window owns the orb's (Windows keeps an owned window just above its owner, and
        hides it with it), and the orb isn't on top of everything. Floating (owner 0): on top again."""
        try:
            user32 = ctypes.windll.user32
            hwnd = int(view.winId())
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner or None)
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            on_top = HWND_TOPMOST if (not owner and args.on_top) else HWND_NOTOPMOST
            user32.SetWindowPos(hwnd, on_top, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except (AttributeError, OSError):   # not Windows (tests): only the place changes
            pass

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
        # until the page reports its shapes, the orb itself takes clicks (it's where it will be)
        shapes[:] = [{"c": orb["inner"], "r": canvas * 0.36}]

        through = {"now": None, "over": None}

        def follow_mouse() -> None:
            """Take clicks only while the mouse is over the orb or the open control centre's parts, and tell
            the page when the mouse comes over them or leaves (outside them it gets no mouse events): the
            menu blooms while the mouse is over the orb and folds away once it has left."""
            pos = view.mapFromGlobal(QCursor.pos())
            held = bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            over = hit(shapes, pos.x(), pos.y())
            if over != through["over"]:
                through["over"] = over
                page.runJavaScript(f"window.jarvisHover && window.jarvisHover({'true' if over else 'false'})")
            want = not over
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
    if not orb_mode and args.maximized:
        view.showMaximized()   # its normal size and place stay as kept, for un-maximizing
    else:
        view.show()
    if not orb_mode:
        view.raise_()
        view.activateWindow()
    code = app.exec()
    _save_state(args.state, state)
    return code


# ---------------------------------------------------------------- the parent side


class WindowProcess:
    def __init__(self, url: str, *, state_path: Path, mode: str = "orb", size: int = 112, width: int = 1100,
                 height: int = 760, on_top: bool = True, icon: Path | None = None,
                 on_closed: Callable[[], None] | None = None, python: str | None = None,
                 log_path: Path | None = None, maximized: bool = False) -> None:
        self.url = url
        self.mode = mode
        self.size = size
        self.state_path = state_path
        self.width = width
        self.height = height
        self.on_top = on_top
        self.icon = icon
        self.on_closed = on_closed
        self.maximized = maximized  # chat mode: open maximized
        self.python = python or sys.executable
        self.log_path = log_path  # the window's own errors (Qt WebEngine)
        self._proc: subprocess.Popen | None = None
        self._stopping = False

    def command(self) -> list[str]:
        cmd = [self.python, "-m", "ui.window", "--url", self.url, "--mode", self.mode, "--state", str(self.state_path),
               "--size", str(self.size), "--width", str(self.width), "--height", str(self.height)]
        if self.on_top:
            cmd.append("--on-top")
        if self.maximized:
            cmd.append("--maximized")
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
