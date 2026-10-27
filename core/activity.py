"""What JARVIS is doing right now, in a few words ("Searching the web…"), for the orb and the chat.

Seen live: after the user spoke there could be several seconds of silence while Gemini thought or a tool
ran, with nothing on screen to say JARVIS was working. The runtime feeds this from the dispatcher's
tool events, the gap before Gemini answers, and background work (agent runs the Hub follows, task
mode); every change is published on the bus as an "activity" event: {text, what}. An empty text means
nothing is going on. Texts are written here, never taken from tool output.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

THINKING = "Thinking…"
COMPOSING = "Putting the answer together…"
SLOW_NOTE = " This can take a moment."
SLOW_AFTER_S = 8.0
BACKGROUND_AGENTS = "Your agents are working in the background…"
BACKGROUND_TASK = "Working on your task in the background…"

# A few words per tool. "{app}" and "{site}" come from the call's own arguments (an app or site name the
# user or model gave), cut short; never from what a tool returned.
TOOL_TEXT = {
    "ask_brain": "Thinking it through…", "think_through": "Thinking it through…",
    "search_web": "Searching the web…", "read_page": "Reading the page…", "save_research": "Saving the research…",
    "get_weather": "Checking the weather…", "get_system_status": "Checking the computer…",
    "look_at_screen": "Looking at your screen…", "active_window": "Checking which window is open…",
    "read_app": "Reading {app}…", "type_in_app": "Typing in {app}…", "wait_for_app": "Waiting for {app} to reply…",
    "click_in_app": "Clicking in {app}…", "scroll_app": "Scrolling {app}…", "press_keys": "Pressing keys in {app}…",
    "open_app": "Opening {app}…", "close_app": "Closing {app}…", "focus_app": "Switching to {app}…",
    "window_action": "Arranging the window…",
    "browser_open": "Opening {site}…", "site_search": "Searching {site}…", "read_current_page": "Reading the page…",
    "browser_task": "Working in the browser…", "browser_scroll": "Scrolling the page…",
    "browser_click": "Clicking on the page…", "browser_show": "Showing the browser…", "browser_close": "Closing the browser…",
    "play_youtube": "Finding the video…", "play_music": "Finding the song…", "now_playing": "Checking what's playing…",
    "find_files": "Searching your files…", "open_file": "Opening the file…", "read_file": "Reading the file…",
    "write_file": "Writing the file…", "edit_file": "Editing the file…", "delete_file": "Deleting the file…",
    "clipboard_action": "Using the clipboard…", "clipboard_fix": "Fixing the clipboard text…",
    "remember": "Saving to memory…", "recall": "Checking my memory…", "forget": "Updating my memory…",
    "set_reminder": "Setting the reminder…", "list_reminders": "Checking your reminders…",
    "cancel_reminder": "Cancelling the reminder…",
    "job_search_status": "Checking your job search…", "agent_hub_request": "Asking your agents…",
    "agent_hub_status": "Checking your agents…", "save_job_answer": "Saving the answer…",
    "next_job_question": "Finding the next question…",
    "run_plan": "Working through the plan…", "preview_voice": "Getting a voice sample…",
}
# Quick app-state tools: no status line (it would only flash).
QUIET = {"get_datetime", "go_to_sleep", "set_voice_replies", "set_reply_voice", "show_on_screen", "media_key",
         "set_volume", "set_music_volume", "start_task", "finish_task", "stop_task", "task_status",
         "pause_conversation_log", "forget_conversation"}


def _name(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").split())[:30]
    return text or fallback


def describe(tool: str, args: dict | None) -> str:
    """The status line for a tool call, or "" for quick ones."""
    if tool in QUIET:
        return ""
    args = args or {}
    template = TOOL_TEXT.get(tool)
    if template is None:
        return "Working on it…"
    site = args.get("site") or args.get("url") or ""
    site = str(site).replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    return template.format(app=_name(args.get("app") or args.get("name"), "the app"), site=_name(site, "the page"))


class Activity:
    """The one status line. `set` publishes only on a change."""

    def __init__(self, publish: Callable[..., None], clock: Callable[[], float] = time.monotonic) -> None:
        self._publish = publish
        self.clock = clock
        self.text = ""
        self.kind = ""          # thinking | tool | composing | background | ""
        self.tool = ""          # the tool running, while kind is "tool" (for JARVIS's spoken progress)
        self.since = 0.0
        self.slow = False
        self.background_jobs = 0   # agent runs started and not reported back yet

    def set(self, text: str, kind: str, tool: str = "") -> None:
        if not text:
            kind = ""
        self.tool = tool if kind == "tool" else ""
        if text == self.text and kind == self.kind:
            return
        self.text, self.kind, self.since, self.slow = text, kind, self.clock(), False
        self._publish(text=text, what=kind)

    def clear(self) -> None:
        self.set("", "")

    def tick(self, background: str = "") -> None:
        """Called a few times a second: mark a long step, and show background work when nothing else is."""
        if self.kind in {"tool", "thinking"} and not self.slow and self.clock() - self.since >= SLOW_AFTER_S:
            self.slow = True
            self.text += SLOW_NOTE
            self._publish(text=self.text, what=self.kind)
        if not self.kind or self.kind == "background":
            self.set(background, "background" if background else "")
