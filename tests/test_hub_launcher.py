"""Starting and stopping the Agent Hub from JARVIS: the launcher (core/hub_launcher.py), automatic starts
for requests that need the Hub (actions/agent_hub.py), the Command centre's buttons and the JobPilot
dashboard button."""

from __future__ import annotations

import asyncio
import json

import pytest

from core import hub_launcher
from core.hub_launcher import HubLauncher, script_from
from core.registry import ToolError
from tests.test_web_ui import assistant  # noqa: F401  (fixture)


class FakeProc:
    def __init__(self, out: str, code: int, delay: float = 0.0) -> None:
        self.out, self.returncode, self.delay = out.encode(), None, delay
        self._code = code
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(self.delay)
        self.returncode = self._code
        return self.out, None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "agent-hub" / "scripts" / "start_all.ps1"
    path.parent.mkdir(parents=True)
    path.write_text("# fake", encoding="utf-8")
    return path


def fake_shell(monkeypatch, *procs):
    calls = []
    queue = list(procs)

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return queue.pop(0)

    monkeypatch.setattr(hub_launcher.asyncio, "create_subprocess_exec", create)
    return calls


def test_the_script_comes_from_config_only_and_must_exist(script, tmp_path) -> None:
    assert script_from({"agent_hub": {"start_script": str(script)}}) == script
    assert script_from({"agent_hub": {"start_script": str(tmp_path / "missing.ps1")}}) is None
    (tmp_path / "x.bat").write_text("", encoding="utf-8")
    assert script_from({"agent_hub": {"start_script": str(tmp_path / "x.bat")}}) is None   # PowerShell scripts only
    assert script_from({"agent_hub": {"start_script": ""}}) is None and script_from({}) is None


async def test_start_and_stop_run_the_script(script, monkeypatch) -> None:
    said = []
    calls = fake_shell(monkeypatch,
                       FakeProc("[ ok ] jobpilot-api: http://127.0.0.1:8000\r\n\r\n[ ok ] agent-hub\r\n", 0),
                       FakeProc("Stopped agent-hub (PID 12).\n", 0))
    launcher = HubLauncher(script, status=said.append)
    started = await launcher.run("start")
    assert started == {"ok": True, "message": "The Agent Hub is running.",
                       "lines": ["[ ok ] jobpilot-api: http://127.0.0.1:8000", "[ ok ] agent-hub"]}
    args, kwargs = calls[0]
    assert args[0] == "powershell.exe" and args[-2:] == ("-File", str(script)) and kwargs["cwd"] == str(script.parent.parent)
    stopped = await launcher.run("stop")
    assert stopped["ok"] and calls[1][0][-1] == "-Stop"
    assert said == ["(Agent Hub: starting)", "(The Agent Hub is running.)", "(Agent Hub: stopping)",
                    "(Stopped what the Agent Hub's script had started.)"]
    assert (await launcher.run("restart"))["ok"] is False and len(calls) == 2   # only start and stop
    assert (await HubLauncher(None).run("start"))["message"].startswith("No Agent Hub start script")


async def test_a_failed_start_shows_the_scripts_lines_without_secrets(script, monkeypatch) -> None:
    fake_shell(monkeypatch, FakeProc("[FAIL] agent-hub did not come up. Last log lines:\n"
                                     "HUB_TOKEN=abcdefghijklmnopqrstuvwxyz123456\nport 8765 refused\n", 1))
    result = await HubLauncher(script).run("start")
    assert result["ok"] is False and "didn't start" in result["message"]
    assert result["lines"] == ["[FAIL] agent-hub did not come up. Last log lines:",
                               "(a line was hidden: it looked like a secret)", "port 8765 refused"]


async def test_one_run_at_a_time(script, monkeypatch) -> None:
    calls = fake_shell(monkeypatch, FakeProc("[ ok ] agent-hub\n", 0, delay=0.2))
    launcher = HubLauncher(script)
    first = asyncio.create_task(launcher.run("start"))
    await asyncio.sleep(0.05)
    assert launcher.running == "start"
    refused = await launcher.run("stop")                                # can't stop while it's starting
    assert refused["ok"] is False and "starting right now" in refused["message"]
    shared = await launcher.run("start")                                # a second start waits for the first
    assert shared["ok"] and (await first)["ok"] and len(calls) == 1 and launcher.running is None


async def test_a_start_that_hangs_is_stopped(script, monkeypatch) -> None:
    proc = FakeProc("", 0, delay=5)
    fake_shell(monkeypatch, proc)
    monkeypatch.setattr(hub_launcher, "START_TIMEOUT_S", 0.05)
    result = await HubLauncher(script).run("start")
    assert result["ok"] is False and proc.killed


async def test_automatic_starts_wait_after_a_failed_one(script, monkeypatch) -> None:
    calls = fake_shell(monkeypatch, FakeProc("[FAIL]\n", 1), FakeProc("[ ok ]\n", 0))
    launcher = HubLauncher(script)
    assert (await launcher.ensure_started())["ok"] is False
    assert await launcher.ensure_started() is None and len(calls) == 1   # too soon: not again
    monkeypatch.setattr(hub_launcher, "START_RETRY_S", 0)
    assert (await launcher.ensure_started())["ok"] and len(calls) == 2
    assert await HubLauncher(None).ensure_started() is None


# ---- automatic starts: only for what the user asked


class FakeLauncher:
    def __init__(self, result) -> None:
        self.result, self.asked = result, 0

    async def ensure_started(self):
        self.asked += 1
        return self.result


def down_then_up(client, monkeypatch, fails: int = 1):
    from actions import agent_hub

    calls = []

    async def once(tool, args):
        calls.append(tool)
        if len(calls) <= fails:
            raise ToolError(agent_hub.START_HINT)
        return {"status": "accepted", "say": "On it."}

    monkeypatch.setattr(client, "_call_once", once)
    return calls


async def test_a_request_starts_the_hub_when_it_isnt_running(monkeypatch) -> None:
    from actions.agent_hub import HubClient

    launcher = FakeLauncher({"ok": True, "message": "The Agent Hub is running."})
    client = HubClient("http://127.0.0.1:8765/mcp", "t" * 32, launcher=launcher)
    calls = down_then_up(client, monkeypatch)
    assert (await client.submit("run discovery"))["say"] == "On it."
    assert calls == ["submit_request", "submit_request"] and launcher.asked == 1


async def test_the_poller_and_lookups_never_start_the_hub(monkeypatch) -> None:
    from actions.agent_hub import START_HINT, HubClient

    launcher = FakeLauncher({"ok": True, "message": ""})
    client = HubClient("http://127.0.0.1:8765/mcp", "t" * 32, launcher=launcher)
    for call in (client.updates(), client.find_tools("jobs"), client.agent_status(), client.sync_tools([])):
        down_then_up(client, monkeypatch, fails=99)
        with pytest.raises(ToolError, match="isn't running"):
            await call
    assert launcher.asked == 0 and START_HINT.startswith("The Agent Hub isn't running")


async def test_when_starting_fails_or_isnt_tried_the_user_hears_why(monkeypatch) -> None:
    from actions.agent_hub import START_HINT, HubClient

    failed = HubClient("http://127.0.0.1:8765/mcp", "t" * 32,
                       launcher=FakeLauncher({"ok": False, "message": "The Agent Hub didn't start: see the lines below."}))
    down_then_up(failed, monkeypatch, fails=99)
    with pytest.raises(ToolError, match="starting it didn't work"):
        await failed.status("r1")
    not_tried = HubClient("http://127.0.0.1:8765/mcp", "t" * 32, launcher=FakeLauncher(None))
    down_then_up(not_tried, monkeypatch, fails=99)
    with pytest.raises(ToolError) as exc:
        await not_tried.call_tool("jobpilot.get_jobs", {})
    assert str(exc.value) == START_HINT
    off = HubClient("http://127.0.0.1:8765/mcp", "t" * 32)            # agent_hub.auto_start: false
    down_then_up(off, monkeypatch, fails=99)
    with pytest.raises(ToolError):
        await off.submit("x")


def test_the_hub_module_wires_the_launcher(script, tmp_path) -> None:
    from actions import Services, agent_hub
    from core.registry import Registry

    services = Services(data_dir=tmp_path)
    agent_hub.register(Registry(), {"agent_hub": {"start_script": str(script)}}, services)
    assert services.hub_launcher.script == script and services.hub.launcher is services.hub_launcher
    off = Services(data_dir=tmp_path)
    agent_hub.register(Registry(), {"agent_hub": {"start_script": str(script), "auto_start": False}}, off)
    assert off.hub.launcher is None and off.hub_launcher.available        # the buttons still work


# ---- the Command centre


async def test_the_buttons_run_the_launcher_and_status_says_so(assistant, script, monkeypatch) -> None:  # noqa: F811
    from core.runtime import WebBridge

    a = assistant
    a.services.hub_launcher = launcher = HubLauncher(script)
    fake_shell(monkeypatch, FakeProc("[ ok ] agent-hub\n", 0))
    bridge = WebBridge(a)
    st = await bridge.status()
    assert st["hub"]["control"] is True and st["hub"]["controlling"] is None
    assert (await bridge.hub_control("start"))["ok"] and launcher.running is None
    assert (await bridge.hub_control("format"))["ok"] is False
    a.services.hub_launcher = None
    a.config["agent_hub"]["start_script"] = ""
    assert (await bridge.status())["hub"]["control"] is False             # no script: no buttons


def test_hub_control_messages_reach_the_bridge(assistant) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from core.runtime import WebBridge
    from tests.test_web_ui import PORT, TOKEN, connect
    from ui.server import ACTIONS, create_app

    assert "open_jobpilot" in ACTIONS
    bridge = WebBridge(assistant)
    asked = []

    async def hub_control(action):
        asked.append(action)
        return {"ok": True, "message": "The Agent Hub is running.", "lines": []}

    bridge.hub_control = hub_control
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())       # hello
        ws.send_text(json.dumps({"type": "hub_control", "action": "delete", "id": 1}))   # dropped
        ws.send_text(json.dumps({"type": "hub_control", "action": "start", "id": 2}))
        while (msg := json.loads(ws.receive_text()))["kind"] != "reply":
            pass
    assert msg["id"] == 2 and msg["data"]["ok"] and asked == ["start"]


async def test_the_dashboard_button_opens_jobpilot_and_starts_it_if_needed(assistant, monkeypatch) -> None:  # noqa: F811
    import urllib.request
    import webbrowser

    a = assistant
    opened, said = [], []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(a, "_status", said.append)
    a.config["agent_hub"]["dashboard_url"] = "http://127.0.0.1:8000"

    class Up:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    up = {"now": True}

    def urlopen(url, timeout):
        if not up["now"]:
            raise OSError("refused")
        return Up()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    await a._open_jobpilot()
    assert opened == ["http://127.0.0.1:8000"]

    class Starter:
        available = True

        async def run(self, action):
            up["now"] = True
            return {"ok": True, "message": "The Agent Hub is running."}

    up["now"] = False
    a.services.hub_launcher = Starter()
    await a._open_jobpilot()
    assert opened[-1] == "http://127.0.0.1:8000" and len(opened) == 2    # started first, then opened

    up["now"] = False
    a.services.hub_launcher = None
    await a._open_jobpilot()
    assert len(opened) == 2 and "isn't running" in said[-1]
    a.config["agent_hub"]["dashboard_url"] = "https://example.com/dashboard"   # never somewhere else
    await a._open_jobpilot()
    assert len(opened) == 2 and "this computer" in said[-1]
