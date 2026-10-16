"""The web UI (ui/): its server's safety checks, the WebSocket protocol, the bridge, the window process."""

from __future__ import annotations

import asyncio
import json
import re
import signal
from pathlib import Path

import pytest

from core.bus import EventBus
from ui.server import MAX_MESSAGE_BYTES, STATIC, WebUI, create_app

PORT = 54321
TOKEN = "t" * 43
ORIGIN = f"http://127.0.0.1:{PORT}"


class FakeBridge:
    def __init__(self) -> None:
        self.bus = EventBus()
        self.chats: list[tuple[str, bool]] = []
        self.controls: list[str] = []
        self.state = "listening"

    def snapshot(self) -> dict:
        return {"state": self.state, "level": 0.0, "user": "Krishna", "speak": False,
                "pending": {"id": 7, "summary": "Close Notepad"}}

    def chat(self, text: str, speak: bool) -> bool:
        self.chats.append((text, speak))
        return True

    def control(self, action: str) -> bool:
        self.controls.append(action)
        return True


@pytest.fixture
def ui():
    from fastapi.testclient import TestClient

    bridge = FakeBridge()
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    return bridge, client


def connect(client, token=TOKEN, origin=ORIGIN, host=None):
    headers = {"origin": origin} if origin else {}
    headers["host"] = host or f"127.0.0.1:{PORT}"  # the test client would send "testserver"
    return client.websocket_connect(f"/ws?t={token}", headers=headers)


# ---------------------------------------------------------------- the page


def test_page_and_assets_are_served_with_a_strict_policy(ui) -> None:
    _, client = ui
    page = client.get("/")
    assert page.status_code == 200 and "<canvas id=\"orb\"" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and f"connect-src ws://127.0.0.1:{PORT}" in csp and "unsafe" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    for asset, kind in (("app.js", "text/javascript"), ("app.css", "text/css")):
        reply = client.get(f"/static/{asset}")  # with nosniff, a wrong type means the page doesn't run
        assert reply.status_code == 200 and reply.headers["content-type"].startswith(kind)
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_requests_for_another_host_name_are_refused(ui) -> None:
    _, client = ui
    assert client.get("/", headers={"host": f"evil.example:{PORT}"}).status_code == 400


def test_page_never_renders_text_as_html() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "orb.js", "orb3d.js", "chat.js"):
        js = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", js), name
    chat = (STATIC / "chat.html").read_text(encoding="utf-8")
    for page in (html, chat):
        assert "<script>" not in page and "onclick" not in page  # no inline code (the policy blocks it anyway)
    for f in STATIC.iterdir():  # nothing is loaded from outside
        if f.is_file():
            assert not re.search(r"https?://", f.read_text(encoding="utf-8")), f.name
    for f in (STATIC / "vendor").rglob("*.js"):  # the stored libraries import only each other
        imports = re.findall(r"""from\s+['"]([^'"]+)['"]""", f.read_text(encoding="utf-8"))
        assert imports == [] or all(i.startswith(".") for i in imports), (f.name, imports)


def test_media_events_reach_the_page(ui) -> None:
    bridge, client = ui
    with connect(client) as ws:
        json.loads(ws.receive_text())
        bridge.bus.publish("media", type="image", title="Looking at your screen", data="data:image/jpeg;base64,AA==")
        for _ in range(20):
            msg = json.loads(ws.receive_text())
            if msg["kind"] == "media":
                assert msg["data"].startswith("data:image/jpeg")
                break
        else:
            raise AssertionError("no media event")
    assert bridge.bus.recent(kinds={"media"}) == []  # pictures of the screen are never kept


# ---------------------------------------------------------------- the WebSocket's checks


@pytest.mark.parametrize("token, origin, host", [
    ("", ORIGIN, None),                                   # no token
    ("x" * 43, ORIGIN, None),                             # wrong token
    (TOKEN, "http://evil.example", None),                 # another site's page
    (TOKEN, None, None),                                  # no origin at all
    (TOKEN, ORIGIN, f"attacker.example:{PORT}"),          # DNS rebinding
])
def test_websocket_refuses_without_token_origin_and_host(ui, token, origin, host) -> None:
    from starlette.websockets import WebSocketDisconnect

    bridge, client = ui
    with pytest.raises(WebSocketDisconnect):
        with connect(client, token, origin, host) as ws:
            ws.receive_text()
    assert bridge.chats == [] and bridge.controls == []


def test_hello_has_the_snapshot_and_recent_history(ui) -> None:
    bridge, client = ui
    bridge.bus.publish("message", channel="voice", role="user", label="Krishna", text="hello", source="mic")
    bridge.bus.publish("partial", channel="voice", role="jarvis", text="Hi")  # not history
    with connect(client) as ws:
        hello = json.loads(ws.receive_text())
    assert hello["kind"] == "hello" and hello["snapshot"]["pending"]["summary"] == "Close Notepad"
    assert [(e["kind"], e["text"]) for e in hello["history"]] == [("message", "hello")]


def test_events_stream_to_the_page_and_chat_and_buttons_come_back(ui) -> None:
    bridge, client = ui
    with connect(client) as ws:
        json.loads(ws.receive_text())  # hello
        bridge.bus.publish("tool_started", channel="chat", tool="get_datetime", args={}, step=False)
        seen = []
        for _ in range(10):
            msg = json.loads(ws.receive_text())
            seen.append(msg["kind"])
            if msg["kind"] == "tool_started":
                assert msg["tool"] == "get_datetime" and msg["channel"] == "chat"
                break
        assert "tool_started" in seen
        ws.send_text(json.dumps({"type": "chat", "text": "what's the time", "speak": False}))
        ws.send_text(json.dumps({"type": "control", "action": "confirm"}))
        ws.send_text(json.dumps({"type": "control", "action": "shutdown_pc"}))  # not a button
        ws.send_text(json.dumps({"type": "tool", "name": "delete_file"}))  # no such message type
        ws.send_text("not json")
        ws.send_text(json.dumps({"type": "chat", "text": "x" * MAX_MESSAGE_BYTES}))  # oversized
        ws.send_text(json.dumps({"type": "control", "action": "mute"}))
        for _ in range(50):
            if len(bridge.controls) == 2:
                break
            json.loads(ws.receive_text())  # level updates keep coming
    assert bridge.chats == [("what's the time", False)]
    assert bridge.controls == ["confirm", "mute"]


# ---------------------------------------------------------------- the real server


async def test_server_runs_on_loopback_and_leaves_ctrl_c_alone() -> None:
    import websockets

    before = signal.getsignal(signal.SIGINT)
    bridge = FakeBridge()
    web = WebUI(bridge)
    assert web.url.startswith(f"http://127.0.0.1:{web.port}/#t=") and len(web.token) >= 40
    task = asyncio.create_task(web.serve())
    for _ in range(100):
        if web._server is not None and web._server.started:
            break
        await asyncio.sleep(0.02)
    assert signal.getsignal(signal.SIGINT) is before  # uvicorn didn't take over Ctrl+C
    uri = f"ws://127.0.0.1:{web.port}/ws?t={web.token}"
    async with websockets.connect(uri, origin=f"http://127.0.0.1:{web.port}") as ws:
        hello = json.loads(await ws.recv())
        assert hello["kind"] == "hello"
        await ws.send(json.dumps({"type": "control", "action": "talk"}))
        for _ in range(50):
            if bridge.controls:
                break
            await asyncio.sleep(0.02)
    assert bridge.controls == ["talk"]
    web.stop()
    await asyncio.wait_for(task, 3)


async def test_the_token_never_reaches_the_log(caplog) -> None:
    import logging

    import websockets

    caplog.set_level(logging.DEBUG)
    web = WebUI(FakeBridge())
    task = asyncio.create_task(web.serve())
    for _ in range(100):
        if web._server is not None and web._server.started:
            break
        await asyncio.sleep(0.02)
    async with websockets.connect(f"ws://127.0.0.1:{web.port}/ws?t={web.token}", origin=f"http://127.0.0.1:{web.port}") as ws:
        await ws.recv()
    logging.getLogger("uvicorn.error").warning("request /ws?t=%s failed", web.token)  # even a warning is masked
    web.stop()
    await asyncio.wait_for(task, 3)
    ours = " | ".join(r.getMessage() for r in caplog.records if r.name.startswith(("uvicorn", "ui.")))  # not the test's client
    assert web.token not in ours and "/ws?t=***" in ours


# ---------------------------------------------------------------- the bridge and the runtime


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    import main
    from actions import testing
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    cfg["features"].update(test_tools=True, web_ui=True, window=False, tray=False)
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    monkeypatch.setattr(a.session, "send_text", lambda text: None)
    monkeypatch.setattr(a.session, "send_notice", lambda text: None)
    testing.NOTES.clear()
    yield a
    a.web.stop()


def test_web_ui_replaces_the_tk_window(assistant) -> None:
    a = assistant
    assert a.web is not None and a.face is None and a.window_proc is not None
    assert a.window_proc.command()[1:4] == ["-m", "ui.window", "--url"]
    assert a.window_proc.url == a.web.url


async def test_bridge_buttons_do_what_the_tray_and_hotkeys_do(assistant) -> None:
    from actions import testing
    from core.runtime import WebBridge

    a = assistant
    a._loop = asyncio.get_running_loop()
    bridge = WebBridge(a)
    assert bridge.control("mute")
    await asyncio.sleep(0.01)
    assert a.gate.muted
    await a.on_tool_call("test_write_note", {"text": "milk"}, "c1")
    snap = bridge.snapshot()
    assert snap["pending"]["summary"] == "Save a test note saying: milk" and snap["speak"] is True
    assert bridge.control("confirm")
    await asyncio.sleep(0.05)
    assert testing.NOTES == ["milk"]
    assert not bridge.control("format_disk")


async def test_bridge_chat_goes_through_the_chat_channel(assistant, monkeypatch) -> None:
    from core.runtime import WebBridge

    a = assistant
    sent = []
    monkeypatch.setattr(a.session, "send_text", sent.append)
    a.chat.attach(asyncio.get_running_loop())
    events = []
    a.bus.subscribe(events.append)
    assert WebBridge(a).chat("  hello   there ", speak=False)
    await asyncio.sleep(0.05)
    assert sent == ["hello there"]
    msg = next(e for e in events if e.kind == "message")
    assert msg.channel == "chat" and msg.data["source"] == "web"
    assert a._quiet_reply  # speak=False: a text-only answer


def test_closing_the_window_quits_or_hides(assistant, monkeypatch) -> None:
    a = assistant
    quits = []
    monkeypatch.setattr(a, "request_exit", quits.append)
    a._window_closed()
    assert quits == ["window closed"]
    a.config.setdefault("web_ui", {})["close_button"] = "hide"
    a.tray = object()  # hide only makes sense with a tray to come back from
    a._window_closed()
    assert quits == ["window closed"]


def test_show_window_reopens_or_asks_the_page_to_come_forward(assistant, monkeypatch) -> None:
    a = assistant
    started, events = [], []
    monkeypatch.setattr(a.window_proc, "start", lambda: started.append(1))
    a.bus.subscribe(events.append)
    a._show_window()
    assert started == [1]
    monkeypatch.setattr(type(a.window_proc), "alive", property(lambda self: True))
    a._show_window()
    assert started == [1] and events[-1].kind == "focus"


# ---------------------------------------------------------------- the window process


def test_window_only_opens_jarvis_own_page() -> None:
    from ui.window import run_window

    assert run_window(["--url", "https://evil.example/"]) == 2


def test_window_state_is_read_safely(tmp_path) -> None:
    from ui.window import _load_state, _save_state

    path = tmp_path / "w.json"
    assert _load_state(path) == {}
    _save_state(path, {"x": 10, "y": 20, "width": 900, "height": 600})
    assert _load_state(path) == {"x": 10, "y": 20, "width": 900, "height": 600}
    path.write_text("{broken", encoding="utf-8")
    assert _load_state(path) == {}


async def test_window_process_reports_when_the_user_closes_it(tmp_path) -> None:
    import sys

    from ui.window import WindowProcess

    closed = []
    proc = WindowProcess("http://127.0.0.1:1/#t=x", state_path=tmp_path / "w.json", on_closed=lambda: closed.append(1),
                         log_path=tmp_path / "window.log")
    proc.command = lambda: [sys.executable, "-c", "pass"]  # a window that closes at once
    proc.start()
    watcher = asyncio.create_task(proc.watch(interval_s=0.05))
    for _ in range(100):
        if closed:
            break
        await asyncio.sleep(0.05)
    watcher.cancel()
    assert closed == [1] and not proc.alive
    proc.command = lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    proc.start()
    assert proc.alive
    proc.stop()  # JARVIS quitting: no "closed" callback
    assert not proc.alive and closed == [1]
    assert Path(tmp_path / "window.log").exists()


def test_stopping_the_window_closes_the_whole_process_tree(tmp_path) -> None:
    """The venv's python.exe is a launcher with the real interpreter as its child: both must go."""
    import sys
    import time

    import psutil

    from ui.window import WindowProcess

    proc = WindowProcess("http://127.0.0.1:1/#t=x", state_path=tmp_path / "w.json")
    child = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    proc.command = lambda: [sys.executable, "-c", child]
    proc.start()
    root = psutil.Process(proc._proc.pid)
    for _ in range(100):
        tree = root.children(recursive=True)
        if len(tree) >= 1:
            break
        time.sleep(0.05)
    pids = [root.pid, *(p.pid for p in root.children(recursive=True))]
    assert len(pids) >= 2
    proc.stop()
    time.sleep(0.3)
    assert not any(psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE for p in pids)


def test_click_through_shapes() -> None:
    from ui.window import hit, parse_shapes

    shapes = parse_shapes('[{"c": [590, 360], "r": 150}, {"x": 400, "y": 520, "w": 380, "h": 46}, {"bad": 1}, 7]')
    assert len(shapes) == 2  # malformed entries are dropped
    assert hit(shapes, 590, 360) and hit(shapes, 700, 400)       # on the orb
    assert hit(shapes, 410, 530)                                   # on the typing box
    assert not hit(shapes, 20, 20) and not hit(shapes, 590, 700)  # empty space: clicks go through
    assert parse_shapes("not json") == [] and parse_shapes('{"c": 1}') == []


async def test_looking_at_the_screen_shows_a_redacted_preview_that_is_never_kept(monkeypatch) -> None:
    import io

    from PIL import Image

    from actions import Services, screen
    from core.bus import EventBus

    img = Image.new("RGB", (1280, 720), "navy")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    monkeypatch.setattr(screen, "capture", lambda: (buf.getvalue(), ["Bank login"]))

    class FakeLLM:
        async def generate(self, prompt, **kw):
            return "A blue screen."

    bus = EventBus()
    seen = []
    bus.subscribe(seen.append)
    s = screen.Screen(Services(llm=FakeLLM(), bus=bus))
    answer = await s.look_at_screen("what's on it?")
    assert answer.startswith("A blue screen.") and "blacked out" in answer
    (ev,) = [e for e in seen if e.kind == "media"]
    assert ev.data["type"] == "image" and ev.data["data"].startswith("data:image/jpeg;base64,")
    assert len(ev.data["data"]) < 120_000  # a small preview, not the full capture
    assert bus.recent() == []


# ---------------------------------------------------------------- the control centre and the chat page


def test_chat_page_is_served_and_history_starts_at_the_new_chat(ui) -> None:
    from ui.server import current_conversation

    bridge, client = ui
    page = client.get("/chat")
    assert page.status_code == 200 and 'id="messages"' in page.text and "content-security-policy" in page.headers
    bridge.bus.publish("message", channel="chat", role="user", label="Krishna", text="old chat", source="web")
    bridge.bus.publish("conversation", action="new")
    bridge.bus.publish("message", channel="chat", role="user", label="Krishna", text="new chat", source="web")
    assert [e.data["text"] for e in current_conversation(bridge.bus)] == ["new chat"]
    with connect(client) as ws:
        hello = json.loads(ws.receive_text())
    assert [e["text"] for e in hello["history"]] == ["new chat"]


def test_new_chat_and_open_chat_are_buttons_the_page_may_press(ui) -> None:
    bridge, client = ui
    with connect(client) as ws:
        json.loads(ws.receive_text())
        for action in ("new_chat", "open_chat", "delete_everything"):
            ws.send_text(json.dumps({"type": "control", "action": action}))
        for _ in range(50):
            if len(bridge.controls) == 2:
                break
            json.loads(ws.receive_text())
    assert bridge.controls == ["new_chat", "open_chat"]


def test_orb_window_geometry() -> None:
    from ui.window import EXPANDED, orb_canvas, orb_geometry

    screen = (0, 0, 1536, 824)
    canvas = orb_canvas(160)
    assert 220 <= canvas <= 250
    rect, inner = orb_geometry((700, 400), False, canvas, screen)        # closed: just the orb
    assert rect[2:] == (canvas, canvas) and (rect[0] + inner[0], rect[1] + inner[1]) == (700, 400)
    rect, inner = orb_geometry((700, 160), True, canvas, screen)         # opened: the orb stays put, on top
    assert rect[2:] == EXPANDED and (rect[0] + inner[0], rect[1] + inner[1]) == (700, 160)
    assert inner[1] < EXPANDED[1] / 3                                     # everything else stacks under it
    rect, inner = orb_geometry((700, 760), True, canvas, screen)         # low on the screen: moves up to fit
    assert rect[1] + rect[3] <= 824 and rect[1] + inner[1] < 760
    rect, inner = orb_geometry((1500, 30), True, canvas, screen)         # near a corner: kept on the screen
    assert rect[0] + rect[2] <= 1536 and rect[1] >= 0 and rect[0] >= 0
    rect, inner = orb_geometry((1530, 820), False, canvas, screen)
    assert rect[0] + rect[2] <= 1536 and rect[1] + rect[3] <= 824


def test_window_commands_carry_the_mode_and_size(tmp_path) -> None:
    from ui.window import WindowProcess

    orb = WindowProcess("http://127.0.0.1:1/#t=x", state_path=tmp_path / "o.json", mode="orb", size=150)
    chat = WindowProcess("http://127.0.0.1:1/chat#t=x", state_path=tmp_path / "c.json", mode="chat", on_top=False)
    o, c = orb.command(), chat.command()
    assert o[o.index("--mode") + 1] == "orb" and o[o.index("--size") + 1] == "150" and "--on-top" in o
    assert c[c.index("--mode") + 1] == "chat" and "--on-top" not in c


async def test_new_chat_starts_fresh_clears_views_and_opens_the_chat_page(assistant, monkeypatch) -> None:
    from core.runtime import WebBridge

    a = assistant
    a._loop = asyncio.get_running_loop()
    fresh, opened, events = [], [], []
    monkeypatch.setattr(a.session, "new_conversation", lambda: fresh.append(1))
    monkeypatch.setattr(a.chat_proc, "start", lambda: opened.append(1))
    a.bus.subscribe(events.append)
    assert WebBridge(a).control("new_chat")
    await asyncio.sleep(0.01)
    assert fresh == [1] and opened == [1]
    assert any(e.kind == "conversation" and e.data == {"action": "new"} for e in events)
    monkeypatch.setattr(type(a.chat_proc), "alive", property(lambda self: True))
    a.open_chat()  # already open: it's asked to come forward instead
    assert opened == [1] and events[-1].kind == "focus" and events[-1].data == {"target": "chat"}


def test_quick_actions_come_from_the_config(assistant) -> None:
    from core.runtime import WebBridge

    assistant.config["web_ui"]["quick_actions"] = ["What's on my screen?", "x" * 200]
    snap = WebBridge(assistant).snapshot()
    assert snap["quick_actions"][0] == "What's on my screen?" and len(snap["quick_actions"][1]) == 80
