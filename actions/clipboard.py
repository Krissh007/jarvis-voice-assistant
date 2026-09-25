"""Summarise / translate / explain / fix copied text.

Copied text is untrusted. It is never sent anywhere if it looks like a secret (API keys, private
keys, .env lines, card numbers, random tokens). Emails and phone numbers are swapped for
placeholders before the model sees the text and put back afterwards. Writing back to the
clipboard (fix) is previewed and confirmed first.
"""

from __future__ import annotations

import asyncio
import logging

from core.llm import USAGE_LIMIT_MESSAGE, LLMUnavailable
from core.privacy import redact_reversible, restore, secret_in_text
from core.registry import NeedsConfirmation, Registry, Tier, Tool
from core.untrusted import unwrap, wrap

log = logging.getLogger(__name__)

MAX_CHARS = 20_000
TASKS = {
    "summarize": "Summarise this in 2 to 4 short spoken sentences.",
    "translate": "Translate this into {language}. Reply with only the translation.",
    "explain": "Explain this simply in 2 to 4 short spoken sentences, as if to a smart friend.",
}


def read_clipboard() -> str:
    import pyperclip

    return pyperclip.paste() or ""


def clipboard_sequence() -> int:
    """Windows increments this on every copy, so it tells whether anything new was copied."""
    import ctypes

    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


def write_clipboard(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)


def preview(text: str, limit: int = 120) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit].rsplit(" ", 1)[0] + "…"


class Clipboard:
    def __init__(self, services) -> None:
        self.llm = services.llm
        self._last_seq: int | None = None
        self._written_seq: int | None = None  # our own write (clipboard_fix) doesn't count as a stale copy

    async def _stale_note(self) -> str:
        """If nothing was copied since the last read, say so: the user's copy may not have worked."""
        seq = await asyncio.to_thread(clipboard_sequence)
        stale = self._last_seq is not None and seq == self._last_seq and seq != self._written_seq
        self._last_seq = seq
        if not stale:
            return ""
        return ("Note for the user: nothing new has been copied since I last read the clipboard, so this is the "
                "same text as before. If they meant to copy something else, the copy didn't work: select it and "
                "press Ctrl+C again.\n")

    async def _copied(self) -> tuple[str | None, str | None]:
        """(text, refusal). Refuses empty, huge or secret-looking clipboards."""
        text = await asyncio.to_thread(read_clipboard)
        if not text.strip():
            return None, "There's no text on the clipboard. Copy something first."
        if len(text) > MAX_CHARS:
            return None, f"That's too long for me ({len(text):,} characters); copy a smaller part."
        if kind := secret_in_text(text):
            log.warning("clipboard refused: looks like %s", kind)
            return None, (f"What you copied looks like it contains {kind}, so I won't send it anywhere. "
                          "Clear it from the clipboard when you're done.")
        return text, None

    async def clipboard_action(self, action: str, language: str = "English") -> str:
        """Summarise, translate or explain the copied text. Rung 2 (clipboard) + Flash. Tier SAFE,
        read-only; output untrusted."""
        action = action.lower().strip()
        if action not in TASKS:
            return "I can summarise, translate, explain or fix what you copied."
        note = await self._stale_note()
        text, refusal = await self._copied()
        if refusal:
            return note + refusal
        safe, mapping = redact_reversible(text)
        prompt = TASKS[action].format(language=language or "English") + (
            " Keep placeholders like [EMAIL1] exactly as they are.\n\n" + wrap("clipboard", safe))
        try:
            result = await self.llm.generate(prompt, system="You help with text the user copied.", max_tokens=1200)
        except LLMUnavailable:
            return USAGE_LIMIT_MESSAGE
        return note + restore(unwrap(result), mapping)

    async def clipboard_fix(self, instructions: str = "Fix spelling, grammar and punctuation") -> str | NeedsConfirmation:
        """Rewrite the copied text and put it back on the clipboard. Rung 2 + Flash. The new text is
        written first, then previewed to the user; the clipboard changes only after they confirm."""
        text, refusal = await self._copied()
        if refusal:
            return refusal
        safe, mapping = redact_reversible(text)
        prompt = (f"{instructions}. Keep the meaning, language and formatting. Keep placeholders like [EMAIL1] "
                  "exactly as they are. Reply with only the rewritten text.\n\n" + wrap("clipboard", safe))
        try:
            fixed = await self.llm.generate(prompt, system="You edit text the user copied.", max_tokens=4000)
        except LLMUnavailable:
            return USAGE_LIMIT_MESSAGE
        fixed = restore(unwrap(fixed), mapping)
        if fixed == text.strip():
            return "It already looks right; I didn't change anything."

        async def resume() -> str:
            current = await asyncio.to_thread(read_clipboard)
            if current != text:
                return "The clipboard changed since I read it, so I didn't overwrite it."
            await asyncio.to_thread(write_clipboard, fixed)
            self._written_seq = self._last_seq = await asyncio.to_thread(clipboard_sequence)
            return "Done: the corrected text is on the clipboard, ready to paste."

        words = len(fixed.split())
        return NeedsConfirmation(
            summary=f'Replace the copied text with the corrected version ({words} words), starting: "{preview(fixed)}"',
            resume=resume,
            step={"action": "fix clipboard", "instructions": instructions[:100]},
        )


def register(registry: Registry, config: dict, services) -> None:
    clip = Clipboard(services)
    registry.register(Tool(
        name="clipboard_action",
        description=("Summarise, translate or explain the text the user copied. For translate, give the language, "
                     "then read the returned translation aloud in that language, word for word; don't translate "
                     "it back into English."),
        func=clip.clipboard_action,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,
        rung=2,
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["summarize", "translate", "explain"]},
                "language": {"type": "string", "description": "Target language for translate, e.g. 'Hindi'."},
            },
            "required": ["action"],
        },
    ))
    registry.register(Tool(
        name="clipboard_fix",
        description="Fix or rewrite the copied text and put the new version back on the clipboard (asks first).",
        func=clip.clipboard_fix,
        tier=Tier.SAFE,  # only drafts; the clipboard is written through a code-written confirmation
        self_gated=True,
        untrusted_output=True,
        rung=2,
        parameters={"type": "object", "properties": {
            "instructions": {"type": "string", "description": "e.g. 'make it more formal'"}}},
    ))
