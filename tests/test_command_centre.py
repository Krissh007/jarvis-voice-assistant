"""Desktop UI phase 4: the command centre's events (route, agent_update) and its status request."""

from __future__ import annotations

import asyncio
import json

from actions import Services
from actions.agent_hub import HubBridge
from core.bus import EventBus
from core.registry import ToolError
from tests.test_agent_hub import FakeHub, item, make_services
from tests.test_web_ui import assistant  # noqa: F401  (fixture)


def test_agent_results_reach_the_timeline_spoken_or_shown(tmp_path) -> None:
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    spoken = make_services(bus=bus)
    asyncio.run(HubBridge(FakeHub(updates=[item("Found 3 roles.")]), spoken).deliver_once())
    quiet = make_services(bus=bus, announce=lambda: False)
    asyncio.run(HubBridge(FakeHub(updates=[item("Boom.", status="failed")]), quiet).deliver_once())
    updates = [e.data for e in seen if e.kind == "agent_update"]
    assert [(u["status"], u["say"]) for u in updates] == [("done", "Found 3 roles."), ("failed", "Boom.")]


def test_reasoning_decisions_are_published_without_the_request(tmp_path) -> None:
    from actions.brain import Brain
    from core.router import Decision, ToolPick

    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    brain = Brain.__new__(Brain)          # only _write_log is exercised
    brain.services = Services(data_dir=tmp_path, bus=bus)
    brain.log_path = None
    pick = ToolPick(id="jarvis.search_web", name="search_web", executor="jarvis", category="search", access="read",
                    description="search", score=3.1)
    decision = Decision(route="tools", tools=[pick], categories=["search"], confidence=0.8, reason="")
    brain._write_log("what's my bank password", decision, "answered", [], {}, 2, ["openai/gpt-oss-120b"], 3.44)
    (ev,) = [e for e in seen if e.kind == "route"]
    assert ev.data["route"] == "tools" and ev.data["tools"] == ["jarvis.search_web"]
    assert ev.data["models"] == ["openai/gpt-oss-120b"] and ev.data["seconds"] == 3.4 and ev.data["fallback"] is False
    assert "password" not in json.dumps(ev.data)


class StatusHub:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def agent_status(self):
        if self.behaviour == "down":
            raise ToolError("The Agent Hub isn't running.")
        if self.behaviour == "slow":
            await asyncio.sleep(30)
        return {"agents": [{"name": "jobpilot", "reachable": True, "tools": 7, "optional": False}],
                "planner": True, "tools_total": 60}


async def test_status_reports_the_hub_its_agents_models_and_tools(assistant, monkeypatch) -> None:  # noqa: F811
    from core import runtime
    from core.runtime import WebBridge

    a = assistant
    a.config["features"]["agent_hub"] = True
    bridge = WebBridge(a)
    a.services.hub = StatusHub("ok")
    st = await bridge.status()
    assert st["hub"]["reachable"] and st["hub"]["agents"][0]["tools"] == 7 and st["hub"]["registry_tools"] == 60
    assert st["models"]["voice"] == a.config["models"]["live"] and st["tools"]["loaded"] == len(a.registry.visible())
    a.bus.publish("route", route="reasoning", tools=[], models=["m"], outcome="answered")
    assert (await bridge.status())["route"]["route"] == "reasoning"

    a.services.hub = StatusHub("down")
    st = await bridge.status()
    assert st["hub"]["reachable"] is False and "isn't running" in st["hub"]["error"]

    real_wait = asyncio.wait_for
    monkeypatch.setattr(runtime.asyncio, "wait_for", lambda aw, timeout: real_wait(aw, 0.05))
    a.services.hub = StatusHub("slow")
    st = await bridge.status()                                  # a hanging Hub doesn't hang the page
    assert st["hub"]["reachable"] is False
    assert "KEY" not in json.dumps(st) or all(v not in json.dumps(st) for v in ("AIza", "gsk_"))


def test_status_request_is_answered_without_blocking_the_page(assistant) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from core.runtime import WebBridge
    from tests.test_web_ui import PORT, TOKEN, connect
    from ui.server import create_app

    bridge = WebBridge(assistant)

    async def slow_status():
        await asyncio.sleep(0.3)
        return {"hub": {"reachable": True}}

    bridge.status = slow_status
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())       # hello
        ws.send_text(json.dumps({"type": "request", "what": "status", "id": 1}))
        ws.send_text(json.dumps({"type": "request", "what": "facts", "id": 2}))
        got = []
        while len(got) < 2:
            msg = json.loads(ws.receive_text())
            if msg["kind"] == "reply":
                got.append(msg["id"])
    assert got == [2, 1]                     # the quick reply didn't wait for the slow one
