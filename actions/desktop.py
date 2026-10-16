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

click_in_app, press_keys, scroll_app, window_action:
- clicks find a button by name or read_app number ("did you mean...?" when unsure); they press it
  through UI Automation when the app allows, else with a real mouse click that must land on that app
- account, sign-out and settings controls are never clicked; risky words (delete, send, pay, buy,
  submit, install...) ask first, except Send in AI chat apps; so does acting after untrusted text
- only listed keys are pressed: saving, closing, printing and Enter (outside AI chat apps) ask first;
  Windows-key combos, Alt+Tab, Ctrl+Alt+Delete and Shift+Delete (permanent delete) never happen
- window_action only moves windows (minimise, maximise, restore, front, left/right half)

wait_for_app waits until an app stops changing (an AI chat app finishing its reply) or shows some
text, and returns only what's new since the message was sent: untrusted, scrubbed like read_app.
In a user-approved plan, a step with fixed arguments doesn't ask again because of untrusted text
(APPROVED_STEP); risky steps still ask.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import difflib
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from actions.app_ui import looks_secret
from actions.screen import PRIVATE_PROCESSES
from actions.system import normalize_app_name, process_name, resolve_app
from actions.system import focus_window
from core import chat, textinput, uia
from core.privacy import hide_secrets, is_blocked_control, is_sensitive_name, redact, secret_in_text
from core.registry import APPROVED_STEP, NeedsConfirmation, Registry, Tier, Tool, ToolError

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
WHAT = {"all", "text", "controls", "latest"}
# Pasting into a terminal can run commands, so typing there always asks first.
TERMINALS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "wsl.exe",
             "bash.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe", "openconsole.exe"}
# AI chat apps: pressing Enter sends a prompt, which is what the user asked for, so it doesn't ask.
AI_CHATS = {"claude.exe", "chatgpt.exe", "copilot.exe", "perplexity.exe"}
CHAT_BOX = re.compile(r"prompt|message|reply|chat|ask|write|type|compose", re.IGNORECASE)
MAX_TYPED = 2000
MAX_TYPED_EDITOR = 10000  # into a code editor: a whole file goes in one paste, so no part gets lost
# A terminal panel inside an app (VS Code, Cursor...): xterm.js's input has this class. Pasting there can run
# commands, so it's treated like a terminal app.
TERMINAL_CLASS = re.compile(r"xterm", re.IGNORECASE)
TERMINAL_NAME = re.compile(r"^terminal\b", re.IGNORECASE)
IDES = {"code.exe", "cursor.exe", "windsurf.exe", "code - insiders.exe", "idea64.exe", "pycharm64.exe",
        "webstorm64.exe", "rider64.exe", "clion64.exe", "goland64.exe", "phpstorm64.exe", "devenv.exe"}
# Editors whose title starts with the open file: "● index.html - portfolio - Visual Studio Code".
TITLE_FILE_IDES = {"code.exe", "cursor.exe", "windsurf.exe", "code - insiders.exe"}
WHOLE_PAGE_ID = "RootWebArea"  # a Chromium/Electron app's whole page: never a place to type
REPEAT_GUARD_S = 10  # the same text into the same window this soon again is a repeated call, not a request


def is_terminal_field(win: uia.Window, name: str, class_name: str) -> bool:
    """A terminal inside an app: by xterm's class, or an IDE field named "Terminal..."."""
    return bool(TERMINAL_CLASS.search(class_name)) or (win.exe in IDES and bool(TERMINAL_NAME.search(name)))


def is_whole_page(win: uia.Window, kind: str, name: str, auto_id: str) -> bool:
    """The whole window's page (Chromium's root document, named like the window), not a text field."""
    return auto_id == WHOLE_PAGE_ID or (kind == "document" and name.strip() == win.title.strip() and bool(name.strip()))


def open_file(win: uia.Window) -> str:
    """The file an editor has open, from its title ("" when the title doesn't say)."""
    if win.exe not in TITLE_FILE_IDES:
        return ""
    title = win.title.strip().lstrip("●").strip()
    return title.split(" - ")[0].strip() if " - " in title else ""


def file_key(name: str) -> str:
    """A file name to compare: lower case, without its folders."""
    return re.split(r"[\\/]", name.strip())[-1].strip().lower()


def same_file(field: str, name: str) -> bool:
    want = file_key(field)
    return bool(want) and want in name.lower()


def field_words(name: str) -> str:
    """How to name a field aloud: short names as they are; long accessibility notices as 'the text box'."""
    name = " ".join(name.split())
    return f"the {name} box" if name and len(name) <= 40 else "the text box"


def typed_words(text: str, limit: int = 150) -> str:
    """How to describe text to be typed: the words if short and on one line, else its size and first line."""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    flat = " ".join(text.split())
    if len(lines) <= 2 and len(flat) <= limit:
        return f'"{flat}"'
    first = lines[0].strip() if lines else flat
    first = first[:60] + ("..." if len(first) > 60 else "")
    return f'{len(lines)} line{"s" if len(lines) != 1 else ""} ({len(text)} characters) starting "{first}"'
RISKY = re.compile(
    r"\b(delete|remove|erase|discard|save|print|close|exit|quit|send|submit|post|publish|share|pay|purchase|buy|order|checkout|transfer|"
    r"confirm|accept|agree|allow|install|uninstall|update now|restart|shut ?down|reset|format|empty|clear all|"
    r"unsubscribe|sign ?in|log ?in|sign ?out|log ?out|subscribe|join|leave|end call|hang up|block|report)\b",
    re.IGNORECASE)
ACCOUNT = re.compile(r"\b(account|profile|avatar|switch user|manage (your )?google|passwords?|billing)\b", re.IGNORECASE)
CLICKABLE = {"button", "link", "menu item", "tab", "list item", "tree item", "checkbox", "radio button",
             "dropdown", "slider", "text box", "document"}

# Keys JARVIS may press. Anything else is refused.
PLAIN_KEYS = {"enter", "tab", "escape", "space", "backspace", "delete", "up", "down", "left", "right", "home",
              "end", "pageup", "pagedown", "f2", "f3", "f5", "f11"}
SAFE_COMBOS = ({f"ctrl+{c}" for c in "acxvzyfbiuntlkhge0123456789"}  # not ctrl+d: deletes in some apps
               | {"ctrl+plus", "ctrl+minus", "ctrl+tab", "ctrl+shift+tab", "shift+tab", "ctrl+home", "ctrl+end",
                  "ctrl+left", "ctrl+right", "ctrl+backspace", "ctrl+delete", "ctrl+shift+t", "ctrl+shift+n",
                  "shift+enter", "alt+left", "alt+right", "alt+up", "shift+f10"}
               | {f"shift+{k}" for k in ("left", "right", "up", "down", "home", "end", "pageup", "pagedown")}
               | {f"ctrl+shift+{k}" for k in ("left", "right", "home", "end")})
ASK_KEYS = {"ctrl+s": "save", "ctrl+shift+s": "save as", "ctrl+w": "close the tab", "ctrl+f4": "close the tab",
            "alt+f4": "close the window", "ctrl+p": "print", "ctrl+enter": "send", "ctrl+r": "reload",
            "ctrl+shift+r": "reload"}
REPEATABLE = {"tab", "shift+tab", "up", "down", "left", "right", "pageup", "pagedown", "backspace", "space",
              "ctrl+tab", "ctrl+shift+tab", "ctrl+z", "ctrl+y", "ctrl+plus", "ctrl+minus"}
KEY_ALIASES = {"control": "ctrl", "ctl": "ctrl", "return": "enter", "esc": "escape", "del": "delete",
               "page up": "pageup", "page down": "pagedown", "pgup": "pageup", "pgdn": "pagedown",
               "arrow up": "up", "arrow down": "down", "arrow left": "left", "arrow right": "right",
               "up arrow": "up", "down arrow": "down", "left arrow": "left", "right arrow": "right",
               "option": "alt", "spacebar": "space", "=": "plus", "-": "minus"}
WAIT_POLL_S = 1.5
WAIT_STABLE_POLLS = 2  # unchanged this many polls in a row = finished
LAUNCH_WAIT_S = 10  # an app that's running but has no window yet (just opened) gets this long
_now = time.monotonic  # the wait clock (tests replace it)


def new_text(before: str, after: str) -> str:
    """The words in `after` that weren't in `before`, in order; separate passages joined by ' ... '."""
    a, b = before.split(), after.split()
    parts = []
    for op, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("insert", "replace"):
            parts.append(" ".join(b[j1:j2]))
    worded = [p for p in parts if re.search(r"[^\W\d_]{2}", p)]  # drop status-bar numbers ("Line 2, 32")
    return " ... ".join(worded or parts)


WINDOW_ACTIONS = {"minimise", "maximise", "restore", "front", "left half", "right half"}


def normalize_keys(keys: str) -> str:
    """'Control + Shift + T' -> 'ctrl+shift+t'; modifiers first in a fixed order."""
    k = " ".join(keys.lower().split())
    k = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "+", k)  # "ctrl-n"
    for alias, real in sorted(KEY_ALIASES.items(), key=lambda kv: -len(kv[0])):
        k = re.sub(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", real, k)
    parts = [p for p in re.split(r"\s*\+\s*|\s+", k) if p]
    mods = [m for m in ("ctrl", "shift", "alt", "win", "windows") if m in parts]
    rest = [p for p in parts if p not in mods]
    if "windows" in mods:
        mods = [m if m != "windows" else "win" for m in mods]
    return "+".join(mods + rest)


def key_rule(combo: str, exe: str) -> tuple[str, str]:
    """('ok' | 'ask' | 'no', reason) for pressing `combo` in app `exe`."""
    parts = combo.split("+")
    if "win" in parts or len(parts) - sum(p in ("ctrl", "shift", "alt") for p in parts) != 1:
        return "no", "I only press one ordinary key at a time, and never Windows-key shortcuts"
    if combo == "shift+delete":
        return "no", "Shift+Delete deletes permanently"
    if combo in ("alt+tab", "ctrl+alt+delete", "ctrl+shift+escape"):
        return "no", "that switches or controls Windows itself"
    if exe in TERMINALS:
        return "ask", "it's a terminal"
    if combo in ASK_KEYS:
        return "ask", ASK_KEYS[combo]
    if combo == "enter":
        return ("ok", "") if exe in AI_CHATS else ("ask", "Enter can send or submit")
    if combo == "delete" and exe == "explorer.exe":
        return "ask", "it deletes files"
    if combo in PLAIN_KEYS or combo in SAFE_COMBOS or (len(combo) == 1 and combo.isalnum()):
        return "ok", ""
    return "no", f"{combo} isn't one of the keys I press"


def match_controls(name: str, controls: list[uia.Control]) -> tuple[list[uia.Control], list[str]]:
    """Controls whose name matches `name` (best kind of match only), and close names for "did you mean"."""
    want = " ".join(name.lower().split())
    named = [c for c in controls if c.name and c.kind in CLICKABLE]
    for test in (lambda n: n == want, lambda n: n.startswith(want), lambda n: want in n,
                 lambda n: all(w in n for w in want.split())):
        found = [c for c in named if test(c.name.lower())]
        if found:
            return found, []
    names = list(dict.fromkeys(c.name for c in named))
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    if not close:
        lowered = {n.lower(): n for n in names}
        close = [lowered[m] for m in difflib.get_close_matches(want, list(lowered), n=3, cutoff=0.6)]
    return [], close
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
        self.chat_profiles = chat.load_profiles(config)  # exe -> where its messages are (config chat_apps)
        self.chat_max_chars = int((config.get("chat_apps") or {}).get("max_chars", 6000))
        self.current: uia.Window | None = None
        self.last: Snapshot | None = None
        self.baseline: dict[int, str] = {}  # window -> its text just before a message was sent
        self.sent_after: dict[int, int] = {}  # chat window -> newest message number just before sending
        self.mismatch_warned: set[int] = set()  # windows already warned about (once per window per session)
        self.repeat_guard_s = float(opts.get("repeat_guard_s", REPEAT_GUARD_S))
        self.max_typed = int(opts.get("max_typed", MAX_TYPED))
        self.max_typed_editor = int(opts.get("max_typed_editor", MAX_TYPED_EDITOR))
        self.recent_typing: dict[tuple, float] = {}  # (window, text, field, send) -> when it was typed

    # ------------------------------------------------------------- finding the app

    def target(self, app: str = "", verb: str = "read") -> uia.Window:
        """The window to work on: the named app, else the current app, else the app in front."""
        if app.strip():
            win = self._find(app)
            if win is None and self._launching(app):
                for _ in range(int(LAUNCH_WAIT_S / 0.5)):
                    time.sleep(0.5)
                    if win := self._find(app):
                        time.sleep(0.5)  # let it finish drawing
                        break
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

    def _launching(self, app: str) -> bool:
        """The named app's program is running but has no window yet (it was probably just opened)."""
        match = resolve_app(app, self.allowlist, self.aliases)
        if match is None or "://" in match[1]:
            return False
        exe = process_name(match[1])
        try:
            import psutil

            return any((p.info["name"] or "").lower() == exe for p in psutil.process_iter(["name"]))
        except Exception:
            return False

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
        if what == "latest":
            self.current = win
            if (reply := await self._latest_reply(win)) is not None:
                return reply
            what = "text"  # not a chat app JARVIS knows, or its layout didn't match: read the window
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

    # ------------------------------------------------------------- chat apps

    async def _messages(self, win: uia.Window) -> tuple[chat.Profile, list[chat.Message]] | None:
        """The window's chat messages, or None (with a warning) if its configured profile doesn't match."""
        profile = self.chat_profiles.get(win.exe)
        if profile is None:
            return None
        try:
            messages = await uia.call(chat.read_messages, win.hwnd, profile)
        except chat.NoMatch as exc:
            self._mismatch(win, str(exc))
            return None
        if not any(m.role != "unknown" for m in messages):
            self._mismatch(win, "no message starts with a known role label")
            return None
        return profile, messages

    def _mismatch(self, win: uia.Window, reason: str) -> None:
        """Warn once per window per session that its chat profile didn't fit; later times go to debug."""
        warn = win.hwnd not in self.mismatch_warned
        self.mismatch_warned.add(win.hwnd)
        log.log(logging.WARNING if warn else logging.DEBUG,
                "chat profile for %s didn't match (%s); falling back to reading the whole window%s",
                win.exe, reason, "" if warn else " (already warned for this window)")

    async def _latest_reply(self, win: uia.Window) -> str | None:
        """The newest finished reply in a chat app, scrubbed; None to fall back to a whole-window read."""
        start = time.perf_counter()
        found = await self._messages(win)
        if found is None:
            return None
        profile, messages = found
        who = profile.reply_role.capitalize()
        reply = chat.latest(messages, profile.reply_role, finished=True)
        newest = chat.latest(messages)
        notes = []
        if newest is not None and newest.streaming:
            notes.append(f"{who} is still writing a newer reply.")
        elif newest is not None and reply is not None and newest.role != profile.reply_role \
                and newest.number > reply.number:
            notes.append("Your newest message hasn't been answered yet.")
        log.info("latest reply in %s: %s in %.0f ms", win.exe,
                 f"message {reply.number}, {len(reply.text)} chars" if reply else "none",
                 (time.perf_counter() - start) * 1000)
        if reply is None:
            return f"There's no finished reply from {who} in this chat yet. " + " ".join(notes)
        text, hidden = hide_secrets(reply.text)
        text = redact(text)
        if len(text) > self.chat_max_chars:
            text = text[:self.chat_max_chars].rstrip() + " ... (cut short)"
        if hidden:
            notes.append(f"{hidden} thing{'s' if hidden != 1 else ''} that looked like a key or card number hidden.")
        of = f" of {reply.total}" if reply.total else ""
        return (f"App: {redact(win.label)} ({win.exe}). {who}'s latest reply (message {reply.number}{of}):\n"
                f"{text}" + (f"\n({' '.join(notes)})" if notes else ""))

    async def _wait_for_reply(self, win: uia.Window, timeout: int) -> str | None:
        """Wait for a new finished reply in a chat app; None (with a warning) to fall back to text diffing."""
        found = await self._messages(win)
        if found is None:
            return None
        profile, messages = found
        who = profile.reply_role.capitalize()
        base = self.sent_after.pop(win.hwnd, None)
        if base is None:  # nothing sent through JARVIS: wait for an answer to the user's newest message
            mine = [m.number for m in messages if m.role not in (profile.reply_role, "unknown") and not m.streaming]
            base = max(mine, default=0)
        start = _now()
        while True:
            reply = chat.latest(messages, profile.reply_role, finished=True)
            if reply is not None and reply.number > base:
                took = round(_now() - start)
                log.info("waited %ds for a reply in %s (message %d)", took, win.exe, reply.number)
                text = redact(hide_secrets(reply.text)[0])
                if len(text) > self.chat_max_chars:
                    text = text[:self.chat_max_chars].rstrip() + " ... (cut short)"
                return f"{who} replied after {took} seconds:\n{text}"
            if _now() - start >= timeout:
                writing = chat.latest(messages)
                if writing is not None and writing.streaming:
                    return (f"{who} was still writing after {timeout} seconds. So far:\n"
                            + redact(hide_secrets(writing.text)[0])[:self.chat_max_chars])
                return f"{who} hasn't replied after {timeout} seconds."
            await asyncio.sleep(WAIT_POLL_S)
            if uia.window(win.hwnd) is None:
                raise ToolError(f"{safe_label(win)} was closed while I waited.")
            found = await self._messages(win)
            if found is None:
                return None
            profile, messages = found

    # ------------------------------------------------------------- typing

    def _tainted(self) -> bool:
        if APPROVED_STEP.get():
            return False  # a plan step the user approved word for word
        return bool(getattr(getattr(self.services, "dispatcher", None), "tainted", False))

    def _text(self, win: uia.Window) -> str:
        """The window's visible text, scrubbed (runs on the UIA thread)."""
        controls = uia.read(win.hwnd, with_text=True, limit=self.max_items * 2, max_chars=self.max_chars * 2)
        return describe(Snapshot(win, controls), "text")[0]

    async def type_in_app(self, text: str, app: str = "", field: str = "", send: bool = False
                          ) -> str | NeedsConfirmation:
        """Type text into a field of an open app; with send, press Enter afterwards."""
        text = text.strip("\r\n") if send else text
        if not text.strip():
            raise ToolError("There's nothing to type.")
        if kind := secret_in_text(text):
            raise ToolError(f"That text looks like it contains {kind}, so I won't type it anywhere.")
        win = await uia.call(self.target, app, "type into")
        self.current = win
        limit = self.max_typed_editor if win.exe in IDES else self.max_typed
        if len(text) > limit:
            raise ToolError(f"That's too long to type in one go (over {limit} characters). Type it in parts, in "
                            "order, into the same place, and leave nothing out.")
        key = (win.hwnd, " ".join(text.split()), field.strip().lower(), bool(send))
        if (when := self.recent_typing.get(key)) is not None and _now() - when < self.repeat_guard_s:
            log.info("type_in_app: same text into %s %.0f s ago; not typing it twice", win.exe, _now() - when)
            return (f"I already typed that into {safe_label(win)} {_now() - when:.0f} seconds ago, so I didn't type "
                    "it again.")
        reasons = []
        if win.exe in TERMINALS:
            reasons.append("terminal")
        if send and win.exe not in AI_CHATS:
            reasons.append("enter")
        if self._tainted():
            reasons.append("untrusted")
        field_label = f" the {field} field of" if field and not field.strip().isdigit() else ""
        summary = (f"type {typed_words(text)} into{field_label} {redact(win.label)}"
                   + (" and press Enter" if send else ""))
        if reasons:
            async def resume() -> str:
                return await self._type(win, text, field, send)
            return NeedsConfirmation(summary=summary[:300], resume=resume,
                                     step={"app": win.exe, "field": field, "send": send, "why": reasons})
        return await self._type(win, text, field, send)

    async def _type(self, win: uia.Window, text: str, field: str, send: bool,
                    allow_terminal: bool = False) -> str | NeedsConfirmation:
        key = (win.hwnd, " ".join(text.split()), field.strip().lower(), bool(send))
        self.recent_typing[key] = _now()  # a repeat that arrives while this one is typing is caught too
        try:
            label, terminal = await uia.call(self._focus_field, win, field)
        except BaseException:
            self.recent_typing.pop(key, None)
            raise
        if terminal and len(text) > self.max_typed:
            self.recent_typing.pop(key, None)
            raise ToolError(f"That's too long to paste into a terminal (over {self.max_typed} characters).")
        if terminal and not allow_terminal and win.exe not in TERMINALS:
            self.recent_typing.pop(key, None)

            async def resume() -> str | NeedsConfirmation:
                return await self._type(win, text, field, send, allow_terminal=True)
            return NeedsConfirmation(
                summary=(f"type {typed_words(text)} into the terminal in {safe_label(win)}"
                         + (" and press Enter" if send else "") + " (pasting into a terminal can run commands)")[:300],
                resume=resume, step={"app": win.exe, "field": "terminal", "send": send, "why": ["terminal"]})
        try:
            how = await uia.call(textinput.type_text, win.hwnd, text)
            if send:
                await asyncio.sleep(0.2)
                try:
                    if (found := await self._messages(win)) is not None and found[1]:
                        done = [m.number for m in found[1] if not m.streaming]
                        self.sent_after[win.hwnd] = max(done, default=0)  # wait_for_app wants a newer reply
                    else:
                        self.baseline[win.hwnd] = await uia.call(self._text, win)  # so wait_for_app finds the reply
                except Exception:
                    log.exception("couldn't read %s before sending", win.exe)
                await uia.call(textinput.press_enter, win.hwnd)
        except RuntimeError as exc:
            self.recent_typing.pop(key, None)  # it didn't go through: asking again must work
            raise ToolError(f"I stopped typing: {exc}.") from exc
        self.recent_typing[key] = _now()
        log.info("%s %d characters into %s (%s)%s", how, len(text), win.exe, label, " and sent" if send else "")
        return (f"Typed {typed_words(text)} into {label} in {safe_label(win)}"
                + (" and pressed Enter." if send else "."))

    def _focus_field(self, win: uia.Window, field: str) -> tuple[str, bool]:
        """Bring the app to the front and put the cursor in the right field. Runs on the UIA thread.
        Returns (how to name the field, whether it's a terminal); raises ToolError if there's no safe one."""
        focus_window(win.hwnd)
        for _ in range(10):
            if textinput.foreground_root() == win.hwnd:
                break
            time.sleep(0.1)
        else:
            raise ToolError(f"I couldn't bring {safe_label(win)} to the front, so I didn't type anything.")
        time.sleep(0.15)
        now = uia.focused()
        usable = now is not None and now.editable and not is_whole_page(win, now.kind, now.name, now.auto_id)
        terminal_now = usable and is_terminal_field(win, now.name, now.class_name)
        f = field.strip()
        # In a code editor the file is checked, except for a read_app number or the cursor in the terminal
        # (which asks first).
        if win.exe in IDES and not f.isdigit() and not (not f and terminal_now):
            return self._focus_file(win, f, now if usable and not terminal_now else None)
        if not f and usable:
            self._check_private(now.name, now.password)
            return field_words(now.name), terminal_now
        return self._focus_named(win, field)

    def _focus_file(self, win: uia.Window, field: str, editor: uia.Focused | None) -> tuple[str, bool]:
        """In a code editor, type only into the file asked for (field = its name), so code can't land in
        whatever file happens to be open. `editor` is the focused editor field, if the cursor is in one."""
        current = open_file(win)
        if not field:
            where = f" The file open now is {current}." if current else ""
            raise ToolError(f"Tell me which file this goes in (field = its file name, e.g. index.html), so it "
                            f"can't land in the wrong one.{where} If that isn't the right file, ask the user to "
                            "open the right one and click inside it first.")
        if TERMINAL_NAME.search(field):  # "terminal": the usual search by name, which asks first
            return self._focus_named(win, field)
        if editor is not None and (same_file(field, editor.name) or same_file(field, current)):
            self._check_private(editor.name, editor.password)
            return (current or field), False
        controls = uia.read(win.hwnd, limit=self.max_items)
        self.last = Snapshot(win, controls)
        editors = [c for c in controls if c.kind in uia.TYPEABLE and not is_private_control(c)
                   and not is_whole_page(win, c.kind, c.name, c.auto_id)
                   and not is_terminal_field(win, c.name, c.class_name)]
        named = [c for c in editors if same_file(field, c.name)]
        if not named and same_file(field, current) and editors:
            if len(editors) > 1:  # the file's open, but its editor isn't named after it: which one is it?
                raise ToolError(f"{field} is open in {safe_label(win)}, but the cursor isn't in it. Ask the user "
                                f"to click inside {field}, then try again.")
            named = editors  # its one editor just isn't named after it
        if not named:
            now_open = f" (the file open now is {current})" if current else ""
            raise ToolError(f"{field} isn't open in {safe_label(win)}{now_open}, so I didn't type anything. Ask "
                            f"the user to open {field} and click inside it, then try again.")
        if len(named) > 1:
            raise ToolError(f"{field} is open in more than one place in {safe_label(win)}. Ask the user to click "
                            "inside the one to use, then try again.")
        self._land(win, named[0])
        return (current if same_file(field, current) else named[0].name) or field, False

    def _focus_named(self, win: uia.Window, field: str) -> tuple[str, bool]:
        choice = self._choose(win, field)
        now = self._land(win, choice)
        return field_words(choice.name), is_terminal_field(win, now.name, now.class_name)

    def _land(self, win: uia.Window, choice: uia.Control) -> uia.Focused:
        """Put the cursor in `choice` and check where it really landed."""
        self._check_private(choice.name, choice.password)
        if not uia.set_focus(win.hwnd, choice.id):
            raise ToolError(f"I couldn't put the cursor in {choice.name or 'that field'}.")
        time.sleep(0.15)
        now = uia.focused()  # check where the cursor really landed
        if now is None or not now.editable or is_whole_page(win, now.kind, now.name, now.auto_id):
            raise ToolError(f"I couldn't put the cursor in {choice.name or 'that field'}.")
        self._check_private(now.name, now.password)
        return now

    # ------------------------------------------------------------- waiting

    async def wait_for_app(self, app: str = "", until: str = "", timeout: int = 60) -> str:
        """Wait until an app stops changing (or shows `until`); return what's new since the message was sent."""
        timeout = max(5, min(int(timeout or 60), 180))
        win = await uia.call(self.target, app)
        self.current = win
        if not until and win.exe in self.chat_profiles:
            if (reply := await self._wait_for_reply(win, timeout)) is not None:
                return reply
        before = self.baseline.pop(win.hwnd, None)
        if before is None:
            before = await uia.call(self._text, win)
        start = _now()
        last, stable, changed, text = None, 0, False, before
        while _now() - start < timeout:
            await asyncio.sleep(WAIT_POLL_S)
            if uia.window(win.hwnd) is None:
                raise ToolError(f"{safe_label(win)} was closed while I waited.")
            text = await uia.call(self._text, win)
            fresh = new_text(before, text)
            if until and until.lower() in fresh.lower():
                break
            changed = changed or text != before
            stable = stable + 1 if text == last else 0
            last = text
            if changed and not until and stable >= WAIT_STABLE_POLLS:
                break
        else:
            fresh = new_text(before, text)
            if not fresh:
                return f"Nothing changed in {safe_label(win)} for {timeout} seconds."
            log.info("waited %ds for %s: still changing", timeout, win.exe)
            return (f"{safe_label(win)} was still changing after {timeout} seconds. New text so far:\n"
                    + fresh[-self.max_chars:])
        took = round(_now() - start)
        log.info("waited %ds for %s", took, win.exe)
        fresh = new_text(before, text)
        return (f"{safe_label(win)} finished after {took} seconds. New text:\n{fresh[-self.max_chars:]}"
                if fresh else f"{safe_label(win)} changed, but there's no new text.")

    # ------------------------------------------------------------- clicking

    async def click_in_app(self, target: str, app: str = "", double: bool = False, right: bool = False
                           ) -> str | NeedsConfirmation:
        """Click a button, link, tab or item, found by name or read_app number."""
        win = await uia.call(self.target, app, "click in")
        self.current = win
        control = await uia.call(self._pick_control, win, target)
        name = control.name or f"number {control.id}"
        if is_private_control(control):
            raise ToolError("That's a password or payment field, so I won't touch it.")
        if is_blocked_control(control.name) or ACCOUNT.search(control.name):
            raise ToolError(f"I won't click {name}: it manages accounts or settings.")
        risky = RISKY.search(control.name)
        chat_send = win.exe in AI_CHATS and re.fullmatch(r"(send|submit)( message| prompt)?", control.name.strip(),
                                                         re.IGNORECASE)
        what = ("double-click" if double else "right-click" if right else "click") + f" {control.kind} \"{name}\""
        summary = f"{what} in {redact(win.label)}"[:300]
        if (risky and not chat_send) or self._tainted():
            async def resume() -> str:
                return await self._click(win, control, double, right)
            return NeedsConfirmation(summary=summary, resume=resume, step={"app": win.exe, "target": control.name})
        return await self._click(win, control, double, right)

    def _pick_control(self, win: uia.Window, target: str) -> uia.Control:
        t = target.strip().lstrip("#")
        if t.isdigit():
            if not (self.last and self.last.window.hwnd == win.hwnd):
                raise ToolError("I don't know which one that number is. Let me read the app first.")
            c = self.last.get(int(t))
            if c is None:
                raise ToolError(f"There's no number {t}. Read the app again and tell me which one.")
            return c
        controls = uia.read(win.hwnd, limit=self.max_items, patient=True)
        self.last = Snapshot(win, controls)
        found, close = match_controls(t, controls)
        if not found:
            if close:
                raise ToolError(f"I can't find {target} in {safe_label(win)}. Did you mean "
                                + " or ".join(f'"{c}"' for c in close) + "?")
            raise ToolError(f"I can't find {target} in {safe_label(win)}.")
        names = list(dict.fromkeys(c.name for c in found))
        if len(names) > 1:
            shown = ", ".join(f'"{n}"' for n in names[:4])
            raise ToolError(f"Several things match {target}: {shown}. Which one?")
        if len(found) > 1:
            ids = ", ".join(str(c.id) for c in found[:5])
            raise ToolError(f'There are {len(found)} called "{found[0].name}" (numbers {ids}). Which number?')
        return found[0]

    async def _click(self, win: uia.Window, control: uia.Control, double: bool, right: bool) -> str:
        how = await uia.call(self._do_click, win, control, double, right)
        log.info("%s %s %r in %s", how, control.kind, control.name, win.exe)
        await asyncio.sleep(0.3)
        return f"{how.capitalize()} {control.name or 'it'} in {safe_label(win)}."

    def _do_click(self, win: uia.Window, control: uia.Control, double: bool, right: bool) -> str:
        self._front(win)
        if not (double or right):
            if word := uia.press(win.hwnd, control.id):
                return word
        rect = uia.current_rect(win.hwnd, control.id) or control.rect
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        try:
            textinput.click_at(win.hwnd, x, y, "right" if right else "left", double)
        except RuntimeError as exc:
            raise ToolError(f"I didn't click: {exc}.") from exc
        return "double-clicked" if double else "right-clicked" if right else "clicked"

    def _front(self, win: uia.Window) -> None:
        focus_window(win.hwnd)
        for _ in range(10):
            if textinput.foreground_root() == win.hwnd:
                time.sleep(0.1)
                return
            time.sleep(0.1)
        raise ToolError(f"I couldn't bring {safe_label(win)} to the front.")

    # ------------------------------------------------------------- keys, scrolling, windows

    async def press_keys(self, keys: str, app: str = "", times: int = 1) -> str | NeedsConfirmation:
        """Press a key or shortcut in an open app."""
        combo = normalize_keys(keys)
        if not combo:
            raise ToolError("Which key?")
        win = await uia.call(self.target, app, "press keys in")
        self.current = win
        rule, why = key_rule(combo, win.exe)
        if rule == "no":
            raise ToolError(f"I won't press {keys}: {why}.")
        times = max(1, min(int(times or 1), 20)) if combo in REPEATABLE else 1
        shown = combo.replace("+", " + ").title()
        summary = f"press {shown}" + (f" {times} times" if times > 1 else "") + f" in {redact(win.label)}"
        if rule == "ask" or self._tainted():
            async def resume() -> str:
                return await self._press(win, combo, times, shown)
            return NeedsConfirmation(summary=summary[:300], resume=resume, step={"app": win.exe, "keys": combo})
        return await self._press(win, combo, times, shown)

    async def _press(self, win: uia.Window, combo: str, times: int, shown: str) -> str:
        await uia.call(self._front, win)
        try:
            await uia.call(textinput.send_combo, win.hwnd, combo, times)
        except RuntimeError as exc:
            raise ToolError(f"I stopped: {exc}.") from exc
        log.info("pressed %s x%d in %s", combo, times, win.exe)
        return f"Pressed {shown}" + (f" {times} times" if times > 1 else "") + f" in {safe_label(win)}."

    async def scroll_app(self, direction: str = "down", app: str = "", amount: int = 5) -> str:
        """Scroll an app up or down with the mouse wheel over the middle of its window."""
        up = direction.lower().strip() in {"up", "top", "back"}
        win = await uia.call(self.target, app, "scroll")
        self.current = win
        notches = max(1, min(int(amount or 5), 30))

        def do() -> None:
            self._front(win)
            rect = wintypes.RECT()
            uia._user32.GetWindowRect(win.hwnd, ctypes.byref(rect))
            textinput.scroll_at(win.hwnd, (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2,
                                notches if up else -notches)
        try:
            await uia.call(do)
        except RuntimeError as exc:
            raise ToolError(f"I couldn't scroll: {exc}.") from exc
        return f"Scrolled {'up' if up else 'down'} in {safe_label(win)}."

    async def window_action(self, action: str, app: str = "") -> str:
        """Minimise, maximise, restore, bring to front, or snap an app's window to half the screen."""
        action = {"minimize": "minimise", "maximize": "maximise", "hide": "minimise", "focus": "front",
                  "show": "restore", "left": "left half", "right": "right half"}.get(action.lower().strip(),
                                                                                    action.lower().strip())
        if action not in WINDOW_ACTIONS:
            raise ToolError(f"I can {', '.join(sorted(WINDOW_ACTIONS))} a window.")
        win = await uia.call(self._window_target, app)
        self.current = win if not blocked_reason(win) else self.current
        await uia.call(_move_window, win.hwnd, action)
        return f"Done: {safe_label(win) if blocked_reason(win) else redact(win.label)}, {action}."

    def _window_target(self, app: str) -> uia.Window:
        """Like target(), but any app's window may be moved (moving shows nothing of what's inside)."""
        if app.strip():
            win = self._find(app)
            if win is None:
                raise ToolError(f"I can't find {app} open.")
            return win
        return self.target("", "move")

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
        boxes = [c for c in controls if c.kind in uia.TYPEABLE and not is_private_control(c)
                 and not is_whole_page(win, c.kind, c.name, c.auto_id)]
        if f and not f.isdigit():
            want = f.lower()
            boxes = [c for c in boxes if want in c.name.lower()]
            if not boxes:
                raise ToolError(f"I can't find a {field} field in {safe_label(win)}.")
            return max(boxes, key=lambda c: (c.kind == "text box", _area(c)))
        boxes = [c for c in boxes if not is_terminal_field(win, c.name, c.class_name)]  # never guess a terminal
        if not boxes:
            raise ToolError(f"I can't find anywhere to type in {safe_label(win)}. Click where the text should go, "
                            "then ask me again.")
        if len(boxes) == 1:
            return boxes[0]
        chat_boxes = [c for c in boxes if c.kind == "text box" and CHAT_BOX.search(c.name)]
        if len(chat_boxes) == 1:
            return chat_boxes[0]
        raise ToolError(f"The cursor isn't in a text field in {safe_label(win)}, and there are {len(boxes)} places "
                        "I could type. Click where the text should go, then ask me again.")


def _area(c: uia.Control) -> int:
    left, top, right, bottom = c.rect
    return (right - left) * (bottom - top)


def _move_window(hwnd: int, action: str) -> None:
    user32 = uia._user32
    if action == "minimise":
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return
    if action == "maximise":
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    elif action == "restore":
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    elif action in ("left half", "right half"):
        user32.ShowWindow(hwnd, 9)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))  # nearest monitor
        work = info.rcWork
        half = (work.right - work.left) // 2
        left = work.left if action == "left half" else work.left + half
        user32.MoveWindow(hwnd, left, work.top, half, work.bottom - work.top, True)
    focus_window(hwnd)


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _app_words(args: dict) -> str:
    return str(args.get("app") or "the current app")


def _type_summary(args: dict) -> str:
    """Plan line: the exact words, so the user approves what will be typed."""
    field = f" the {args['field']} field of" if args.get("field") and not str(args["field"]).isdigit() else ""
    return (f"type {typed_words(str(args.get('text', '')))} into{field} {_app_words(args)}"
            + (" and press Enter" if args.get("send") else ""))


def register(registry: Registry, config: dict, services) -> None:
    desktop = Desktop(config, services)
    services.desktop = desktop
    registry.register(Tool(
        name="read_app",
        description=("Read what an open desktop app shows: its buttons, fields and visible text, e.g. 'read me "
                     "Claude's reply', 'what's in Notepad', 'what can I click here'. app is the app's name; leave "
                     "it empty for the app you last used or the one in front. what: all, text, controls, or latest "
                     "(only the newest reply in a chat app such as Claude: use it for 'read me Claude's reply'). "
                     "Prefer this over look_at_screen for text in an app. Not for web browsers."),
        func=desktop.read_app,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        # Button numbers must stay exact for clicking; one chat reply keeps its start if it must be cut.
        condense=lambda a: "start" if a.get("what") == "latest" else a.get("what") != "controls",
        parameters={
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "App name, e.g. claude, notepad, word. Empty = current."},
                "what": {"type": "string", "enum": sorted(WHAT)},
            },
        },
        summarize=lambda a: f"read {_app_words(a)}",
    ))
    registry.register(Tool(
        name="type_in_app",
        description=("Type text into an open desktop app, e.g. 'type hello in Notepad', 'ask Claude what a "
                     "monad is' (send=true presses Enter to send it). Types where the cursor is, else into the "
                     "field named or numbered by field (numbers from read_app). app empty = the current app. "
                     "In a code editor (VS Code, Cursor...), field must be the file name, e.g. index.html: it "
                     "types only if that file is open. Up to 10000 characters into a code editor (a whole file "
                     "in one call), 2000 elsewhere. "
                     "Never for passwords or payment details. Not for web browsers."),
        func=desktop.type_in_app,
        tier=Tier.SAFE,  # asks itself for terminals, Enter outside AI chat apps, and after untrusted text
        self_gated=True,
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Exactly what to type."},
                "app": {"type": "string", "description": "App name; empty = the current app."},
                "field": {"type": "string", "description": ("Field name or read_app number; empty = cursor. "
                                                            "In a code editor: the file name.")},
                "send": {"type": "boolean", "description": "Press Enter afterwards (sends a chat message)."},
            },
            "required": ["text"],
        },
        summarize=_type_summary,
    ))
    app_param = {"type": "string", "description": "App name; empty = the current app."}
    registry.register(Tool(
        name="wait_for_app",
        description=("Wait until an open app finishes changing, e.g. Claude finishing its reply after "
                     "type_in_app with send=true, or until it shows some text (until). Returns only the new "
                     "text. timeout in seconds (default 60)."),
        func=desktop.wait_for_app,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        condense="start",
        parameters={
            "type": "object",
            "properties": {"app": app_param, "until": {"type": "string"},
                           "timeout": {"type": "integer", "minimum": 5, "maximum": 180}},
        },
        summarize=lambda a: f"wait for {a.get('app') or 'the app'}" + (f" to show {a['until']}" if a.get("until")
                                                                        else " to finish"),
    ))
    registry.register(Tool(
        name="click_in_app",
        description=("Click a button, link, tab, menu item or list item in an open desktop app, by its name or "
                     "its read_app number, e.g. 'click New chat in Claude'. If it says 'did you mean', ask the "
                     "user. Risky buttons (delete, send, pay...) are confirmed by the app. Not for web browsers."),
        func=desktop.click_in_app,
        tier=Tier.SAFE,  # asks itself for risky controls and after untrusted text
        self_gated=True,
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "The control's name, or its read_app number."},
                "app": app_param,
                "double": {"type": "boolean"},
                "right": {"type": "boolean", "description": "Right-click."},
            },
            "required": ["target"],
        },
        summarize=lambda a: f"click {a.get('target')} in {_app_words(a)}",
    ))
    registry.register(Tool(
        name="press_keys",
        description=("Press a key or shortcut in an open desktop app, e.g. 'enter', 'ctrl+n', 'ctrl+shift+t', "
                     "'page down', 'escape'. times repeats navigation keys. Saving, closing, printing and Enter "
                     "(outside AI chat apps) are confirmed by the app; Windows-key shortcuts are refused."),
        func=desktop.press_keys,
        tier=Tier.SAFE,
        self_gated=True,
        parameters={
            "type": "object",
            "properties": {
                "keys": {"type": "string"},
                "app": app_param,
                "times": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["keys"],
        },
        summarize=lambda a: f"press {a.get('keys')}" + (f" {a['times']} times" if (a.get("times") or 1) > 1 else "")
        + f" in {_app_words(a)}",
    ))
    registry.register(Tool(
        name="scroll_app",
        description="Scroll an open desktop app up or down (amount = mouse-wheel notches, default 5).",
        func=desktop.scroll_app,
        tier=Tier.SAFE,
        parameters={
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["up", "down"]}, "app": app_param,
                           "amount": {"type": "integer", "minimum": 1, "maximum": 30}},
        },
        summarize=lambda a: f"scroll {a.get('direction', 'down')} in {_app_words(a)}",
    ))
    registry.register(Tool(
        name="window_action",
        description=("Move an app's window: minimise, maximise, restore, front (bring to front), left half or "
                     "right half of the screen. Works for any app, including your browser."),
        func=desktop.window_action,
        tier=Tier.SAFE,
        parameters={
            "type": "object",
            "properties": {"action": {"type": "string", "enum": sorted(WINDOW_ACTIONS)}, "app": app_param},
            "required": ["action"],
        },
        summarize=lambda a: f"{a.get('action')}: {_app_words(a)}",
    ))
