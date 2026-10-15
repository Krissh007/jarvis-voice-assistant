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

type_in_app puts text into a field of an open app (pasted through the clipboard, which is put back):
- the same apps are off-limits; private fields are never typed into; text that looks like a secret
  (a key, token or card number) is never typed
- it asks first for terminals (pasting there can run commands), for pressing Enter outside AI chat
  apps (Enter sends messages and submits forms), and after anything untrusted was read this turn
- it types only while the target window is in front, checked again right before each key press
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

from actions.app_ui import looks_secret
from actions.screen import PRIVATE_PROCESSES
from actions.system import normalize_app_name, process_name, resolve_app
from actions.system import focus_window
from core import textinput, uia
from core.privacy import hide_secrets, is_sensitive_name, redact, secret_in_text
from core.registry import NeedsConfirmation, Registry, Tier, Tool, ToolError

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
# Pasting into a terminal can run commands, so typing there always asks first.
TERMINALS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wsl.exe",
             "bash.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe", "openconsole.exe"}
# AI chat apps: pressing Enter sends a prompt, which is what the user asked for, so it doesn't ask.
AI_CHATS = {"claude.exe", "chatgpt.exe", "copilot.exe", "perplexity.exe"}
CHAT_BOX = re.compile(r"prompt|message|reply|chat|ask|write|type|compose", re.IGNORECASE)
MAX_TYPED = 2000
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
        self.services = services
        apps = config.get("apps", {})
        self.allowlist: dict[str, str] = apps.get("allowlist") or {}
        self.aliases: dict[str, str] = apps.get("aliases") or {}
        opts = config.get("desktop") or {}
        self.max_items = int(opts.get("max_items", 150))
        self.max_chars = int(opts.get("max_chars", 4000))
        self.current: uia.Window | None = None
        self.last: Snapshot | None = None

    # ------------------------------------------------------------- finding the app

    def target(self, app: str = "", verb: str = "read") -> uia.Window:
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
            raise ToolError(f"I won't {verb} {safe_label(win)}: {why}.")
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
        win = await uia.call(self.target, app)
        if uia._user32.IsIconic(win.hwnd):
            self.current = win
            raise ToolError(f"{win.label} is minimised, so there's nothing on screen to read. "
                            "Bring it up first and ask again.")
        controls = await uia.call(uia.read, win.hwnd, with_text=what != "controls",
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

    # ------------------------------------------------------------- typing

    def _tainted(self) -> bool:
        return bool(getattr(getattr(self.services, "dispatcher", None), "tainted", False))

    async def type_in_app(self, text: str, app: str = "", field: str = "", send: bool = False
                          ) -> str | NeedsConfirmation:
        """Type text into a field of an open app; with send, press Enter afterwards."""
        text = text.strip("\r\n") if send else text
        if not text.strip():
            raise ToolError("There's nothing to type.")
        if len(text) > MAX_TYPED:
            raise ToolError(f"That's too long to type in one go (over {MAX_TYPED} characters).")
        if kind := secret_in_text(text):
            raise ToolError(f"That text looks like it contains {kind}, so I won't type it anywhere.")
        win = await uia.call(self.target, app, "type into")
        self.current = win
        reasons = []
        if win.exe in TERMINALS:
            reasons.append("terminal")
        if send and win.exe not in AI_CHATS:
            reasons.append("enter")
        if self._tainted():
            reasons.append("untrusted")
        field_label = f" the {field} field of" if field and not field.strip().isdigit() else ""
        shown = text[:150] + ("..." if len(text) > 150 else "")
        summary = (f'type "{shown}" into{field_label} {redact(win.label)}' + (" and press Enter" if send else ""))
        if reasons:
            async def resume() -> str:
                return await self._type(win, text, field, send)
            return NeedsConfirmation(summary=summary[:300], resume=resume,
                                     step={"app": win.exe, "field": field, "send": send, "why": reasons})
        return await self._type(win, text, field, send)

    async def _type(self, win: uia.Window, text: str, field: str, send: bool) -> str:
        label = await uia.call(self._focus_field, win, field)
        try:
            how = await uia.call(textinput.type_text, win.hwnd, text)
            if send:
                await asyncio.sleep(0.2)
                await uia.call(textinput.press_enter, win.hwnd)
        except RuntimeError as exc:
            raise ToolError(f"I stopped typing: {exc}.") from exc
        log.info("%s %d characters into %s (%s)%s", how, len(text), win.exe, label, " and sent" if send else "")
        return f"Typed into {label} in {safe_label(win)}" + (" and pressed Enter." if send else ".")

    def _focus_field(self, win: uia.Window, field: str) -> str:
        """Bring the app to the front and put the cursor in the right field. Runs on the UIA thread.
        Returns a description of the field; raises ToolError if there's no safe one."""
        focus_window(win.hwnd)
        for _ in range(10):
            if textinput.foreground_root() == win.hwnd:
                break
            time.sleep(0.1)
        else:
            raise ToolError(f"I couldn't bring {safe_label(win)} to the front, so I didn't type anything.")
        time.sleep(0.15)
        now = uia.focused()
        if not field and now is not None and now.editable:
            self._check_private(now.name, now.password)
            return f"the {now.name} box" if now.name else "the text box"
        choice = self._choose(win, field)
        self._check_private(choice.name, choice.password)
        if not uia.set_focus(win.hwnd, choice.id):
            raise ToolError(f"I couldn't put the cursor in {choice.name or 'that field'}.")
        time.sleep(0.15)
        now = uia.focused()  # check where the cursor really landed
        if now is None or not now.editable:
            raise ToolError(f"I couldn't put the cursor in {choice.name or 'that field'}.")
        self._check_private(now.name, now.password)
        return f"the {choice.name} box" if choice.name else "the text box"

    @staticmethod
    def _check_private(name: str, password: bool) -> None:
        if password or is_sensitive_name(name):
            raise ToolError("That's a password or payment field, so I won't type into it.")

    def _choose(self, win: uia.Window, field: str) -> uia.Control:
        """The field named or numbered by `field` (numbers come from the last read_app), else the likeliest one."""
        f = field.strip()
        if f.isdigit() and self.last and self.last.window.hwnd == win.hwnd:
            c = self.last.get(int(f))
            if c is None or c.kind not in uia.TYPEABLE:
                raise ToolError(f"Number {f} isn't a text field. Read the app again and tell me which one.")
            return c
        controls = uia.read(win.hwnd, limit=self.max_items)
        self.last = Snapshot(win, controls)
        boxes = [c for c in controls if c.kind in uia.TYPEABLE and not is_private_control(c)]
        if f and not f.isdigit():
            want = f.lower()
            boxes = [c for c in boxes if want in c.name.lower()]
            if not boxes:
                raise ToolError(f"I can't find a {field} field in {safe_label(win)}.")
        if not boxes:
            raise ToolError(f"I can't find anywhere to type in {safe_label(win)}.")

        def score(c: uia.Control):
            left, top, right, bottom = c.rect
            return (c.kind == "text box" and bool(CHAT_BOX.search(c.name)), c.kind != "dropdown",
                    (right - left) * (bottom - top))
        return max(boxes, key=score)


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
    registry.register(Tool(
        name="type_in_app",
        description=("Type text into an open desktop app, e.g. 'type hello in Notepad', 'ask Claude what a "
                     "monad is' (send=true presses Enter to send it). Types where the cursor is, else into the "
                     "field named or numbered by field (numbers from read_app). app empty = the current app. "
                     "Never for passwords or payment details. Not for web browsers."),
        func=desktop.type_in_app,
        tier=Tier.SAFE,  # asks itself for terminals, Enter outside AI chat apps, and after untrusted text
        self_gated=True,
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Exactly what to type."},
                "app": {"type": "string", "description": "App name; empty = the current app."},
                "field": {"type": "string", "description": "Field name or read_app number; empty = cursor."},
                "send": {"type": "boolean", "description": "Press Enter afterwards (sends a chat message)."},
            },
            "required": ["text"],
        },
        summarize=lambda a: f"type into {a.get('app') or 'the current app'}",
    ))
