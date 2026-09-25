"""`--install-shortcut`: a "JARVIS" icon on the desktop that starts run.bat. Not auto-start."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.tray import save_ico

ROOT = Path(__file__).resolve().parent.parent


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shortcut_script(lnk: str, target: Path, workdir: Path, icon: Path) -> str:
    """PowerShell that writes the .lnk through the Windows Shell (WScript.Shell COM)."""
    return (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + "); "
        f"$s.TargetPath = {_ps_quote(str(target))}; "
        f"$s.WorkingDirectory = {_ps_quote(str(workdir))}; "
        f"$s.IconLocation = {_ps_quote(str(icon))}; "
        "$s.Description = 'Start JARVIS, your voice assistant'; "
        "$s.Save()"
    )


def install_shortcut() -> int:
    icon = save_ico(ROOT / "assets" / "jarvis.ico")
    desktop = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    lnk = str(Path(desktop) / "JARVIS.lnk")
    subprocess.run(["powershell", "-NoProfile", "-Command", shortcut_script(lnk, ROOT / "run.bat", ROOT, icon)],
                   check=True)
    print(f"Created {lnk}. Double-click it to start JARVIS. Delete it any time to remove it.")
    return 0
