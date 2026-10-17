"""For tests/test_router.py: the labelled samples, a stand-in Hub that
ranks JARVIS's skills plus the Hub's JobPilot tools exactly like the real registry, and a scorer."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from core import catalog
from core.router import LocalIndex, Router

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def samples() -> list[dict]:
    with open(FIXTURES / "routing_samples.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["samples"]


class IndexHub:
    """find_tools over JARVIS's skills + the Hub's own tools (tests/fixtures/hub_tools.json)."""

    def __init__(self, registry) -> None:
        hub_tools = json.loads((FIXTURES / "hub_tools.json").read_text(encoding="utf-8"))["tools"]
        self.index = LocalIndex(catalog.export(registry) + hub_tools)
        self.calls = 0

    async def find_tools(self, query, categories=None, access=None, limit=8):
        self.calls += 1
        return [{**p.__dict__} for p in self.index.find(query, categories, limit)
                if access is None or p.access == access]


async def score(router: Router, cases: list[dict] | None = None) -> dict:
    """Route every sample; count route hits (expected or an allowed alternative) and tool hits."""
    rows, route_ok, tool_ok, tool_n = [], 0, 0, 0
    for case in cases or samples():
        d = await router.route(case["text"])
        good_route = d.route == case["route"] or d.route in case.get("ok", [])
        good_tool = None
        if case.get("tool"):
            tool_n += 1
            good_tool = case["tool"] in d.tool_ids
            tool_ok += good_tool
        if case.get("missing"):
            good_route = good_route and case["missing"] in d.missing
        route_ok += good_route
        rows.append({"text": case["text"], "want": case["route"], "got": d.route, "tool": case.get("tool"),
                     "tools": d.tool_ids, "ok": good_route and good_tool is not False, "reason": d.reason,
                     "n": len(d.tools), "missing": d.missing})
    n = len(rows)
    return {"n": n, "route_acc": route_ok / n, "tool_recall": tool_ok / max(tool_n, 1), "rows": rows}
