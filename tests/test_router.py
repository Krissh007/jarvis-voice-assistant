"""Intent routing (core/router.py): routes, tool selection, Hub fallback, the optional classifier, logging."""

import asyncio
import json

import pytest
import yaml

from actions.agent_hub import HubClient
from core import router as router_mod
from core.router import MAX_TOOLS, Router, tokens
from tests.routing_helpers import FIXTURES, IndexHub, samples, score
from tests.test_agent_hub import fake_hub_server
from tests.test_catalog import all_tools_registry


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    return all_tools_registry(tmp_path_factory.mktemp("router"))


@pytest.fixture
def router(registry, tmp_path):
    return Router(registry, hub=IndexHub(registry), log_path=tmp_path / "routing.jsonl")


def holdout() -> list[dict]:
    with open(FIXTURES / "routing_samples.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["holdout"]


# ---------------------------------------------------------------- accuracy on labelled requests

async def test_labelled_samples(router) -> None:
    result = await score(router)
    misses = [r for r in result["rows"] if not r["ok"]]
    assert result["route_acc"] >= 0.95 and result["tool_recall"] >= 0.95, misses


async def test_held_out_requests(router) -> None:
    result = await score(router, holdout())
    misses = [r for r in result["rows"] if not r["ok"]]
    assert result["route_acc"] >= 0.85 and result["tool_recall"] >= 0.85, misses


async def test_without_the_hub_jarvis_skills_still_route(registry) -> None:
    cases = [c for c in samples() if not str(c.get("tool", "")).startswith("jobpilot.")]
    result = await score(Router(registry, hub=None), cases)
    assert result["route_acc"] >= 0.9 and result["tool_recall"] >= 0.9


# ---------------------------------------------------------------- what gets attached

async def test_between_one_and_eight_tools_never_the_registry(router) -> None:
    for case in samples() + holdout():
        d = await router.route(case["text"])
        assert 1 <= len(d.tools) <= MAX_TOOLS, case["text"]
        assert len(set(d.tool_ids)) == len(d.tool_ids)
        if d.route == "skill":
            assert len(d.tools) <= 3
        if d.route == "tools" and any(t.access == "write" for t in d.tools):
            assert len(d.tools) >= 3, (case["text"], d.tool_ids)   # questions may get fewer: read-only


async def test_questions_get_read_only_tools(router) -> None:
    for text in ("what reminders do I have", "what's on my shortlist", "which song is this"):
        d = await router.route(text)
        assert {t.access for t in d.tools} == {"read"}, (text, d.tool_ids)


async def test_requests_to_act_keep_the_write_tool(router) -> None:
    d = await router.route("approve application 12")
    assert d.tools[0].id == "jobpilot.approve_application" and d.tools[0].access == "write"


async def test_reasoning_gets_web_tools_and_fitting_context(router) -> None:
    d = await router.route("why is the sky blue")
    assert d.route == "reasoning" and d.tool_ids == ["jarvis.search_web", "jarvis.read_page"]
    d = await router.route("explain what's on my screen")
    assert d.route == "reasoning" and "jarvis.look_at_screen" in d.tool_ids
    d = await router.route("should I bring an umbrella, is the weather forecast rainy")
    assert d.route == "reasoning" and "jarvis.get_weather" in d.tool_ids


async def test_areas_without_tools_are_reported_missing(router) -> None:
    d = await router.route("check my email for anything from HR")
    assert d.route == "reasoning" and d.missing == ["mail"] and d.reason == "no mail tools yet"
    assert not any(t.category == "browser" for t in d.tools)


# ---------------------------------------------------------------- the Hub, and falling back

class BrokenHub:
    def __init__(self, error=None, delay=0.0) -> None:
        self.error, self.delay, self.calls = error, delay, 0

    async def find_tools(self, *a, **k):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        raise self.error or RuntimeError("down")


async def test_hub_failure_uses_local_skills_and_waits_before_retrying(registry) -> None:
    now = [0.0]
    hub = BrokenHub()
    r = Router(registry, hub=hub, clock=lambda: now[0])
    d = await r.route("will it rain tomorrow in Pune")
    assert d.index == "local" and d.tools[0].id == "jarvis.get_weather"
    calls = hub.calls
    await r.route("play some music")
    assert hub.calls == calls                      # not asked again straight away
    now[0] += router_mod.HUB_RETRY_S + 1
    await r.route("play some music")
    assert hub.calls > calls                        # tried again later


async def test_a_slow_hub_is_not_waited_for(registry, monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "HUB_TIMEOUT_S", 0.05)
    d = await Router(registry, hub=BrokenHub(delay=5)).route("what's my battery level")
    assert d.index == "local" and d.tools[0].id == "jarvis.get_system_status"


async def test_hub_listed_skills_must_exist_here(registry) -> None:
    class Hub:
        async def find_tools(self, *a, **k):
            row = {"executor": "jarvis", "category": "files", "access": "write", "description": "x", "score": 9}
            return [{**row, "id": "jarvis.format_disk", "name": "format_disk"},
                    {**row, "id": "jarvis.run_plan", "name": "run_plan"},
                    {**row, "id": "jarvis.delete_file", "name": "delete_file"}]

    d = await Router(registry, hub=Hub()).route("delete the old notes file")
    assert "jarvis.format_disk" not in d.tool_ids and "jarvis.run_plan" not in d.tool_ids
    assert "jarvis.delete_file" in d.tool_ids


async def test_client_asks_the_hub_registry_over_mcp() -> None:
    server = fake_hub_server()
    seen = {}

    @server.tool()
    async def find_tools(query: str = "", categories: list[str] | None = None, access: str | None = None,
                         limit: int = 5) -> list[dict]:
        seen.update(query=query, categories=categories, limit=limit)
        return [{"id": "jobpilot.get_shortlist", "name": "get_shortlist", "executor": "hub", "category": "jobs",
                 "access": "read", "description": "Shortlist.", "requires_confirmation": False, "score": 5.8}]

    rows = await HubClient(server, None).find_tools("what's on my shortlist", ["jobs"], None, 8)
    assert rows[0]["id"] == "jobpilot.get_shortlist" and seen == {
        "query": "what's on my shortlist", "categories": ["jobs"], "limit": 8}


# ---------------------------------------------------------------- the optional classifier

async def test_classifier_only_in_the_grey_zone(registry, monkeypatch) -> None:
    asked = []

    async def classify(text, categories):
        asked.append(text)
        return ["media"]

    r = Router(registry, hub=IndexHub(registry), classifier=classify)
    await r.route("play believer by imagine dragons")       # a clear skill: not asked
    await r.route("why is the sky blue")                    # clearly thinking: not asked
    assert asked == []
    monkeypatch.setattr(router_mod, "GREY_LOW", 0.0)
    monkeypatch.setattr(router_mod, "GREY_HIGH", 100.0)
    d = await r.route("put something nice on")
    assert asked == ["put something nice on"] and {t.category for t in d.tools} == {"media"}
    assert d.reason == "classifier"


async def test_classifier_saying_none_means_reasoning_and_failure_is_ignored(registry, monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "GREY_LOW", 0.0)
    monkeypatch.setattr(router_mod, "GREY_HIGH", 100.0)

    async def none(text, categories):
        return []

    async def broken(text, categories):
        raise TimeoutError

    assert (await Router(registry, hub=IndexHub(registry), classifier=none).route("pause the music")).route \
        == "reasoning"
    d = await Router(registry, hub=IndexHub(registry), classifier=broken).route("pause the music")
    assert d.tools[0].id == "jarvis.media_key"


# ---------------------------------------------------------------- logging and limits

async def test_routing_log_has_no_request_text(router, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleValue1234567890abcdef")
    secret_text = "remind me that my bank PIN is 4321 at 6"
    d = await router.route(secret_text, source="voice")
    raw = (tmp_path / "routing.jsonl").read_text(encoding="utf-8")
    record = json.loads(raw.splitlines()[-1])
    assert "4321" not in raw and "PIN" not in raw and "AIzaSy" not in raw
    assert record["route"] == d.route and record["source"] == "voice" and len(record["request_sha"]) == 12
    assert record["tools"][0]["id"] == d.tool_ids[0] and record["chars"] == len(secret_text)


async def test_long_requests_are_cut(router) -> None:
    d = await router.route("play music " * 400)
    assert d.route in ("skill", "tools") and d.tools[0].category == "media"


def test_tokens_match_the_hub() -> None:
    assert tokens("What's on my shortlist?") == ["shortlist"]
    assert tokens("searching emails") == tokens("search email")
