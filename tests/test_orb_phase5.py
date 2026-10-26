"""Desktop UI phase 5: the orb's radial menu, gestures, show/hide hotkey and error ring."""

from __future__ import annotations

import asyncio
import json
import re
import time

import main
from core.settings_store import BY_KEY, check
from tests.test_web_ui import STATIC, assistant  # noqa: F401  (fixture)


def test_hotkeys_show_orb_on_ctrl_alt_space_and_talk_moves() -> None:
    cfg = main.load_config()
    assert cfg["hotkeys"]["show_orb"] == "ctrl+alt+space" and cfg["hotkeys"]["push_to_talk"] == "ctrl+alt+t"
    assert check(BY_KEY["hotkeys.show_orb"], "ctrl+alt+o", cfg) == "ctrl+alt+o"
    try:
        check(BY_KEY["hotkeys.push_to_talk"], "ctrl+alt+space", cfg)
    except ValueError as exc:
        assert "already Show or hide the orb" in str(exc)
    else:
        raise AssertionError("a clash with the show/hide key should be refused")


def test_the_show_hide_key_is_listened_for_and_toggles_the_orb(assistant) -> None:  # noqa: F811
    from core.hotkeys import parse_combo

    a = assistant
    assert a.hotkeys._matcher.combos["show_orb"] == parse_combo("ctrl+alt+space")   # JARVIS listens for it
    seen = []
    a.bus.subscribe(seen.append)
    a.on_hotkey("show_orb", True)
    a.on_hotkey("show_orb", False)               # the key's release does nothing
    a.on_hotkey("show_orb", True)
    assert [e.data["visible"] for e in seen if e.kind == "orb_visibility"] == [False, True]
    assert a.orb_visible is True


async def test_the_orb_shows_a_problem_in_words(assistant, monkeypatch) -> None:  # noqa: F811
    from core import runtime
    from core.runtime import WebBridge

    a = assistant
    bridge = WebBridge(a)
    a._ready, a._ready_at = True, time.monotonic()
    a.mode = "wake_word"
    a.session.connected.clear()
    assert bridge.problem() is None                                    # still within the start-up grace
    monkeypatch.setattr(runtime, "PROBLEM_GRACE_S", -1)
    assert bridge.problem() == "The voice session isn't connected."
    a.session.connected.set()
    a.config["features"]["agent_hub"] = True
    a.services.hub_reachable = False
    assert bridge.problem() == "The Agent Hub isn't reachable."
    assert bridge.snapshot()["problem"] == "The Agent Hub isn't reachable."
    a.services.hub_reachable = True
    assert bridge.problem() is None and bridge.snapshot()["orb_visible"] is True


def test_the_level_stream_carries_the_problem() -> None:
    from fastapi.testclient import TestClient

    from tests.test_web_ui import FakeBridge, PORT, TOKEN, connect
    from ui.server import create_app

    class ProblemBridge(FakeBridge):
        def snapshot(self):
            return {**super().snapshot(), "problem": "The Agent Hub isn't reachable."}

    client = TestClient(create_app(ProblemBridge(), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())
        while True:
            msg = json.loads(ws.receive_text())
            if msg["kind"] == "level":
                break
    assert msg["problem"] == "The Agent Hub isn't reachable."


def test_menu_controls_open_the_app_on_its_sections(assistant) -> None:  # noqa: F811
    from core.runtime import WebBridge

    a = assistant
    opened = []
    a.open_app = lambda section="brain": opened.append(section)
    a.ui = lambda label, fn, *args: fn(*args)
    bridge = WebBridge(a)
    assert bridge.control("open_app") and bridge.control("open_command") and bridge.control("open_settings")
    assert bridge.control("open_brain")
    assert opened == ["brain", "command", "settings", "brain"]   # the app opens on the Brain


def test_the_orb_page_has_the_menu_and_gestures() -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'data-go="([a-z]+)"', page) == ["chat", "command", "hub", "dashboards", "settings"]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for needle in ('action: "open_command"', 'action: "open_chat"', 'action: "open_settings"', 'type: "hub_control"',
                   'type: "open_dashboard"', 'action: "mute"', 'action: "talk"', "HOLD_MS", "window.jarvisHover", 'case "orb_visibility"',
                   'toWindow(ev.visible ? "show" : "hide")', '"error"', '"muted"'):
        assert needle in js, needle
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    radial = css[css.index("#radial"):css.index("#problem[hidden]")]
    assert "transition" not in radial.replace("No CSS transitions", "")   # the see-through window doesn't run them
    for name in ("error", "muted", "thinking"):
        assert f"{name}:" in (STATIC / "orb3d.js").read_text(encoding="utf-8")
        assert f"{name}:" in (STATIC / "orb.js").read_text(encoding="utf-8")


def test_the_orb_window_hides_and_shows_on_its_page_s_word() -> None:
    src = (STATIC.parent / "window.py").read_text(encoding="utf-8")
    assert 'command == "drag" and orb["dock"] is None' in src   # docked, the orb stays in the app's dot
    assert 'command == "hide" and orb_mode' in src and 'command == "show" and orb_mode' in src


def test_the_snapshot_says_what_kind_of_work_is_going_on(assistant) -> None:  # noqa: F811
    from core.runtime import WebBridge

    a = assistant
    a.activity.set("Thinking…", "thinking")
    snap = WebBridge(a).snapshot()
    assert snap["activity"] == "Thinking…" and snap["activity_kind"] == "thinking"   # the orb shows "thinking"


# ---- the radial menu (desktop update, section 4): the window around it and the Dashboards ring


def test_the_menu_window_keeps_the_orb_where_it_is() -> None:
    from ui.window import RING, orb_geometry

    screen = (0, 0, 1920, 1040)
    rect, inner = orb_geometry((900, 500), True, 165, screen, ring=True)
    assert rect == (900 - RING // 2, 500 - RING // 2, RING, RING) and inner == (RING // 2, RING // 2)   # centred
    rect, inner = orb_geometry((1880, 40), True, 59, screen, ring=True)     # docked in the app's top-right dot
    assert rect[0] + rect[2] <= 1920 and rect[1] >= 0                        # on the screen,
    assert (rect[0] + inner[0], rect[1] + inner[1]) == (1880, 40)            # and the orb hasn't moved
    _, column = orb_geometry((1880, 40), True, 59, screen)                   # the control centre still moves it in
    assert column != inner


def test_the_window_tells_the_page_about_hover_and_keeps_a_menu_drag_under_the_mouse() -> None:
    src = (STATIC.parent / "window.py").read_text(encoding="utf-8")
    assert "window.jarvisHover" in src and 'd.get("ring")' in src and "view.move(view.x() + dx" in src


def test_dashboards_come_from_config_and_bad_ones_are_left_out() -> None:
    from core.runtime import dashboards

    cfg = main.load_config()
    assert [(d["id"], d["short"]) for d in dashboards(cfg)] == [("pmo", "PMO"), ("jobpilot", "JobPilot")]
    assert dashboards(cfg)[0]["url"].startswith("https://") and dashboards(cfg)[1]["open"] == "jobpilot"
    odd = {"web_ui": {"dashboards": [
        {"id": "ok", "label": "Sales board", "url": "https://example.com/board"},
        {"id": "ok", "label": "Same id again", "url": "https://example.com/2"},
        {"id": "js", "label": "Script", "url": "javascript:alert(1)"},
        {"id": "file", "label": "A file", "url": "file:///C:/x.html"},
        {"id": "Bad Id!", "label": "Bad id", "url": "https://example.com"},
        {"id": "nolabel", "url": "https://example.com"},
        {"id": "other", "label": "Other", "open": "notepad"},
        "not a dict"]}}
    assert [d["id"] for d in dashboards(odd)] == ["ok"] and dashboards(odd)[0]["short"] == "Sales boar"
    assert dashboards({}) == []


async def test_opening_a_dashboard(assistant, monkeypatch) -> None:  # noqa: F811
    import webbrowser

    from core.runtime import WebBridge

    a = assistant
    a._loop = asyncio.get_running_loop()
    opened, jobpilot = [], []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(a, "open_jobpilot", lambda: jobpilot.append(True))
    a.config["web_ui"]["dashboards"] = [{"id": "pmo", "label": "PMO Dashboard", "url": "https://example.com/pmo"},
                                        {"id": "jobpilot", "label": "JobPilot dashboard", "open": "jobpilot"}]
    bridge = WebBridge(a)
    assert [d["id"] for d in bridge.snapshot()["dashboards"]] == ["pmo", "jobpilot"]
    assert "url" not in bridge.snapshot()["dashboards"][0]                  # the page gets names, not addresses
    assert bridge.open_dashboard("pmo") and bridge.open_dashboard("jobpilot")
    assert not bridge.open_dashboard("https://example.com/evil")            # only configured ones
    for _ in range(50):
        if opened and jobpilot:
            break
        await asyncio.sleep(0.02)
    assert opened == ["https://example.com/pmo"] and jobpilot == [True]


def test_the_server_routes_dashboards_from_the_pc_only() -> None:
    from ui.server import PC_ONLY, _handle

    class Bridge:
        def __init__(self) -> None:
            self.opened = []

        def open_dashboard(self, dashboard_id):
            self.opened.append(dashboard_id)
            return True

    bridge = Bridge()
    _handle(bridge, json.dumps({"type": "open_dashboard", "id": "pmo"}))
    _handle(bridge, json.dumps({"type": "open_dashboard", "id": "pmo"}), phone=True)   # not from a phone
    _handle(bridge, json.dumps({"type": "open_dashboard", "id": 3}))                    # dropped
    assert bridge.opened == ["pmo"] and "open_dashboard" in PC_ONLY
