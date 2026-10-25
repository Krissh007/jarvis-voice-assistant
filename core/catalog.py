"""JARVIS's own skills as tool-registry entries, for the Agent Hub's registry (hub/tools in E:\\agent-hub).

The Hub keeps one catalogue of every tool (its agents' actions, connectors, and these), so a request can
be matched to the few tools it needs. JARVIS still runs its own skills: the Hub only lists them.

Every visible tool must appear in CATALOG (category + keywords) or in EXCLUDED; a test enforces it, so a
new skill can't be left out of the registry by accident. `access` comes from the tool itself: read-only
tools are "read", everything else is "write" and always needs the user's yes on the reasoning path.
"""

from __future__ import annotations

import logging
from typing import Any

from core.registry import Registry, Tool

log = logging.getLogger(__name__)

MAX_DESCRIPTION = 800  # the Hub's limit
JARVIS_SOURCE = "jarvis"  # the Hub files these skills as jarvis.<name>

# The Hub's categories (hub/tools/schema.py CATEGORIES, kept in step by hand): the router ranks with
# the same words as the Hub, so its local scores match the Hub's.
CATEGORIES: dict[str, str] = {
    "search": "Web search, reading web pages, weather and current facts.",
    "reasoning": "Careful thinking, maths, comparisons and multi-step plans.",
    "files": "Finding, opening, reading and editing the user's own files.",
    "mail": "Email: reading, searching, drafting and sending.",
    "calendar": "Calendar events and schedules.",
    "docs": "Documents, notes and knowledge bases (Drive, Notion...).",
    "code": "Code hosting: repositories, issues and pull requests.",
    "browser": "Driving a web browser: opening sites, searching sites, filling pages.",
    "desktop": "Reading and operating desktop apps and windows.",
    "screen": "Looking at the screen and the clipboard.",
    "media": "Music, video and volume.",
    "system": "Apps, the time, system status and settings JARVIS may change.",
    "memory": "Facts the user asked JARVIS to remember.",
    "reminders": "Reminders and timers.",
    "jobs": "The job search: jobs, the shortlist, applications and job agent runs.",
}

# Meta tools and gateways: not skills to pick for a request. The Hub lists its own agents' actions
# directly (jobpilot.*), so the two Agent Hub gateway tools would only duplicate them.
EXCLUDED = {
    "run_plan", "think_through", "ask_brain", "go_to_sleep", "list_issues", "start_task", "finish_task", "stop_task", "task_status",
    "agent_hub_request", "agent_hub_status", "test_write_note", "test_fetch_untrusted", "show_on_screen",
    "set_voice_replies", "set_reply_voice", "preview_voice",
}

# name -> (category, keywords). Categories are the Hub's (hub/tools/schema.py CATEGORIES).
CATALOG: dict[str, tuple[str, list[str]]] = {
    "get_datetime": ("system", ["time", "date", "day", "today", "clock"]),
    "get_system_status": ("system", ["battery", "cpu", "memory", "ram", "charging"]),
    "open_app": ("system", ["open", "launch", "start", "app", "program", "chrome", "excel", "spotify"]),
    "close_app": ("system", ["close", "quit", "exit", "app"]),
    "focus_app": ("system", ["switch", "bring to front", "focus", "window"]),
    "open_website": ("browser", ["open", "website", "site", "url", "link"]),
    "set_volume": ("system", ["volume", "louder", "quieter", "mute", "sound"]),
    "search_web": ("search", ["search", "google", "look up", "news", "facts", "price", "latest", "won", "score", "result"]),
    "read_page": ("search", ["read", "page", "article", "summarise", "website", "link"]),
    "get_weather": ("search", ["weather", "forecast", "rain", "temperature", "sunny", "hot", "cold"]),
    "save_research": ("files", ["save", "research", "notes", "markdown"]),
    "play_youtube": ("media", ["youtube", "video", "watch", "play"]),
    "browser_open": ("browser", ["open", "website", "browser", "site", "jarvis browser"]),
    "site_search": ("browser", ["search", "youtube", "amazon", "flipkart", "site", "results"]),
    "browser_close": ("browser", ["close", "browser"]),
    "read_current_page": ("browser", ["read", "page", "this page", "summarise"]),
    "browser_task": ("browser", ["fill", "form", "steps", "website", "task"]),
    "browser_scroll": ("browser", ["scroll", "down", "up", "top", "bottom", "more", "page"]),
    "browser_click": ("browser", ["click", "open", "select", "link", "second", "first", "one"]),
    "browser_show": ("browser", ["show", "browser", "window", "front"]),
    "play_music": ("media", ["music", "song", "play", "put on", "artist", "playlist", "album"]),
    "media_key": ("media", ["pause", "resume", "next", "previous", "skip", "stop"]),
    "set_music_volume": ("media", ["music volume", "louder", "quieter", "turn up", "turn down"]),
    "now_playing": ("media", ["playing", "song", "track", "what's this"]),
    "app_ui_action": ("desktop", ["app", "menu", "older"]),
    "look_at_screen": ("screen", ["screen", "look", "see", "chart", "picture", "error"]),
    "active_window": ("screen", ["window", "front", "which app"]),
    "clipboard_action": ("screen", ["clipboard", "copied", "translate", "explain", "summarise"]),
    "clipboard_fix": ("screen", ["clipboard", "copied", "fix", "rewrite", "grammar"]),
    "find_files": ("files", ["find", "file", "folder", "document", "pdf", "where"]),
    "open_file": ("files", ["open", "file", "folder", "document", "resume"]),
    "read_file": ("files", ["read", "file", "contents", "code", "notes"]),
    "write_file": ("files", ["create", "new file", "write", "save", "html"]),
    "edit_file": ("files", ["edit", "change", "replace", "update", "file"]),
    "delete_file": ("files", ["delete", "remove", "recycle bin", "file"]),
    "read_app": ("desktop", ["read", "app", "window", "claude", "reply", "message"]),
    "type_in_app": ("desktop", ["type", "write", "app", "notepad", "message", "ask claude"]),
    "wait_for_app": ("desktop", ["wait", "finish", "reply", "loading"]),
    "click_in_app": ("desktop", ["click", "button", "tab", "menu", "link"]),
    "press_keys": ("desktop", ["press", "key", "shortcut", "enter", "ctrl"]),
    "scroll_app": ("desktop", ["scroll", "up", "down", "page"]),
    "window_action": ("desktop", ["minimise", "maximise", "restore", "move window", "snap"]),
    "remember": ("memory", ["remember", "note", "don't forget", "save fact"]),
    "recall": ("memory", ["remember", "recall", "what did i tell you", "my"]),
    "forget": ("memory", ["forget", "remove fact", "delete memory"]),
    "pause_conversation_log": ("memory", ["don't remember this conversation", "off the record", "stop recording"]),
    "forget_conversation": ("memory", ["forget conversation", "forget today", "delete conversation history"]),
    "set_reminder": ("reminders", ["remind", "reminder", "timer", "alarm", "later", "tomorrow"]),
    "list_reminders": ("reminders", ["reminders", "pending", "what reminders"]),
    "cancel_reminder": ("reminders", ["cancel", "reminder", "remove"]),
    "save_job_answer": ("jobs", ["screening answer", "answer bank", "application question"]),
    "job_search_status": ("jobs", ["job search", "how's my job search", "summary", "progress"]),
}

# Env var names a skill needs (names only). Optional = it works without them, just less well.
AUTH: dict[str, dict[str, Any]] = {
    "search_web": {"env": ["TAVILY_API_KEY"], "optional": True},
}

# Seconds a skill may take; anything not listed gets the Hub's default (30 s).
TIMEOUTS = {"browser_task": 180, "wait_for_app": 130, "play_music": 60, "play_youtube": 60,
            "look_at_screen": 60, "read_page": 60}


def _description(tool: Tool) -> str:
    text = " ".join(tool.description.split())
    if len(text) <= MAX_DESCRIPTION:
        return text
    cut = text[:MAX_DESCRIPTION]
    return cut[: cut.rfind(". ") + 1] or cut  # end on a whole sentence when there is one


def uncatalogued(registry: Registry) -> list[str]:
    """Visible tools that are neither catalogued nor excluded (should always be empty)."""
    return [t.name for t in registry.visible() if t.name not in CATALOG and t.name not in EXCLUDED]


def export(registry: Registry) -> list[dict[str, Any]]:
    """Registry entries for every catalogued, visible skill. Excluded and uncatalogued tools are left out."""
    out = []
    for tool in registry.visible():
        if tool.name in EXCLUDED:
            continue
        if tool.name not in CATALOG:
            log.warning("tool %s has no registry category (core/catalog.py); not sent to the Agent Hub", tool.name)
            continue
        category, keywords = CATALOG[tool.name]
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": _description(tool),
            "category": category,
            "keywords": keywords,
            "access": "read" if tool.read_only else "write",
            "params": tool.parameters,
        }
        if tool.name in AUTH:
            entry["auth"] = AUTH[tool.name]
        if tool.name in TIMEOUTS:
            entry["timeout_s"] = TIMEOUTS[tool.name]
        out.append(entry)
    return out
