"""Phase 3: system actions. Nothing is really launched: `_launch` is replaced by a recorder."""

import pytest

import main
from actions import system
from core.registry import Tier
from tests.fakes import fake_live as fl

CONFIG = main.load_config()
ALLOW = CONFIG["apps"]["allowlist"]
ALIASES = CONFIG["apps"]["aliases"]
SITES = CONFIG["sites"]["read_allowlist"]


@pytest.fixture
def launched(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(system, "_launch", calls.append)
    return calls


@pytest.fixture
def h(tmp_path, launched):
    return fl.SafetyHarness(tmp_path, config={"apps": CONFIG["apps"], "sites": CONFIG["sites"]})


# ---------------------------------------------------------------- app allowlist matching

@pytest.mark.parametrize("said, expected", [
    ("calculator", "calculator"), ("Calculator", "calculator"), ("the calculator app", "calculator"),
    ("calc", "calculator"), ("Chrome", "chrome"), ("Google Chrome", "chrome"), ("VS Code", "vs code"),
    ("vscode", "vs code"), ("Visual Studio Code", "vs code"), ("file explorer", "file explorer"),
    ("terminal", "terminal"), ("Microsoft Word", "word"),
    ("excel", "excel"), ("notepad", "notepad"),
])
def test_allowlisted_apps_resolve(said, expected) -> None:
    assert system.resolve_app(said, ALLOW, ALIASES)[0] == expected


@pytest.mark.parametrize("said", ["youtube music", "regedit", "cmd.exe", "crome", "calculator2", "task manager", "", "chrome.exe",
                                  "notepad; del C:\\", "control panel", "settings"])
def test_unknown_apps_are_never_guessed(said) -> None:
    assert system.resolve_app(said, ALLOW, ALIASES) is None


def test_every_config_alias_points_at_a_real_app() -> None:
    assert all(target in ALLOW for target in ALIASES.values())


async def test_open_app_launches_only_the_allowlisted_command(h, launched) -> None:
    resp = await h.model_calls("open_app", name="Calculator")
    assert resp["status"] == "ok" and resp["result"] == "Opened calculator."
    assert launched == ["calc.exe"]


async def test_open_regedit_is_refused(h, launched) -> None:
    resp = await h.model_calls("open_app", name="regedit")
    assert "isn't on my list" in resp["result"] and launched == []


def test_no_destructive_tools_exist(h) -> None:
    names = [d.name for d in h.registry.declarations()]
    for word in ["delete", "remove", "move", "shell", "run_command", "exec", "format"]:
        assert not any(word in n for n in names), word


# ---------------------------------------------------------------- websites

@pytest.mark.parametrize("url, tier", [
    ("youtube.com", Tier.SAFE), ("https://www.youtube.com/watch?v=x", Tier.SAFE), ("music.youtube.com", Tier.SAFE),
    ("en.wikipedia.org/wiki/Python", Tier.SAFE), ("google.com", Tier.SAFE),
    ("naukri.com", Tier.CONFIRM), ("https://www.naukri.com/jobs", Tier.CONFIRM),
    ("evilyoutube.com", Tier.CONFIRM), ("youtube.com.evil.example", Tier.CONFIRM),
    ("javascript:alert(1)", Tier.BLOCKED), ("file:///C:/Windows", Tier.BLOCKED), ("ms-settings:privacy", Tier.BLOCKED),
    ("https://user:pass@youtube.com", Tier.BLOCKED), ("not a url", Tier.BLOCKED), ("", Tier.BLOCKED),
])
def test_website_tiers(url, tier) -> None:
    assert system.website_tier(url, SITES) == tier


async def test_allowlisted_site_opens_immediately(h, launched) -> None:
    resp = await h.model_calls("open_website", url="youtube.com")
    assert resp["status"] == "ok" and launched == ["https://youtube.com"]


async def test_other_site_needs_confirmation(h, launched) -> None:
    resp = await h.model_calls("open_website", url="naukri.com")
    assert resp["status"] == "awaiting_confirmation" and launched == []
    assert resp["summary"] == "Open naukri.com in your browser"
    await h.dispatcher.on_hotkey("confirm")
    assert launched == ["https://naukri.com"]


def test_website_summary_shows_path_and_query() -> None:
    _, summarize, _ = system.make_open_website(CONFIG)
    assert summarize({"url": "https://www.naukri.com/jobs/?q=python"}) == "Open www.naukri.com/jobs?q=python in your browser"


async def test_non_web_links_are_blocked(h, launched) -> None:
    resp = await h.model_calls("open_website", url="file:///C:/Windows/System32")
    assert resp["status"] == "refused" and launched == []


async def test_taint_makes_open_app_need_confirmation(h, launched) -> None:
    await h.model_calls("test_fetch_untrusted")
    resp = await h.model_calls("open_app", name="terminal")
    assert resp["status"] == "awaiting_confirmation" and resp["summary"] == "Open terminal"
    assert launched == []


# ---------------------------------------------------------------- status, volume, media

def test_system_status_mentions_battery_cpu_memory() -> None:
    text = system.get_system_status()
    assert "CPU" in text and "memory" in text and ("battery" in text)


def test_set_volume_clamps_and_uses_core_audio(monkeypatch) -> None:
    import pycaw.pycaw

    levels = []

    class FakeVolume:
        def SetMasterVolumeLevelScalar(self, v, _ctx):
            levels.append(v)

        def GetMute(self):
            return 1

        def SetMute(self, m, _ctx):
            levels.append(("mute", m))

    class FakeDevice:
        EndpointVolume = FakeVolume()

    monkeypatch.setattr(pycaw.pycaw.AudioUtilities, "GetSpeakers", staticmethod(lambda: FakeDevice()))
    assert system.set_volume(30) == "Volume set to 30%."
    assert system.set_volume(250) == "Volume set to 100%."
    assert levels[0] == 0.30 and ("mute", 0) in levels and 1.0 in levels


def test_media_key_rejects_unknown_actions() -> None:
    assert "I don't know" in system.media_key("rewind 10 minutes")


def test_media_key_sends_the_virtual_key(monkeypatch) -> None:
    import ctypes

    sent = []

    class User32:
        def keybd_event(self, vk, scan, flags, extra):
            sent.append((vk, flags))

    class WinDLL:
        user32 = User32()

    monkeypatch.setattr(ctypes, "windll", WinDLL())
    assert system.media_key("pause") == "Toggled play/pause."
    assert sent == [(0xB3, 0), (0xB3, 2)]


# ---------------------------------------------------------------- close_app

class FakeProc:
    def __init__(self, name: str, cmdline: list[str] | None = None) -> None:
        self._name, self._cmd = name, cmdline or [name]

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd


@pytest.fixture
def desktop(monkeypatch):
    """Fake open windows: (hwnd, pid, class) plus the process behind each pid."""
    import psutil

    procs = {
        1: FakeProc("Notepad.exe"), 2: FakeProc("explorer.exe"), 3: FakeProc("explorer.exe"),
        4: FakeProc("chrome.exe", ["chrome.exe", "--profile-directory=Default"]),
        5: FakeProc("chrome.exe", ["chrome.exe", "--user-data-dir=E:/jarvis/data/browser_profile"]),
        6: FakeProc("ApplicationFrameHost.exe"), 7: FakeProc("CalculatorApp.exe"),
    }
    windows = [(101, 1, "Notepad"), (102, 2, "CabinetWClass"), (103, 3, "Shell_TrayWnd"),
               (104, 4, "Chrome_WidgetWin_1"), (105, 5, "Chrome_WidgetWin_1"), (106, 6, "ApplicationFrameWindow")]
    monkeypatch.setattr(system, "_visible_windows", lambda: windows)
    monkeypatch.setattr(system, "_frame_app_pids", lambda hwnd: {7} if hwnd == 106 else set())
    monkeypatch.setattr(psutil, "Process", lambda pid: procs[pid])
    return windows


@pytest.mark.parametrize("command, expected", [
    ("notepad.exe", [101]),
    ("explorer.exe", [102]),  # folder windows only, never the taskbar/desktop
    ("chrome.exe", [104]),  # the user's Chrome, never JARVIS's own
    ("calc.exe", [106]),  # store app inside an ApplicationFrameHost frame
    ("winword.exe", []),
])
def test_windows_to_close(desktop, command, expected) -> None:
    assert system.windows_to_close(command) == expected


async def test_close_app_needs_confirmation_and_is_polite(h, desktop, monkeypatch) -> None:
    import ctypes

    posted = []

    class User32:
        def PostMessageW(self, hwnd, msg, w, l):
            posted.append((hwnd, msg))

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": User32()})())
    resp = await h.model_calls("close_app", name="Notepad")
    assert resp["status"] == "awaiting_confirmation" and resp["summary"] == "Close all notepad windows"
    assert posted == []
    await h.dispatcher.on_hotkey("confirm")
    assert posted == [(101, system.WM_CLOSE)]  # WM_CLOSE, not a force-kill


async def test_close_app_refuses_unlisted_apps(h, desktop) -> None:
    await h.model_calls("close_app", name="regedit")
    await h.dispatcher.on_hotkey("confirm")
    assert "isn't on my list" in h.audit()[-1]["result"]


async def test_discarding_unsaved_work_is_spelled_out_in_the_confirmation(h, desktop, monkeypatch) -> None:
    import ctypes

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": type("U", (), {
        "PostMessageW": staticmethod(lambda *a: None)})()})())
    pressed = []
    monkeypatch.setattr(system, "click_dont_save", lambda command: pressed.append(command) or 1)
    resp = await h.model_calls("close_app", name="notepad", discard_unsaved=True)
    assert resp["summary"] == "Close all notepad windows and throw away any unsaved changes"
    assert pressed == []  # nothing pressed before the user confirms
    await h.dispatcher.on_hotkey("confirm")
    assert pressed == ["notepad.exe"]


def test_dont_save_button_names() -> None:
    for name in ["Don't save", "Don’t Save", "Do not save", "don't save"]:
        assert system.DONT_SAVE.match(name)
    for name in ["Save", "Cancel", "Don't save and delete all files"]:
        assert not system.DONT_SAVE.match(name)


async def test_focus_app_is_safe_and_brings_the_window_forward(h, desktop, monkeypatch) -> None:
    focused = []
    monkeypatch.setattr(system, "focus_window", focused.append)
    resp = await h.model_calls("focus_app", name="notepad")
    assert resp["status"] == "ok" and resp["result"] == "Switched to notepad." and focused == [101]
    assert "isn't open" in (await h.model_calls("focus_app", name="word"))["result"]
