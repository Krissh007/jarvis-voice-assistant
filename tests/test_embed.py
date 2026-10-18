"""Memory plan step 2: on-device embeddings (core/embed.py) and fact vectors in the memory store."""

import hashlib
from pathlib import Path

import numpy as np
import pytest

from actions.memory import MemoryStore
from core import embed
from core.embed import Embedder, file_ok, from_blob, to_blob

REAL_MODEL = embed.model_dir(Path(__file__).resolve().parent.parent / "data")


def test_file_checks_size_and_hash(tmp_path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    sha = hashlib.sha256(b"hello").hexdigest()
    blob = hashlib.sha1(b"blob 5\0hello").hexdigest()  # what git (and Hugging Face) list for small files
    assert file_ok(f, 5, ("sha256", sha)) and file_ok(f, 5, ("git-blob", blob))
    assert not file_ok(f, 6, ("sha256", sha))
    assert not file_ok(f, 5, ("sha256", "0" * 64))
    assert not file_ok(tmp_path / "missing", 5, ("sha256", sha))


def test_download_refuses_a_file_that_does_not_match(tmp_path, monkeypatch) -> None:
    import httpx

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, _):
            yield b"not the model"

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Response())
    monkeypatch.setattr(embed, "FILES", {"model.onnx": ("onnx/model.onnx", 13, ("sha256", "0" * 64))})
    with pytest.raises(ValueError, match="didn't match"):
        embed.download(tmp_path, progress=lambda _: None)
    assert list(tmp_path.iterdir()) == []  # nothing half-downloaded is left to be loaded


def test_blob_round_trip() -> None:
    v = np.arange(4, dtype=np.float32) / 3
    assert np.array_equal(from_blob(to_blob(v)), v)


def test_without_the_model_memory_falls_back_to_keywords(tmp_path) -> None:
    e = Embedder(tmp_path / "none")
    assert not e.available() and e.warm() is None
    store = MemoryStore(tmp_path / "m.sqlite", embedder=e)
    store.add("My car is on level 3")
    assert store.backfill() == 0 and store.search("car")[0]["text"] == "My car is on level 3"


class FakeEmbedder:
    def __init__(self, ready=True):
        self.ready, self.calls = ready, []

    def available(self):
        return True

    def encode(self, texts):
        self.calls.append(list(texts))
        return np.array([[len(t), 1.0] for t in texts], dtype=np.float32)


def _vectors(store):
    with store.db.connect() as db:
        return {r["text"]: r["embedding"] for r in db.execute("SELECT text, embedding FROM facts")}


def test_facts_get_vectors_when_the_model_is_ready_and_backfill_fills_the_rest(tmp_path) -> None:
    fake = FakeEmbedder(ready=False)
    store = MemoryStore(tmp_path / "m.sqlite", embedder=fake)
    store.add("I like tea")  # model still loading: never blocks the tool call
    assert _vectors(store)["I like tea"] is None and fake.calls == []
    fake.ready = True
    store.add("I like Queen")
    assert from_blob(_vectors(store)["I like Queen"]).tolist() == [12.0, 1.0]
    assert store.backfill() == 1 and from_blob(_vectors(store)["I like tea"]).tolist() == [10.0, 1.0]
    assert store.backfill() == 0
    store.add("I like Queen and AC/DC")  # reworded: a new vector for the new words
    assert from_blob(_vectors(store)["I like Queen and AC/DC"]).tolist() == [22.0, 1.0]


def test_embeddings_can_be_turned_off(tmp_path) -> None:
    from actions import Services
    from actions.memory import register
    from core.registry import Registry

    services = Services(data_dir=tmp_path)
    register(Registry(), {"memory": {"embeddings": False}}, services)
    assert services.memory.embedder is None


@pytest.mark.skipif(not (REAL_MODEL / "model.onnx").exists(), reason="run main.py --setup-memory first")
def test_real_model_matches_meaning() -> None:
    e = Embedder(REAL_MODEL)
    food, bread, baby = e.encode(["A man is eating food.", "A man is eating a piece of bread.",
                                  "The girl is carrying a baby."])
    assert np.allclose(np.linalg.norm([food, bread, baby], axis=1), 1, atol=1e-5)
    assert food @ bread > 0.7 > 0.1 > food @ baby  # sentence-transformers gives 0.76 and about -0.1
    ask, fact, other = e.encode(["When is my wife's birthday?", "Priya was born on 3 March.",
                                 "I parked on level 3."])
    assert ask @ fact > ask @ other
