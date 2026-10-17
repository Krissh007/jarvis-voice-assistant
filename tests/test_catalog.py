"""JARVIS's skills as Agent Hub tool-registry entries (core/catalog.py)."""

import json
import re

import yaml

from actions import MODULES, Services, load_actions
from core import catalog
from core.registry import Registry

# The Hub's categories and limits (hub/tools/schema.py in E:\agent-hub); kept in step by hand.
HUB_CATEGORIES = {"search", "reasoning", "files", "mail", "calendar", "docs", "code", "browser", "desktop",
                  "screen", "media", "system", "memory", "reminders", "jobs"}
NAME = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def all_tools_registry(tmp_path) -> Registry:
    """Every action module switched on (as in the shipped config, plus the ones that are off)."""
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["features"].update({flag: True for flag in MODULES.values() if flag})
    config["features"]["test_tools"] = False
    services = Services()
    services.data_dir = tmp_path
    return load_actions(Registry(), config, services)


def test_every_skill_has_a_registry_category(tmp_path) -> None:
    assert catalog.uncatalogued(all_tools_registry(tmp_path)) == []
    assert set(catalog.CATALOG).isdisjoint(catalog.EXCLUDED)


def test_entries_fit_the_hub_schema(tmp_path) -> None:
    entries = catalog.export(all_tools_registry(tmp_path))
    assert len(entries) > 30
    for e in entries:
        assert NAME.match(e["name"]) and e["category"] in HUB_CATEGORIES
        assert 0 < len(e["description"]) <= catalog.MAX_DESCRIPTION
        assert e["access"] in ("read", "write")
        assert e["params"]["type"] == "object"
        assert len(e["keywords"]) <= 20 and all(len(k) <= 40 for k in e["keywords"])
        assert set(e) <= {"name", "description", "category", "keywords", "access", "params", "auth", "timeout_s"}
    json.dumps(entries)  # must travel over MCP as JSON


def test_access_follows_the_tool_and_meta_tools_stay_out(tmp_path) -> None:
    by_name = {e["name"]: e for e in catalog.export(all_tools_registry(tmp_path))}
    assert by_name["search_web"]["access"] == "read"
    assert by_name["delete_file"]["access"] == "write" and by_name["play_music"]["access"] == "write"
    assert not catalog.EXCLUDED & set(by_name)                 # run_plan, agent_hub_request... are not skills
    assert by_name["search_web"]["auth"] == {"env": ["TAVILY_API_KEY"], "optional": True}


def test_nothing_secret_or_personal_is_exported(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleValue1234567890abcdef")
    text = json.dumps(catalog.export(all_tools_registry(tmp_path)))
    assert "AIzaSyExampleValue" not in text and "memory.json" not in text


def test_long_descriptions_end_on_a_sentence() -> None:
    from core.registry import Tier, Tool
    reg = Registry()
    reg.register(Tool(name="find_files", description="Find files. " * 100, func=lambda: "", tier=Tier.SAFE,
                      read_only=True))
    (entry,) = catalog.export(reg)
    assert len(entry["description"]) <= catalog.MAX_DESCRIPTION and entry["description"].endswith(".")
