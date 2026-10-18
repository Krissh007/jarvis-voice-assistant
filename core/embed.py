"""Sentence embeddings on this PC: all-MiniLM-L6-v2 run with onnxruntime (memory plan, step 2).

Turns a sentence into 384 numbers so memory can be searched by meaning ("my wife's birthday" finds
"Priya was born on 3 March"). The model files live in data/models/ and are fetched once by
`main.py --setup-memory`, pinned to one revision and checked by hash. Text never leaves the PC.
Without the files, memory still works with keyword search only (`available()` is False).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

REPO = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"  # checked 18 October 2026
DIM = 384
MAX_TOKENS = 256  # what the model was trained on; longer text is cut
# local name -> (path in the repo, size, how to check it)
FILES = {
    "model.onnx": ("onnx/model.onnx", 90_405_214,
                   ("sha256", "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452")),
    "tokenizer.json": ("tokenizer.json", 466_247, ("git-blob", "cb202bfe2e3c98645018a6d12f182a434c9d3e02")),
}


def model_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / "all-MiniLM-L6-v2"


def file_ok(path: Path, size: int, check: tuple[str, str]) -> bool:
    if not path.exists() or path.stat().st_size != size:
        return False
    kind, want = check
    if kind == "sha256":
        h = hashlib.sha256()
    else:  # git's blob id: sha1 of a header plus the content
        h = hashlib.sha1(f"blob {size}\0".encode())
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest() == want


def download(folder: Path, progress: Callable[[str], None] = print) -> None:
    """Fetch the pinned model files that are missing or wrong. Raises on a failed download or a bad hash."""
    import httpx

    folder.mkdir(parents=True, exist_ok=True)
    for name, (remote, size, check) in FILES.items():
        target = folder / name
        if file_ok(target, size, check):
            progress(f"  {name}: already here")
            continue
        url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{remote}"
        progress(f"  {name}: downloading {size / 1e6:.1f} MB...")
        part = target.with_name(name + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r, part.open("wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        if not file_ok(part, size, check):
            part.unlink(missing_ok=True)
            raise ValueError(f"{name} didn't match the expected file (size or hash); not used")
        part.replace(target)


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class Embedder:
    """Loads the model on first use (or in the background with `warm()`); encode() is thread-safe."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self.load_s: float | None = None

    def available(self) -> bool:
        return all((self.folder / name).exists() for name in FILES)

    @property
    def ready(self) -> bool:
        return self._session is not None

    def _load(self) -> None:
        import time

        import onnxruntime as ort
        from tokenizers import Tokenizer

        started = time.monotonic()
        tokenizer = Tokenizer.from_file(str(self.folder / "tokenizer.json"))
        tokenizer.enable_truncation(MAX_TOKENS)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2  # leave the CPU to the voice loop
        session = ort.InferenceSession(str(self.folder / "model.onnx"), options,
                                       providers=["CPUExecutionProvider"])
        self._tokenizer, self._session = tokenizer, session
        self.load_s = time.monotonic() - started
        log.info("embeddings: model loaded in %.2fs", self.load_s)

    def warm(self, then: Callable[[], None] | None = None) -> threading.Thread | None:
        """Load in a background thread (so startup isn't slowed), then call `then` (e.g. a backfill)."""
        if not self.available():
            return None

        def run() -> None:
            try:
                self.encode(["warm up"])
                if then is not None:
                    then()
            except Exception:
                log.exception("embeddings: background load failed; memory uses keyword search only")

        thread = threading.Thread(target=run, name="embed-warm", daemon=True)
        thread.start()
        return thread

    def encode(self, texts: list[str]) -> np.ndarray:
        """(len(texts), 384) float32, unit length: a dot product is the cosine similarity."""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        with self._lock:
            if self._session is None:
                self._load()
            batch = self._tokenizer.encode_batch(list(texts))
            ids = np.array([e.ids for e in batch], dtype=np.int64)
            mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if any(i.name == "token_type_ids" for i in self._session.get_inputs()):
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feeds)[0]  # (batch, tokens, 384)
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)  # mean of real tokens
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)
