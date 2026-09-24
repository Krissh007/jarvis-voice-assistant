"""Action plugins. Each module exposes `register(registry, config)`; adding a capability = adding a module."""

from __future__ import annotations

import importlib

from core.registry import Registry

# module name -> feature flag in config.yaml (None = always on)
MODULES: dict[str, str | None] = {
    "actions.system": "system",
    "actions.testing": "test_tools",
}


def load_actions(registry: Registry, config: dict) -> Registry:
    features = config.get("features", {})
    for module_name, flag in MODULES.items():
        if flag is None or features.get(flag):
            importlib.import_module(module_name).register(registry, config)
    return registry
