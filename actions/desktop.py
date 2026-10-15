"""read_app: read the buttons, fields and text of any open desktop app (Windows UI Automation).

Safety, enforced in code:
- some apps are never read: password managers, web browsers (your Chrome profile holds your accounts;
  JARVIS has its own browser), Windows Settings and Security, sign-in prompts, Registry Editor, Task
  Manager, JARVIS itself, and any window whose title looks like it holds secrets
- password fields and fields named like card, CVV, PIN or one-time-code fields are listed as private:
  their contents are never read
- everything read is scrubbed (emails, phone numbers, keys, tokens, card numbers), wrapped as untrusted
  and taints the turn, so anything JARVIS does after reading needs the user's OK
- the "current app" is remembered so follow-ups ("read it again", "what does it say now") need no name
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from actions.app_ui import looks_secret
from actions.screen import PRIVATE_PROCESSES
from actions.system import normalize_app_name, process_name, resolve_app
from core import uia
from core.privacy import hide_secrets, is_sensitive_name, redact
from core.registry import Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe", "vivaldi.exe",
            "arc.exe", "iexplore.exe"}
SYSTEM_APPS = {"systemsettings.exe", "sechealthui.exe", "securityhealthsystray.exe", "consent.exe",
               "credentialuibroker.exe", "regedit.exe", "taskmgr.exe", "mmc.exe", "lockapp.exe",
               "logonui.exe", "useraccountcontrolsettings.exe"}
# Shell surfaces that sit in front but aren't apps: skipped when working out "the app in front".
SHELL_HOSTS = {"textinputhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
               "searchapp.exe", "lockapp.exe", "widgets.exe", "shellhost.exe"}
SHELL_TITLES = {"program manager", "windows input experience", "start", "search", "task switching"}
WHAT = {"all", "text", "controls"}
# A Store-app frame whose app couldn't be identified: judged by its title instead.
FRAME_BLOCKED = re.compile(r"^(settings|windows security|security|sign in|accounts?)\b", re.IGNORECASE)


def safe_label(win: uia.Window) -> str:
    """How to name a blocked window aloud without repeating a title that may itself be private."""
    if win.exe in BROWSERS:
        return "your browser"
    if win.exe and win.exe != "applicationframehost.exe":
        return win.exe.removesuffix(".exe")
    return "that window"


def blocked_reason(win: uia.Window) -> str | None:
    """Why this window must not be read or touched, or None if it may be."""
    title = win.title.strip()
    if win.pid == os.getpid() or title == "JARVIS":
        return "that's my own window"
    if win.exe in PRIVATE_PROCESSES:
        return "it's a password manager"
    if win.exe in BROWSERS:
        return "I don't read your web browser, since it holds your accounts; I can use my own browser instead"
    if win.exe in SYSTEM_APPS or (win.exe == "applicationframehost.exe" and FRAME_BLOCKED.search(title)):
        return "it's a Windows settings, security or sign-in window"
    if looks_secret(title):
        return "its title looks like it holds passwords or keys"
    return None


def is_shell(win: uia.Window) -> bool:
    return win.exe in SHELL_HOSTS or win.title.strip().lower() in SHELL_TITLES


def _name_matches(spoken: str, win: uia.Window) -> bool:
    want = normalize_app_name(spoken)
    if not want:
        return False
    exe = normalize_app_name(win.exe.removesuffix(".exe"))
    return want == exe or want in normalize_app_name(win.title)


@dataclass
class Snapshot:
    window: uia.Window
    controls: list[uia.Control] = field(default_factory=list)

    def get(self, cid: int) -> uia.Control | None:
        return next((c for c in self.controls if c.id == cid), None)


def is_private_control(c: uia.Control) -> bool:
    return c.password or (c.kind in uia.TYPEABLE and is_sensitive_name(c.name))


def describe(snap: Snapshot, what: str) -> tuple[str, int]:
    """The text the model sees: one line per control (with its number) and the visible text.
    Returns (text, how many secrets were hidden)."""
    lines: list[str] = []
    for c in snap.controls:
        if what == "text" and c.kind not in {"text", "document", "text box"}:
            continue
        if what == "controls" and c.kind == "text":
            continue
        name = f' "{c.name}"' if c.name else ""
        if is_private_control(c):
            lines.append(f"[{c.id}] {c.kind}{name} (private: not read)")
        elif c.kind == "text":
            lines.append(c.name)
        elif c.value and c.value != c.name:
            lines.append(f"[{c.id}] {c.kind}{name}: {c.value}")
        else:
            lines.append(f"[{c.id}] {c.kind}{name}")
    text, hidden = hide_secrets("\n".join(lines))
    return redact(text), hidden


class Desktop:
    def __init__(self, config: dict, services) -> None:
        apps = config.get("apps", {})
        self.allowlist: dict[str, str] = apps.get("allowlist") or {}
        self.aliases: dict[str, str] = apps.get("aliases") or {}
        opts = config.get("desktop") or {}
        self.max_items = int(opts.get("max_items", 150))
        self.max_chars = int(opts.get("max_chars", 4000))
        self.current: uia.Window | None = None
        self.last: Snapshot | None = None

    # ------------------------------------------------------------- finding the app

    def target(self, app: str = "") -> uia.Window:
        """The window to work on: the named app, else the current app, else the app in front."""
        if app.strip():
            win = self._find(app)
            if win is None:
                raise ToolError(f"I can't find {app} open on the screen.")
        elif self.current is not None and (win := uia.window(self.current.hwnd)) is not None:
            pass
        else:
            win = next((w for w in uia.top_windows() if not is_shell(w) and w.pid != os.getpid()
                        and w.title.strip() != "JARVIS"), None)
            if win is None:
                raise ToolError("I don't see an app open. Tell me which one.")
        if why := blocked_reason(win):
            raise ToolError(f"I won't read {safe_label(win)}: {why}.")
        return win

    def _find(self, app: str) -> uia.Window | None:
        windows = [w for w in uia.all_windows() if not is_shell(w)]
        match = resolve_app(app, self.allowlist, self.aliases)
        if match is not None:
            exe = process_name(match[1])
            if found := [w for w in windows if w.exe == exe]:
                return found[0]  # front-most first
        return next((w for w in windows if _name_matches(app, w)), None)

    # ------------------------------------------------------------- tool

    async def read_app(self, app: str = "", what: str = "all") -> str:
        """Read an open app's buttons, fields and visible text."""
        what = what if what in WHAT else "all"
        win = await asyncio.to_thread(self.target, app)
        if uia._user32.IsIconic(win.hwnd):
            self.current = win
            raise ToolError(f"{win.label} is minimised, so there's nothing on screen to read. "
                            "Bring it up first and ask again.")
        controls = await asyncio.to_thread(uia.read, win.hwnd, with_text=what != "controls",
                                           limit=self.max_items, max_chars=self.max_chars, patient=True)
        self.current = win
        self.last = Snapshot(win, controls)
        body, hidden = describe(self.last, what)
        log.info("read %s (%s): %d items, %d secrets hidden", win.exe, what, len(controls), hidden)
        if not body.strip():
            return f"{win.label} ({win.exe}) shows nothing I can read."
        note = f"\n({hidden} thing{'s' if hidden != 1 else ''} that looked like a key or card number hidden.)" \
            if hidden else ""
        return (f"App: {redact(win.label)} ({win.exe}). Numbers in brackets identify buttons and fields.\n"
                f"{body}{note}")


def register(registry: Registry, config: dict, services) -> None:
    desktop = Desktop(config, services)
    services.desktop = desktop
    registry.register(Tool(
        name="read_app",
        description=("Read what an open desktop app shows: its buttons, fields and visible text, e.g. 'read me "
                     "Claude's reply', 'what's in Notepad', 'what can I click here'. app is the app's name; leave "
                     "it empty for the app you last used or the one in front. what: all, text or controls. "
                     "Prefer this over look_at_screen for text in an app. Not for web browsers."),
        func=desktop.read_app,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "App name, e.g. claude, notepad, word. Empty = current."},
                "what": {"type": "string", "enum": sorted(WHAT)},
            },
        },
        summarize=lambda a: f"read {a.get('app') or 'the current app'}",
    ))
