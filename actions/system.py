"""Time, battery/CPU/RAM, volume, media keys, open allowlisted apps/sites. (Phase 2: get_datetime only.)"""

from __future__ import annotations

from datetime import datetime

from core.registry import Registry, Tier, Tool


def get_datetime() -> str:
    """Current local date and time. Rung 2 (OS clock). Tier SAFE, read-only."""
    now = datetime.now().astimezone()
    return now.strftime("%A %d %B %Y, %H:%M (%Z)")


def register(registry: Registry, config: dict) -> None:
    registry.register(Tool(
        name="get_datetime",
        description="Get the current local date, time and day of the week.",
        func=get_datetime,
        tier=Tier.SAFE,
        read_only=True,
    ))
