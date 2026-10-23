"""Staying awake: JARVIS listens until a minute passes with nothing from the user, or they say to sleep."""

from __future__ import annotations

import time

import pytest

from core.wakeword import WakeState, sleep_request


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_awake_for_a_minute_after_the_users_last_input() -> None:
    clock = Clock()
    ws = WakeState(clock=clock)
    assert ws.active_window_s == 60
    ws.wake()
    clock.t += 45
    assert not ws.check_sleep(playing=False)       # 45 s of thinking time is fine
    ws.touch()                                      # the user said something
    clock.t += 59
    assert not ws.check_sleep(playing=False)
    clock.t += 2
    assert ws.check_sleep(playing=False) and not ws.awake


def test_the_minute_waits_while_jarvis_is_talking() -> None:
    clock = Clock()
    ws = WakeState(clock=clock)
    ws.wake()
    clock.t += 90
    assert not ws.check_sleep(playing=True)        # a long answer: the user hasn't had the chance to reply
    clock.t += 59
    assert not ws.check_sleep(playing=False)
    clock.t += 2
    assert ws.check_sleep(playing=False)


@pytest.mark.parametrize("text, kind", [
    ("Go to sleep.", "sleep"), ("Jarvis, go to sleep", "sleep"), ("okay stop listening", "sleep"),
    ("That's all for now, thanks.", "sleep"), ("That’s it", "sleep"), ("Goodbye Jarvis.", "sleep"),
    ("good night", "sleep"), ("Stop.", "stop"), ("stop it please", "stop"),
    ("stop the music", ""), ("go to sleep in an hour", ""), ("that's all I needed to know about Rust", ""),
    ("remind me to sleep at ten", ""), ("", ""),
])
def test_sleep_requests_are_the_whole_sentence(text, kind) -> None:
    assert sleep_request(text) == kind


@pytest.fixture
def assistant(tmp_path):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "wake_word"
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    a.settings.path = tmp_path / "settings.json"   # never this PC's own settings
    a.wake = a.wake or WakeState()
    a.wake.wake()
    return a


def said(a, text: str, ago: float = 1.5) -> None:
    a.on_input_transcript(text)
    a._sleep_heard_at = time.monotonic() - ago     # the sentence ended this long ago
    a._last_reply_at = time.monotonic() - 10


def test_go_to_sleep_sleeps_once_the_sentence_is_finished(assistant) -> None:
    a = assistant
    a.on_input_transcript("Go to")
    a._check_sleep_request(working=False)
    assert a.wake.awake                              # still talking: maybe "go to the settings"
    said(a, " sleep.")
    a._check_sleep_request(working=False)
    assert not a.wake.awake


def test_a_longer_sentence_is_a_request_not_sleep(assistant) -> None:
    a = assistant
    said(a, "Go to sleep after you open Spotify.")
    a._check_sleep_request(working=False)
    assert a.wake.awake


def test_a_bare_stop_sleeps_only_when_jarvis_is_idle(assistant) -> None:
    a = assistant
    said(a, "Stop.")
    a._turn_open = True                              # JARVIS was answering: "stop" means stop talking
    a._check_sleep_request(working=False)
    assert a.wake.awake
    a.transcript.flush()
    said(a, "Stop.")
    a._turn_open = False
    a._check_sleep_request(working=True)             # a tool is running: "stop" stops that
    assert a.wake.awake
    a.transcript.flush()
    said(a, "Stop.")
    a._check_sleep_request(working=False)
    assert not a.wake.awake


def test_the_settings_page_changes_the_minute_at_once(assistant) -> None:
    from core.runtime import WebBridge

    a = assistant
    out = WebBridge(a).change_setting("wake_word.active_window_s", 120)
    assert out["ok"] and "120 seconds" in out["message"] and a.wake.active_window_s == 120
    item = next(i for c in out["settings"]["categories"] for i in c["items"] if i["key"] == "wake_word.active_window_s")
    assert item["value"] == 120 and item["restart"] is False
    assert not WebBridge(a).change_setting("wake_word.active_window_s", 5)["ok"]   # too short to be useful
