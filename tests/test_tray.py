"""Tray icon, talk-without-the-wake-word, staying awake while a tool works, desktop shortcut."""

import time

import pytest

from core.tray import STATES, Tray, draw_icon, save_ico


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    import main
    from core import wakeword
    from core.runtime import Assistant, Options

    monkeypatch.setattr(wakeword.WakeWordDetector, "from_config", classmethod(lambda cls, *a, **k: object()))
    cfg = main.load_config()
    cfg["listening"]["mode"] = "wake_word"
    cfg["features"].update(wake_word=True, tray=True)
    return Assistant(cfg, client=None, options=Options(log_dir=tmp_path))


@pytest.mark.parametrize("state", list(STATES))
def test_every_state_has_a_distinct_icon(state) -> None:
    img = draw_icon(state)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # transparent corner
    others = [draw_icon(s).tobytes() for s in STATES if s != state]
    assert img.tobytes() not in others


def test_shortcut_icon_file(tmp_path) -> None:
    from PIL import Image

    path = save_ico(tmp_path / "assets" / "jarvis.ico")
    with Image.open(path) as ico:
        assert (256, 256) in ico.info["sizes"] and (16, 16) in ico.info["sizes"]


def test_shortcut_script_quotes_paths(tmp_path) -> None:
    from pathlib import Path

    from core.shortcut import shortcut_script

    script = shortcut_script(r"C:\Users\O'Neil\Desktop\JARVIS.lnk", Path(r"E:\JARVIS Agent\jarvis\run.bat"),
                             Path(r"E:\JARVIS Agent\jarvis"), Path(r"E:\JARVIS Agent\jarvis\assets\jarvis.ico"))
    assert "CreateShortcut('C:\\Users\\O''Neil\\Desktop\\JARVIS.lnk')" in script
    assert "$s.TargetPath = 'E:\\JARVIS Agent\\jarvis\\run.bat'" in script and script.endswith("$s.Save()")


def test_tray_only_redraws_on_change() -> None:
    class Icon:
        def __init__(self):
            self.updates = 0
            self.icon = self.title = None

        def update_menu(self):
            self.updates += 1

    tray = Tray(on_talk=lambda: None, on_stop=lambda: None, on_mute=lambda: None, on_quit=lambda: None,
                is_muted=lambda: False)
    tray._icon = Icon()
    for state in ["asleep", "listening", "listening", "listening", "muted"]:
        tray.set_state(state)
    assert tray._icon.updates == 2 and tray._icon.title == "JARVIS: microphone muted"


def test_talk_wakes_without_the_wake_word_and_unmutes(assistant) -> None:
    a = assistant
    assert a.tray is not None and not a.wake.awake and a._tray_state() == "asleep"
    a.gate.muted = True
    assert a._tray_state() == "muted"
    a.talk()
    assert a.wake.awake and not a.gate.muted
    assert a._tray_state() in {"listening", "speaking"}  # the wake beep may be playing


def test_push_to_talk_key_wakes_in_wake_word_mode(assistant) -> None:
    assistant.on_hotkey("push_to_talk", True)
    assert assistant.wake.awake
    assistant.on_hotkey("push_to_talk", False)  # releasing does nothing
    assert assistant.wake.awake


def test_mute_from_the_tray_toggles(assistant) -> None:
    assistant.on_hotkey("mute", True)
    assert assistant.gate.muted and assistant._tray_state() == "muted"
    assistant.on_hotkey("mute", True)
    assert not assistant.gate.muted


async def test_stays_awake_while_a_tool_is_working(assistant) -> None:
    """Live log: JARVIS fell asleep while it was still reading pages about Auckland's time."""
    a = assistant
    a.wake.wake()
    a.wake._last_activity -= 60  # quiet for a minute...
    a._last_tool_at = time.monotonic()  # ...but a tool call's turn is still open
    working = time.monotonic() - a._last_tool_at < 90
    assert not a.wake.check_sleep(a.playback.playing or working)
    a.on_turn_complete()  # the answer came: the normal quiet window applies again
    assert a._last_tool_at == 0.0
    a.wake._last_activity -= 60
    assert a.wake.check_sleep(False)


async def test_quit_is_a_clean_exit(assistant) -> None:
    import asyncio

    a = assistant

    async def main_task():
        a._main_task = asyncio.current_task()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            assert a._exiting
            return "clean"

    task = asyncio.ensure_future(main_task())
    await asyncio.sleep(0)
    a.request_exit("tray")
    assert await task == "clean"


def _other_process(code: str) -> str:
    """Run app_mode code in a separate process (a real second launch), return what it prints."""
    import subprocess
    import sys

    from core.shortcut import ROOT

    out = subprocess.run([sys.executable, "-c", "from core import app_mode as m\n" + code], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def test_single_instance_lock_waits_for_a_quitting_jarvis() -> None:
    import uuid

    from core import app_mode

    name = f"Local\\JARVIS-test-{uuid.uuid4().hex}"
    saved = app_mode._mutex_handle
    try:
        assert app_mode.claim_single_instance(name) is True
        # a second launch while this "JARVIS" runs: refused after its short wait
        assert _other_process(f"print(m.claim_single_instance({name!r}, wait_s=0.3))") == "False"
        app_mode.release_single_instance()  # Quit: released as shutdown starts
        assert _other_process(f"print(m.claim_single_instance({name!r}, wait_s=0.3))") == "True"
    finally:
        app_mode.release_single_instance()
        app_mode._mutex_handle = saved


def test_second_launch_brings_the_window_to_the_front() -> None:
    import uuid

    from core import app_mode

    name = f"Local\\JARVIS-show-test-{uuid.uuid4().hex}"
    saved = app_mode._show_event
    try:
        app_mode.create_show_event(name)
        assert not app_mode.show_requested()
        assert _other_process(f"print(m.request_show({name!r}))") == "True"
        assert app_mode.show_requested() is True
        assert app_mode.show_requested() is False  # auto-reset: one request, one bring-to-front
        assert _other_process("print(m.request_show('JARVIS-no-such-event'))") == "False"
    finally:
        app_mode._show_event = saved


def test_desktop_icon_starts_jarvis_without_a_terminal() -> None:
    from core.shortcut import ROOT, app_command, shortcut_script

    target, arguments = app_command()
    assert target.name == "pythonw.exe" and arguments == "main.py"
    script = shortcut_script("x.lnk", target, ROOT, ROOT / "assets" / "jarvis.ico", arguments)
    assert "$s.Arguments = 'main.py'" in script and "pythonw.exe" in script


def test_windowless_console_goes_to_a_file(tmp_path, monkeypatch) -> None:
    import sys

    from core import app_mode

    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    path = app_mode.redirect_console(tmp_path)
    print("JARVIS online (नमस्ते)")
    sys.stdout.close()
    assert "JARVIS online (नमस्ते)" in path.read_text(encoding="utf-8")


def test_talk_while_awake_says_yes(assistant) -> None:
    a = assistant
    a._ready = True
    notices = []
    a._notify = notices.append
    a.talk()  # asleep -> wakes, beeps, no "Yes?"
    assert a.wake.awake and notices == []
    a.player.clear()
    a.talk()  # already awake -> "Yes?"
    assert len(notices) == 1 and '"Yes?"' in notices[0]
    a.talk()  # double click: only one "Yes?"
    assert len(notices) == 1


def test_talk_while_speaking_cuts_the_reply_off(assistant) -> None:
    a = assistant
    a.talk()
    a._last_ack = 0.0
    a._notify = lambda text: None
    a.player.enqueue(b"\x00" * 4000)
    a.talk()
    assert a._drop_reply and not a.playback.playing


def test_taskbar_id_is_written_to_a_desktop_shortcut_but_never_a_pinned_one(tmp_path) -> None:
    """Live lesson: editing a pinned taskbar shortcut makes Windows unpin it."""
    import pytest

    from core import taskbar
    from core.shortcut import ROOT, app_command, shortcut_script

    target, arguments = app_command()
    lnk = tmp_path / "JARVIS.lnk"
    import subprocess

    subprocess.run(["powershell", "-NoProfile", "-Command",
                    shortcut_script(str(lnk), target, ROOT, ROOT / "assets" / "jarvis.ico", arguments)], check=True)
    assert taskbar.shortcut_app_id(lnk) is None
    assert taskbar.tag_shortcut(lnk, ROOT) is True
    assert taskbar.shortcut_app_id(lnk) == taskbar.APP_ID
    assert taskbar.tag_shortcut(lnk, ROOT) is False  # already tagged: left alone

    pinned = tmp_path / "User Pinned" / "TaskBar" / "JARVIS.lnk"
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(lnk.read_bytes())
    assert taskbar.tag_shortcut(pinned, ROOT) is False
    with pytest.raises(ValueError, match="unpin"):
        taskbar.set_shortcut_app_id(pinned)


def test_other_apps_shortcuts_are_not_jarvis(tmp_path) -> None:
    import subprocess

    from core import taskbar
    from core.shortcut import ROOT, shortcut_script

    lnk = tmp_path / "Notepad.lnk"
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    shortcut_script(str(lnk), __import__("pathlib").Path(r"C:\Windows\notepad.exe"), tmp_path,
                                    tmp_path / "x.ico")], check=True)
    assert taskbar.is_jarvis_shortcut(lnk, ROOT) is False
    assert taskbar.tag_shortcut(lnk, ROOT) is False


async def test_typed_message_is_shown_and_sent_without_waking_the_mic(assistant) -> None:
    import asyncio

    a = assistant
    a.send_typed("  what's   the time  ")
    await asyncio.sleep(0)
    assert a.transcript.lines[-1] == (a.transcript._labels["user"], "what's the time")
    assert a.session._outgoing.get_nowait() == ("text", "what's the time")
    assert not a.wake.awake  # typing never opens the microphone


def test_answer_to_a_typed_message_can_be_silent(assistant) -> None:
    a = assistant
    a._quiet_reply = True  # "Speak replies to typed messages" is off
    a.on_audio(b"\x01\x00" * 480)
    assert not a.playback.playing and not a.player._chunks
    a.on_turn_complete()
    assert not a._quiet_reply  # the next answer (e.g. to speech) is spoken again


def test_answering_while_asleep_shows_speaking_and_lowers_music(assistant) -> None:
    a = assistant
    a.playback.set_playing(True)
    assert not a.wake.awake and a._tray_state() == "speaking" and a._should_duck()
    a.playback.set_playing(False)


def test_talking_keeps_jarvis_awake_before_gemini_transcribes_it(assistant) -> None:
    """Live log: JARVIS fell asleep while the user was still talking, and the rest was lost."""
    import numpy as np
    from types import SimpleNamespace

    from core.denoise import VoiceActivity

    a = assistant
    a._denoiser, a._voice = SimpleNamespace(speech_prob=0.0), VoiceActivity()
    quiet = (np.random.default_rng(0).normal(0, 50, 1280)).astype(np.int16).tobytes()
    voice = (np.sin(np.arange(1280) / 5) * 3000).astype(np.int16).tobytes()
    for _ in range(20):
        a._send_mic_audio(quiet)  # the room
    a.wake.wake()
    a.wake._last_activity -= 14.5  # nearly the end of the quiet window
    a._denoiser.speech_prob = 0.97  # the user starts talking; no transcript yet
    for _ in range(5):
        a._send_mic_audio(voice)
    a.wake._last_activity -= 1.0
    assert not a.wake.check_sleep(False)


def test_steady_noise_does_not_keep_jarvis_awake() -> None:
    import numpy as np

    from core.denoise import VoiceActivity

    va = VoiceActivity()
    noise = (np.random.default_rng(1).normal(0, 300, 1280 * 200)).astype(np.int16)
    fooled = [va.update(0.95, noise[i:i + 1280].tobytes()) for i in range(0, len(noise), 1280)]
    assert not any(fooled[20:])  # sounds like a voice to RNNoise now and then, but it's no louder than the room
    va2 = VoiceActivity()
    for i in range(0, 1280 * 20, 1280):
        va2.update(0.1, noise[i:i + 1280].tobytes())
    loud = (noise[:1280] * 8).astype(np.int16).tobytes()
    assert [va2.update(0.95, loud) for _ in range(4)] == [False, False, False, True]  # a moment of real speech
