"""The app's Settings page: what may change, how it's checked, kept and laid over config.yaml."""

import json

import pytest

import main
from core import settings_store as ss
from core.settings_store import BY_KEY, SettingsStore, apply_overlay, check, secret_status


@pytest.fixture
def config():
    return main.load_config()


def test_every_setting_points_at_a_real_config_value(config) -> None:
    for s in ss.SETTINGS:
        if s.key.startswith(("prefs.", "look.")):
            continue
        assert ss._get(config, s.key) is not None, s.key
    assert {s.category for s in ss.SETTINGS} == {c for c, _, _ in ss.CATEGORIES}


@pytest.mark.parametrize("key, value, expected", [
    ("prefs.voice", False, False),
    ("listening.end_pause_ms", 850.4, 850),
    ("wake_word.chime_volume", 0.1234, 0.123),
    ("voice.name", "Puck", "Puck"),
    ("models.live", " gemini-3.8-live ", "gemini-3.8-live"),
    ("groq.models", ["openai/gpt-oss-120b", " qwen/qwen3.8-27b"], ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]),
    ("hotkeys.mute", "Ctrl + Alt + K", "ctrl+alt+k"),
])
def test_allowed_values_are_normalised(config, key, value, expected) -> None:
    assert check(BY_KEY[key], value, config) == expected


@pytest.mark.parametrize("key, value, words", [
    ("prefs.voice", "yes", "on or off"),
    ("listening.end_pause_ms", 50, "goes from 400 to 3000"),
    ("listening.end_pause_ms", True, "needs a number"),
    ("voice.name", "Jarvis", "listed options"),
    ("listening.mode", "telepathy", "listed options"),
    ("models.live", "gemini live; rm -rf /", "model id"),
    ("groq.models", [], "1 to 6"),
    ("groq.models", ["ok-model", "bad model"], "doesn't look like"),
    ("hotkeys.mute", "k", "Ctrl, Alt or the Windows key"),
    ("hotkeys.mute", "ctrl+alt+%", "can't listen for"),
    ("hotkeys.mute", "ctrl+alt+s", "already Stop"),          # clashes with another JARVIS key
    ("features.agent_hub", False, "can't be changed here"),  # shown, never changed
    ("agent_hub.url", "http://evil.example/mcp", "can't be changed here"),
])
def test_disallowed_values_are_refused_in_words(config, key, value, words) -> None:
    with pytest.raises(ValueError, match=words):
        check(BY_KEY[key], value, config)


def test_a_clash_counts_saved_but_not_yet_applied_keys(config) -> None:
    with pytest.raises(ValueError, match="already Mute"):
        check(BY_KEY["hotkeys.stop"], "ctrl+alt+k", config, {"hotkeys.mute": "ctrl+alt+k"})


def test_changes_are_kept_and_marked_for_restart(config, tmp_path) -> None:
    store = SettingsStore(config, tmp_path / "settings.json")
    assert store.change("voice.name", "Puck") == "Puck"
    assert store.change("hotkeys.mute", "ctrl+alt+k") == "ctrl+alt+k"
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == \
        {"hotkeys.mute": "ctrl+alt+k", "voice.name": "Puck"}
    assert store.pending_restart() == ["hotkeys.mute", "voice.name"]
    voice = next(c for c in store.view({})["categories"] if c["id"] == "voice")
    item = next(i for i in voice["items"] if i["key"] == "voice.name")
    assert item["value"] == "Puck" and item["restart"] is True
    store.change("voice.name", config["voice"]["name"])           # back to what's running: nothing to apply
    assert store.pending_restart() == ["hotkeys.mute"]
    with pytest.raises(ValueError):
        store.change("features.test_tools", True)                 # not a setting the page offers
    with pytest.raises(ValueError):
        store.change("listening.mode", "telepathy")
    assert "features.test_tools" not in store.saved()


def test_live_values_come_from_the_runtime(config, tmp_path) -> None:
    store = SettingsStore(config, tmp_path / "settings.json")
    view = store.view({"prefs.voice": False, "look.orb_size": 140})
    items = {i["key"]: i for c in view["categories"] for i in c["items"]}
    assert items["prefs.voice"]["value"] is False and items["prefs.voice"]["applies"] == "now"
    assert items["look.orb_size"]["value"] == 140 and items["look.orb_size"]["restart"] is False
    assert items["features.agent_hub"]["editable"] is False


def test_overlay_applies_only_known_valid_keys_at_start(config, tmp_path, caplog) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "voice.name": "Puck", "hotkeys.mute": "ctrl+alt+k", "listening.end_pause_ms": 900,
        "features.test_tools": True,           # not changeable: ignored
        "agent_hub.url": "http://evil.example/mcp",  # shown only: ignored
        "listening.mode": "telepathy",         # no longer valid: ignored
        "prefs.voice": False,                  # the runtime's own file, not this one
    }), encoding="utf-8")
    applied = apply_overlay(config, path)
    assert sorted(applied) == ["hotkeys.mute", "listening.end_pause_ms", "voice.name"]
    assert config["voice"]["name"] == "Puck" and config["hotkeys"]["mute"] == "ctrl+alt+k"
    assert config["features"]["test_tools"] is False and config["agent_hub"]["url"].startswith("http://127.0.0.1")
    assert config["listening"]["mode"] == "wake_word"
    assert "ignored features.test_tools" in caplog.text


def test_overlay_survives_a_broken_file(config, tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert apply_overlay(config, path) == []
    assert apply_overlay(config, tmp_path / "missing.json") == []


def test_secrets_are_reported_as_set_or_not_never_their_values() -> None:
    values = {"GOOGLE_API_KEY": "AIzaSyREALLOOKINGKEY000000000000000", "AGENT_HUB_TOKEN": "t" * 43}
    status = secret_status(values.get)
    assert {s["name"]: s["set"] for s in status} == {
        "GOOGLE_API_KEY": True, "GROQ_API_KEY": False, "TAVILY_API_KEY": False, "AGENT_HUB_TOKEN": True,
        "OPENROUTER_API_KEY": False}
    text = json.dumps(status)
    assert all(v not in text for v in values.values())
