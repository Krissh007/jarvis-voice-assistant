"""data/memory.sqlite: long-term memory shared by voice, chat and ask_brain (docs/memory-plan.md).

One file on this PC. `facts` holds what the user asked JARVIS to remember; `conversations` and
`messages` hold past conversations (filled from step 4 of the plan). The FTS5 tables are keyword
indexes kept in step by triggers; `embedding` columns are filled from step 2 (ONNX MiniLM).
A connection is opened per operation (callers run on several threads); WAL lets reads overlap a write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 2: facts.uses
SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    added TEXT NOT NULL,             -- ISO time, local; updated when a fact is reworded
    source TEXT NOT NULL DEFAULT '', -- voice / chat / import
    embedding BLOB,                  -- float32 vector, or NULL until computed
    uses INTEGER NOT NULL DEFAULT 0  -- times recall returned it: the most-used go in the system instruction
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    ended TEXT,
    channels TEXT NOT NULL DEFAULT '',
    title TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    time TEXT NOT NULL,
    role TEXT NOT NULL,              -- user / assistant
    channel TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS messages_time ON messages(time);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, content='facts', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


class MemoryDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                if version == 1:  # made by memory step 1 or 2
                    db.execute("ALTER TABLE facts ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")
                db.executescript(SCHEMA)  # IF NOT EXISTS throughout: safe on an existing file
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for one operation: commits on success, rolls back on error, always closes."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_legacy_facts(self, json_path: Path) -> int:
        """One-time move of the old data/memory.json facts. The file is renamed (kept as a backup),
        so this runs once. Returns how many facts were imported."""
        json_path = Path(json_path)
        if not json_path.exists():
            return 0
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            facts = [f for f in data.get("facts", []) if isinstance(f, dict) and f.get("text")]
        except (OSError, ValueError, AttributeError):
            log.exception("old memory file unreadable; left in place")
            return 0
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]:
                facts = []  # already has facts (imported before, or new ones): never duplicate
            db.executemany("INSERT INTO facts(text, added, source) VALUES (?, ?, 'import')",
                           [(str(f["text"]), str(f.get("added", ""))) for f in facts])
        json_path.replace(json_path.with_name(json_path.name + ".imported"))
        log.info("memory: imported %d facts from %s", len(facts), json_path.name)
        return len(facts)
