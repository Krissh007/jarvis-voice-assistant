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
    assert re.findall(r'data-go="([a-z]+)"', page) == ["chat", "talk", "command"]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for needle in ('action: "open_command"', 'action: "mute"', 'action: "talk"', "HOLD_MS", 'case "orb_visibility"',
                   'toWindow(ev.visible ? "show" : "hide")', '"error"', '"muted"'):
        assert needle in js, needle
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    radial = css[css.index("#radial"):css.index("#problem[hidden]")]
    assert "transition" not in radial.replace("No CSS transitions", "")   # the see-through window doesn't run them
    for name in ("error", "muted"):
        assert f"{name}:" in (STATIC / "orb3d.js").read_text(encoding="utf-8")
        assert f"{name}:" in (STATIC / "orb.js").read_text(encoding="utf-8")


def test_the_orb_window_hides_and_shows_on_its_page_s_word() -> None:
    src = (STATIC.parent / "window.py").read_text(encoding="utf-8")
    assert 'command == "hide" and orb_mode' in src and 'command == "show" and orb_mode' in src
