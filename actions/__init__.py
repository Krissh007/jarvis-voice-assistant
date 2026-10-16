"""Action plugins. Each module exposes `register(registry, config)`; adding a capability = adding a module."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.registry import Registry

# module name -> feature flag in config.yaml (None = always on)
MODULES: dict[str, str | None] = {
    "actions.system": "system",
    "actions.web": "web",
    "actions.reasoning": "reasoning",  # after web: it searches through it
    "actions.browser": "browser",
    "actions.media": "media",
    "actions.app_ui": "app_ui",
    "actions.screen": "screen",
    "actions.clipboard": "clipboard",
    "actions.files": "files",
    "actions.file_edit": "file_edit",  # after files: bare names are looked up in its index
    "actions.desktop": "desktop",
    "actions.memory": "memory",
    "actions.reminders": "reminders",
    "actions.testing": "test_tools",
    "actions.job_search": "job_search",
    "actions.agent_hub": "agent_hub",
    "actions.agent_mode": "agent_mode",  # last: it plans over every tool registered before it
}


@dataclass
class Services:
    """Shared things action modules may need. The LLM never sees secrets or memory."""

    llm: Any = None  # core.llm.LLM
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    browser: Any = None  # actions.browser.BrowserTools, shared with media
    desktop: Any = None  # actions.desktop.Desktop: the current app, for reading and acting in apps
    files: Any = None  # actions.files.FileTools: the file-name index, for finding a file by name
    dispatcher: Any = None  # core.dispatcher.Dispatcher, set by the runtime (agent mode runs steps through it)
    web: Any = None  # actions.web.Web, for the morning briefing
    memory: Any = None  # actions.memory.MemoryStore, for the system instruction
    reminders: Any = None  # actions.reminders.Reminders, started by the runtime
    ducker: Any = None  # core.ducking.Ducker: music-only volume and ducking while JARVIS is awake
    on_start: list = field(default_factory=list)  # called once when JARVIS starts (not in tests), e.g. warm-ups
    background: list = field(default_factory=list)  # async callables the runtime runs as tasks for its lifetime
    notify: Callable[[str], None] | None = None  # sends a [system notice] to the model; set by the runtime
    can_speak: Callable[[], bool] | None = None  # True when a result may be announced right now
    taint: Callable[[], None] | None = None  # marks the turn as carrying untrusted text


def load_actions(registry: Registry, config: dict, services: Services | None = None) -> Registry:
    features = config.get("features", {})
    services = services or Services()
    for module_name, flag in MODULES.items():
        if flag is None or features.get(flag):
            importlib.import_module(module_name).register(registry, config, services)
    return registry
