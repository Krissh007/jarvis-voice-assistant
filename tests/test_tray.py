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


def test_single_instance_lock() -> None:
    import uuid

    from core import app_mode

    name = f"Local\JARVIS-test-{uuid.uuid4().hex}"
    saved = app_mode._mutex_handle
    try:
        assert app_mode.claim_single_instance(name) is True
        assert app_mode.claim_single_instance(name) is False  # a second JARVIS is refused
    finally:
        app_mode._mutex_handle = saved


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
