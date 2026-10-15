"""Tool registration, schemas, tiers, untrusted-output flag.

Each action module calls `registry.register(Tool(...))`. The registry decides what the
model is allowed to see: BLOCKED tools are never sent to it.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from google.genai import types


# True while a step of a user-approved plan runs with exactly the arguments the user heard (no blanks
# filled from results). Self-gated tools may then skip asking just because the turn is tainted; their
# own risky-step checks still apply.
APPROVED_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar("approved_step", default=False)


class ToolError(Exception):
    """Raise from a tool when the message itself is what the user should hear."""


class Tier(IntEnum):
    """Risk tier. Higher is stricter; tiers can be escalated at runtime, never lowered."""

    SAFE = 0
    CONFIRM = 1
    BLOCKED = 2


ToolFunc = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass
class NeedsConfirmation:
    """Returned by a multi-step tool when its next step needs the user's OK.

    `summary` is written by code from the actual step; `resume` runs that step and continues,
    returning a str or another NeedsConfirmation. The dispatcher treats it exactly like a
    CONFIRM tool call: same accept phrases, echo gating, timeout and audit.
    """

    summary: str
    resume: Callable[[], Awaitable[Any]]
    step: dict[str, Any] = field(default_factory=dict)
    progress: str = ""


def default_summary(name: str, args: dict[str, Any]) -> str:
    """Code-written summary of an action, built only from the tool name and actual args."""
    shown = ", ".join(f"{k} {v!r}" for k, v in args.items())
    text = name.replace("_", " ") + (f" with {shown}" if shown else "")
    return text[:300]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    func: ToolFunc
    tier: Tier | Callable[[dict[str, Any]], Tier]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only: bool = False
    untrusted_output: bool = False
    # Enforces its own per-step tiers (e.g. browser_task), so taint doesn't escalate the whole call.
    self_gated: bool = False
    summarize: Callable[[dict[str, Any]], str] | None = None
    rung: int = 2

    def base_tier(self, args: dict[str, Any]) -> Tier:
        return self.tier(args) if callable(self.tier) else self.tier

    def summary(self, args: dict[str, Any]) -> str:
        return self.summarize(args) if self.summarize else default_summary(self.name, args)

    @property
    def statically_blocked(self) -> bool:
        return not callable(self.tier) and self.tier == Tier.BLOCKED


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._turn_hooks: list[Callable[[], None]] = []
        self.last_user_text = ""  # the user's own latest utterance or typed line (never model/tool text)

    def on_user_turn(self, hook: Callable[[], None]) -> None:
        """Run `hook` whenever the user starts a new turn (e.g. to reset per-question caps)."""
        self._turn_hooks.append(hook)

    def user_turn(self) -> None:
        for hook in self._turn_hooks:
            hook()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} registered twice")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def visible(self) -> list[Tool]:
        """Tools the model may know about (everything except BLOCKED)."""
        return [t for t in self._tools.values() if not t.statically_blocked]

    def declarations(self) -> list[types.FunctionDeclaration]:
        return [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters,
                behavior=types.Behavior.BLOCKING,
            )
            for t in self.visible()
        ]

    def summaries(self) -> list[str]:
        """One line per visible tool, for the system prompt."""
        return [f"{t.name}: {t.description.splitlines()[0]}" for t in self.visible()]
