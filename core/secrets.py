"""Read API keys from the environment, ignoring blanks and pasted-in comments."""

from __future__ import annotations

import os


def env_key(name: str) -> str | None:
    """The key's value, or None if unset, blank, or a leftover comment like '# optional fallback'."""
    value = (os.environ.get(name) or "").strip().strip('"').strip("'").strip()
    if not value or value.startswith("#") or " " in value:
        return None
    return value
