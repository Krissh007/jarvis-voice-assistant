"""Ways in to the one conversation. Voice is the mic and Gemini Live's transcription (core/runtime.py);
chat is typed text from any surface: the window's box, the terminal in --text mode, the web UI.

Every chat surface goes through ChatChannel, so typed text is handled the same wherever it's typed: it
joins the same Live conversation as speech, uses the same tools and dispatcher, and a typed "yes"
answers a pending confirmation (it comes from the user's own keyboard, never from the model or a tool).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

MAX_CHARS = 2000  # characters per typed message


class ChatChannel:
    name = "chat"

    def __init__(self, submit: Callable[[str, bool, str], None]) -> None:
        """`submit(text, speak, source)` runs on the asyncio loop and hands the text to the conversation."""
        self._submit = submit
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def send(self, text: str, *, speak: bool = True, source: str = "window") -> bool:
        """A typed message, from any thread. `speak`: also say the answer aloud. `source` names the surface
        ("window", "stdin", "web"). False if it was empty, too long, or JARVIS is still starting."""
        text = " ".join((text or "").split())
        if not text or len(text) > MAX_CHARS:
            return False
        if self._loop is None:
            log.info("chat message from %s ignored: still starting up", source)
            return False
        try:
            self._loop.call_soon_threadsafe(self._submit, text, speak, source)
        except RuntimeError:  # the loop has closed: shutting down
            return False
        return True
