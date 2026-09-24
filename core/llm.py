"""Non-live text calls: Gemini Flash, then fallback Gemini models, then OpenRouter (§0.6, §0.6a).

The Flash model never sees secrets or memory; callers pass untrusted text already wrapped
with core.untrusted.wrap, and every call carries the untrusted-content rule.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.prompts import UNTRUSTED_RULE
from core.ratelimit import TokenBucket, with_backoff

log = logging.getLogger(__name__)

USAGE_LIMIT_MESSAGE = "I've hit my usage limit, try again in a minute."
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMUnavailable(Exception):
    """Every model and fallback failed; the tool should say USAGE_LIMIT_MESSAGE."""


def is_retryable(exc: Exception) -> bool:
    """429 (quota) and 5xx (overloaded) are worth retrying; bad requests are not."""
    code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if code in (429, 500, 502, 503, 504) or isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


class LLM:
    def __init__(
        self,
        client: Any,
        models: list[str],
        rpm: dict[str, float | None] | None = None,
        openrouter_key: str | None = None,
        openrouter_model: str | None = None,
        retries: int = 3,
        base_delay_s: float = 1.0,
        thinking: dict[str, str] | None = None,
        fallback_retries: int = 1,
        timeout_s: float = 25.0,
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
        self.timeout_s = timeout_s

    async def generate(self, prompt: str, system: str = "", max_tokens: int = 800) -> str:
        """Return text, trying each model with backoff before falling back. Raises LLMUnavailable."""
        system = f"{system}\n\n{UNTRUSTED_RULE}".strip()
        for i, model in enumerate(self.models):
            try:
                return await with_backoff(
                    lambda m=model: self._gemini(m, prompt, system, max_tokens),
                    is_retryable, retries=self.retries if i == 0 else self.fallback_retries, base_s=self.base_delay_s,
                )
            except Exception as exc:
                log.warning("model %s failed: %s", model, exc)
        if self.openrouter_key and self.openrouter_model:
            try:
                return await with_backoff(
                    lambda: self._openrouter(prompt, system, max_tokens),
                    is_retryable, retries=self.fallback_retries, base_s=self.base_delay_s,
                )
            except Exception as exc:
                log.warning("OpenRouter failed: %s", exc)
        raise LLMUnavailable(USAGE_LIMIT_MESSAGE)

    async def _gemini(self, model: str, prompt: str, system: str, max_tokens: int) -> str:
        from google.genai import types

        await self.buckets[model].acquire()
        level = self.thinking.get(model)
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_level=level) if level else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                http_options=types.HttpOptions(
                    timeout=int(self.timeout_s * 1000),  # milliseconds; an overloaded model can hang otherwise
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError(f"{model} returned no text")
        candidates = getattr(response, "candidates", None) or []
        if candidates and getattr(candidates[0].finish_reason, "name", "") == "MAX_TOKENS":
            log.warning("%s hit max_output_tokens=%d; reply is cut short", model, max_tokens)
        return text

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
