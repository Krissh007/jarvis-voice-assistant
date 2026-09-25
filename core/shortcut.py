"""`--install-shortcut`: a "JARVIS" desktop icon that starts JARVIS as an app (no terminal). Not auto-start."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.tray import save_ico

ROOT = Path(__file__).resolve().parent.parent


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shortcut_script(lnk: str, target: Path, workdir: Path, icon: Path, arguments: str = "") -> str:
    """PowerShell that writes the .lnk through the Windows Shell (WScript.Shell COM)."""
    return (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + "); "
        f"$s.TargetPath = {_ps_quote(str(target))}; "
        f"$s.Arguments = {_ps_quote(arguments)}; "
        f"$s.WorkingDirectory = {_ps_quote(str(workdir))}; "
        f"$s.IconLocation = {_ps_quote(str(icon))}; "
        "$s.Description = 'Start JARVIS, your voice assistant'; "
        "$s.Save()"
    )


def app_command() -> tuple[Path, str]:
    """(program, arguments) that start JARVIS without a terminal; run.bat if pythonw is missing."""
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if pythonw.exists():
        return pythonw, "main.py"
    return ROOT / "run.bat", ""


def desktop_shortcut() -> Path:
    """Where the desktop icon lives (the real Desktop folder, e.g. inside OneDrive)."""
    import ctypes
    from ctypes import wintypes

    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
    return Path(buf.value) / "JARVIS.lnk"


def install_shortcut() -> int:
    from core.taskbar import set_shortcut_app_id

    icon = save_ico(ROOT / "assets" / "jarvis.ico")
    lnk = str(desktop_shortcut())
    target, arguments = app_command()
    subprocess.run(["powershell", "-NoProfile", "-Command", shortcut_script(lnk, target, ROOT, icon, arguments)],
                   check=True)
    set_shortcut_app_id(Path(lnk))  # a pin made from this icon groups with JARVIS's window
    print(f"Created {lnk}. Double-click it to start JARVIS (its window and tray icon, no terminal).")
    print("To pin it: right-click the icon > Show more options > Pin to taskbar.")
    print("Delete it any time to remove it. For the terminal version, run run.bat.")
    return 0
