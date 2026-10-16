"""Time, battery/CPU/RAM, volume, media keys, open allowlisted apps/sites."""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from core.registry import Registry, Tier, Tool

# Replaced in tests. os.startfile goes through ShellExecute, which resolves App Paths
# (chrome.exe, winword.exe...) and opens URLs in the default browser.
_launch = getattr(os, "startfile", None)

VK_MEDIA = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
MEDIA_ALIASES = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "toggle": "play_pause",
                 "skip": "next", "prev": "previous", "back": "previous"}


# ---------------------------------------------------------------- pure helpers (tested directly)

def normalize_app_name(name: str) -> str:
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    return " ".join(w for w in words if w not in {"the", "app", "application", "program", "please"})


def resolve_app(name: str, allowlist: dict[str, str], aliases: dict[str, str]) -> tuple[str, str] | None:
    """Exact match on a friendly name or alias, after normalising. Unknown names are never guessed."""
    wanted = normalize_app_name(name)
    names = {normalize_app_name(k): k for k in allowlist}
    if wanted in names:
        key = names[wanted]
        return key, allowlist[key]
    for alias, target in aliases.items():
        if normalize_app_name(alias) == wanted and target in allowlist:
            return target, allowlist[target]
    return None


def normalize_url(url: str) -> str | None:
    """Add https:// if missing. Only http(s) with a real host is allowed; anything else is None."""
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE) and not re.match(r"^[\w.-]+:\d", url):
            return None  # javascript:, file:, ms-settings: ...
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "." not in parts.hostname:
        return None
    if parts.username or parts.password:
        return None  # no credentials in URLs
    return url


def domain_allowed(url: str, allowlist: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (d.lower().lstrip(".") for d in allowlist))


def website_tier(url: str, allowlist: list[str]) -> Tier:
    normalized = normalize_url(url)
    if normalized is None:
        return Tier.BLOCKED
    return Tier.SAFE if domain_allowed(normalized, allowlist) else Tier.CONFIRM


# ---------------------------------------------------------------- tools

def get_datetime() -> str:
    """Current local date and time. Rung 2 (OS clock). Tier SAFE, read-only."""
    return datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M (%Z)")


def get_system_status() -> str:
    """Battery, CPU and memory. Rung 2 (psutil). Tier SAFE, read-only."""
    import psutil

    parts = []
    battery = psutil.sensors_battery()
    if battery is None:
        parts.append("no battery detected")
    else:
        state = "plugged in" if battery.power_plugged else "on battery"
        left = ""
        if not battery.power_plugged and battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            hours, minutes = divmod(battery.secsleft // 60, 60)
            left = f", about {hours} h {minutes} min left"
        parts.append(f"battery {battery.percent:.0f}% ({state}{left})")
    parts.append(f"CPU {psutil.cpu_percent(interval=0.5):.0f}%")
    mem = psutil.virtual_memory()
    parts.append(f"memory {mem.percent:.0f}% used ({mem.used / 2**30:.1f} of {mem.total / 2**30:.1f} GB)")
    return "; ".join(parts) + "."


def set_volume(level: int) -> str:
    """Set the master volume to 0-100. Rung 2 (Windows Core Audio via pycaw). Tier SAFE."""
    import comtypes
    from pycaw.pycaw import AudioUtilities

    level = max(0, min(100, int(round(float(level)))))
    comtypes.CoInitialize()  # tools run in a worker thread; COM needs initialising per thread
    try:
        volume = AudioUtilities.GetSpeakers().EndpointVolume
        volume.SetMasterVolumeLevelScalar(level / 100, None)
        if level > 0 and volume.GetMute():
            volume.SetMute(0, None)
    finally:
        comtypes.CoUninitialize()
    return f"Volume set to {level}%."


def media_key(action: str) -> str:
    """Press a media key: play_pause, next, previous or stop. Rung 2 (virtual keys). Tier SAFE."""
    import ctypes

    key = MEDIA_ALIASES.get(action.lower().strip(), action.lower().strip())
    if key not in VK_MEDIA:
        return f"I don't know the media action {action!r}. I can do play, pause, next, previous or stop."
    vk = VK_MEDIA[key]
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
    return {"play_pause": "Toggled play/pause.", "next": "Skipped to the next track.",
            "previous": "Went back a track.", "stop": "Stopped playback."}[key]


def make_open_app(config: dict):
    apps = config.get("apps", {})
    allowlist: dict[str, str] = apps.get("allowlist") or {}
    aliases: dict[str, str] = apps.get("aliases") or {}

    def open_app(name: str) -> str:
        """Open an app from the allowlist. Rung 2 (ShellExecute). Tier SAFE; unknown names are refused."""
        match = resolve_app(name, allowlist, aliases)
        if match is None:
            known = ", ".join(sorted(allowlist))
            return f"{name!r} isn't on my list of apps, so I won't open it. I can open: {known}."
        friendly, command = match
        _launch(command)
        return f"Opened {friendly}."

    def summarize(args: dict[str, Any]) -> str:
        match = resolve_app(str(args.get("name", "")), allowlist, aliases)
        return f"Open {match[0]}" if match else f"Open {args.get('name')!r}"

    return open_app, summarize, sorted(allowlist)


# What a launch command actually runs as (Windows 11 store apps differ from their launchers).
PROCESS_NAMES = {"calc.exe": ["calculatorapp.exe", "calc.exe"], "wt.exe": ["windowsterminal.exe"],
                 "code.exe": ["code.exe"], "notepad.exe": ["notepad.exe"]}
EXPLORER_WINDOW_CLASS = "CabinetWClass"  # folder windows only; the desktop/taskbar is the same process
WM_CLOSE = 0x0010


def _visible_windows() -> list[tuple[int, int, str]]:
    """(hwnd, pid, window class) for every visible top-level window with a title."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            found.append((int(hwnd), int(pid.value), cls.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def _frame_app_pids(hwnd: int) -> set[int]:
    """Store apps (Calculator...) live inside an ApplicationFrameHost frame: the pids of its child windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(child, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
        pids.add(int(pid.value))
        return True

    user32.EnumChildWindows(hwnd, callback, 0)
    return pids


def _is_jarvis_browser(proc) -> bool:
    try:
        return "browser_profile" in " ".join(proc.cmdline())
    except Exception:
        return False


def process_name(command: str) -> str:
    """The .exe an allowlisted launch command runs as. Store apps (shell:AppsFolder\\Family!App) run as App.exe."""
    if command.lower().startswith("shell:appsfolder\\") and "!" in command:
        return command.rsplit("!", 1)[1].lower() + ".exe"
    return os.path.basename(command).lower()


def windows_to_close(command: str) -> list[int]:
    """Top-level windows belonging to an allowlisted app. Never JARVIS's own Chrome, never the desktop."""
    import psutil

    exe = process_name(command)
    wanted = set(PROCESS_NAMES.get(exe, [exe]))
    hwnds = []
    for hwnd, pid, cls in _visible_windows():
        try:
            proc = psutil.Process(pid)
            name = proc.name().lower()
        except psutil.Error:
            continue
        if exe == "explorer.exe":
            if name == "explorer.exe" and cls == EXPLORER_WINDOW_CLASS:
                hwnds.append(hwnd)
        elif name == "applicationframehost.exe":
            for child_pid in _frame_app_pids(hwnd):
                try:
                    if psutil.Process(child_pid).name().lower() in wanted:
                        hwnds.append(hwnd)
                        break
                except psutil.Error:
                    continue
        elif name in wanted and not (name == "chrome.exe" and _is_jarvis_browser(proc)):
            hwnds.append(hwnd)
    return hwnds


DONT_SAVE = re.compile(r"^(don['’]?t save|do not save)$", re.IGNORECASE)


def click_dont_save(command: str, timeout_s: float = 4.0) -> int:
    """Press "Don't save" on the app's save prompts (UI Automation). Returns how many were pressed."""
    import time

    import comtypes
    from pywinauto import Desktop

    comtypes.CoInitialize()
    pressed = 0
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found = False
            for hwnd in windows_to_close(command):
                try:
                    win = Desktop(backend="uia").window(handle=hwnd)
                    for btn in win.descendants(control_type="Button"):
                        if DONT_SAVE.match((btn.element_info.name or "").strip()):
                            btn.invoke()
                            pressed += 1
                            found = True
                            time.sleep(0.5)
                            break
                except Exception:
                    continue
            if not found:
                if pressed or not windows_to_close(command):
                    break
                time.sleep(0.4)
        return pressed
    finally:
        comtypes.CoUninitialize()


def focus_window(hwnd: int) -> None:
    """Bring a window to the front, leaving its cursor where it is.

    Already in front: nothing to do. A synthetic Alt tap there would land in the app itself, and in
    VS Code (and other apps with a custom menu bar) Alt moves focus to the File menu, so the next
    typing found no text field. Otherwise the switch is made by attaching to the foreground window's
    input, which Windows accepts without a key press; the Alt tap is only the last resort, and then it
    goes to the window being left, not the one coming to the front."""
    import ctypes

    user32 = ctypes.windll.user32

    def in_front() -> bool:
        return int(user32.GetAncestor(user32.GetForegroundWindow(), 3) or 0) == int(hwnd)  # GA_ROOTOWNER

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    elif in_front():
        return
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(theirs and theirs != me and user32.AttachThreadInput(me, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, theirs, False)
    if not in_front():
        # Windows only lets the foreground app hand over focus; a tap of Alt makes the request count as input.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)


def make_focus_app(config: dict):
    apps = config.get("apps", {})
    allowlist: dict[str, str] = apps.get("allowlist") or {}
    aliases: dict[str, str] = apps.get("aliases") or {}

    def focus_app(name: str) -> str:
        """Bring an already-open app's window to the front. Rung 2 (Win32). Tier SAFE, changes nothing."""
        match = resolve_app(name, allowlist, aliases)
        if match is None:
            return f"{name!r} isn't on my list of apps."
        friendly, command = match
        hwnds = windows_to_close(command) if "://" not in command else []
        if not hwnds:
            return f"{friendly} isn't open. I can open it if you like."
        focus_window(hwnds[0])
        more = f" There are {len(hwnds)} {friendly} windows; this is the most recent." if len(hwnds) > 1 else ""
        return f"Switched to {friendly}.{more}"

    return focus_app


def make_close_app(config: dict):
    apps = config.get("apps", {})
    allowlist: dict[str, str] = apps.get("allowlist") or {}
    aliases: dict[str, str] = apps.get("aliases") or {}

    def close_app(name: str, discard_unsaved: bool = False) -> str:
        """Close an allowlisted app's windows politely (like clicking X, so it can ask to save).
        With discard_unsaved, also press "Don't save". Rung 2 (WM_CLOSE) / 4 (UIA). Tier CONFIRM.
        Never force-kills; never touches JARVIS's browser."""
        import ctypes

        match = resolve_app(name, allowlist, aliases)
        if match is None:
            return f"{name!r} isn't on my list of apps, so I won't close it."
        friendly, command = match
        if "://" in command:
            return f"{friendly} is a website, not an app. Say 'close the browser' to close JARVIS's browser."
        hwnds = windows_to_close(command)
        if not hwnds:
            return f"{friendly} isn't open."
        for hwnd in hwnds:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if discard_unsaved:
            pressed = click_dont_save(command)
            left = len(windows_to_close(command))
            if left == 0:
                return f"Closed {friendly}" + (" without saving." if pressed else ".")
            return f"{friendly} is still open ({left} window); it may be waiting on something I can't press."
        plural = "window" if len(hwnds) == 1 else f"{len(hwnds)} windows"
        return (f"Asked {friendly} to close ({plural}). If it asks to save, the user can say "
                "'close it without saving' and I'll press Don't save.")

    def summarize(args: dict[str, Any]) -> str:
        match = resolve_app(str(args.get("name", "")), allowlist, aliases)
        name = match[0] if match else repr(args.get("name"))
        if args.get("discard_unsaved"):
            return f"Close all {name} windows and throw away any unsaved changes"
        return f"Close all {name} windows"

    return close_app, summarize


def make_open_website(config: dict):
    read_allowlist: list[str] = config.get("sites", {}).get("read_allowlist") or []

    def open_website(url: str) -> str:
        """Open a website in the default browser. Rung 2 (URL via ShellExecute).
        Tier SAFE on sites.read_allowlist, otherwise CONFIRM; non-http(s) links are BLOCKED."""
        normalized = normalize_url(url)
        if normalized is None:
            return "I can only open ordinary web addresses."
        _launch(normalized)
        return f"Opened {urlsplit(normalized).hostname} in your browser."

    def summarize(args: dict[str, Any]) -> str:
        """Everything that will be opened except the scheme, so it reads naturally but hides nothing."""
        normalized = normalize_url(str(args.get("url", "")))
        if normalized is None:
            return f"Open {args.get('url')!r}"
        parts = urlsplit(normalized)
        target = (parts.netloc + parts.path).rstrip("/") + (f"?{parts.query}" if parts.query else "")
        return f"Open {target} in your browser"

    return open_website, summarize, lambda args: website_tier(str(args.get("url", "")), read_allowlist)


def register(registry: Registry, config: dict, services=None) -> None:
    registry.register(Tool(
        name="get_datetime",
        description="Get the current local date, time and day of the week.",
        func=get_datetime,
        tier=Tier.SAFE,
        read_only=True,
    ))
    registry.register(Tool(
        name="get_system_status",
        description="Get battery level and charging state, CPU load and memory use.",
        func=get_system_status,
        tier=Tier.SAFE,
        read_only=True,
    ))
    open_app, app_summary, app_names = make_open_app(config)
    registry.register(Tool(
        name="open_app",
        description="Open an application. Only these names work: " + ", ".join(app_names) + ".",
        func=open_app,
        tier=Tier.SAFE,
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "App name, e.g. 'calculator'."}},
            "required": ["name"],
        },
        summarize=app_summary,
    ))
    close_app, close_summary = make_close_app(config)
    registry.register(Tool(
        name="close_app",
        description="Close an app on the list (like clicking X; it may ask to save). Needs confirmation.",
        func=close_app,
        tier=Tier.CONFIRM,
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "App name, e.g. 'notepad'."},
                "discard_unsaved": {"type": "boolean",
                                    "description": "Only if the user said not to save: press Don't save."},
            },
            "required": ["name"],
        },
        summarize=close_summary,
    ))
    registry.register(Tool(
        name="focus_app",
        description="Switch to an app that's already open (bring its window to the front).",
        func=make_focus_app(config),
        tier=Tier.SAFE,
        read_only=True,  # only changes which window is in front
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ))
    # With the browser module on, websites open in JARVIS's own Chrome (browser_open) so it can read,
    # control and close them; this rung-2 fallback (the user's default browser) is only for without it.
    if not config.get("features", {}).get("browser"):
        open_website, site_summary, site_tier = make_open_website(config)
        registry.register(Tool(
            name="open_website",
            description="Open a website in the user's browser. Sites not on the trusted list need confirmation.",
            func=open_website,
            tier=site_tier,
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Web address, e.g. 'naukri.com'."}},
                "required": ["url"],
            },
            summarize=site_summary,
        ))
    registry.register(Tool(
        name="set_volume",
        description=("Set the whole computer's master volume, 0 to 100. This also changes your own voice; "
                     "for 'turn the music down' use set_music_volume instead."),
        func=set_volume,
        tier=Tier.SAFE,
        parameters={
            "type": "object",
            "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["level"],
        },
        summarize=lambda args: f"Set the volume to {args.get('level')}%",
    ))
    # The media module replaces this with per-app Windows media session control.
    if not config.get("features", {}).get("media"):
        registry.register(Tool(
            name="media_key",
            description="Control whatever media is playing: play, pause, next, previous or stop.",
            func=media_key,
            tier=Tier.SAFE,
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "stop"]}},
                "required": ["action"],
            },
            summarize=lambda args: f"Press the media {args.get('action')} key",
        ))
