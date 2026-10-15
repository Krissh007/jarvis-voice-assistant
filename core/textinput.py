"""Typing into the window in front: paste through the clipboard, then put the user's clipboard back.

Pasting is fast, keeps line breaks as line breaks (typed newlines would press Enter and send a chat
early) and works in every app. The pasted text is marked so Windows clipboard history and cloud
clipboard never keep it. When the clipboard holds something that can't be put back exactly (a
picture, copied files), the text is typed key by key instead, with line breaks as Shift+Enter.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
TEXT_FORMATS = {1, 7, 13, 16}  # CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE (Windows adds these itself)
OBJECT_FORMATS = {2, 3, 8, 14, 15, 17}  # bitmaps, metafiles, files (CF_HDROP), DIBV5
PRIVATE_MARKS = [  # https://learn.microsoft.com/windows/win32/dataxchg/clipboard-formats#cloud-clipboard-and-clipboard-history-formats
    ("ExcludeClipboardContentFromMonitorProcessing", b"\x00"),
    ("CanIncludeInClipboardHistory", b"\x00\x00\x00\x00"),
    ("CanUploadToCloudClipboard", b"\x00\x00\x00\x00"),
]
PASTE_SETTLE_S = 0.6  # apps read the clipboard after Ctrl+V arrives; restoring sooner pastes the old text


class _Clipboard:
    def __enter__(self):
        import win32clipboard

        for _ in range(20):  # another app may hold it for a moment
            try:
                win32clipboard.OpenClipboard()
                return win32clipboard
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("the clipboard is busy")

    def __exit__(self, *exc):
        import win32clipboard

        win32clipboard.CloseClipboard()


def _formats(cb) -> set[int]:
    found, fmt = set(), 0
    while fmt := cb.EnumClipboardFormats(fmt):
        found.add(fmt)
    return found


def save() -> str | None:
    """The clipboard's text ("" if empty), or None if it holds something that isn't plain text."""
    with _Clipboard() as cb:
        formats = _formats(cb)
        if formats & OBJECT_FORMATS:
            return None
        if CF_UNICODETEXT in formats:
            return cb.GetClipboardData(CF_UNICODETEXT)
        return "" if not formats - TEXT_FORMATS else None


def _put(text: str) -> None:
    with _Clipboard() as cb:
        cb.EmptyClipboard()
        if text:
            cb.SetClipboardData(CF_UNICODETEXT, text)
            for name, data in PRIVATE_MARKS:
                cb.SetClipboardData(cb.RegisterClipboardFormat(name), data)


def foreground_root() -> int:
    user32 = ctypes.windll.user32
    return int(user32.GetAncestor(user32.GetForegroundWindow(), 3) or 0)  # GA_ROOTOWNER


def _check(hwnd: int) -> None:
    if foreground_root() != hwnd:
        raise RuntimeError("the app is no longer in front")


def type_text(hwnd: int, text: str) -> str:
    """Put `text` at the cursor of window `hwnd`, which must be in front. Returns 'pasted' or 'typed'."""
    from pywinauto.keyboard import send_keys

    _check(hwnd)
    previous = save()
    if previous is None:
        escaped = "".join("{%s}" % c if c in "{}+^%~()[]" else c for c in text)
        send_keys(escaped.replace("\r\n", "\n").replace("\n", "+{ENTER}"), with_spaces=True, with_tabs=True,
                  with_newlines=False, pause=0.005)
        return "typed"
    _put(text)
    try:
        _check(hwnd)
        send_keys("^v")
        time.sleep(PASTE_SETTLE_S)
    finally:
        try:
            _put(previous)
        except Exception:
            log.exception("couldn't put the clipboard back")
    return "pasted"


def press_enter(hwnd: int) -> None:
    from pywinauto.keyboard import send_keys

    _check(hwnd)
    send_keys("{ENTER}")


# ---------------------------------------------------------------- mouse

MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010)}  # (down, up) MOUSEEVENTF_ flags
WHEEL = 0x0800


def _inside(hwnd: int, x: int, y: int) -> bool:
    """The point is on this window (not covered by another one)."""
    user32 = ctypes.windll.user32
    hit = user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
    return int(user32.GetAncestor(hit, 3) or 0) == hwnd


def click_at(hwnd: int, x: int, y: int, button: str = "left", double: bool = False) -> None:
    """A real mouse click at (x, y) on window `hwnd`; the pointer goes back where it was."""
    user32 = ctypes.windll.user32
    _check(hwnd)
    if not _inside(hwnd, x, y):
        raise RuntimeError("something else is covering that spot")
    old = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    down, up = MOUSE[button]
    try:
        user32.SetCursorPos(x, y)
        time.sleep(0.03)
        for _ in range(2 if double else 1):
            user32.mouse_event(down, 0, 0, 0, 0)
            user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)
    finally:
        time.sleep(0.05)
        user32.SetCursorPos(old.x, old.y)


def scroll_at(hwnd: int, x: int, y: int, notches: int) -> None:
    """Turn the mouse wheel over (x, y): positive scrolls up, negative down."""
    user32 = ctypes.windll.user32
    _check(hwnd)
    old = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    try:
        user32.SetCursorPos(x, y)
        for _ in range(abs(notches)):
            user32.mouse_event(WHEEL, 0, 0, ctypes.c_uint32(120 if notches > 0 else -120).value, 0)
            time.sleep(0.03)
    finally:
        user32.SetCursorPos(old.x, old.y)


# ---------------------------------------------------------------- keys

KEY_NAMES = {"enter": "{ENTER}", "tab": "{TAB}", "escape": "{ESC}", "space": "{SPACE}", "backspace": "{BACKSPACE}",
             "delete": "{DELETE}", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
             "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}", "plus": "{+}",
             "minus": "-", **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)}}
MODIFIERS = {"ctrl": "^", "shift": "+", "alt": "%"}


def send_combo(hwnd: int, combo: str, times: int = 1) -> None:
    """Press a normalised combo such as 'ctrl+shift+t' (see actions.desktop.normalize_keys)."""
    from pywinauto.keyboard import send_keys

    *mods, key = combo.split("+")
    keys = "".join(MODIFIERS[m] for m in mods) + KEY_NAMES.get(key, key)
    for _ in range(times):
        _check(hwnd)
        send_keys(keys)
        time.sleep(0.04)
