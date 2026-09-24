"""Token buckets, backoff with jitter, and a small persistent TTL cache (§0.6a)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class TokenBucket:
    """`rate_per_min` requests per minute, bursting up to `burst`. rate None = unlimited."""

    def __init__(self, rate_per_min: float | None, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> None:
        self.rate = rate_per_min
        self.capacity = float(burst or max(1, int(rate_per_min or 1)))
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if not self.rate:
            return
        async with self._lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate / 60)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self.sleep((1 - self.tokens) * 60 / self.rate)


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    retryable: Callable[[Exception], bool],
    retries: int = 3,
    base_s: float = 1.0,
    sleep=asyncio.sleep,
) -> T:
    """Call `fn`; on a retryable error wait base*2^n + jitter and try again, at most `retries` times."""
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt == retries or not retryable(exc):
                raise
            delay = base_s * 2**attempt + random.uniform(0, base_s)
            log.info("retryable error (%s); retry %d in %.1fs", exc, attempt + 1, delay)
            await sleep(delay)
    raise AssertionError("unreachable")


class TTLCache:
    """JSON-file cache. Keys are hashed; entries older than `ttl_s` are ignored and pruned on save."""

    def __init__(self, path: Path, ttl_s: float, clock: Callable[[], float] = time.time, max_entries: int = 500) -> None:
        self.path = path
        self.ttl_s = ttl_s
        self.clock = clock
        self.max_entries = max_entries
        self._data: dict[str, dict[str, Any]] = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry and self.clock() - entry["t"] < self.ttl_s:
            return entry["v"]
        return None

    def set(self, key: str, value: Any) -> None:
        now = self.clock()
        self._data[key] = {"t": now, "v": value}
        fresh = {k: e for k, e in self._data.items() if now - e["t"] < self.ttl_s}
        self._data = dict(sorted(fresh.items(), key=lambda kv: kv[1]["t"])[-self.max_entries:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError:
            log.exception("could not write cache %s", self.path)
