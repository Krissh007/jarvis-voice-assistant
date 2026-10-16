"""Windows UI Automation (the accessibility API screen readers use), via comtypes.

Reads a window's controls (buttons, text boxes, menus...) and its visible text in one cached
call: fast, exact, no screenshots and no AI. Everything here runs in a worker thread; COM is
initialised once per thread. Callers decide what may be read or touched (actions/desktop.py).
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass

_local = threading.local()
# All UI Automation work runs on one thread, so elements found by read() stay usable for focus/click.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")


async def call(fn, *args, **kwargs):
    """Run fn on the UI Automation thread."""
    return await asyncio.get_running_loop().run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GA_ROOTOWNER = 3
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80


def _uia():
    """The UI Automation client for this thread (COM objects can't be shared between threads)."""
    if getattr(_local, "uia", None) is None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        _local.UIA = UIA
        _local.uia = comtypes.client.CreateObject(UIA.CUIAutomation._reg_clsid_, interface=UIA.IUIAutomation)
    return _local.uia, _local.UIA


# ---------------------------------------------------------------- windows

@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str  # process file name, lower case, e.g. "notepad.exe"

    @property
    def label(self) -> str:
        return self.title or self.exe or f"window {self.hwnd}"


def _title(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe(pid: int) -> str:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def window(hwnd: int) -> Window | None:
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _exe(pid.value)
    if exe == "applicationframehost.exe":  # Store apps (Calculator...) live inside a frame window
        exe = _frame_child_exe(hwnd) or exe
    return Window(int(hwnd), _title(hwnd), int(pid.value), exe)


def _frame_child_exe(hwnd: int) -> str:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(child, _):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
        exe = _exe(pid.value)
        if exe and exe != "applicationframehost.exe":
            found.append(exe)
            return False
        return True

    _user32.EnumChildWindows(hwnd, callback, 0)
    return found[0] if found else ""


def top_windows() -> list[Window]:
    """Visible, non-minimised app windows with a title, front to back (Windows' z-order)."""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if (_user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd)
                and _user32.GetWindowTextLengthW(hwnd) > 0
                and _user32.GetAncestor(hwnd, GA_ROOTOWNER) == hwnd
                and not _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW):
            found.append(int(hwnd))
        return True

    _user32.EnumWindows(callback, 0)
    return [w for h in found if (w := window(h)) is not None]


def all_windows() -> list[Window]:
    """Like top_windows, but minimised ones too (an app minimised to the taskbar is still open)."""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if (_user32.IsWindowVisible(hwnd) and _user32.GetWindowTextLengthW(hwnd) > 0
                and _user32.GetAncestor(hwnd, GA_ROOTOWNER) == hwnd
                and not _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW):
            found.append(int(hwnd))
        return True

    _user32.EnumWindows(callback, 0)
    return [w for h in found if (w := window(h)) is not None]


def foreground() -> Window | None:
    return window(_user32.GetForegroundWindow())


# ---------------------------------------------------------------- reading

KINDS = {  # UIA control type id -> friendly name (filled in on first use)
    "Button": "button", "SplitButton": "button", "Edit": "text box", "Document": "document",
    "Hyperlink": "link", "MenuItem": "menu item", "TabItem": "tab", "ListItem": "list item",
    "TreeItem": "tree item", "CheckBox": "checkbox", "RadioButton": "radio button", "ComboBox": "dropdown",
    "Slider": "slider", "Text": "text",
}
TYPEABLE = {"text box", "document", "dropdown"}


@dataclass
class Control:
    id: int
    kind: str
    name: str
    rect: tuple[int, int, int, int]
    password: bool = False
    value: str = ""  # a text box's or document's visible text (filled only when reading text)
    auto_id: str = ""  # e.g. "RootWebArea": the whole page of a Chromium/Electron app, not a field
    class_name: str = ""  # e.g. "xterm-helper-textarea": a terminal inside an app (VS Code)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


def _type_ids(UIA) -> dict[int, str]:
    return {getattr(UIA, f"UIA_{name}ControlTypeId"): kind for name, kind in KINDS.items()}


def _element_text(el, UIA, max_chars: int) -> str:
    """A document's or text box's visible text (TextPattern), else its value."""
    import comtypes

    try:
        pattern = el.GetCurrentPattern(UIA.UIA_TextPatternId)
        if pattern:
            tp = pattern.QueryInterface(UIA.IUIAutomationTextPattern)
            ranges = tp.GetVisibleRanges()
            text = " ".join(ranges.GetElement(j).GetText(max_chars) for j in range(ranges.Length))
            if text.strip():
                return text
    except comtypes.COMError:
        pass
    try:
        pattern = el.GetCurrentPattern(UIA.UIA_ValuePatternId)
        if pattern:
            value = pattern.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
            if value and value.strip():
                return value
    except comtypes.COMError:
        pass
    return ""


def read(hwnd: int, *, with_text: bool = False, limit: int = 150, max_chars: int = 4000,
         patient: bool = False) -> list[Control]:
    """Controls (and, with `with_text`, text) inside a window, top to bottom, left to right.

    Chromium/Electron apps (Claude, ChatGPT, VS Code) switch accessibility on when first asked, so
    the first read can be nearly empty; `patient` waits a little for them.
    """
    found = _read(hwnd, with_text, limit, max_chars)
    for _ in range(3 if patient else 0):
        if len(found) >= 8:
            break
        time.sleep(1.0)
        found = _read(hwnd, with_text, limit, max_chars)
    return found


def _read(hwnd: int, with_text: bool, limit: int, max_chars: int) -> list[Control]:
    import comtypes

    uia, UIA = _uia()
    try:
        root = uia.ElementFromHandle(hwnd)
    except comtypes.COMError:
        return []
    types = _type_ids(UIA)
    if not with_text:
        types = {k: v for k, v in types.items() if v != "text"}
    cache = uia.CreateCacheRequest()
    for prop in (UIA.UIA_NamePropertyId, UIA.UIA_ControlTypePropertyId, UIA.UIA_BoundingRectanglePropertyId,
                 UIA.UIA_IsOffscreenPropertyId, UIA.UIA_IsEnabledPropertyId, UIA.UIA_IsPasswordPropertyId,
                 UIA.UIA_AutomationIdPropertyId, UIA.UIA_ClassNamePropertyId):
        cache.AddProperty(prop)
    condition = uia.CreateOrConditionFromArray([uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, t)
                                                for t in types])
    try:
        elements = root.FindAllBuildCache(UIA.TreeScope_Descendants, condition, cache)
    except comtypes.COMError:
        return []
    win = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(win))
    out: list[tuple[Control, object]] = []
    seen: set[tuple] = set()
    budget = names_budget = max_chars  # documents and long labels each get their own share
    for i in range(elements.Length):
        el = elements.GetElement(i)
        try:
            if el.CachedIsOffscreen:
                continue
            kind = types.get(el.CachedControlType, "control")
            if kind not in {"text", "document", "text box"} and not el.CachedIsEnabled:
                continue
            r = el.CachedBoundingRectangle
            if r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            if r.right < win.left or r.left > win.right or r.bottom < win.top or r.top > win.bottom:
                continue  # scrolled out of the window
            name = " ".join((el.CachedName or "").replace("\ufffc", " ").split())
            if with_text and kind in {"text", "list item"}:  # chat messages and labels: part of the text budget
                name = name[:max(200, min(1000, names_budget))]
                names_budget -= max(0, len(name) - 200)
            else:
                name = name[:200]
            password = bool(el.CachedIsPassword)
            value = ""
            if with_text and kind in {"document", "text box"} and not password and budget > 0:
                value = " ".join(_element_text(el, UIA, budget).replace("\ufffc", " ").split())[:budget]
                budget -= len(value)
            key = (kind, name, value[:40], r.left, r.top)
            if key in seen or (not name and not value and kind not in TYPEABLE):
                continue
            seen.add(key)
            out.append((Control(0, kind, name, (r.left, r.top, r.right, r.bottom), password, value,
                                el.CachedAutomationId or "", el.CachedClassName or ""), el))
        except comtypes.COMError:
            continue
    out.sort(key=lambda pair: (pair[0].rect[1] // 20, pair[0].rect[0]))
    out = out[:limit]
    for i, (c, _) in enumerate(out, 1):
        c.id = i
    _local.elements = (hwnd, {c.id: el for c, el in out})
    return [c for c, _ in out]


# ---------------------------------------------------------------- acting (call on the UIA thread)

EDIT_TYPES = ("Edit", "Document", "ComboBox")


def element(hwnd: int, cid: int):
    """The live element behind control `cid` of the last read() of this window, or None."""
    cached = getattr(_local, "elements", None)
    if not cached or cached[0] != hwnd:
        return None
    return cached[1].get(cid)


def set_focus(hwnd: int, cid: int) -> bool:
    import comtypes

    el = element(hwnd, cid)
    if el is None:
        return False
    try:
        el.SetFocus()
        return True
    except comtypes.COMError:
        return False


def press(hwnd: int, cid: int) -> str | None:
    """Activate a control the way a screen reader would (no mouse): Invoke, Toggle, Select or Expand.
    Returns which one worked, or None if the control supports none of them."""
    import comtypes

    el = element(hwnd, cid)
    if el is None:
        return None
    _, UIA = _uia()
    attempts = [
        (UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern, lambda p: p.Invoke(), "pressed"),
        (UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern, lambda p: p.Toggle(), "toggled"),
        (UIA.UIA_SelectionItemPatternId, UIA.IUIAutomationSelectionItemPattern, lambda p: p.Select(), "selected"),
        (UIA.UIA_ExpandCollapsePatternId, UIA.IUIAutomationExpandCollapsePattern,
         lambda p: p.Collapse() if p.CurrentExpandCollapseState == 1 else p.Expand(), "opened"),
    ]
    for pattern_id, interface, act, word in attempts:
        try:
            pattern = el.GetCurrentPattern(pattern_id)
            if pattern:
                act(pattern.QueryInterface(interface))
                return word
        except comtypes.COMError:
            continue
    return None


def current_rect(hwnd: int, cid: int) -> tuple[int, int, int, int] | None:
    """Where control `cid` is now (it may have moved since it was read)."""
    import comtypes

    el = element(hwnd, cid)
    if el is None:
        return None
    try:
        r = el.CurrentBoundingRectangle
        return (r.left, r.top, r.right, r.bottom) if r.right > r.left and r.bottom > r.top else None
    except comtypes.COMError:
        return None


@dataclass(frozen=True)
class Focused:
    kind: str
    name: str
    password: bool
    editable: bool
    auto_id: str = ""
    class_name: str = ""


def focused() -> Focused | None:
    """The element that has keyboard focus right now (in whichever window is in front)."""
    import comtypes

    uia, UIA = _uia()
    try:
        el = uia.GetFocusedElement()
        kinds = _type_ids(UIA)
        kind = kinds.get(el.CurrentControlType, "control")
        editable = el.CurrentControlType in {getattr(UIA, f"UIA_{t}ControlTypeId") for t in EDIT_TYPES}
        return Focused(kind, " ".join((el.CurrentName or "").split())[:200], bool(el.CurrentIsPassword), editable,
                       el.CurrentAutomationId or "", el.CurrentClassName or "")
    except comtypes.COMError:
        return None
