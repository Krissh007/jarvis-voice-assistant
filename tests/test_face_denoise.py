"""The JARVIS window (pure drawing/layout helpers + runtime wiring) and RNNoise noise suppression."""

import threading
import time

import numpy as np
import pytest

from core.face import STATE_LABELS, blend, clamp_position, orb_shapes, trim_caption
from core.tray import STATES

# ---------------------------------------------------------------- window helpers


def test_blend() -> None:
    assert blend("#000000", "#ffffff", 0) == "#000000"
    assert blend("#000000", "#ffffff", 1) == "#ffffff"
    assert blend("#000000", "#ffffff", 0.5) == "#808080"
    assert blend("#102030", "#102030", 0.7) == "#102030"
    assert blend("#000000", "#ffffff", 3) == "#ffffff"  # clamped


@pytest.mark.parametrize("state", list(STATES))
def test_orb_shapes_for_every_state(state) -> None:
    shapes = orb_shapes(state, t=1.0, level=0.5)
    kinds = {k for k, _, _ in shapes}
    assert kinds <= {"oval", "arc", "line"} and "arc" in kinds
    ring = STATES[state][0]
    assert any(opts.get("outline") == ring for _, _, opts in shapes)
    for _, coords, _ in shapes:  # everything stays on the canvas
        assert all(-1 <= v <= 201 for v in coords)
    assert state in STATE_LABELS


def test_orb_spins_and_pulses() -> None:
    def arc_starts(t):
        return [o["start"] for k, _, o in orb_shapes("listening", t, 0.0) if k == "arc"]

    assert arc_starts(0.0) != arc_starts(0.5)  # rings rotate over time

    def core_radius(level):
        ovals = [c for k, c, o in orb_shapes("listening", 0.0, level) if k == "oval" and o.get("fill") == STATES["listening"][1]]
        x0, _, x1, _ = ovals[-1]
        return (x1 - x0) / 2

    assert core_radius(1.0) > core_radius(0.0)  # louder voice, bigger core


def test_muted_orb_has_a_slash_and_asleep_ignores_the_mic() -> None:
    assert len([s for s in orb_shapes("muted", 0, 0) if s[0] == "line"]) > len(
        [s for s in orb_shapes("listening", 0, 0) if s[0] == "line"])
    assert orb_shapes("asleep", 0.3, 0.0) == orb_shapes("asleep", 0.3, 1.0)  # no pulse from background noise


def test_trim_caption() -> None:
    assert trim_caption("", "") == ""
    assert trim_caption("JARVIS", "  Hello   there ") == "JARVIS: Hello there"
    long = " ".join(f"word{i}" for i in range(100))
    out = trim_caption("JARVIS", long, limit=50)
    assert out.startswith("JARVIS: …") and out.endswith("word99") and len(out) <= len("JARVIS: ") + 50


def test_clamp_position_keeps_the_window_on_screen() -> None:
    assert clamp_position(100, 100, 240, 400, 1920, 1080) == (100, 100)
    assert clamp_position(3000, 2000, 240, 400, 1920, 1080) == (1680, 680)  # monitor unplugged
    assert clamp_position(-50, -10, 240, 400, 1920, 1080) == (0, 0)


# ---------------------------------------------------------------- runtime wiring


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    import main
    from core import wakeword
    from core.runtime import Assistant, Options

    monkeypatch.setattr(wakeword.WakeWordDetector, "from_config", classmethod(lambda cls, *a, **k: object()))
    cfg = main.load_config()
    cfg["listening"]["mode"] = "wake_word"
    cfg["features"].update(wake_word=True, window=True, tray=False)
    cfg["noise_suppression"]["enabled"] = False
    return Assistant(cfg, client=None, options=Options(log_dir=tmp_path))


def test_window_status_follows_the_assistant(assistant) -> None:
    from core.runtime import rms_level

    a = assistant
    assert a.face is not None
    assert a._face_status()["state"] == "starting"  # until run() has started
    a._ready = True
    assert a._face_status()["state"] == "asleep"
    a.talk()
    a.player.clear()
    a._mic_level = rms_level(np.full(1280, 1500, dtype=np.int16).tobytes(), 3000)
    st = a._face_status()
    assert st["state"] == "listening" and st["level"] == pytest.approx(0.5, abs=0.01)
    a.gate.muted = True
    st = a._face_status()
    assert (st["state"], st["level"], st["lines"]) == ("muted", 0.0, [])


def test_captions_show_live_text_then_the_last_line(assistant) -> None:
    t = assistant.transcript
    t.add("user", "what's the ")
    t.add("user", "weather?")
    assert t.caption() == (t._labels["user"], "what's the weather?")
    t.add("jarvis", "It's sunny.")
    assert t.caption() == ("JARVIS", "It's sunny.")
    t.flush()
    assert t.caption() == ("JARVIS", "It's sunny.")  # stays up after the line is printed


def test_rms_level() -> None:
    from core.runtime import rms_level

    assert rms_level(b"", 3000) == 0.0
    assert rms_level(np.zeros(160, dtype=np.int16).tobytes(), 3000) == 0.0
    assert rms_level(np.full(160, 30000, dtype=np.int16).tobytes(), 3000) == 1.0


def test_muting_while_audio_is_being_cleaned_drops_it(assistant) -> None:
    sent = []
    assistant.session.send_audio = sent.append
    assistant.gate.mute_while_speaking = False  # independent of this PC's speakers/headphones
    assistant._send_mic_audio(b"\x00\x01")
    assistant.gate.muted = True
    assistant._send_mic_audio(b"\x00\x02")
    assert sent == [b"\x00\x01"]


# ---------------------------------------------------------------- noise suppression


def _speech_and_noise(seconds=3.0, seed=1):
    rng = np.random.default_rng(seed)
    sr = 16000
    t = np.arange(int(sr * seconds)) / sr
    speech = np.zeros_like(t)
    for a, b in [(0.5, 1.2), (1.6, 2.4)]:
        m = (t >= a) & (t < b)
        speech[m] = sum(np.sin(2 * np.pi * 180 * k * t[m]) / k for k in range(1, 8)) * 3000
    x = np.clip(speech + rng.normal(0, 800, len(t)), -32768, 32767).astype(np.int16)
    quiet = (t < 0.45) | ((t > 1.3) & (t < 1.55)) | (t > 2.5)
    loud = (t > 0.7) & (t < 1.1)
    return x, quiet, loud


def _rms(v):
    return float(np.sqrt(np.mean(v.astype(np.float64) ** 2)))


def test_denoiser_removes_hiss_and_keeps_speech() -> None:
    from core.denoise import Denoiser

    d = Denoiser()
    x, quiet, loud = _speech_and_noise()
    out = [np.frombuffer(d.process(x[i:i + 1280].tobytes()), dtype=np.int16) for i in range(0, len(x), 1280)]
    assert [len(o) for o in out] == [len(x[i:i + 1280]) for i in range(0, len(x), 1280)]  # same chunk sizes
    y = np.concatenate(out)
    assert _rms(y[quiet]) < _rms(x[quiet]) / 5  # background hiss at least ~14 dB quieter
    assert _rms(y[loud]) > _rms(x[loud]) * 0.6  # the voice survives
    assert 0.0 <= d.speech_prob <= 1.0
    d.close()


def test_denoiser_strength_zero_passes_audio_through() -> None:
    from core.denoise import Denoiser

    d = Denoiser(strength=0.0)
    x, quiet, _ = _speech_and_noise(1.0)
    y = np.concatenate([np.frombuffer(d.process(x[i:i + 1280].tobytes()), dtype=np.int16)
                        for i in range(0, len(x), 1280)])
    assert _rms(y[quiet][3000:]) > _rms(x[quiet][3000:]) * 0.8  # hiss kept (after warm-up)
    d.close()


def test_make_denoiser_is_optional() -> None:
    from core.denoise import make_denoiser

    assert make_denoiser({}) is None
    assert make_denoiser({"noise_suppression": {"enabled": False}}) is None
    d = make_denoiser({"noise_suppression": {"enabled": True, "strength": 0.5}})
    assert d is not None and d.strength == 0.5
    d.close()


def test_worker_keeps_order_and_passes_raw_when_behind() -> None:
    from core.denoise import DenoiseWorker

    gate = threading.Event()

    class SlowDenoiser:
        def process(self, pcm):
            gate.wait(2)
            return b"clean:" + pcm

        def close(self):
            pass

    got = []
    w = DenoiseWorker(SlowDenoiser(), got.append)
    w.MAX_BACKLOG = 3
    for i in range(6):
        w.submit(bytes([i]))
    time.sleep(0.05)
    gate.set()
    for _ in range(100):
        if len(got) == 6:
            break
        time.sleep(0.02)
    w.stop()
    cleaned = [g for g in got if g.startswith(b"clean:")]
    assert cleaned == sorted(cleaned) and len(got) == 6  # in order, nothing lost
    assert w.passed_raw >= 1  # the backlog overflowed, so some went through raw


def test_mic_test_noise_report_helpers() -> None:
    from core.mictest import background, block_rms, reduction_db

    assert block_rms(np.full(100, 300, dtype=np.int16)) == pytest.approx(300)
    levels = [50.0] * 8 + [3000.0] * 2  # mostly room noise, a little speech
    assert background(levels) == pytest.approx(50)
    assert reduction_db(800, 80) == pytest.approx(20)
    assert reduction_db(0, 0) == 0


# ---------------------------------------------------------------- ducking, Stop, Sleep, conversation


def test_duck_plan_lowers_then_restores_exactly() -> None:
    from core.ducking import DuckPlan

    plan = DuckPlan(0.3)
    changes = plan.duck([("chrome.exe:1", 0.8), ("spotify.exe:2", 1.0)])
    assert changes == pytest.approx({"chrome.exe:1": 0.24, "spotify.exe:2": 0.3})
    assert plan.duck([("chrome.exe:1", 0.24), ("vlc.exe:3", 0.5)]) == pytest.approx({"vlc.exe:3": 0.15})  # new app only
    assert plan.restore() == {"chrome.exe:1": 0.8, "spotify.exe:2": 1.0, "vlc.exe:3": 0.5}
    assert plan.restore() == {}


def test_music_volume_while_ducked_sets_the_normal_level() -> None:
    from core.ducking import DuckPlan

    plan = DuckPlan(0.5)
    plan.duck([("chrome.exe:1", 1.0)])
    assert plan.set_base(["chrome.exe:1"], 0.4, ducked=True) == pytest.approx({"chrome.exe:1": 0.2})
    assert plan.restore() == pytest.approx({"chrome.exe:1": 0.4})  # "turn it down" survives waking up
    assert plan.set_base(["chrome.exe:1"], 1.7, ducked=False) == {"chrome.exe:1": 1.0}  # clamped


def test_ducker_never_touches_jarvis_itself() -> None:
    import os

    from core.ducking import Ducker

    assert os.getpid() in Ducker(own_pids=[42]).own_pids and 42 in Ducker(own_pids=[42]).own_pids


def test_should_duck_follows_the_wake_state(assistant) -> None:
    a = assistant
    assert a._should_duck() is False
    a.talk()
    assert a._should_duck() is True
    a.sleep_now()
    assert a._should_duck() is False and not a.wake.awake


def test_sleep_button_cuts_off_the_reply(assistant) -> None:
    a = assistant
    a.talk()
    a.player.enqueue(b"\x00" * 2000)
    a.sleep_now()
    assert not a.wake.awake and a._drop_reply and not a.playback.playing


async def test_stop_button_stops_jarvis_and_pauses_the_music(assistant) -> None:
    import asyncio

    a = assistant
    calls = []

    class Media:
        async def func(self, action):
            calls.append(action)

    a.registry._tools["media_key"] = type("T", (), {"func": Media().func})()
    a.talk()
    a.stop_all()
    await asyncio.sleep(0.01)
    assert a._drop_reply and calls == ["pause"]


def test_conversation_snapshot_has_history_and_live_text(assistant) -> None:
    t = assistant.transcript
    v0, lines = t.snapshot()
    assert lines == []
    t.add("user", "play some rock")
    t.add("jarvis", "Playing Back In Black.")
    v1, lines = t.snapshot()
    assert v1 > v0 and lines[0][1] == "play some rock" and lines[-1] == ("JARVIS", "Playing Back In Black.")
    t.flush()
    v2, lines = t.snapshot()
    assert v2 > v1 and [text for _, text in lines] == ["play some rock", "Playing Back In Black."]
