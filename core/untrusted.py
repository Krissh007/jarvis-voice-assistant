"""Wrapping/tagging helpers for untrusted content (web pages, clipboard, screen)."""

from __future__ import annotations

import re

_CLOSE_TAG = re.compile(r"</\s*untrusted_content", re.IGNORECASE)
_OPEN_TAG = re.compile(r"<\s*untrusted_content", re.IGNORECASE)


_ANY_TAG = re.compile(r"</?\s*untrusted_content[^>]*>", re.IGNORECASE)


def unwrap(text: str) -> str:
    """Remove wrapper tags a model may echo back, before its text is used or written anywhere."""
    return _ANY_TAG.sub("", text).strip()


def wrap(source: str, text: str) -> str:
    """Tag content as data. Tags inside the content are defused so it can't break out."""
    safe_source = re.sub(r"[^\w.:/ -]", "", source)[:100]
    body = _OPEN_TAG.sub("&lt;untrusted_content", _CLOSE_TAG.sub("&lt;/untrusted_content", text))
    return f'<untrusted_content source="{safe_source}">\n{body}\n</untrusted_content>'
