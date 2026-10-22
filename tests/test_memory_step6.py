"""Memory plan step 6 (check): every kept conversation line gets a meaning vector, without slowing
searches. Before this, lines were embedded only during searches, 64 at a time and only among the
newest 2000, so a month of conversations was mostly keyword-only and the first searches took ~0.8 s
longer (measured on 3000 lines)."""

import numpy as np
import pytest

from core import conversation_log as cl
from core.conversation_log import ConversationLog
from core.memory_db import MemoryDB


class FakeEmbedder:
    def __init__(self, ready=True):
        self.ready, self.calls = ready, []

    def available(self):
        return True

    def encode(self, texts):
        self.calls.append(len(texts))
        return np.array([[len(t), 1.0] for t in texts], dtype=np.float32)


def _missing(log):
    with log.db.connect() as db:
        return db.execute("SELECT COUNT(*) FROM messages WHERE embedding IS NULL").fetchone()[0]


@pytest.fixture
def db(tmp_path):
    return MemoryDB(tmp_path / "memory.sqlite")


def test_a_line_is_saved_with_its_vector_when_the_model_is_loaded(db) -> None:
    fake = FakeEmbedder()
    log = ConversationLog(db, embedder=fake)
    log.add("user", "Should I learn Rust or Go?")
    assert _missing(log) == 0 and fake.calls == [1]


def test_lines_kept_while_the_model_loads_are_filled_by_the_backfill(db) -> None:
    fake = FakeEmbedder(ready=False)
    log = ConversationLog(db, embedder=fake)
    for i in range(150):
        log.add("user", f"line {i}")
    assert _missing(log) == 150 and fake.calls == []  # never blocks a save on the first load
    fake.ready = True
    assert log.backfill(batch=64) == 150 and _missing(log) == 0
    assert fake.calls == [64, 64, 22] and log.backfill() == 0


def test_a_save_during_the_backfill_leaves_its_vector_to_it(db) -> None:
    fake = FakeEmbedder()
    log = ConversationLog(db, embedder=fake)
    log._backfilling = True  # the background thread is busy embedding a batch
    log.add("user", "hello")
    assert _missing(log) == 1 and fake.calls == []  # JARVIS's loop didn't wait for the batch
    log._backfilling = False
    assert log.backfill() == 1


def test_a_search_computes_only_a_few_missing_vectors(db, monkeypatch) -> None:
    fake = FakeEmbedder(ready=False)
    log = ConversationLog(db, embedder=fake)
    for i in range(100):
        log.add("user", f"trip note {i}")
    fake.ready = True
    log.search("trip")
    assert fake.calls[0] == cl.SEARCH_TOPUP and _missing(log) == 100 - cl.SEARCH_TOPUP


def test_start_up_backfills_facts_and_conversations(tmp_path, monkeypatch) -> None:
    from actions import Services
    from actions import memory as mem
    from core import embed
    from core.registry import Registry

    warmed = []

    class Model(FakeEmbedder):
        def __init__(self, folder):
            super().__init__(ready=False)

        def warm(self, then=None):
            self.ready = True
            warmed.append(then())

    # kept earlier, while there was no model
    earlier = mem.MemoryStore(tmp_path / "memory.sqlite")
    earlier.add("I like tea")
    ConversationLog(earlier.db).add("user", "hello there")
    monkeypatch.setattr(embed, "Embedder", Model)
    services = Services(data_dir=tmp_path)
    mem.register(Registry(), {"memory": {}}, services)
    assert warmed == [(1, 1)]  # one fact and one line filled in the background
    assert _missing(services.conversations) == 0
