"""Short lines JARVIS says by itself, in its own voice, while it works: "Still looking at your screen.",
"Google's model is busy, so I'm trying the backup.", or the answer to "what are you doing?".

Seen live: while a tool ran or a model was slow, JARVIS went silent for up to a minute and a half; it only
showed a line on screen. Gemini can't speak while it waits for a tool, so these lines don't come from it:
each is recorded once per voice by a short Live session that reads the fixed sentence (as voice previews
are, core/voice_preview.py) and kept in data/voice_lines/<voice>/<key>-<hash>.pcm. Nothing but the fixed
sentence is sent. A line that isn't recorded yet is simply not said (the status line still shows).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# key -> the sentence. Changing a sentence records it again (the file name carries a hash of it).
LINES = {
    # spoken progress, while a step runs quietly
    "moment": "One moment.",
    "still": "Still on it.",
    "screen": "Still looking at your screen.",
    "web": "Still searching the web.",
    "reading": "Still reading it.",
    "reply": "Still waiting for the reply.",
    "thinking": "Still thinking it through.",
    "agents": "Still waiting on your agents.",
    "jobs": "Still checking JobPilot.",
    "hub": "Starting the Agent Hub. That takes a moment.",
    "files": "Still searching your files.",
    "browser": "Still working in the browser.",
    "backup": "Google's model is busy, so I'm trying the backup.",
    "reconnect": "Lost the line for a second. Reconnecting.",
    # what are you doing?
    "doing_screen": "I'm looking at your screen.",
    "doing_web": "I'm searching the web.",
    "doing_reading": "I'm reading it now.",
    "doing_reply": "I'm waiting for the reply.",
    "doing_typing": "I'm typing it in.",
    "doing_thinking": "I'm thinking it through.",
    "doing_agents": "Your agents are working on it.",
    "doing_jobs": "I'm checking JobPilot.",
    "doing_hub": "I'm starting the Agent Hub.",
    "doing_files": "I'm searching your files.",
    "doing_browser": "I'm working in the browser.",
    "doing_work": "I'm working on your request.",
    "doing_nothing": "Nothing right now. I'm listening.",
}

# What a running tool is, for choosing a line: tool name -> topic.
TOPIC = {
    "look_at_screen": "screen", "active_window": "screen",
    "search_web": "web", "site_search": "web", "read_page": "web", "save_research": "web",
    "read_app": "reading", "read_file": "reading", "read_current_page": "reading", "recall": "reading",
    "wait_for_app": "reply",
    "type_in_app": "typing",
    "ask_brain": "thinking", "think_through": "thinking", "run_plan": "thinking",
    "agent_hub_request": "agents", "agent_hub_status": "agents",
    # JobPilot's own data, read directly (seen live: "Still waiting on your agents." while it only read JobPilot)
    "job_search_status": "jobs", "next_job_question": "jobs", "save_job_answer": "jobs",
    "find_files": "files",
    "browser_task": "browser", "browser_open": "browser", "browser_click": "browser", "browser_scroll": "browser",
}
GAP_S = 2.0   # between recordings (the free tier allows few Live sessions at once)


def topic_of(tool: str) -> str:
    return TOPIC.get(tool or "", "")


def progress_key(tool: str, kind: str, said: int, fell_back: bool) -> str:
    """The line for the `said`-th update (0 = first) while `tool` runs, or while JARVIS thinks (kind)."""
    if fell_back:
        return "backup"
    topic = topic_of(tool)
    if topic in ("", "typing"):
        return "moment" if said == 0 else "still"
    if said and said % 2 == 1:
        return "still"          # don't say the same thing every time
    return topic


def doing_key(tool: str, kind: str) -> str:
    """The answer to "what are you doing?"."""
    topic = topic_of(tool)
    if topic:
        return f"doing_{topic}"
    if kind == "thinking" or kind == "composing":
        return "doing_thinking"
    if kind == "tool":
        return "doing_work"
    if kind == "background":
        return "doing_agents"
    return "doing_nothing"


def _file(key: str) -> str:
    digest = hashlib.sha1(LINES[key].encode("utf-8")).hexdigest()[:8]
    return f"{key}-{digest}.pcm"


class VoiceLines:
    """The recorded lines for the reply voice; `prepare` records missing ones in the background."""

    def __init__(self, previews: Any, cache_dir: Path) -> None:
        self._previews = previews          # core.voice_preview.VoicePreviews: its Live client and lock
        self.cache_dir = cache_dir
        self._task: asyncio.Task | None = None

    def get(self, voice: str, key: str) -> bytes | None:
        if key not in LINES or not voice:
            return None
        try:
            data = (self.cache_dir / voice / _file(key)).read_bytes()
        except OSError:
            return None
        return data or None

    def missing(self, voice: str) -> list[str]:
        return [k for k in LINES if self.get(voice, k) is None]

    def prepare(self, voice: str, delay_s: float = 0.0) -> None:
        """Record what's missing for this voice, one line at a time, without blocking anything."""
        if self._previews is None or not voice or (self._task is not None and not self._task.done()):
            return
        if self.missing(voice):
            self._task = asyncio.ensure_future(self._record(voice, delay_s))

    async def _record(self, voice: str, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        made = 0
        for key in self.missing(voice):
            try:
                async with self._previews._lock:
                    data = await asyncio.wait_for(self._previews._generate(voice, LINES[key]), 25)
            except Exception as exc:   # quota, network: try again at the next start
                log.warning("voice lines: couldn't record %r in %s (%s); %d left for later", key, voice,
                            type(exc).__name__, len(self.missing(voice)))
                return
            folder = self.cache_dir / voice
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / (_file(key) + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(folder / _file(key))
            made += 1
            await asyncio.sleep(GAP_S)
        if made:
            log.info("voice lines: recorded %d line%s in %s", made, "" if made == 1 else "s", voice)
