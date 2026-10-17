"""Groq chat completions (free tier) with tool calling, for the reasoning layer (actions/brain.py).

Groq speaks the OpenAI chat-completions format at https://api.groq.com/openai/v1. Models are tried in
order (groq.models in config.yaml); a model that is rate-limited for longer than a few seconds is
skipped until its retry-after has passed, so a used-up daily quota doesn't cost a wait on every
request. Each call has its own timeout; 5xx errors and timeouts are retried once with backoff.

The key is GROQ_API_KEY (from .env) and is only ever put in the Authorization header: errors are
logged by status code and Groq's error code, never with headers or bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.ratelimit import TokenBucket, with_backoff
from core.secrets import env_key

log = logging.getLogger(__name__)

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
SHORT_WAIT_S = 5.0       # a retry-after up to this long is waited out; longer skips the model
MAX_COOLDOWN_S = 3600.0

# Replaced in tests: an httpx transport (e.g. MockTransport).
_transport: httpx.AsyncBaseTransport | None = None


class GroqUnavailable(Exception):
    """No key, or every model failed or is rate-limited. The caller falls back."""


class _Retryable(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall]
    model: str
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        """The assistant turn to send back with the tool results."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": c.arguments}} for c in self.tool_calls]
        return msg


class Groq:
    def __init__(self, config: dict | None = None, key: str | None = None, clock=time.monotonic) -> None:
        opts = (config or {}).get("groq") or {}
        self.models = [m for m in (opts.get("models") or DEFAULT_MODELS) if m]
        self.timeout_s = float(opts.get("timeout_s", 15))
        self.max_tokens = int(opts.get("max_tokens", 700))
        self.key = key if key is not None else env_key("GROQ_API_KEY")
        self.clock = clock
        self.buckets = {m: TokenBucket(float(rpm)) for m, rpm in (opts.get("rpm") or {}).items() if rpm}
        self._cooling: dict[str, float] = {}
        self.calls: dict[str, int] = {}  # model -> requests sent since start (for the logs)

    @property
    def available(self) -> bool:
        return bool(self.key) and any(self._cooling.get(m, 0) <= self.clock() for m in self.models)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   models: list[str] | None = None, max_tokens: int | None = None, timeout_s: float | None = None,
                   json_mode: bool = False) -> ChatResult:
        """One completion: the first model that answers. Raises GroqUnavailable."""
        if not self.key:
            raise GroqUnavailable("GROQ_API_KEY isn't set")
        timeout = min(self.timeout_s, timeout_s) if timeout_s else self.timeout_s
        for model in models or self.models:
            if self._cooling.get(model, 0) > self.clock():
                continue
            try:
                return await with_backoff(
                    lambda m=model: self._once(m, messages, tools, max_tokens or self.max_tokens, timeout, json_mode),
                    lambda e: isinstance(e, _Retryable), retries=1, base_s=0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Groq %s failed: %s", model, exc)
        raise GroqUnavailable("every Groq model failed or is rate-limited")

    async def _once(self, model: str, messages, tools, max_tokens: int, timeout: float, json_mode: bool) -> ChatResult:
        if model in self.buckets:
            await self.buckets[model].acquire()
        body: dict[str, Any] = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
                                "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self.calls[model] = self.calls.get(model, 0) + 1
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=_transport) as http:
                r = await http.post(API_URL, headers={"Authorization": f"Bearer {self.key}"}, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise _Retryable(type(exc).__name__) from None
        if r.status_code == 429:
            wait = _retry_after(r)
            if wait <= SHORT_WAIT_S:
                await asyncio.sleep(wait)
                raise _Retryable("rate limited briefly")
            self._cooling[model] = self.clock() + min(wait, MAX_COOLDOWN_S)
            raise RuntimeError(f"rate limited for {wait:.0f} s; skipping {model} until then")
        if r.status_code >= 500:
            raise _Retryable(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} ({_error_code(r)})")
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [ToolCall(c.get("id") or f"call_{i}", c["function"]["name"], c["function"].get("arguments") or "{}")
                 for i, c in enumerate(msg.get("tool_calls") or [])]
        usage = {k: int(v) for k, v in (data.get("usage") or {}).items()
                 if k in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(v, (int, float))}
        return ChatResult((msg.get("content") or "").strip(), calls, model, usage)


def _retry_after(r: httpx.Response) -> float:
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        value = r.headers.get(name)
        if value:
            try:
                return float(value.rstrip("s"))
            except ValueError:
                return _duration(value)
    return 60.0


def _duration(text: str) -> float:
    """Groq's reset headers look like '2m59.56s' or '7.66s'."""
    total, num = 0.0, ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            total += float(num) * {"h": 3600, "m": 60, "s": 1}.get(ch, 0)
            num = ""
    return total or 60.0


def _error_code(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return str(err.get("code") or err.get("type") or "error")[:60]
    except (ValueError, AttributeError):
        return "error"


def parse_args(text: str) -> dict[str, Any]:
    """Tool-call arguments as a dict; raises ValueError for anything else."""
    value = json.loads(text or "{}")
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value
