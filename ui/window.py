"""JARVIS's window for the web UI: a pywebview (Edge WebView2) window in a child process.

pywebview has to own its process's main thread, and JARVIS's main thread runs the asyncio loop, so
the window lives in its own process (`python -m ui.window ...`) and only loads the local page. That
also keeps a WebView2 crash from taking the assistant down. The page talks to JARVIS over the
WebSocket like any view; the only thing the window itself offers the page is bring_to_front().

The parent side, WindowProcess, starts the child, notices when the user closes it, and asks it to come
forward (through the page) when a second launch wants the window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
BG = "#0a1520"


# ---------------------------------------------------------------- the child process


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


class _Api:
    """Exposed to the page as window.pywebview.api."""

    def __init__(self, on_top: bool) -> None:
        self._window = None
        self._on_top = on_top

    def bring_to_front(self) -> None:
        w = self._window
        if w is None:
            return
        w.restore()
        w.show()
        w.on_top = True  # Windows only lets a window jump in front this way
        w.on_top = self._on_top


def run_window(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui.window")
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="JARVIS")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--width", type=int, default=980)
    parser.add_argument("--height", type=int, default=680)
    parser.add_argument("--on-top", action="store_true")
    parser.add_argument("--icon", type=Path)
    args = parser.parse_args(argv)
    if not args.url.startswith("http://127.0.0.1:"):
        print("ui.window only opens JARVIS's own local page.", file=sys.stderr)
        return 2

    from core.taskbar import set_process_app_id

    set_process_app_id()  # groups this window with JARVIS's pinned icon
    import webview

    state = {"width": args.width, "height": args.height, **_load_state(args.state)}
    api = _Api(args.on_top)
    window = webview.create_window(
        args.title, args.url, js_api=api, width=state["width"], height=state["height"],
        x=state.get("x"), y=state.get("y"), min_size=(360, 420), on_top=args.on_top, background_color=BG,
        text_select=True,
    )
    api._window = window

    def moved(x, y):
        state.update(x=x, y=y)

    def resized(width, height):
        state.update(width=width, height=height)

    window.events.moved += moved
    window.events.resized += resized
    window.events.closing += lambda: _save_state(args.state, state)
    icon = str(args.icon) if args.icon and args.icon.is_file() else None
    webview.start(gui="edgechromium", private_mode=True, icon=icon)  # private: nothing stored on disk
    _save_state(args.state, state)
    return 0


# ---------------------------------------------------------------- the parent side


class WindowProcess:
    def __init__(self, url: str, *, state_path: Path, width: int = 980, height: int = 680, on_top: bool = False,
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
        self.log_path = log_path  # the window's own errors (WebView2, pywebview)
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
        """Runs for JARVIS's lifetime: tells `on_closed` when the user closes the window."""
        while True:
            await asyncio.sleep(interval_s)
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._proc = None
                log.info("web UI window closed (exit code %s)", proc.returncode)
                if not self._stopping and self.on_closed is not None:
                    self.on_closed()

    def stop(self) -> None:
        self._stopping = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(run_window())
