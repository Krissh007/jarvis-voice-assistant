"""Conversation maturity: JARVIS waits for the end of a sentence and says what really happened."""

from __future__ import annotations

import pytest

from core.utterance import unfinished


@pytest.mark.parametrize("text, still_talking", [
    ("I'm very hungry. So", True), ("and the concept is", True), ("Okay, just write a prompt in Claude. Saying", True),
    ("which are like really irrelevant to", True), ("the", True), ("A", True), ("Ahm", True),
    ("I'm very hungry.", False), ("Open Notepad.", False), ("Yes.", False), ("browser task", False), ("", False),
])
def test_unfinished_sentences(text, still_talking) -> None:
    assert unfinished(text) is still_talking


@pytest.fixture
def assistant(tmp_path):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    a.player.enqueue = lambda pcm: a.played.append(pcm)
    a.played = []
    return a


async def test_half_a_sentence_gets_no_answer_and_no_action(assistant) -> None:
    a = assistant
    a.on_input_transcript("Okay, just write a prompt in Claude. Saying")
    out = await a.on_tool_call("get_datetime", {}, "1")   # the model tries to act on half a request
    assert out["status"] == "waiting" and "hasn't finished" in out["message"]
    a.on_output_transcript("I'm listening, please go on.")
    a.on_audio(b"\x01\x00" * 10)
    assert a.played == [] and "listening" not in " ".join(line for _, line in a.transcript.recent)
    a.on_turn_complete()
    a.on_input_transcript(" ask Claude why irrelevant jobs appear.")
    assert a.dispatcher.hold == "" and a.transcript.last_user().startswith("Okay, just write a prompt")
    a.on_audio(b"\x02\x00" * 10)
    assert a.played == [b"\x02\x00" * 10]                 # the whole request gets its answer


def test_a_failed_agent_request_asks_for_the_real_reason() -> None:
    from actions.agent_hub import HubBridge

    notice = HubBridge._notice({"status": "failed", "say": "No safe plan."})
    assert "never a vague 'unable to safely'" in notice and "click_in_app" in notice
    assert "never a vague" not in HubBridge._notice({"status": "done", "say": "3 jobs."})


def test_persona_rules_for_maturity() -> None:
    from core.prompts import PERSONA

    for rule in ("the user is still talking: don't answer", "Only say something is done",
                 "never a vague", "dictates a list", "what Claude actually said", "keep one draft"):
        assert rule in PERSONA
