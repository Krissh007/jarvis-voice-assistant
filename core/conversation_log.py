"""The conversation log: finished lines of what was said, kept in data/memory.sqlite (memory step 4).

It listens to the bus: every `message` event (a whole line from you or JARVIS, spoken or typed) is
stored in the current conversation; a `conversation` event ("New chat") closes it, and the next line
starts another. Nothing else is stored: never tool output, web pages, screen or clipboard text, file
contents or confirmations. A line that looks like it holds a secret (a key, a card number, a password
or PIN) is dropped, not stored.

"Don't remember this conversation" pauses logging until the next new chat. Lines older than
memory.keep_conversations_days (default 30) are deleted at start and once a day.

Lines from JARVIS can echo web text, so whatever is found here goes back to the model wrapped as
untrusted data (actions/memory.py).
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any

from core.memory_db import MemoryDB
from core.privacy import secret_in_text

log = logging.getLogger(__name__)

KEEP_DAYS = 30
MAX_LINE = 2000                # longest line kept (a long reply's start)
SEARCH_LIMIT = 6
MIN_SCORE = 0.3                # meaning match needed for a line with no shared word
BACKFILL_BATCH = 64
PRIVATE = re.compile(r"\b(password|passcode|pin|otp|cvv|one[- ]time code|security code|card number)\s*(is|:|=)",
                     re.IGNORECASE)
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9']*")
STOP = {"the", "a", "an", "my", "is", "are", "i", "me", "to", "of", "that", "what", "do", "you", "about", "and",
        "in", "on", "for", "it", "was", "be", "am", "did", "we", "say", "said", "tell", "told", "talk", "talked"}


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def when_range(when: str, now: datetime | None = None) -> tuple[str, str] | None:
    """(start, end) ISO times for "today", "yesterday", "this week", "last week", "this month" or a date
    (2026-10-19); None for anything else (no time limit)."""
    now = now or _now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    w = " ".join((when or "").lower().split())
    if w in ("today", "this morning", "this afternoon", "this evening", "tonight"):
        return _iso(day), _iso(now + timedelta(seconds=1))
    if w in ("yesterday", "last night"):
        return _iso(day - timedelta(days=1)), _iso(day)
    if w in ("this week", "past week", "last 7 days"):
        return _iso(day - timedelta(days=6)), _iso(now + timedelta(seconds=1))
    if w == "last week":
        return _iso(day - timedelta(days=13)), _iso(day - timedelta(days=6))
    if w in ("this month", "last 30 days"):
        return _iso(day - timedelta(days=29)), _iso(now + timedelta(seconds=1))
    try:
        d = datetime.strptime(w, "%Y-%m-%d")
        return _iso(d), _iso(d + timedelta(days=1))
    except ValueError:
        return None


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "")} - STOP


class ConversationLog:
    def __init__(self, db: MemoryDB, keep_days: int = KEEP_DAYS, enabled: bool = True, embedder: Any = None) -> None:
        self.db = db
        self.keep_days = max(1, int(keep_days))
        self.enabled = enabled
        self.embedder = embedder
        self.paused = False            # "don't remember this conversation": until the next new chat
        self.dropped = 0               # lines not stored because they looked private
        self._current: int | None = None
        self._lock = threading.Lock()
        self._last_purge: datetime | None = None

    # ---- the bus

    def attach(self, bus) -> None:
        bus.subscribe(self.on_event)

    def on_event(self, event) -> None:
        try:
            if event.kind == "message":
                d = event.data
                self.add(str(d.get("role") or ""), str(d.get("text") or ""), str(event.channel or d.get("channel") or ""))
            elif event.kind == "conversation":
                self.new_conversation()
        except Exception:
            log.exception("conversation log: couldn't store a line")

    # ---- writing

    def new_conversation(self) -> None:
        """New chat: close the current conversation; logging resumes if it was paused."""
        with self._lock:
            if self._current is not None:
                with self.db.connect() as db:
                    db.execute("UPDATE conversations SET ended = ? WHERE id = ?", (_iso(_now()), self._current))
            self._current = None
            self.paused = False

    def add(self, role: str, text: str, channel: str = "") -> bool:
        """Store one finished line. False when it wasn't stored (off, paused, empty or private)."""
        role = {"jarvis": "assistant", "assistant": "assistant", "user": "user"}.get(role)
        text = " ".join(text.split())[:MAX_LINE]
        if not self.enabled or self.paused or role is None or not text:
            return False
        if secret_in_text(text) or PRIVATE.search(text):
            self.dropped += 1
            log.info("conversation log: a line that looked private wasn't stored")
            return False
        now = _iso(_now())
        with self._lock, self.db.connect() as db:
            if self._current is None:
                self._current = db.execute("INSERT INTO conversations(started, channels) VALUES (?, ?)",
                                           (now, channel)).lastrowid
            else:
                row = db.execute("SELECT channels FROM conversations WHERE id = ?", (self._current,)).fetchone()
                have = set(filter(None, (row["channels"] if row else "").split(",")))
                if channel and channel not in have:
                    db.execute("UPDATE conversations SET channels = ? WHERE id = ?",
                               (",".join(sorted(have | {channel})), self._current))
            db.execute("INSERT INTO messages(conversation, time, role, channel, text) VALUES (?, ?, ?, ?, ?)",
                       (self._current, now, role, channel, text))
        self.purge_if_due()
        return True

    def pause(self) -> str:
        self.paused = True
        return "I won't keep anything more from this conversation. It starts again with the next new chat."

    # ---- forgetting

    def count(self, scope: str) -> int:
        where, args = self._scope(scope)
        if where is None:
            return 0
        with self.db.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", args).fetchone()[0]

    def forget(self, scope: str) -> int:
        """Delete "this" conversation, everything from "today", or "all". Returns how many lines went."""
        where, args = self._scope(scope)
        if where is None:
            return 0
        with self._lock, self.db.connect() as db:
            n = db.execute(f"DELETE FROM messages WHERE {where}", args).rowcount
            db.execute("DELETE FROM conversations WHERE id NOT IN (SELECT DISTINCT conversation FROM messages)"
                       + (" AND id != ?" if self._current is not None and scope != "all" else ""),
                       (self._current,) if self._current is not None and scope != "all" else ())
            if scope in ("this", "all"):
                self._current = None
        log.info("conversation log: forgot %d lines (%s)", n, scope)
        return n

    def _scope(self, scope: str) -> tuple[str | None, tuple]:
        if scope == "all":
            return "1 = 1", ()
        if scope == "today":
            start, end = when_range("today")
            return "time >= ? AND time < ?", (start, end)
        if scope == "this":
            return ("conversation = ?", (self._current,)) if self._current is not None else (None, ())
        return None, ()

    def purge_if_due(self) -> int:
        now = _now()
        if self._last_purge and now - self._last_purge < timedelta(hours=24):
            return 0
        self._last_purge = now
        return self.purge()

    def purge(self) -> int:
        """Delete lines older than keep_days, and conversations left empty."""
        cutoff = _iso(_now() - timedelta(days=self.keep_days))
        with self._lock, self.db.connect() as db:
            n = db.execute("DELETE FROM messages WHERE time < ?", (cutoff,)).rowcount
            db.execute("DELETE FROM conversations WHERE id NOT IN (SELECT DISTINCT conversation FROM messages)"
                       + (" AND id != ?" if self._current is not None else ""),
                       (self._current,) if self._current is not None else ())
        if n:
            log.info("conversation log: deleted %d lines older than %d days", n, self.keep_days)
        return n

    # ---- reading

    def search(self, topic: str = "", when: str = "", limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
        """Lines about a topic (keywords, plus meaning when the embedding model is loaded), newest first
        among equals; `when` limits the time ("yesterday"). No topic: the latest lines in that time."""
        span = when_range(when)
        clause, args = ("time >= ? AND time < ?", span) if span else ("1 = 1", ())
        want = words(topic)
        with self.db.connect() as db:
            if not want:
                rows = db.execute(f"SELECT id, conversation, time, role, channel, text FROM messages WHERE {clause} "
                                  "ORDER BY time DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
                return [dict(r) for r in reversed(rows)]
            rows = db.execute(f"SELECT id, conversation, time, role, channel, text, embedding FROM messages "
                              f"WHERE {clause} ORDER BY time DESC LIMIT 2000", args).fetchall()
        rows = [dict(r) for r in rows]
        meaning = self._meaning(topic, rows)
        scored = []
        for i, r in enumerate(rows):
            shared = len(want & words(r["text"]))
            score = shared + (meaning[i] * 3 if meaning else 0)
            if shared or (meaning and meaning[i] >= MIN_SCORE):
                scored.append((score, r["time"], r))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)  # best first; newer wins a tie
        best = [r for _, _, r in scored[:limit]]
        best.sort(key=lambda r: (r["time"], r["id"]))
        for r in best:
            r.pop("embedding", None)
        return best

    def _meaning(self, topic: str, rows: list[dict]) -> list[float] | None:
        if self.embedder is None or not rows or not getattr(self.embedder, "available", lambda: False)():
            return None
        try:
            from core.embed import from_blob, to_blob

            missing = [r for r in rows if not r.get("embedding")][:BACKFILL_BATCH]
            if missing:
                vectors = self.embedder.encode([r["text"] for r in missing])
                with self.db.connect() as db:
                    db.executemany("UPDATE messages SET embedding = ? WHERE id = ?",
                                   [(to_blob(v), r["id"]) for v, r in zip(vectors, missing)])
                for v, r in zip(vectors, missing):
                    r["embedding"] = to_blob(v)
            query = self.embedder.encode([topic])[0]
            return [float(from_blob(r["embedding"]) @ query) if r.get("embedding") else 0.0 for r in rows]
        except Exception:
            log.exception("conversation log: meaning search failed; keywords only")
            return None

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, each with its line count and the first thing you said."""
        with self.db.connect() as db:
            rows = db.execute(
                "SELECT c.id, c.started, c.ended, c.channels, COUNT(m.id) AS lines, "
                "(SELECT text FROM messages WHERE conversation = c.id AND role = 'user' ORDER BY id LIMIT 1) AS first "
                "FROM conversations c JOIN messages m ON m.conversation = c.id "
                "GROUP BY c.id ORDER BY c.started DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def lines(self, conversation_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as db:
            rows = db.execute("SELECT id, time, role, channel, text FROM messages WHERE conversation = ? ORDER BY id",
                              (conversation_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: int) -> int:
        with self._lock, self.db.connect() as db:
            n = db.execute("DELETE FROM messages WHERE conversation = ?", (conversation_id,)).rowcount
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            if self._current == conversation_id:
                self._current = None
        return n
