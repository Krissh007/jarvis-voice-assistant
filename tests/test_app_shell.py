"""The app shell (ui/static/app): its page and vendored libraries, the request/reply messages, and the
bridge methods behind Settings and Memory."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.test_web_ui import FakeBridge, STATIC, connect, assistant  # noqa: F401  (assistant is a fixture)


class AppBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.asked: list = []

    def request(self, what):
        self.asked.append(("request", what))
        return {"categories": [], "status": {"secrets": [{"name": "GOOGLE_API_KEY", "set": True}]}} if what == "settings" \
            else {"facts": [{"id": 1, "text": "x"}]}

    def change_setting(self, key, value):
        self.asked.append(("setting", key, value))
        return {"ok": key == "voice.name", "key": key, "message": "saved"}

    def forget_fact(self, fact_id):
        self.asked.append(("forget", fact_id))
        return {"ok": True, "facts": []}


@pytest.fixture
def app_ui():
    from fastapi.testclient import TestClient

    from tests.test_web_ui import PORT, TOKEN
    from ui.server import create_app

    bridge = AppBridge()
    return bridge, TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")


def _replies(ws, n):
    """The next n replies (skipping the bus events and orb levels in between)."""
    out = []
    while len(out) < n:
        msg = json.loads(ws.receive_text())
        if msg["kind"] == "reply":
            out.append(msg)
    return out


# ---------------------------------------------------------------- the page and its libraries

def test_app_page_is_served_with_the_strict_policy(app_ui) -> None:
    _, client = app_ui
    page = client.get("/app", headers={"host": "127.0.0.1:54321"})
    assert page.status_code == 200 and '<script type="module" src="/static/app/main.js">' in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe" not in csp
    js = client.get("/static/vendor/preact/hooks.module.js", headers={"host": "127.0.0.1:54321"})
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")


def test_vendored_modules_import_each_other_by_path_not_by_name() -> None:
    # The page's policy has no import maps, so a bare "preact" import would fail to load.
    for path in (STATIC / "vendor").glob("*/*.js"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""from\s*["'](?![./])""", text), path
    hooks = (STATIC / "vendor" / "preact" / "hooks.module.js").read_text(encoding="utf-8")
    assert 'from"./preact.module.js"' in hooks
    for lib in ("preact", "htm", "lucide"):
        assert (STATIC / "vendor" / lib / "LICENSE").is_file()


def test_every_icon_the_app_uses_is_in_the_sprite() -> None:
    sprite = (STATIC / "vendor" / "lucide" / "sprite.svg").read_text(encoding="utf-8")
    have = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite))
    used = set()
    for path in list((STATIC / "app").rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'name="([a-z0-9-]+)"', text)) | set(re.findall(r'icon: "([a-z0-9-]+)"', text))
    from core.settings_store import CATEGORIES
    used |= {icon for _, _, icon in CATEGORIES}
    assert used and used <= have, sorted(used - have)


def test_app_text_is_never_set_as_html() -> None:
    for path in list((STATIC / "app").rglob("*.js")) + list((STATIC / "shared").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text and "dangerouslySetInnerHTML" not in text, path


# ---------------------------------------------------------------- requests and replies

def test_requests_get_a_reply_for_that_page_only(app_ui) -> None:
    bridge, client = app_ui
    with connect(client) as ws, connect(client) as other:
        json.loads(ws.receive_text())      # hello
        json.loads(other.receive_text())
        ws.send_text(json.dumps({"type": "request", "what": "settings", "id": 7}))
        ws.send_text(json.dumps({"type": "setting", "key": "voice.name", "value": "Puck", "id": 8}))
        ws.send_text(json.dumps({"type": "forget", "fact": 3, "id": 9}))
        ws.send_text(json.dumps({"type": "request", "what": "everything", "id": 10}))   # not a request: dropped
        first, second, third = _replies(ws, 3)
        assert (first["id"], first["what"]) == (7, "settings") and first["data"]["status"]["secrets"][0]["set"] is True
        assert (second["id"], second["data"]["ok"]) == (8, True)
        assert (third["id"], third["what"]) == (9, "forget")
        other.send_text(json.dumps({"type": "request", "what": "facts", "id": 1}))
        (mine,) = _replies(other, 1)
        assert mine["id"] == 1 and mine["data"]["facts"][0]["text"] == "x"   # the first page's replies didn't leak here
    assert ("request", "everything") not in bridge.asked
    assert ("setting", "voice.name", "Puck") in bridge.asked and ("forget", 3) in bridge.asked


def test_new_buttons_are_allowed_controls(app_ui) -> None:
    bridge, client = app_ui
    with connect(client) as ws:
        json.loads(ws.receive_text())
        for action in ("open_app", "open_settings", "format_disk"):
            ws.send_text(json.dumps({"type": "control", "action": action}))
        ws.send_text(json.dumps({"type": "request", "what": "facts", "id": 1}))
        _replies(ws, 1)                    # everything before it has been handled
    assert bridge.controls == ["open_app", "open_settings"]


# ---------------------------------------------------------------- the real bridge

async def test_bridge_settings_change_live_and_restart_values(assistant, tmp_path) -> None:
    from core.runtime import WebBridge
    from core.settings_store import SettingsStore

    a = assistant
    a._loop = asyncio.get_running_loop()
    a.prefs_path = tmp_path / "prefs.json"
    a.settings = SettingsStore(a.config, tmp_path / "settings.json")
    a.voice = a.announce = True
    bridge = WebBridge(a, look_path=tmp_path / "orb_look.json")
    seen = []
    a.bus.subscribe(seen.append)

    view = bridge.request("settings")
    assert {c["id"] for c in view["categories"]} == {"voice", "models", "memory", "hotkeys", "connections", "appearance"}
    assert all(set(s) == {"name", "set"} for s in view["status"]["secrets"])

    off = bridge.change_setting("prefs.voice", False)                     # applies at once
    assert off["ok"] and a.voice is False and [e.data["on"] for e in seen if e.kind == "voice"] == [False]
    look = bridge.change_setting("look.orb_size", 150)
    assert look["ok"] and json.loads((tmp_path / "orb_look.json").read_text(encoding="utf-8"))["orb_size"] == 150
    assert any(e.kind == "look" and e.data["look"]["orb_size"] == 150 for e in seen)
    kept = bridge.change_setting("hotkeys.mute", "ctrl+alt+k")            # kept for the next start
    assert kept["ok"] and "Restart" in kept["message"] and kept["settings"]["status"]["restart"] == ["hotkeys.mute"]
    refused = bridge.change_setting("features.test_tools", True)
    assert not refused["ok"] and "isn't a setting" in refused["message"]
    bad = bridge.change_setting("look.orb_size", 9999)
    assert not bad["ok"] and "goes from 70 to 260" in bad["message"]
    assert bridge.request("nonsense") is None


async def test_bridge_forgets_a_fact_by_id_only(assistant, tmp_path) -> None:
    from actions.memory import MemoryStore
    from core.runtime import WebBridge

    a = assistant
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.add("Krishna's friend is Sanjay.")
    store.add("Krishna likes short answers.")
    a.services.memory = store
    bridge = WebBridge(a, look_path=tmp_path / "orb_look.json")
    facts = bridge.request("facts")["facts"]
    assert [f["text"] for f in facts] == ["Krishna's friend is Sanjay.", "Krishna likes short answers."]
    gone = bridge.forget_fact(facts[0]["id"])
    assert gone["ok"] and [f["text"] for f in gone["facts"]] == ["Krishna likes short answers."]
    assert not bridge.forget_fact(facts[0]["id"])["ok"]                   # already gone
    assert not bridge.forget_fact("1")["ok"] and not bridge.forget_fact(True)["ok"]
    assert len(store.facts()) == 1


def test_open_app_starts_the_window_on_a_section_or_brings_it_forward(assistant) -> None:
    a = assistant
    started, seen = [], []
    a.bus.subscribe(seen.append)

    class FakeWindow:
        url = ""
        alive = False

        def start(self):
            started.append(self.url)
            self.alive = True

    a.app_proc = FakeWindow()
    a.open_app("settings")
    assert started and started[0].startswith(a.web.app_url) and started[0].endswith("&s=settings")
    a.open_app("nonsense")                       # already open: switch it and bring it forward
    assert len(started) == 1
    assert [(e.data["target"], e.data["section"]) for e in seen if e.kind == "focus"] == [("app", "brain")]  # the default


class OpenWindow:
    url = ""
    alive = False

    def start(self):
        self.alive = True


def orb_events(seen):
    return [e.data["visible"] for e in seen if e.kind == "orb_visibility"]


def dock_events(seen):
    return [(e.data["docked"], e.data.get("x"), e.data.get("y")) for e in seen if e.kind == "orb_dock"]


def test_the_orb_never_hides_for_the_app_and_docks_while_it_is_full_screen(assistant) -> None:
    a = assistant
    seen = []
    a.bus.subscribe(seen.append)
    assert a.app_proc.on_closed == a._app_closed                         # the real app window says when it closes
    a.app_proc = OpenWindow()
    a.open_app()
    assert a.orb_visible is True and orb_events(seen) == []              # opening the app doesn't hide the orb
    a.dock_orb({"x": 1880, "y": 60, "size": 40})                        # the app went full screen
    a.dock_orb({"x": 1880, "y": 60, "size": 40})                        # the same again: nothing new
    assert a.orb_dock == {"x": 1880, "y": 60, "size": 40} and dock_events(seen) == [(True, 1880, 60)]
    assert a.web.bridge.snapshot()["orb_dock"] == {"x": 1880, "y": 60, "size": 40}   # a reloaded orb page docks too
    a.dock_orb(None)                                                    # minimised or restored to a window
    assert a.orb_dock is None and dock_events(seen)[-1] == (False, None, None)
    a.dock_orb({"x": 1880, "y": 60, "size": 40})
    a._app_closed()                                                     # closed: floats again
    assert a.orb_dock is None and dock_events(seen)[-1][0] is False and a.orb_visible is True


def test_the_orb_can_keep_floating(assistant) -> None:
    a = assistant
    a.config["web_ui"]["dock_orb_in_app"] = False
    a.dock_orb({"x": 1880, "y": 60, "size": 40})
    assert a.orb_dock is None


def test_dock_messages_are_checked(assistant) -> None:
    from core.runtime import WebBridge

    a = assistant
    a.ui = lambda label, fn, *args: fn(*args)
    bridge = WebBridge(a)
    bridge.orb_dock({"docked": True, "x": 1880.4, "y": 59.6, "size": 400, "hwnd": 132456})
    assert a.orb_dock == {"x": 1880, "y": 60, "size": 80, "hwnd": 132456}   # rounded; the size kept in range
    for bad in ({"docked": True, "x": "far", "y": 1}, {"docked": True, "x": float("inf"), "y": 1},
                {"docked": True, "y": 1}, {"docked": True, "x": 1e9, "y": 1}):
        bridge.orb_dock(bad)
        assert a.orb_dock == {"x": 1880, "y": 60, "size": 80, "hwnd": 132456}, bad
    for hwnd in (-5, True, "132456", 2 ** 60):                          # not a window handle: docked without an owner
        bridge.orb_dock({"docked": True, "x": 10, "y": 10, "hwnd": hwnd})
        assert a.orb_dock["hwnd"] == 0, hwnd
    bridge.orb_dock({"docked": False})
    assert a.orb_dock is None


def test_the_orb_and_app_pages_carry_the_dock() -> None:
    from ui.server import PC_ONLY, STREAM_KINDS

    assert "orb_dock" in STREAM_KINDS and "orb_dock" in PC_ONLY        # a phone can't move the PC's orb
    orb_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'case "orb_dock": return dock(ev);' in orb_js and 'toWindow("dock"' in orb_js
    main_js = (STATIC / "app" / "main.js").read_text(encoding="utf-8")
    assert 'toWindow("dock_slot"' in main_js and "window.jarvisDock" in main_js and "!phone && html`<${OrbDot} />`" in main_js
    win = (STATIC.parent / "window.py").read_text(encoding="utf-8")
    assert 'command == "dock" and orb_mode' in win and 'command == "dock_slot" and not orb_mode' in win
    assert "view.isMaximized() or view.isFullScreen()" in win and "QEvent.WindowStateChange" in win
    assert 'orb["dock"] is None:  # the user dragged it' in win               # docked moves never become the floating place
    assert '"hwnd": int(view.winId())' in win and "GWLP_HWNDPARENT" in win and "HWND_NOTOPMOST" in win   # owned, not topmost
