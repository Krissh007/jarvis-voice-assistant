"""Reminders: "remind me in 20 minutes to..." -> spoken reminder + Windows toast (spec Phase 9).

Jobs are kept by APScheduler in a SQLite jobstore (data/reminders.sqlite), so they survive a
restart. A reminder that came due while JARVIS was closed fires on the next start and says
it is late. set_reminder is SAFE (the dispatcher makes it CONFIRM in a tainted turn);
cancelling is CONFIRM. A firing reminder taints the turn, so its text can't trigger actions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.registry import Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

MAX_PENDING = 50
MAX_AHEAD = timedelta(days=30)
MAX_TEXT = 200
LATE_GRACE_S = 7 * 24 * 3600  # still deliver reminders missed while JARVIS was closed, up to a week
LATE_AFTER_S = 120  # later than this counts as "missed"

FireHandler = Callable[[str, datetime, bool], Awaitable[None] | None]
_handler: FireHandler | None = None  # set by Reminders.start; jobs look it up when they run


async def fire_reminder(reminder_id: str, text: str, due: str) -> None:
    """APScheduler job (referenced by name so it can be stored). Hands the reminder to the app."""
    due_at = datetime.fromisoformat(due)
    late = (datetime.now() - due_at).total_seconds() > LATE_AFTER_S
    log.info("reminder %s fired%s", reminder_id, " (late)" if late else "")
    if _handler is None:
        log.warning("reminder fired with no handler: %s", text)
        return
    result = _handler(text, due_at, late)
    if asyncio.iscoroutine(result):
        await result


def resolve_due(now: datetime, in_minutes: float | None = None, at: str | None = None) -> datetime:
    """When a reminder is due. `at` is 'HH:MM' (next occurrence) or 'YYYY-MM-DD HH:MM', local time."""
    if (in_minutes is None) == (not at):
        raise ToolError("Tell me either how many minutes from now, or a clock time.")
    if in_minutes is not None:
        try:
            minutes = float(in_minutes)
        except (TypeError, ValueError):
            raise ToolError("I didn't understand how long to wait.") from None
        if minutes <= 0:
            raise ToolError("That time has already passed.")
        due = now + timedelta(minutes=minutes)
    else:
        due = _parse_clock(str(at).strip(), now)
        if due <= now:
            raise ToolError("That time has already passed.")
    if due - now > MAX_AHEAD:
        raise ToolError("I can only set reminders up to 30 days ahead.")
    return due.replace(microsecond=0)


def _parse_clock(at: str, now: datetime) -> datetime:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", at.lower())
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm:
            if not 1 <= hour <= 12:
                raise ToolError(f"I didn't understand the time {at!r}.")
            hour = hour % 12 + (12 if ampm.startswith("p") else 0)
        if hour > 23 or minute > 59:
            raise ToolError(f"I didn't understand the time {at!r}.")
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    try:
        return datetime.fromisoformat(at.replace("T", " ")).replace(tzinfo=None)
    except ValueError:
        raise ToolError(f"I didn't understand the time {at!r}. Use a time like 17:30.") from None


def spoken_time(due: datetime, now: datetime) -> str:
    clock = due.strftime("%I:%M %p").lstrip("0")
    if due.date() == now.date():
        return f"{clock} today"
    if due.date() == (now + timedelta(days=1)).date():
        return f"{clock} tomorrow"
    return f"{clock} on {due.strftime('%A %d %B').replace(' 0', ' ')}"


def clean_text(text: str) -> str:
    text = " ".join(str(text).split())
    text = re.sub(r"^(to|that)\s+", "", text, flags=re.IGNORECASE)
    if not text:
        raise ToolError("What should I remind you about?")
    if len(text) > MAX_TEXT:
        raise ToolError("That reminder is too long. Give me a shorter version.")
    return text


def show_toast(title: str, message: str) -> None:
    """Windows toast notification (winotify). Never raises."""
    try:
        from winotify import Notification, audio

        toast = Notification(app_id="JARVIS", title=title, msg=message[:250], duration="long")
        toast.set_audio(audio.Reminder, loop=False)
        toast.show()
    except Exception:
        log.exception("toast failed")


class Reminders:
    def __init__(self, db_path: Path, clock: Callable[[], datetime] = datetime.now) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.scheduler = None

    # ---- lifecycle (the runtime calls these inside its event loop) ----

    def start(self, handler: FireHandler) -> None:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        global _handler
        _handler = handler
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{self.db_path.as_posix()}")},
            job_defaults={"misfire_grace_time": LATE_GRACE_S, "coalesce": True, "max_instances": 1},
        )
        self.scheduler.start()
        log.info("reminders started: %d pending", len(self.scheduler.get_jobs()))

    def stop(self) -> None:
        global _handler
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                log.exception("scheduler shutdown failed")
            self.scheduler = None
        _handler = None

    def _require(self):
        if self.scheduler is None:
            raise ToolError("Reminders aren't running right now.")
        return self.scheduler

    # ---- data ----

    def pending(self) -> list[dict[str, Any]]:
        if self.scheduler is None:
            return []
        items = [{"id": j.id, "text": j.kwargs["text"], "due": datetime.fromisoformat(j.kwargs["due"])}
                 for j in self.scheduler.get_jobs() if j.kwargs.get("text")]
        return sorted(items, key=lambda r: r["due"])

    def best_match(self, which: str) -> dict[str, Any] | None:
        from actions.memory import words

        want = words(which)
        items = self.pending()
        if not items:
            return None
        if not want:
            return items[0] if len(items) == 1 else None
        scored = [(len(want & words(r["text"])), r) for r in items]
        n, best = max(scored, key=lambda s: s[0])
        return best if n else None

    # ---- tools ----

    async def set_reminder(self, text: str, in_minutes: float | None = None, at: str | None = None) -> str:
        """Schedule a spoken reminder + toast. Rung 2 (local scheduler). Tier SAFE (CONFIRM when tainted)."""
        scheduler = self._require()
        text = clean_text(text)
        now = self.clock()
        due = resolve_due(now, in_minutes, at)
        if len(self.pending()) >= MAX_PENDING:
            raise ToolError(f"You already have {MAX_PENDING} reminders. Cancel some first.")
        rid = uuid.uuid4().hex[:12]
        scheduler.add_job("actions.reminders:fire_reminder", "date", run_date=due, id=rid,
                          kwargs={"reminder_id": rid, "text": text, "due": due.isoformat()},
                          replace_existing=True)
        return f"Reminder set for {spoken_time(due, now)}: {text}"

    async def list_reminders(self) -> str:
        """Pending reminders. Read-only."""
        items = self.pending()
        if not items:
            return "No reminders are set."
        now = self.clock()
        return "\n".join(f"- {spoken_time(r['due'], now)}: {r['text']}" for r in items[:15])

    async def cancel_reminder(self, which: str = "", everything: bool = False) -> str:
        """Cancel one pending reminder (or all). Tier CONFIRM."""
        scheduler = self._require()
        if everything:
            items = self.pending()
            for r in items:
                scheduler.remove_job(r["id"])
            return f"Cancelled all {len(items)} reminders."
        match = self.best_match(which)
        if match is None:
            raise ToolError(f"I couldn't find a reminder matching {which!r}." if which else "Which reminder?")
        scheduler.remove_job(match["id"])
        return f"Cancelled the reminder: {match['text']}"

    def set_summary(self, args: dict[str, Any]) -> str:
        try:
            now = self.clock()
            when = spoken_time(resolve_due(now, args.get("in_minutes"), args.get("at")), now)
        except ToolError:
            when = "an unclear time"
        return f'Set a reminder for {when}: "{str(args.get("text", ""))[:200]}"'

    def cancel_summary(self, args: dict[str, Any]) -> str:
        if args.get("everything"):
            return f"Cancel all {len(self.pending())} reminders"
        match = self.best_match(str(args.get("which", "")))
        if match is None:
            return f"Cancel the reminder matching {str(args.get('which', ''))[:100]!r} (nothing matches)"
        return f'Cancel the reminder for {spoken_time(match["due"], self.clock())}: "{match["text"][:200]}"'


def register(registry: Registry, config: dict, services) -> None:
    reminders = Reminders(Path(services.data_dir) / "reminders.sqlite")
    services.reminders = reminders
    registry.register(Tool(
        name="set_reminder",
        description=(
            "Set a reminder: I'll say it aloud and show a Windows notification when it's due, even after a "
            "restart. Give either in_minutes (e.g. 20) or at, a local clock time like '17:30' or "
            "'2026-09-26 09:00'. text is what to remind about, in the user's words."
        ),
        func=reminders.set_reminder,
        tier=Tier.SAFE,
        summarize=reminders.set_summary,
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "in_minutes": {"type": "number"},
                "at": {"type": "string"},
            },
            "required": ["text"],
        },
    ))
    registry.register(Tool(
        name="list_reminders",
        description="List the reminders that are still pending.",
        func=reminders.list_reminders,
        tier=Tier.SAFE,
        read_only=True,
    ))
    registry.register(Tool(
        name="cancel_reminder",
        description="Cancel a pending reminder by what it's about (or everything=true). Needs confirmation.",
        func=reminders.cancel_reminder,
        tier=Tier.CONFIRM,
        summarize=reminders.cancel_summary,
        parameters={"type": "object", "properties": {"which": {"type": "string"}, "everything": {"type": "boolean"}}},
    ))
