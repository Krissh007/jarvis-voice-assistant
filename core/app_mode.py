"""Running JARVIS as a desktop app (the desktop icon starts it with pythonw.exe: no terminal).

Without a console, printed lines go to logs/console.txt, problems the user must see are shown in
a message box, and a named mutex stops a second copy (two mics, one browser profile) from starting.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

MUTEX_NAME = "Local\\JARVIS-voice-assistant"
ERROR_ALREADY_EXISTS = 183
MB_ICONINFORMATION, MB_ICONWARNING = 0x40, 0x30
_mutex_handle = None  # kept for the life of the process; Windows releases it when JARVIS exits


def is_windowless() -> bool:
    """True when started without a console (pythonw.exe, e.g. from the desktop icon)."""
    return sys.stdout is None or Path(sys.executable).name.lower() == "pythonw.exe"


def redirect_console(log_dir: Path) -> Path:
    """Send print() output to logs/console.txt (replaced each start)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "console.txt"
    stream = open(path, "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 (lives until exit)
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


def claim_single_instance(name: str = MUTEX_NAME) -> bool:
    """False if another JARVIS is already running."""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # can't tell; don't block startup
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex_handle = handle
    return True
