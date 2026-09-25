"""Action plugins. Each module exposes `register(registry, config)`; adding a capability = adding a module."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.registry import Registry

# module name -> feature flag in config.yaml (None = always on)
MODULES: dict[str, str | None] = {
    "actions.system": "system",
    "actions.web": "web",
    "actions.browser": "browser",
    "actions.media": "media",
    "actions.app_ui": "app_ui",
    "actions.screen": "screen",
    "actions.clipboard": "clipboard",
    "actions.testing": "test_tools",
    "actions.agent_mode": "agent_mode",  # last: it plans over every tool registered before it
}


@dataclass
class Services:
    """Shared things action modules may need. The LLM never sees secrets or memory."""

    llm: Any = None  # core.llm.LLM
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    browser: Any = None  # actions.browser.BrowserTools, shared with media
    dispatcher: Any = None  # core.dispatcher.Dispatcher, set by the runtime (agent mode runs steps through it)


def load_actions(registry: Registry, config: dict, services: Services | None = None) -> Registry:
    features = config.get("features", {})
    services = services or Services()
    for module_name, flag in MODULES.items():
        if flag is None or features.get(flag):
            importlib.import_module(module_name).register(registry, config, services)
    return registry
