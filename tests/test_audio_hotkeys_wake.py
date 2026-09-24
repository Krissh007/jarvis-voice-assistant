"""Mic gating, echo tail, hotkey matching, wake-word state and runtime hotkey wiring."""

import pytest

from core.audio import MicGate, PlaybackState, Player, chime
from core.hotkeys import HotkeyMatcher, parse_combo
from core.prompts import build_system_instruction
from core.wakeword import WakeState


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


# ---- playback + echo tail ----

def test_echo_tail_lasts_after_playback_stops() -> None:
    clock = Clock()
    pb = PlaybackState(tail_s=0.4, clock=clock)
    assert not pb.speaking_or_tail()
    pb.set_playing(True)
    assert pb.speaking_or_tail()
    pb.set_playing(False)
    clock.t += 0.3
    assert pb.speaking_or_tail()
    clock.t += 0.2
    assert not pb.speaking_or_tail()


def test_player_clear_stops_playback_for_barge_in() -> None:
    pb = PlaybackState()
    player = Player(pb)
    player.enqueue(chime())
    assert pb.playing
    player.clear()
    assert not pb.playing


def test_player_callback_drains_and_goes_idle() -> None:
    import numpy as np

    pb = PlaybackState()
    player = Player(pb)
    player.enqueue(np.arange(100, dtype=np.int16).tobytes())
    out = np.zeros((160, 1), dtype=np.int16)
    player._callback(out, 160, None, None)
    assert list(out[:100, 0]) == list(range(100))
    assert not out[100:].any()
    assert not pb.playing


# ---- mic gate ----

def test_gate_always_on_passes_audio() -> None:
    assert MicGate(PlaybackState()).allows()


def test_gate_mute_blocks() -> None:
    assert not MicGate(PlaybackState(), muted=True).allows()


def test_gate_push_to_talk_only_while_held() -> None:
    gate = MicGate(PlaybackState(), push_to_talk=True)
    assert not gate.allows()
    gate.ptt_held = True
    assert gate.allows()


def test_gate_mute_while_speaking_includes_tail() -> None:
    clock = Clock()
    pb = PlaybackState(tail_s=0.4, clock=clock)
    gate = MicGate(pb, mute_while_speaking=True)
    pb.set_playing(True)
    assert not gate.allows()
    pb.set_playing(False)
    clock.t += 0.1
    assert not gate.allows()
    clock.t += 0.5
    assert gate.allows()


# ---- hotkeys ----

def test_parse_combo_uses_virtual_keys() -> None:
    assert parse_combo("ctrl+alt+y") == frozenset({"ctrl", "alt", ord("Y")})
    assert parse_combo("Ctrl + Alt + Space") == frozenset({"ctrl", "alt", 0x20})
    with pytest.raises(ValueError):
        parse_combo("ctrl+alt")
    with pytest.raises(ValueError):
        parse_combo("ctrl+@@")


def test_matcher_press_and_release() -> None:
    m = HotkeyMatcher({"ptt": parse_combo("ctrl+alt+space"), "mute": parse_combo("ctrl+alt+m")})
    assert m.press("ctrl") == []
    assert m.press("alt") == []
    assert m.press(0x20) == [("ptt", True)]
    assert m.press(0x20) == []  # key repeat doesn't re-fire
    assert m.release("alt") == [("ptt", False)]
    assert m.release(0x20) == []


def test_matcher_ignores_partial_combos() -> None:
    m = HotkeyMatcher({"mute": parse_combo("ctrl+alt+m")})
    m.press("ctrl")
    assert m.press(ord("M")) == []


# ---- wake word state ----

def test_wake_state_sleeps_after_silent_window() -> None:
    clock = Clock()
    ws = WakeState(active_window_s=15, clock=clock)
    assert not ws.awake
    ws.wake()
    clock.t += 10
    assert not ws.check_sleep(playing=False)
    ws.touch()  # user spoke
    clock.t += 14
    assert not ws.check_sleep(playing=False)
    clock.t += 2
    assert ws.check_sleep(playing=False)
    assert not ws.awake


def test_wake_state_stays_awake_while_jarvis_is_talking() -> None:
    clock = Clock()
    ws = WakeState(active_window_s=15, clock=clock)
    ws.wake()
    clock.t += 30
    assert not ws.check_sleep(playing=True)
    clock.t += 10
    assert not ws.check_sleep(playing=False)


# ---- prompt ----

def test_prompt_has_persona_name_and_untrusted_rule() -> None:
    text = build_system_instruction("Alex")
    assert "British" in text and "Alex" in text and "untrusted_content" in text
    assert "user's name" not in build_system_instruction("")


def test_prompt_lists_only_real_capabilities() -> None:
    assert "conversation only" in build_system_instruction(None)
    text = build_system_instruction(None, ["get_datetime: tells the time"])
    assert "get_datetime" in text and "conversation only" not in text


# ---- runtime wiring (no audio devices, no network) ----

@pytest.fixture
def assistant(monkeypatch):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    a = Assistant(cfg, client=None, options=Options())
    sent = []
    monkeypatch.setattr(a.session, "activity_start", lambda: sent.append("start"))
    monkeypatch.setattr(a.session, "activity_end", lambda: sent.append("end"))
    monkeypatch.setattr(a.session, "send_audio", lambda pcm: sent.append("audio"))
    return a, sent


def test_push_to_talk_hotkey_sends_activity_and_barges_in(assistant) -> None:
    a, sent = assistant
    a.player.enqueue(chime())
    a._on_mic_chunk(b"\x00\x00")
    assert sent == []  # not held: nothing sent
    a.on_hotkey("push_to_talk", True)
    assert not a.playback.playing  # barge-in cleared playback
    a._on_mic_chunk(b"\x00\x00")
    a.on_hotkey("push_to_talk", False)
    a._on_mic_chunk(b"\x00\x00")
    assert sent == ["start", "audio", "end"]


def test_mute_hotkey_toggles(assistant) -> None:
    a, _ = assistant
    a.on_hotkey("mute", True)
    assert a.gate.muted
    a.on_hotkey("mute", False)  # release does nothing
    assert a.gate.muted
    a.on_hotkey("mute", True)
    assert not a.gate.muted


def test_interruption_clears_playback(assistant) -> None:
    a, _ = assistant
    a.on_audio(chime())
    a.on_interrupted()
    assert not a.playback.playing
