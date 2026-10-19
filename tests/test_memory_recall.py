"""Memory plan step 3: recall by meaning, top facts in the system instruction, schema upgrade."""

import sqlite3
from pathlib import Path

import numpy as np
import pytest
import yaml

import actions.memory as mem
from actions.memory import MemoryStore, prompt_section
from core.embed import Embedder, model_dir

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = yaml.safe_load((ROOT / "tests/fixtures/memory_recall_samples.yaml").read_text(encoding="utf-8"))
REAL_MODEL = model_dir(ROOT / "data")


def _accuracy(store, key_of, questions):
    top1 = in3 = none_empty = 0
    for s in questions:
        hits = [key_of[f["text"]] for f in store.search(s["q"])]
        if s["want"] == "none":
            none_empty += not hits
        else:
            top1 += bool(hits) and hits[0] == s["want"]
            in3 += s["want"] in hits
    return top1, in3, none_empty


@pytest.mark.skipif(not (REAL_MODEL / "model.onnx").exists(), reason="run main.py --setup-memory first")
def test_recall_by_meaning_on_the_labelled_questions(tmp_path) -> None:
    store = MemoryStore(tmp_path / "m.sqlite", embedder=Embedder(REAL_MODEL))
    key_of = {}
    for key, text in SAMPLES["facts"].items():
        store.add(text)
        key_of[text] = key
    # measured 19 October 2026: tuning 24/26 first, 25/26 in the top 3, 4/6 unanswerable got nothing;
    # holdout 12/12 and 12/12 (its 4 unanswerable all got a nearby fact; recall says they may not answer)
    top1, in3, none_empty = _accuracy(store, key_of, SAMPLES["samples"])
    assert top1 >= 23 and in3 >= 24 and none_empty >= 3
    top1, in3, _ = _accuracy(store, key_of, SAMPLES["holdout"])
    assert top1 >= 11 and in3 >= 11


def test_keyword_search_found_only_half_without_the_model(tmp_path) -> None:
    store = MemoryStore(tmp_path / "m.sqlite")
    key_of = {}
    for key, text in SAMPLES["facts"].items():
        store.add(text)
        key_of[text] = key
    assert _accuracy(store, key_of, SAMPLES["samples"])[0] == 13  # why step 3 exists: 13/26 first


class Vectors:
    """A fake model: each text maps to a fixed unit vector."""

    ready = True

    def __init__(self, table):
        self.table = {k: np.array(v, dtype=np.float32) / np.linalg.norm(v) for k, v in table.items()}

    def available(self):
        return True

    def encode(self, texts):
        return np.stack([self.table[t] for t in texts])


def test_search_keeps_close_candidates_and_drops_unrelated(tmp_path) -> None:
    fake = Vectors({"Priya was born on 3 March": [1, 0, 0], "My wife's name is Priya": [0.9, 0.44, 0],
                    "I parked on level 3": [0, 0, 1], "birthday of my wife": [1, 0.1, 0],
                    "what's my blood group": [0.1, 0, 0.1]})
    store = MemoryStore(tmp_path / "m.sqlite", embedder=fake)
    for text in ["Priya was born on 3 March", "My wife's name is Priya", "I parked on level 3"]:
        store.add(text)
    assert [f["text"] for f in store.search("birthday of my wife")] == [
        "Priya was born on 3 March", "My wife's name is Priya"]  # the level-3 fact is far behind: dropped
    store.embedder.table["what's my blood group"] = np.array([0.0, 0.2, 0.98], dtype=np.float32) * 0.2
    assert store.search("what's my blood group") == []  # best score under MIN_SCORE: nothing


def test_recall_says_the_hits_may_not_answer_and_counts_uses(tmp_path) -> None:
    from core.registry import Registry

    store = MemoryStore(tmp_path / "m.sqlite")
    store.add("My sister Ananya lives in Pune")
    memory = mem.Memory(store, Registry())
    out = memory.recall("when is Ananya's birthday")
    assert "may not answer" in out and "Ananya lives in Pune" in out
    assert store.facts()[0]["uses"] == 1
    assert memory.recall("").startswith("Remembered:") and store.facts()[0]["uses"] == 1  # listing isn't use


def test_system_instruction_gets_talk_facts_and_the_most_used(tmp_path) -> None:
    store = MemoryStore(tmp_path / "m.sqlite")
    store.add("I should be friendly and use humour")
    for i in range(24):
        store.add(f"note number {i} about topic {i}")
    store.mark_used([f for f in store.facts() if f["text"] == "note number 0 about topic 0"])
    text = prompt_section(store)
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == mem.PROMPT_FACTS
    assert "- I should be friendly and use humour" in lines  # how to talk: always there
    assert "- note number 0 about topic 0" in lines  # recalled before: kept although it's old
    assert "- note number 1 about topic 1" not in lines  # old and never recalled: a recall away
    assert "5 more are saved: use recall" in text


def test_a_step_one_database_is_upgraded(tmp_path) -> None:
    path = tmp_path / "memory.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, added TEXT NOT NULL, "
                     "source TEXT NOT NULL DEFAULT '', embedding BLOB);"
                     "INSERT INTO facts(text, added) VALUES ('I like tea', '2026-10-18T09:00:00');"
                     "PRAGMA user_version = 1;")
    db.close()
    store = MemoryStore(path)
    assert store.facts() == [{"id": 1, "text": "I like tea", "added": "2026-10-18T09:00:00", "uses": 0}]
    store.mark_used(store.facts())
    assert MemoryStore(path).facts()[0]["uses"] == 1
