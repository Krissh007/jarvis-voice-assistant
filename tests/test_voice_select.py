"""Choosing JARVIS's voice: previews (a short Live session, cached) and switching at once without a restart."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.voice_preview import SAMPLE, VoicePreviews, find_voice
from tests.fakes import fake_live as fl


def test_find_voice_accepts_how_people_say_it() -> None:
    assert find_voice("kore") == "Kore" and find_voice("Kore.") == "Kore" and find_voice(" PUCK ") == "Puck"
    assert find_voice("Siri") is None and find_voice("") is None


async def test_a_preview_reads_the_fixed_sentence_once_and_is_cached(tmp_path) -> None:
    client = fl.FakeClient([[fl.audio(b"\x01\x00" * 50), fl.audio(b"\x02\x00" * 50), fl.turn_complete()]])
    previews = VoicePreviews(client, "gemini-test-live", tmp_path / "previews")
    pcm = await previews.sample("kore")
    assert pcm == b"\x01\x00" * 50 + b"\x02\x00" * 50
    config = client.connects[0]["config"]
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    (sent,) = client.sessions[0].sent
    assert sent["client_content"].parts[0].text == SAMPLE     # only the sample sentence leaves the PC
    assert await previews.sample("Kore") == pcm and len(client.connects) == 1   # the second one is from the cache
    assert (tmp_path / "previews" / "Kore.pcm").read_bytes() == pcm


async def test_a_preview_refuses_unknown_voices_and_reports_silence(tmp_path) -> None:
    previews = VoicePreviews(fl.FakeClient([[fl.turn_complete()]]), "m", tmp_path)
    with pytest.raises(ValueError):
        await previews.sample("Siri")
    with pytest.raises(RuntimeError):
        await previews.sample("Puck")             # Gemini sent no audio
    assert not (tmp_path / "Puck.pcm").exists()


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    a.settings.path = tmp_path / "settings.json"   # never this PC's own settings
    restarts = []
    monkeypatch.setattr(a.session, "restart_fresh", lambda notice=None, reason="": restarts.append((notice, reason)))
    a.restarts = restarts
    return a


def test_a_new_voice_applies_when_jarvis_is_free_and_is_kept(assistant) -> None:
    a = assistant
    a.config["voice"]["name"] = "Charon"
    a.settings.started["voice.name"] = "Charon"
    assert "Switching to Kore" in a.set_reply_voice("kore")
    assert a.config["voice"]["name"] == "Kore"
    assert json.loads(a.settings.path.read_text(encoding="utf-8"))["voice.name"] == "Kore"   # for the next start
    assert "voice.name" not in a.settings.pending_restart()                                 # no restart needed
    a._turn_open = True                    # still finishing this reply: wait
    a._apply_voice_change()
    assert a.restarts == []
    a._turn_open = False
    a._apply_voice_change()
    (notice, reason), = a.restarts
    assert "now Kore" in notice and "Kore" in reason
    # and the next connection asks Gemini for the new voice
    config = a.session._config_factory(None)
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert "already using Kore" in a.set_reply_voice("Kore")
    assert "isn't one of my voices" in a.set_reply_voice("Siri") and a.config["voice"]["name"] == "Kore"


def test_the_settings_page_changes_the_voice_at_once(assistant) -> None:
    from core.runtime import WebBridge

    a = assistant
    bridge = WebBridge(a)
    out = bridge.change_setting("voice.name", "Puck")
    assert out["ok"] and out["message"].startswith("Now using Puck") and "goodbye" not in out["message"]
    item = next(i for c in out["settings"]["categories"] for i in c["items"] if i["key"] == "voice.name")
    assert item["value"] == "Puck" and item["restart"] is False and item["applies"] == "now"
    assert item["labels"]["Puck"] == "Upbeat"
    assert not bridge.change_setting("voice.name", "Siri")["ok"]


async def test_preview_plays_the_sample_and_changes_nothing(assistant, tmp_path) -> None:
    from core.runtime import WebBridge

    a = assistant
    played = []
    a.player.enqueue = played.append
    a.previews = VoicePreviews(fl.FakeClient([[fl.audio(b"\x01\x00" * 24_000), fl.turn_complete()]]), "m", tmp_path)
    voice_before = a.config["voice"]["name"]
    out = await WebBridge(a).preview_voice("Leda")
    assert out == {"ok": True, "message": "Played a 1-second sample of Leda."}
    assert played == [b"\x01\x00" * 24_000] and a.config["voice"]["name"] == voice_before and a.restarts == []
    assert "isn't one of my voices" in await a.preview_voice("Siri")
    a.previews = None
    assert "need the connection" in await a.preview_voice("Leda")


def test_voice_tools_are_available_to_gemini(assistant) -> None:
    for name in ("set_reply_voice", "preview_voice"):
        tool = assistant.registry.get(name)
        assert tool is not None and tool.read_only


def test_the_page_can_ask_for_a_preview() -> None:
    from fastapi.testclient import TestClient

    from tests.test_web_ui import PORT, TOKEN, FakeBridge, connect
    from ui.server import create_app

    bridge = FakeBridge()
    asked = []

    async def preview_voice(name):
        asked.append(name)
        return {"ok": True, "message": f"Played a 3-second sample of {name}."}

    bridge.preview_voice = preview_voice
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"type": "preview_voice", "name": "Kore", "id": 7}))
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg.get("kind") == "reply":
                break
    assert asked == ["Kore"] and msg["id"] == 7 and msg["data"]["ok"]
