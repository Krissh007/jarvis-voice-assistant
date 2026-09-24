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
    ("YouTube Music", "youtube music"), ("terminal", "terminal"), ("Microsoft Word", "word"),
    ("excel", "excel"), ("notepad", "notepad"),
])
def test_allowlisted_apps_resolve(said, expected) -> None:
    assert system.resolve_app(said, ALLOW, ALIASES)[0] == expected


@pytest.mark.parametrize("said", ["regedit", "cmd.exe", "crome", "calculator2", "task manager", "", "chrome.exe",
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
