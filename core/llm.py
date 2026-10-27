"""Non-live text calls: Gemini Flash, then fallback Gemini models, then OpenRouter (§0.6, §0.6a).

The Flash model never sees secrets or memory; callers pass untrusted text already wrapped
with core.untrusted.wrap, and every call carries the untrusted-content rule.

Seen live (26-27 Oct): an overloaded Flash model answered 503 or hung until the 25 s timeout, three times
over, before the fallback answered in 3 s: "can you see my screen?" took 107 s. So the first model gets a
short timeout and one retry, and a model that keeps failing that way is skipped for a few minutes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from core.prompts import UNTRUSTED_RULE
from core.ratelimit import TokenBucket, with_backoff

log = logging.getLogger(__name__)

USAGE_LIMIT_MESSAGE = "I've hit my usage limit, try again in a minute."
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMUnavailable(Exception):
    """Every model and fallback failed; the tool should say USAGE_LIMIT_MESSAGE."""


class QuotaExceeded(Exception):
    """A 429 that retrying soon won't fix (daily quota, or a long retryDelay)."""


def quota_cooldown(exc: Exception) -> float | None:
    """For a 429, how long to skip this model: 1 h for a daily quota, else the server's retryDelay.
    None if retrying within a few seconds is reasonable."""
    if getattr(exc, "code", None) != 429:
        return None
    text = str(getattr(exc, "details", "")) + str(exc)
    if "PerDay" in text:
        return 3600.0
    match = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", text)
    delay = float(match.group(1)) if match else None
    return delay if delay and delay > 5 else None


def is_retryable(exc: Exception) -> bool:
    """429 (quota) and 5xx (overloaded) are worth retrying; bad requests are not."""
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if code in (429, 500, 502, 503, 504) or isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


def is_timeout(exc: Exception) -> bool:
    """The model ran out of time (504, or our own request timeout): trying it again costs the same wait.
    Seen live: a 504 after 9.5 s, a retry, a 503, then the backup answered: 24 s for a screen check."""
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    return (code == 504 or isinstance(exc, httpx.TimeoutException) or "timed out" in text
            or "deadline" in text or "timeout" in type(exc).__name__.lower())


def _no_retry_on_timeout(exc: Exception) -> bool:
    return is_retryable(exc) and not is_timeout(exc)


class LLM:
    def __init__(
        self,
        client: Any,
        models: list[str],
        rpm: dict[str, float | None] | None = None,
        openrouter_key: str | None = None,
        openrouter_model: str | None = None,
        retries: int = 1,
        base_delay_s: float = 1.0,
        thinking: dict[str, str] | None = None,
        fallback_retries: int = 1,
        timeout_s: float = 25.0,
        first_timeout_s: float = 8.0,
        struggle_cooldown_s: float = 180.0,
    ) -> None:
        self.client = client
        self.models = [m for m in models if m]
        self.buckets = {m: TokenBucket((rpm or {}).get(m)) for m in self.models}
        self.openrouter_key = openrouter_key
        self.openrouter_model = openrouter_model
        self.retries = retries
        self.base_delay_s = base_delay_s
        self.thinking = {k: v.upper() for k, v in (thinking or {}).items() if v}  # model -> thinking level
        self.fallback_retries = fallback_retries  # fallbacks get fewer retries, to bound voice latency
        self.timeout_s = timeout_s            # fallbacks: they're the last chance, so they get longer
        self.first_timeout_s = first_timeout_s  # the first model tried, plus a little for long answers
        self.struggle_cooldown_s = struggle_cooldown_s
        self.clock = time.monotonic
        self._cooling: dict[str, float] = {}  # model -> skip until (monotonic time): quota used up
        self._struggling: dict[str, float] = {}  # model -> skip until: overloaded or timing out
        self.last_fallback_at = 0.0   # when a call last moved on to a backup model (JARVIS can say so)

    def available(self, model: str | None) -> bool:
        """The model isn't being skipped (quota used up, or overloaded lately)."""
        now = self.clock()
        return bool(model) and self._cooling.get(model, 0) <= now and self._struggling.get(model, 0) <= now

    def _attempt_timeout(self, first: bool, max_tokens: int) -> float:
        if not first:
            return self.timeout_s
        return min(self.timeout_s, self.first_timeout_s + max_tokens / 200)

    async def generate(self, prompt: str, system: str = "", max_tokens: int = 800, json: bool = False,
                       images: list[bytes] | None = None, thinking: str | None = None,
                       start_at: str | None = None) -> str:
        """Return text, trying each model with backoff before falling back. Raises LLMUnavailable.
        `thinking` overrides every model's thinking level for this call (e.g. "high" for reasoning);
        `start_at` tries that model first (if it's in the chain), then the rest in order."""
        system = f"{system}\n\n{UNTRUSTED_RULE}".strip()
        models = self.models
        if start_at in models:
            models = [start_at, *(m for m in models if m != start_at)]
        now = self.clock()
        usable = [m for m in models if self._cooling.get(m, 0) <= now]   # quota used up: don't waste time on it
        ready = [m for m in usable if self._struggling.get(m, 0) <= now]
        chain = ready or usable   # all of them overloaded lately: try them anyway
        backup_after = bool(self.openrouter_key and self.openrouter_model and not images)
        for i, model in enumerate(chain):
            first = i == 0
            timeout = self._attempt_timeout(first, max_tokens)
            more = i < len(chain) - 1 or backup_after   # a timeout moves on at once when there's another model
            try:
                return await with_backoff(
                    lambda m=model, t=timeout: self._gemini(m, prompt, system, max_tokens, json, images, thinking, t),
                    _no_retry_on_timeout if more else is_retryable,
                    retries=self.retries if first else self.fallback_retries, base_s=self.base_delay_s,
                )
            except Exception as exc:
                log.warning("model %s failed: %s", model, exc)
                if not isinstance(exc, QuotaExceeded) and is_retryable(exc):
                    self._struggling[model] = self.clock() + self.struggle_cooldown_s
                    log.warning("%s is overloaded or timing out; skipping it for %.0f s", model, self.struggle_cooldown_s)
                self.last_fallback_at = self.clock()
        if self.openrouter_key and self.openrouter_model and not images:
            try:
                return await with_backoff(
                    lambda: self._openrouter(prompt, system, max_tokens),
                    is_retryable, retries=self.fallback_retries, base_s=self.base_delay_s,
                )
            except Exception as exc:
                log.warning("OpenRouter failed: %s", exc)
        raise LLMUnavailable(USAGE_LIMIT_MESSAGE)

    async def _gemini(self, model: str, prompt: str, system: str, max_tokens: int, json: bool = False,
                      images: list[bytes] | None = None, thinking: str | None = None,
                      timeout_s: float | None = None) -> str:
        from google.genai import types

        await self.buckets[model].acquire()
        level = thinking.upper() if thinking else self.thinking.get(model)
        try:
            response = await self._call(model, prompt, system, max_tokens, level, json, images, timeout_s)
        except Exception as exc:
            if (cooldown := quota_cooldown(exc)) is not None:
                self._cooling[model] = self.clock() + cooldown
                log.warning("%s quota exhausted; skipping it for %.0f s", model, cooldown)
                raise QuotaExceeded(str(exc)) from exc
            raise
        text = (response.text or "").strip()
        if not text:
            raise ValueError(f"{model} returned no text")
        candidates = getattr(response, "candidates", None) or []
        if candidates and getattr(candidates[0].finish_reason, "name", "") == "MAX_TOKENS":
            log.warning("%s hit max_output_tokens=%d; reply is cut short", model, max_tokens)
        return text

    async def _call(self, model: str, prompt: str, system: str, max_tokens: int, level: str | None, json: bool = False,
                    images: list[bytes] | None = None, timeout_s: float | None = None):
        from google.genai import types

        return await self.client.aio.models.generate_content(
            model=model,
            contents=[*(types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images or []), prompt],
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                response_mime_type="application/json" if json else None,
                thinking_config=types.ThinkingConfig(thinking_level=level) if level else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                http_options=types.HttpOptions(
                    timeout=int((timeout_s or self.timeout_s) * 1000),  # ms; an overloaded model can hang otherwise
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ),
        )

    async def _openrouter(self, prompt: str, system: str, max_tokens: int) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_s) as http:
            response = await http.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self.openrouter_key}"},
                json={
                    "model": self.openrouter_model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
