"""Memory plan step 5: ask_brain gets read-only recall, a matched question brings its reply, and the
recap after a dropped Live session reads the saved conversation."""

from types import SimpleNamespace

import pytest

from actions import Services, brain as brain_mod
from core.conversation_log import ConversationLog
from core.memory_db import MemoryDB
from core.router import Router
from core.runtime import Assistant, Transcript
from core.untrusted import unwrap
from tests.fakes import fake_live as fl
from tests.test_brain import FakeGroq, FakeHub, make_tools
from tests.test_catalog import all_tools_registry

# ---------------------------------------------------------------- a matched question brings its reply


@pytest.fixture
def log(tmp_path):
    return ConversationLog(MemoryDB(tmp_path / "memory.sqlite"))


def test_a_matched_question_brings_the_reply_after_it(log) -> None:
    log.add("user", "Should I learn Rust or Go for backend work?")
    log.add("assistant", "Go first: simpler, and quicker to ship a backend with.")
    log.add("user", "Play some Queen")
    log.add("assistant", "Playing Queen.")
    found = [r["text"] for r in log.search("rust")]
    assert found == ["Should I learn Rust or Go for backend work?",
                     "Go first: simpler, and quicker to ship a backend with."]
    assert [r["text"] for r in log.search("go backend")].count("Go first: simpler, and quicker to ship a backend "
                                                             "with.") == 1  # matched itself: not twice


def test_a_reply_is_never_taken_from_another_conversation(log) -> None:
    log.add("user", "Should I learn Rust?")
    log.new_conversation()
    log.add("assistant", "Good morning.")
    assert [r["text"] for r in log.search("rust")] == ["Should I learn Rust?"]


def test_current_lines_are_the_conversation_going_on_now(log) -> None:
    assert log.current_lines(16) == []
    for i in range(20):
        log.add("user" if i % 2 == 0 else "assistant", f"line {i}")
    lines = log.current_lines(16)
    assert len(lines) == 16 and lines[0] == ("User", "line 4") and lines[-1] == ("JARVIS", "line 19")
    log.new_conversation()
    assert log.current_lines(16) == []


# ---------------------------------------------------------------- the drop recap reads the saved conversation


def _assistant(log, recent):
    transcript = Transcript("Sir")
    transcript.recent.extend(recent)
    return SimpleNamespace(transcript=transcript, services=SimpleNamespace(conversations=log))


def test_drop_recap_uses_the_saved_conversation_when_it_has_more(log) -> None:
    for i in range(12):
        log.add("user" if i % 2 == 0 else "assistant", f"line {i}")
    recent = [("Sir" if i % 2 == 0 else "JARVIS", f"line {i}") for i in range(4, 12)] + [("Sir", "just said")]
    recap = Assistant._drop_recap(_assistant(log, recent))
    body = unwrap(recap)
    assert "User: line 0" in body and "JARVIS: line 11" in body  # 12 saved lines, not the transcript's 8
    assert body.rstrip().endswith("Sir: just said")  # flushed but not saved yet: still carried


def test_drop_recap_keeps_the_transcript_while_logging_is_paused(log) -> None:
    for i in range(12):
        log.add("user", f"old {i}")
    log.pause()
    recent = [("Sir", "after the pause")]
    assert "old 0" not in unwrap(Assistant._drop_recap(_assistant(log, recent)))


def test_drop_recap_without_a_log_is_the_transcript(log) -> None:
    fake = SimpleNamespace(transcript=Transcript("Sir"), services=SimpleNamespace())
    fake.transcript.recent.append(("Sir", "hello"))
    assert "Sir: hello" in unwrap(Assistant._drop_recap(fake))


# ---------------------------------------------------------------- ask_brain gets read-only recall


@pytest.fixture
def setup(tmp_path, monkeypatch):
    record: list = []
    services = Services()
    services.data_dir = tmp_path / "data"
    services.notify = lambda text: None
    services.taint = lambda: None
    fake = FakeGroq()
    monkeypatch.setattr(brain_mod, "Groq", lambda config: fake)
    h = fl.SafetyHarness(tmp_path, config={"features": {"brain": True, "memory": True}}, services=services,
                         extra_tools=make_tools(record))
    services.dispatcher = h.dispatcher
    services.hub = FakeHub(all_tools_registry(tmp_path / "full"))
    services.brain.log_path = tmp_path / "brain.jsonl"
    services.brain._router = Router(h.registry, hub=services.hub, log_path=tmp_path / "routing.jsonl")
    return h, services, fake


async def test_ask_brain_can_recall_what_the_user_said(setup) -> None:
    h, services, fake = setup
    services.memory.add("I want a fully remote job")
    fake.script.extend([[("jarvis__recall", {"topic": "what kind of job I want"})],
                        "Since you want a fully remote role, the second offer fits better."])
    resp = await h.model_calls("ask_brain", request="I have two offers, one hybrid and one remote, which suits me")
    assert resp["status"] == "ok" and "remote" in unwrap(resp["result"])
    assert "jarvis__recall" in [t["function"]["name"] for t in fake.sent[0]["tools"]]
    assert "I want a fully remote job" in fake.sent[1]["messages"][-1]["content"]


async def test_ask_brain_never_gets_remember_or_forget_this_way(setup) -> None:
    h, services, fake = setup
    fake.script.append("Fine.")
    await h.model_calls("ask_brain", request="explain compound interest simply")
    names = [t["function"]["name"] for t in fake.sent[0]["tools"]]
    assert "jarvis__recall" in names
    assert not {"jarvis__remember", "jarvis__forget", "jarvis__forget_conversation"} & set(names)


async def test_brain_memory_can_be_turned_off(setup) -> None:
    h, services, fake = setup
    services.brain.memory = False
    fake.script.append("Fine.")
    await h.model_calls("ask_brain", request="explain compound interest simply")
    assert "jarvis__recall" not in [t["function"]["name"] for t in fake.sent[0]["tools"]]
