"""Keeping the user in the loop while JARVIS works (seen live 26-27 Oct: up to 107 s of silence): its own
spoken progress lines, "what are you doing?", notices held until it's free, and long chat-app waits moved
to the background."""

from __future__ import annotations

import asyncio
import time

from actions import Services
from actions.desktop import Desktop
from core import voice_lines
from core.voice_lines import LINES, VoiceLines, doing_key, progress_key
from tests.test_desktop import CONFIG, screen, win  # noqa: F401  (fixture)
from tests.test_web_ui import assistant  # noqa: F401  (fixture)

# ---- the lines


def test_progress_lines_follow_what_is_running() -> None:
    assert progress_key("look_at_screen", "tool", 0, False) == "screen"
    assert progress_key("look_at_screen", "tool", 1, False) == "still"         # not the same words every time
    assert progress_key("look_at_screen", "tool", 2, False) == "screen"
    assert progress_key("wait_for_app", "tool", 0, False) == "reply"
    assert progress_key("", "thinking", 0, False) == "moment" and progress_key("", "thinking", 1, False) == "still"
    assert progress_key("search_web", "tool", 0, True) == "backup"              # a slow model: say why
    assert doing_key("look_at_screen", "tool") == "doing_screen" and doing_key("", "thinking") == "doing_thinking"
    assert doing_key("open_app", "tool") == "doing_work" and doing_key("", "") == "doing_nothing"
    for tool in voice_lines.TOPIC:                                             # every line a key can name exists
        for said in range(3):
            assert progress_key(tool, "tool", said, False) in LINES
        assert doing_key(tool, "tool") in LINES


class FakePreviews:
    def __init__(self, fail_after: int = 99) -> None:
        self._lock = asyncio.Lock()
        self.read: list[tuple[str, str]] = []
        self.fail_after = fail_after

    async def _generate(self, voice, text):
        if len(self.read) >= self.fail_after:
            raise RuntimeError("quota")
        self.read.append((voice, text))
        return text.encode()


async def test_lines_are_recorded_once_per_voice_and_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(voice_lines, "GAP_S", 0)
    previews = FakePreviews(fail_after=3)
    lines = VoiceLines(previews, tmp_path)
    lines.prepare("Charon")
    await lines._task
    assert len(previews.read) == 3 and len(lines.missing("Charon")) == len(LINES) - 3   # stopped at the failure
    previews.fail_after = 999
    lines.prepare("Charon")
    await lines._task
    assert lines.missing("Charon") == [] and lines.get("Charon", "screen") == LINES["screen"].encode()
    assert {text for _, text in previews.read} == set(LINES.values())          # only the fixed sentences
    count = len(previews.read)
    lines.prepare("Charon")                                                    # nothing left: no session
    assert len(previews.read) == count and lines.get("Kore", "screen") is None and lines.get("Charon", "nope") is None


# ---- the runtime


def lines_for(a, played):
    class Lines:
        def get(self, voice, key):
            return key.encode()

        def prepare(self, voice, delay_s=0.0):
            pass

    a.voice_lines = Lines()
    a.voice = True
    a.player.enqueue = lambda pcm: played.append(pcm.decode())


def quiet_for(a, seconds: float) -> None:
    then = time.monotonic() - seconds
    a._progress.update(since=then, quiet_from=then)
    a._last_heard = then


def test_jarvis_says_how_it_is_getting_on_while_a_step_runs(assistant) -> None:  # noqa: F811
    a = assistant
    played: list[str] = []
    lines_for(a, played)
    a.activity.set("Looking at your screen…", "tool", "look_at_screen")
    a._speak_progress()
    assert played == []                                                         # not straight away
    quiet_for(a, 8)
    a._speak_progress()
    assert played == ["screen"]
    a._speak_progress()
    assert played == ["screen"]                                                 # not again at once
    quiet_for(a, 13)
    a.services.llm = type("L", (), {"last_fallback_at": time.monotonic()})()   # a model was slow: say why
    a._speak_progress()
    assert played == ["screen", "backup"]
    for _ in range(5):
        quiet_for(a, 13)
        a._speak_progress()
    assert len(played) == a.progress_max                                        # a few lines, not a stream
    a.activity.clear()
    a._speak_progress()
    assert a._progress["said"] == 0                                             # the next request starts afresh


def test_no_progress_lines_when_they_would_be_wrong(assistant, monkeypatch) -> None:  # noqa: F811
    a = assistant
    played: list[str] = []
    lines_for(a, played)
    a.activity.set("Looking at your screen…", "tool", "look_at_screen")
    for setup in (lambda: setattr(a, "voice", False), lambda: setattr(a.gate, "muted", True),
                  lambda: setattr(a, "progress_on", False), lambda: setattr(a, "_quiet_reply", True)):
        a.voice, a.progress_on, a._quiet_reply = True, True, False
        a.gate.muted = False
        setup()
        quiet_for(a, 20)
        a._speak_progress()
    assert played == []
    a.voice, a.progress_on, a._quiet_reply = True, True, False
    a.gate.muted = False
    monkeypatch.setattr(type(a.dispatcher), "awaiting_confirmation", property(lambda self: True))   # waiting for a yes
    quiet_for(a, 20)
    a._speak_progress()
    assert played == []


def test_what_are_you_doing_is_answered_at_once(assistant) -> None:  # noqa: F811
    a = assistant
    played: list[str] = []
    lines_for(a, played)
    statuses: list[str] = []
    a._status = statuses.append
    a.on_input_transcript("what are you doing")                                 # nothing running: Gemini answers
    assert played == []
    a.activity.set("Waiting for Claude to reply…", "tool", "wait_for_app")
    a.on_input_transcript(" Jarvis, what's taking so long?")
    assert played == ["doing_reply"] and "Waiting for Claude to reply" in statuses[-1] and "s so far" in statuses[-1]
    a.on_input_transcript(" ")
    assert played == ["doing_reply"]                                            # once per question


def test_notices_wait_until_jarvis_is_free(assistant) -> None:  # noqa: F811
    a = assistant
    sent: list[str] = []
    a._notify = sent.append
    a._tell_when_free("[system notice] Claude has finished.", True)
    a.dispatcher.tainted = False
    a._last_tool_at = time.monotonic()                                          # busy answering
    a._deliver_later()
    assert sent == []
    a._last_tool_at = 0.0
    a._deliver_later()
    assert sent == ["[system notice] Claude has finished."] and a.dispatcher.tainted   # app text: never acted on


# ---- long waits for a chat app's reply


def test_a_long_wait_for_claude_moves_to_the_background(screen, monkeypatch) -> None:  # noqa: F811
    told: list[tuple[str, bool]] = []
    services = Services()
    services.tell_when_free = lambda text, untrusted: told.append((text, untrusted))
    d = Desktop(CONFIG, services)
    d.chat_profiles = {"claude.exe": object()}
    d.foreground_wait_s, d.background_wait_s = 12, 300
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    asked: list[int] = []

    async def wait_for_reply(w, timeout, out=None):
        asked.append(timeout)
        if len(asked) == 1:
            out["done"] = False
            return "Claude was still writing after 12 seconds. So far: Searched past chats"
        await asyncio.sleep(0.05)
        out["done"] = True
        return "Claude replied after 40 seconds:\nHere's your resume."

    d._wait_for_reply = wait_for_reply

    async def scenario():
        first = await d.wait_for_app("claude")
        again = await d.wait_for_app("claude")                                  # asked again while it watches
        await d.watching[7]
        return first, again

    first, again = asyncio.run(scenario())
    assert asked == [12, 300]
    assert "still writing after 12 seconds" in first and "background" in first and "Don't" not in first
    assert "already watching" in again
    (notice, untrusted), = told
    assert untrusted and "has finished the reply" in notice and "Here's your resume." in notice
    assert "<untrusted_content" in notice and not d.watching


def test_a_quick_reply_is_returned_as_before(screen) -> None:  # noqa: F811
    services = Services()
    services.tell_when_free = lambda text, untrusted: None
    d = Desktop(CONFIG, services)
    d.chat_profiles = {"claude.exe": object()}
    screen["windows"] = [win("claude.exe", "Claude", 7)]

    async def wait_for_reply(w, timeout, out=None):
        out["done"] = True
        return "Claude replied after 3 seconds:\nDone."

    d._wait_for_reply = wait_for_reply
    assert asyncio.run(d.wait_for_app("claude")) == "Claude replied after 3 seconds:\nDone." and not d.watching
    d.services.tell_when_free = None                                            # nothing to tell later: waits it out
    seen: list[int] = []

    async def slow(w, timeout, out=None):
        seen.append(timeout)
        out["done"] = False
        return "Claude was still writing after 60 seconds."

    d._wait_for_reply = slow
    assert "still writing" in asyncio.run(d.wait_for_app("claude")) and seen == [60]
