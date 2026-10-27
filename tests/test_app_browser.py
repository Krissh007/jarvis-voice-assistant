"""Desktop UI phase 6: the app driven in a real (headless) browser, against JARVIS's own web server with a
fake assistant behind it. Skipped when Playwright's Chromium isn't installed."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

import main
from core.attachments import AttachmentStore
from core.bus import EventBus
from core.settings_store import SettingsStore, secret_status

SECRET = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE123"   # must never reach the page


class BrowserBridge:
    """What ui/server.py needs, with records of what the page sent."""

    def __init__(self, tmp_path) -> None:
        self.bus = EventBus()
        self.chats: list[tuple] = []
        self.controls: list[str] = []
        self.attachments = AttachmentStore()
        self.config = main.load_config()
        self.settings = SettingsStore(self.config, tmp_path / "settings.json")
        self.env = {"GOOGLE_API_KEY": SECRET}
        self.brain_graph: dict = {"nodes": [{"id": "jarvis", "type": "core", "label": "JARVIS", "hub": True, "detail": {}}],
                                  "links": [], "counts": {}, "capped": [], "layout": {}}
        self.brain_requests = 0
        self.layouts: list[tuple[dict, bool]] = []
        self.forgotten: list = []
        self.hub_up = True
        self.hub_actions: list = []
        self.docks: list = []
        self.dashboards_opened: list = []
        self.shown: list = []
        self.read: list = []

    def snapshot(self) -> dict:
        return {"state": "listening", "level": 0.0, "user": "Krishna", "speak": True, "voice": True, "announce": True,
                "pending": None, "problem": None, "keep": {"enabled": True, "on": True},
                "hub": {"enabled": True, "reachable": True}, "voice_model": "gemini-3.8-live", "language": "en-IN",
                "quick_actions": ["What's on my screen?", "What are my reminders?"], "caption_s": 8,
                "dashboards": [{"id": "pmo", "label": "PMO Dashboard", "short": "PMO"},
                               {"id": "jobpilot", "label": "JobPilot dashboard", "short": "JobPilot"}]}

    def chat(self, text, speak, web="auto", attachments=None) -> bool:
        self.chats.append((text, speak, web))
        return True

    def control(self, action) -> bool:
        self.controls.append(action)
        return True

    def set_look(self, values) -> dict:
        return {}

    def request(self, what, arg=None):
        if what == "settings":
            view = self.settings.view({"prefs.voice": True, "prefs.announce": True})
            view["status"] = {"voice_session": True, "agent_hub": True, "secrets": secret_status(self.env.get),
                              "restart": [], "accent": "Cyan-blue"}
            return view
        if what == "facts":
            return {"facts": [{"id": 1, "text": "Krishna's friend is Sanjay.", "added": "2026-10-19T10:00:00", "uses": 2}]}
        if what == "conversations":
            return {"kept": True, "paused": False, "conversations": []}
        if what == "conversation":
            self.read.append(arg)
            return {"lines": [{"id": 1, "role": "user", "text": "Call Sanjay tomorrow", "time": "", "channel": "voice"},
                              {"id": 2, "role": "assistant", "text": "Reminder set for 9.", "time": "", "channel": "voice"}]}
        return None

    def change_setting(self, key, value) -> dict:
        return {"ok": True, "key": key, "message": "saved", "settings": self.request("settings")}

    def forget_fact(self, fact_id) -> dict:
        self.forgotten.append(fact_id)
        return {"ok": True, "message": "Forgotten.", "facts": []}

    async def show_file(self, node_id) -> dict:
        self.shown.append(node_id)
        return {"ok": True, "message": "Opened in File Explorer."}

    def delete_conversation(self, conversation_id) -> dict:
        return {"ok": True, "message": "Deleted.", "conversations": []}

    def regenerate(self, speak) -> bool:
        self.controls.append("regenerate")
        return True

    def pin(self, text) -> dict:
        return {"ok": True, "message": "Pinned to memory."}

    async def snap_screen(self) -> dict:
        return {}

    async def brain(self) -> dict:
        self.brain_requests += 1
        return self.brain_graph

    def save_brain_layout(self, positions, fresh=True) -> bool:
        self.layouts.append((positions, fresh))
        return True

    def orb_dock(self, msg) -> None:
        self.docks.append(msg)

    def open_dashboard(self, dashboard_id) -> bool:
        self.dashboards_opened.append(dashboard_id)
        return True

    async def hub_control(self, action) -> dict:
        self.hub_actions.append(action)
        await asyncio.sleep(0.2)
        self.hub_up = action == "start"
        return {"ok": True, "message": "The Agent Hub is running." if self.hub_up else "Stopped what the Agent Hub's script had started.",
                "lines": ["[ ok ] jobpilot-api: http://127.0.0.1:8000", "[ ok ] agent-hub"] if self.hub_up else []}

    async def status(self) -> dict:
        return {"voice_session": True, "hub": {"enabled": True, "reachable": self.hub_up, "planner": True, "registry_tools": 60,
                "control": True, "controlling": None,
                "agents": [{"name": "jobpilot", "reachable": True, "tools": 7, "optional": False, "description": "jobs"}]},
                "models": {"voice": "gemini-3.8-live", "text": "gemini-3.8-flash", "reasoning": "openai/gpt-oss-120b",
                           "reasoning_fallback": "gemini-3.8-flash"},
                "route": None, "tools": {"loaded": 56, "names": []}}


@pytest.fixture
def served(tmp_path):
    import uvicorn

    from ui.server import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    token = "t" * 43
    bridge = BrowserBridge(tmp_path)
    server = uvicorn.Server(uvicorn.Config(create_app(bridge, token, port), log_config=None, lifespan="off"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield bridge, f"http://127.0.0.1:{port}/app#t={token}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
async def page():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:  # no browser installed on this machine
            pytest.skip(f"Playwright's Chromium isn't available: {exc}")
        pg = await browser.new_page(viewport={"width": 1240, "height": 800})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        yield pg
        assert errors == [], errors          # no script errors on any page the test opened
        await browser.close()


async def until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


async def test_settings_shows_keys_only_as_set_or_not(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=settings")
    await page.get_by_role("button", name="Agents and connections").click()
    await page.get_by_text("GOOGLE_API_KEY").wait_for()
    text = await page.inner_text("body")
    assert "Set" in text and "Not set" in text and SECRET not in text
    assert SECRET not in await page.content()
    await page.get_by_role("button", name="Hotkeys").click()
    await page.get_by_text("Show or hide the orb").wait_for()


async def test_chat_sends_and_shows_a_turn_with_its_context(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=chat")
    box = page.get_by_label("Message to JARVIS")
    await box.fill("What's the weather in Pune?")
    await box.press("Enter")
    assert await until(lambda: bridge.chats and bridge.chats[-1][0] == "What's the weather in Pune?")
    b = bridge.bus
    b.publish("message", channel="chat", role="user", label="Krishna", text="What's the weather in Pune?")
    b.publish("tool_started", channel="chat", tool="search_web", args={"query": "pune weather"}, step=False)
    b.publish("sources", channel="chat", via="search", sources=[{"title": "Pune forecast", "site": "weather.example"}])
    b.publish("tool_finished", channel="chat", tool="search_web", step=False, paused=False, ok=True, result="31C")
    b.publish("message", channel="chat", role="jarvis", label="JARVIS", text="Sunny, 31 degrees.")
    await page.get_by_text("Sunny, 31 degrees.").wait_for()
    await page.get_by_text("weather.example").wait_for()        # the context panel's source, as a site name
    assert await page.locator("aside.context a").count() == 0    # never a link

    await page.get_by_role("button", name="Web search: JARVIS decides").click()   # auto -> on
    await page.get_by_role("button", name="Web search: on for this message").click()  # on -> off
    await box.fill("Who won the match?")
    await box.press("Enter")
    assert await until(lambda: bridge.chats[-1] == ("Who won the match?", True, "off"))


async def test_a_plan_card_approves_with_the_confirm_control(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=chat")
    await page.get_by_label("Message to JARVIS").wait_for()
    bridge.bus.publish("message", channel="voice", role="user", label="Krishna", text="Run discovery.")
    bridge.bus.publish("confirm_requested", channel="voice", id=7, tool="agent_hub_request", summary="Run discovery now?",
                       step={"request_id": "r1"})
    await page.get_by_text("Run discovery now?").wait_for()
    await page.get_by_role("button", name="Approve").click()
    assert await until(lambda: "confirm" in bridge.controls)
    bridge.bus.publish("confirm_resolved", channel="voice", id=7, tool="agent_hub_request", outcome="confirmed_voice")
    await page.get_by_text("Approved").wait_for()


async def test_the_command_centre_shows_status_and_failures(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=command")
    await page.get_by_text("jobpilot7 tools").wait_for()         # from the status request
    await page.get_by_text("gemini-3.8-live").wait_for()
    bridge.bus.publish("tool_started", channel="voice", tool="read_page", args={"url": "https://example.com"}, step=False)
    bridge.bus.publish("tool_finished", channel="voice", tool="read_page", step=False, paused=False, ok=False,
                       result="Couldn't read that page.")
    await page.get_by_text("Couldn't read that page.").wait_for()
    assert await page.locator(".entry.failed").count() == 1
    await page.get_by_role("button", name="Failed").click()
    assert await page.locator(".entry").count() == 1


async def test_the_command_centre_starts_and_stops_the_agent_hub(served, page) -> None:
    bridge, url = served
    bridge.hub_up = False
    await page.goto(url + "&s=command")
    start = page.get_by_role("button", name="Start Agent Hub")
    stop = page.get_by_role("button", name="Stop", exact=True)
    await start.wait_for()
    assert await stop.is_disabled()                                     # nothing to stop yet
    await start.click()
    await page.get_by_text("Starting each service in turn").wait_for()
    await page.get_by_text("The Agent Hub is running.").wait_for()
    await page.get_by_text("[ ok ] agent-hub").wait_for()               # what the script said
    assert bridge.hub_actions == ["start"]
    assert await until(lambda: True) and await start.is_disabled()      # running now: Start is greyed out
    await stop.click()
    await page.get_by_text("Stopped what the Agent Hub's script had started.").wait_for()
    assert bridge.hub_actions == ["start", "stop"]
    await page.get_by_role("button", name="JobPilot dashboard").click()
    assert await until(lambda: "open_jobpilot" in bridge.controls)


async def test_chats_speaker_button_mutes_only_replies(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=chat")
    speaker = page.get_by_role("button", name="Spoken replies")
    await speaker.wait_for()
    assert await speaker.get_attribute("aria-pressed") == "true" and "spoken" in await speaker.get_attribute("title")
    await speaker.click()
    assert await until(lambda: bridge.controls == ["voice"])               # the Voice setting, nothing else
    bridge.bus.publish("voice", on=False)
    await page.locator(".composer .tool.muted").wait_for()
    assert await speaker.get_attribute("aria-pressed") == "false" and "muted" in await speaker.get_attribute("title")
    assert "still listens" in await speaker.get_attribute("title")
    assert await page.get_by_role("button", name="Talk").is_enabled()      # talking still works


async def test_one_channel_tag_says_how_you_talk_to_jarvis(served, page) -> None:
    """Krishna, 28 Oct: the channel was said in several places; now only the header's tag says it."""
    bridge, url = served
    await page.goto(url + "&s=chat")
    tag = page.locator("header .channel")
    await tag.get_by_text("Voice", exact=True).wait_for()               # nothing said yet: voice
    bridge.bus.publish("message", channel="chat", role="user", label="Krishna", text="What's the time?")
    await tag.get_by_text("Typing", exact=True).wait_for()
    assert await page.get_by_text("typed", exact=True).count() == 0     # no per-message label any more
    bridge.bus.publish("message", channel="voice", role="user", label="Krishna", text="And the weather?")
    await tag.get_by_text("Voice", exact=True).wait_for()
    context = page.get_by_role("complementary", name="What this answer used")
    assert "none" in await context.inner_text()                         # empty groups fold to one line


async def test_the_app_has_a_dot_for_the_orb_and_reports_full_screen(served, page) -> None:
    bridge, url = served
    await page.goto(url + "&s=chat")
    dot = page.locator("header .orb-dot")
    await dot.wait_for()
    box = await dot.bounding_box()
    assert box["x"] + box["width"] > 1200                               # the top right of the header
    await page.evaluate("window.jarvisDock({docked: true, x: 1880, y: 60, size: 40})")   # what the window says
    await page.evaluate("window.jarvisDock({docked: false})")
    await page.evaluate("window.jarvisDock({x: 1})")                    # not a dock message: ignored
    assert await until(lambda: len(bridge.docks) == 2)
    assert bridge.docks[0]["docked"] is True and bridge.docks[0]["x"] == 1880 and bridge.docks[1] == {"type": "orb_dock", "docked": False}
    assert await page.locator("header .channel").count() == 1
    bridge.bus.publish("orb_dock", docked=True, x=1880, y=60, size=40)
    await page.locator("header .orb-dot.docked").wait_for()             # the orb is in the dot now
    assert await page.locator("header .channel").count() == 1           # the one channel tag either way
    bridge.bus.publish("orb_dock", docked=False)
    await page.locator("header .orb-dot:not(.docked)").wait_for()

