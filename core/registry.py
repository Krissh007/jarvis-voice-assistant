"""Tool registration, schemas, tiers, untrusted-output flag.

Each action module calls `registry.register(Tool(...))`. The registry decides what the
model is allowed to see: BLOCKED tools are never sent to it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from google.genai import types


class Tier(IntEnum):
    """Risk tier. Higher is stricter; tiers can be escalated at runtime, never lowered."""

    SAFE = 0
    CONFIRM = 1
    BLOCKED = 2


ToolFunc = Callable[..., str] | Callable[..., Awaitable[str]]


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
