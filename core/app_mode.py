"""Running JARVIS as a desktop app (the desktop icon starts it with pythonw.exe: no terminal).

Without a console, printed lines go to logs/console.txt, problems the user must see are shown in
a message box, and a named mutex keeps it to one copy (two mics, one browser profile):

- the running JARVIS owns the mutex, and releases it as soon as it starts shutting down (after
  putting the music volume back), so a relaunch right after Quit doesn't have to wait;
- a new launch waits a few seconds for a JARVIS that is shutting down; if one is really running,
  it asks that JARVIS to bring its window to the front (a named event) and exits quietly.
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

MUTEX_NAME = "Local\\JARVIS-voice-assistant"
SHOW_EVENT_NAME = "Local\\JARVIS-show-window"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0x0, 0x80, 0x102
EVENT_MODIFY_STATE = 0x0002
MB_ICONINFORMATION, MB_ICONWARNING = 0x40, 0x30
CONSOLE_MAX_BYTES = 1_000_000
_mutex_handle = None  # owned by the main thread for the life of JARVIS
_show_event = None


def _kernel32():
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateEventW.restype = ctypes.c_void_p
    k.OpenEventW.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.ReleaseMutex.argtypes = [ctypes.c_void_p]
    k.SetEvent.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def is_windowless() -> bool:
    """True when started without a console (pythonw.exe, e.g. from the desktop icon)."""
    return sys.stdout is None or Path(sys.executable).name.lower() == "pythonw.exe"


def redirect_console(log_dir: Path) -> Path:
    """Send print() output to logs/console.txt. Appends (a second launch must not wipe the running
    JARVIS's output); starts over once it passes ~1 MB."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "console.txt"
    mode = "w" if path.exists() and path.stat().st_size > CONSOLE_MAX_BYTES else "a"
    stream = open(path, mode, encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 (lives until exit)
    stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    sys.stdout = sys.stderr = stream
    return path


def message_box(text: str, *, warning: bool = False, title: str = "JARVIS") -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONWARNING if warning else MB_ICONINFORMATION)
    except Exception:
        pass


def tell_user(text: str, windowless: bool, *, warning: bool = True) -> None:
    """Print it, and in app mode also show it (nobody is watching a console)."""
    print(text, flush=True)
    if windowless:
        message_box(text, warning=warning)


def claim_single_instance(name: str = MUTEX_NAME, wait_s: float = 5.0) -> bool:
    """Own the JARVIS mutex. If another JARVIS has it, wait up to `wait_s` in case it is quitting.

    False means another JARVIS is really running.
    """
    global _mutex_handle
    k = _kernel32()
    handle = k.CreateMutexW(None, True, name)
    if not handle:
        return True  # can't tell; don't block startup
    if k.GetLastError() != ERROR_ALREADY_EXISTS:
        _mutex_handle = handle
        return True
    result = k.WaitForSingleObject(handle, int(wait_s * 1000))
    if result in (WAIT_OBJECT_0, WAIT_ABANDONED):  # released at shutdown, or its process ended
        _mutex_handle = handle
        return True
    k.CloseHandle(handle)
    return False


def release_single_instance() -> None:
    """Called when shutdown starts (main thread), so the next launch can start straight away."""
    global _mutex_handle
    if _mutex_handle:
        k = _kernel32()
        k.ReleaseMutex(_mutex_handle)
        k.CloseHandle(_mutex_handle)
        _mutex_handle = None


def create_show_event(name: str = SHOW_EVENT_NAME) -> None:
    """The running JARVIS listens on this; a second launch sets it to say 'show your window'."""
    global _show_event
    _show_event = _kernel32().CreateEventW(None, False, False, name)  # auto-reset


def show_requested() -> bool:
    return bool(_show_event) and _kernel32().WaitForSingleObject(_show_event, 0) == WAIT_OBJECT_0


def request_show(name: str = SHOW_EVENT_NAME) -> bool:
    """From a second launch: ask the running JARVIS to come to the front. False if it can't be reached."""
    k = _kernel32()
    handle = k.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(k.SetEvent(handle))
    finally:
        k.CloseHandle(handle)
