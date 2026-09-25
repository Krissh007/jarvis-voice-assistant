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
    assert "Address the user" not in build_system_instruction("")


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


# ---- echo mode ----

@pytest.mark.parametrize("name, mode", [
    ("Headphones (OnePlus Nord Buds 3 Pro)", "headphones"), ("Headset (Jabra Evolve)", "headphones"),
    ("AirPods Pro", "headphones"), ("Speaker (Realtek(R) Audio)", "speakers"), ("", "speakers"),
])
def test_echo_mode_is_guessed_from_the_output_device(name, mode) -> None:
    from core.audio import resolve_echo_mode

    assert resolve_echo_mode("auto", name) == mode


def test_echo_mode_setting_overrides_the_guess() -> None:
    from core.audio import resolve_echo_mode

    assert resolve_echo_mode("headphones", "Speaker (Realtek)") == "headphones"
    assert resolve_echo_mode("speakers", "Headphones (Buds)") == "speakers"


def make_assistant(monkeypatch, mode: str, output: str):
    import main
    from core import runtime
    from core.runtime import Assistant, Options

    monkeypatch.setattr(runtime, "default_output_name", lambda: output)
    cfg = main.load_config()
    cfg["listening"]["mode"] = mode
    cfg["audio"]["echo_mode"] = "auto"
    return Assistant(cfg, client=None, options=Options())


def test_speakers_ignore_the_mic_while_jarvis_talks(monkeypatch) -> None:
    a = make_assistant(monkeypatch, "always_on", "Speaker (Realtek(R) Audio)")
    sent = []
    monkeypatch.setattr(a.session, "send_audio", lambda pcm: sent.append(pcm))
    a.on_audio(chime())  # JARVIS talking
    a._on_mic_chunk(b"\x00\x00")
    assert sent == []


def test_headphones_allow_barge_in(monkeypatch) -> None:
    a = make_assistant(monkeypatch, "always_on", "Headphones (OnePlus Nord Buds 3 Pro)")
    sent = []
    monkeypatch.setattr(a.session, "send_audio", lambda pcm: sent.append(pcm))
    a.on_audio(chime())
    a._on_mic_chunk(b"\x00\x00")
    assert sent == [b"\x00\x00"]


def test_push_to_talk_on_speakers_is_not_gated(monkeypatch) -> None:
    a = make_assistant(monkeypatch, "push_to_talk", "Speaker (Realtek(R) Audio)")
    assert not a.gate.mute_while_speaking


async def test_stop_key_drops_the_rest_of_the_reply(monkeypatch) -> None:
    a = make_assistant(monkeypatch, "always_on", "Headphones (Buds)")
    a.on_audio(chime())
    a.on_hotkey("stop", True)
    a.on_audio(chime())  # model is still streaming the same reply
    assert not a.playback.playing
    a.on_turn_complete()
    a.on_audio(chime())  # next reply plays again
    assert a.playback.playing


# ---- following Windows audio devices ----

async def test_disconnecting_buds_switches_to_speaker_mode(monkeypatch) -> None:
    import asyncio

    from core import runtime

    a = make_assistant(monkeypatch, "always_on", "Headphones (OnePlus Nord Buds 3 Pro)")
    assert not a.gate.mute_while_speaking  # buds: interrupt by talking
    states = iter([
        ("Headphones (OnePlus Nord Buds 3 Pro)", frozenset({"Headphones (OnePlus Nord Buds 3 Pro)", "Speaker"})),
        ("Speaker (Realtek(R) Audio)", frozenset({"Speaker"})),  # buds turned off
    ])
    reopened = []
    monkeypatch.setattr(runtime, "windows_audio_state", lambda: next(states))
    monkeypatch.setattr(runtime, "refresh_portaudio", lambda: reopened.append("refresh"))
    monkeypatch.setattr(runtime, "default_output_name", lambda: "Speaker (Realtek(R) Audio)")
    monkeypatch.setattr(a.player, "start", lambda: reopened.append("player"))
    monkeypatch.setattr(a.player, "stop", lambda: None)
    task = asyncio.create_task(a._audio_watchdog(interval_s=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    assert reopened == ["refresh", "player"]  # reopened exactly once, on the new device
    assert a.echo_mode == "speakers" and a.gate.mute_while_speaking


async def test_no_switch_when_nothing_changes(monkeypatch) -> None:
    import asyncio

    from core import runtime

    a = make_assistant(monkeypatch, "always_on", "Speaker (Realtek(R) Audio)")
    monkeypatch.setattr(runtime, "windows_audio_state", lambda: ("Speaker", frozenset({"Speaker"})))
    switched = []

    async def fake_switch():
        switched.append(1)

    monkeypatch.setattr(a, "_switch_audio", fake_switch)
    task = asyncio.create_task(a._audio_watchdog(interval_s=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    assert switched == []


def test_closed_pipe_noise_is_hidden_but_other_errors_are_not(monkeypatch) -> None:
    import sys
    import types

    import main

    seen = []
    monkeypatch.setattr(sys, "unraisablehook", lambda args: seen.append(args.exc_value))
    main.quiet_closed_pipe_noise()

    class _ProactorBasePipeTransport:
        pass

    noise = types.SimpleNamespace(exc_value=ValueError("I/O operation on closed pipe"),
                                  object=_ProactorBasePipeTransport())
    real = types.SimpleNamespace(exc_value=ValueError("something else"), object=object())
    sys.unraisablehook(noise)
    sys.unraisablehook(real)
    assert [str(e) for e in seen] == ["something else"]


def test_amplify_clips_instead_of_wrapping() -> None:
    import numpy as np

    from core.wakeword import amplify

    out = amplify(np.array([100, -100, 20000, -20000], dtype=np.int16), 3.0)
    assert out.tolist() == [300, -300, 32767, -32768] and out.dtype == np.int16


def test_mic_test_suggests_the_smallest_gain_that_wakes_most() -> None:
    from core.mictest import gains_to_try, suggest_gain

    assert gains_to_try(1.0) == [1.0, 2.0, 3.0, 4.0]
    assert gains_to_try(2.5) == [1.0, 2.0, 2.5, 3.0, 4.0]
    assert suggest_gain({1.0: 1, 2.0: 5, 3.0: 5, 4.0: 4}, 1.0) == 2.0
    assert suggest_gain({1.0: 5, 2.0: 5}, 1.0) == 1.0
    assert suggest_gain({1.0: 4, 2.0: 5, 3.0: 5}, 3.0) == 3.0  # current already ties the best
    assert suggest_gain({1.0: 0, 2.0: 0}, 1.0) is None


def _bare_detector(threshold=0.5):
    from core.wakeword import WakeWordDetector

    class Model:
        def reset(self):
            pass

    det = WakeWordDetector.__new__(WakeWordDetector)
    det._model, det.threshold, det.gain, det.personal = Model(), threshold, 1.0, False
    det._peak, det._quiet, det.missed = 0.0, 0, None
    return det


def test_near_miss_is_reported_once_after_the_attempt_fades() -> None:
    det = _bare_detector(0.25)
    for s in [0.0, 0.12, 0.19, 0.08] + [0.0] * 6:
        assert det.update(s) is False
    assert det.missed is None  # not quiet long enough yet
    det.update(0.0)
    assert det.missed == 0.19
    det.missed = None
    for s in [0.05, 0.02] + [0.0] * 10:  # background noise never counts as an attempt
        det.update(s)
    assert det.missed is None


def test_a_wake_is_not_also_reported_as_a_near_miss() -> None:
    det = _bare_detector(0.25)
    assert [det.update(s) for s in [0.15, 0.3]] == [False, True]
    for _ in range(10):
        det.update(0.0)
    assert det.missed is None


def test_personal_verifier_is_used_only_when_trained(tmp_path, monkeypatch) -> None:
    from core.wakeword import WakeWordDetector, verifier_path

    made = []
    monkeypatch.setattr(WakeWordDetector, "__init__",
                        lambda self, name, threshold, gain, verifier=None: made.append((threshold, gain, verifier)))
    ww = {"model": "hey_jarvis", "threshold": 0.25, "verifier_threshold": 0.6, "gain": 2.0}
    WakeWordDetector.from_config(ww, tmp_path)
    path = verifier_path(tmp_path, "hey_jarvis")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    WakeWordDetector.from_config(ww, tmp_path, gain=3.0)
    WakeWordDetector.from_config({**ww, "personal_verifier": False}, tmp_path)
    assert made == [(0.25, 2.0, None), (0.6, 3.0, path), (0.25, 2.0, None)]


def test_train_from_clips_saves_a_working_verifier(tmp_path) -> None:
    import pickle

    import numpy as np
    import pytest

    from core.wake_training import PAD_S, pad, train_from_clips

    rng = np.random.default_rng(0)

    class FakeModel:
        def reset(self):
            pass

    def extract(clip, oww, name, threshold, N):
        assert len(clip) > PAD_S * 16000  # padded with leading silence
        loud = int(np.abs(clip).max()) > 1000
        if threshold > 0 and not loud:
            return np.empty((0, 16, 96))
        centre = 1.0 if loud and threshold > 0 else -1.0
        return rng.normal(centre, 0.3, size=(N * 3, 16, 96))

    pos = [np.full(32000, 5000, dtype=np.int16) for _ in range(4)]
    neg = [np.full(64000, 200, dtype=np.int16) for _ in range(3)]
    out = tmp_path / "wake" / "v.pkl"
    stats = train_from_clips(pos, neg, "hey_jarvis", out, extract=extract, new_model=FakeModel)
    assert stats["clips_used"] == 4 and out.exists()
    model = pickle.loads(out.read_bytes())
    assert model.predict_proba(rng.normal(1.0, 0.3, size=(1, 16, 96)))[0][-1] > 0.5
    assert model.predict_proba(rng.normal(-1.0, 0.3, size=(1, 16, 96)))[0][-1] < 0.5
    assert len(pad(np.zeros(10, dtype=np.int16))) == 10 + 24000

    quiet = [np.full(32000, 100, dtype=np.int16) for _ in range(4)]
    with pytest.raises(ValueError, match="sounded like the wake word"):
        train_from_clips(quiet, neg, "hey_jarvis", out, extract=extract, new_model=FakeModel)
