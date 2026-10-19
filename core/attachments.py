"""Pictures attached in the app's chat: uploaded images and "attach my screen" snapshots.

Each is decoded and re-encoded as a JPEG (so only real images get in, and their metadata, such as a
photo's location, is dropped), shrunk to at most MAX_SIDE pixels, and kept in memory only, for a few
minutes, until the message it belongs to is sent. Nothing is written to disk. Screen snapshots go
through actions/screen.capture, which blacks out private windows first.

The model never gets the picture's own text as instructions: what the vision model sees is passed on
wrapped as untrusted data (core/runtime.py).
"""

from __future__ import annotations

import base64
import io
import logging
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SIDE = 1600
MAX_PIXELS = 40_000_000       # refuse decompression bombs
KEEP_S = 15 * 60
MAX_KEPT = 8
PREVIEW_SIDE = 320
MAX_PER_MESSAGE = 4


@dataclass
class Attachment:
    jpeg: bytes
    preview: str            # small data: URL for the chat thread
    name: str
    added: float


class AttachmentError(ValueError):
    """The upload isn't a picture JARVIS can use; the message is for the user."""


def _encode(img, side: int, quality: int) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def to_jpeg(raw: bytes) -> tuple[bytes, str]:
    """(JPEG for the model, data: URL preview). Raises AttachmentError for anything that isn't an image."""
    from PIL import Image, UnidentifiedImageError

    if not raw:
        raise AttachmentError("That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AttachmentError("That picture is too big (10 MB at most).")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
                raise AttachmentError("Attach a PNG, JPEG, WebP, GIF or BMP picture.")
            if probe.width * probe.height > MAX_PIXELS:
                raise AttachmentError("That picture has too many pixels.")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.seek(0)
            jpeg = _encode(img, MAX_SIDE, 85)
            preview = _encode(img, PREVIEW_SIDE, 70)
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AttachmentError("That isn't a picture JARVIS can read.") from None
    return jpeg, "data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii")


class AttachmentStore:
    def __init__(self, clock=time.monotonic) -> None:
        self._items: dict[str, Attachment] = {}
        self._clock = clock

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, a in self._items.items() if now - a.added > KEEP_S]:
            del self._items[key]
        while len(self._items) > MAX_KEPT:
            del self._items[next(iter(self._items))]

    def add(self, raw: bytes, name: str = "picture") -> dict:
        jpeg, preview = to_jpeg(raw)
        return self._keep(jpeg, preview, name)

    def add_jpeg(self, jpeg: bytes, name: str) -> dict:
        """An image JARVIS made itself (a redacted screen capture)."""
        from actions.screen import preview_data_url

        return self._keep(jpeg, preview_data_url(jpeg, PREVIEW_SIDE), name)

    def _keep(self, jpeg: bytes, preview: str, name: str) -> dict:
        self._expire()
        key = secrets.token_urlsafe(12)
        self._items[key] = Attachment(jpeg, preview, " ".join(str(name).split())[:80] or "picture", self._clock())
        self._expire()
        log.info("attachment kept (%d KB, %s)", len(jpeg) // 1024, "screen" if name == "screen" else "upload")
        return {"id": key, "preview": preview, "name": self._items[key].name}

    def take(self, ids: list) -> list[Attachment]:
        """The pictures for one message (each used once), in the order given; unknown ids are skipped."""
        self._expire()
        out = []
        for key in ids[:MAX_PER_MESSAGE] if isinstance(ids, list) else []:
            if isinstance(key, str) and key in self._items:
                out.append(self._items.pop(key))
        return out
