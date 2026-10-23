"""The status line: what JARVIS is doing right now, so a pause never looks like nothing is happening."""

from __future__ import annotations

import time

import pytest

from core.activity import (
    BACKGROUND_AGENTS, BACKGROUND_TASK, COMPOSING, SLOW_NOTE, THINKING, Activity, describe,
)


def test_each_tool_gets_a_few_words_from_jarvis_not_from_results() -> None:
    assert describe("search_web", {"query": "x"}) == "Searching the web…"
    assert describe("look_at_screen", {}) == "Looking at your screen…"
    assert describe("read_app", {"app": "Claude"}) == "Reading Claude…"
    assert describe("browser_open", {"url": "https://www.figma.com/files"}) == "Opening figma.com…"
    assert describe("ask_brain", {"request": "should I learn Rust"}) == "Thinking it through…"
    assert describe("some_new_tool", {}) == "Working on it…"
    assert describe("get_datetime", {}) == "" and describe("go_to_sleep", {}) == ""     # too quick to show
    assert describe("read_app", {"app": "x" * 200}) == "Reading " + "x" * 30 + "…"     # names are cut short


def test_the_line_publishes_changes_marks_long_steps_and_shows_background_work() -> None:
    now = [100.0]
    sent = []
    a = Activity(lambda **d: sent.append(d), clock=lambda: now[0])
    a.set("Searching the web…", "tool")
    a.set("Searching the web…", "tool")                  # no change: nothing sent
    assert sent == [{"text": "Searching the web…", "what": "tool"}]
    now[0] += 9
    a.tick()
    assert a.text == "Searching the web…" + SLOW_NOTE and len(sent) == 2
    a.tick()
    assert len(sent) == 2                                 # the note is added once
    a.clear()
    a.tick(BACKGROUND_AGENTS)
    assert a.text == BACKGROUND_AGENTS and a.kind == "background"
    a.set("Reading the page…", "tool")                    # foreground work wins
    a.tick(BACKGROUND_AGENTS)
    assert a.text == "Reading the page…"
    a.clear()
    a.tick("")
    assert a.text == "" and sent[-1] == {"text": "", "what": ""}


@pytest.fixture
def assistant(tmp_path):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    a.seen = []
    a.bus.subscribe(lambda e: a.seen.append(e) if e.kind == "activity" else None)
    return a


def lines(a) -> list[str]:
    a.bus.flush() if hasattr(a.bus, "flush") else None
    return [e.data["text"] for e in a.seen]


def test_tools_set_the_line_and_the_answer_clears_it(assistant) -> None:
    a = assistant
    a.dispatcher.events("tool_started", tool="look_at_screen", args={}, step=False)
    assert a.activity.text == "Looking at your screen…"
    a.dispatcher.events("tool_finished", tool="look_at_screen", step=False, paused=False, ok=True, result="a desk")
    assert a.activity.text == COMPOSING                    # the model is still writing its answer
    a.on_output_transcript("You have VS Code open.")
    assert a.activity.text == ""
    a.dispatcher.events("tool_started", tool="browser_open", args={"url": "figma.com"}, step=False)
    a.dispatcher.events("tool_finished", tool="browser_open", step=False, paused=True, ok=True, result="Open figma.com?")
    assert a.activity.text == ""                           # waiting for the user's yes: nothing is running


def test_thinking_shows_while_gemini_hasnt_answered(assistant) -> None:
    a = assistant
    a.transcript.add("user", "what's the capital of France")
    a._last_heard = time.monotonic() - 1.5
    a._update_activity()
    assert a.activity.text == THINKING
    a.on_audio(b"\x01\x00" * 10)                           # the answer starts
    assert a.activity.text == ""
    a.transcript.flush()
    a.transcript.add("user", "hm")                         # a sound, not a request
    a._last_heard = time.monotonic() - 1.5
    a._update_activity()
    assert a.activity.text == ""


async def test_a_typed_message_shows_thinking_at_once(assistant) -> None:
    a = assistant
    a.session.send_text = lambda text: None
    await a._on_typed("summarise my day")
    assert a.activity.text == THINKING


def test_background_agents_and_tasks_show_when_nothing_else_does(assistant) -> None:
    a = assistant
    a.dispatcher.events("tool_started", tool="agent_hub_request", args={}, step=False)
    a.dispatcher.events("tool_finished", tool="agent_hub_request", step=False, paused=False, ok=True,
                        result="Discovery started. I'll tell you when it finishes.")
    a.on_turn_complete()
    a._update_activity()
    assert a.activity.text == BACKGROUND_AGENTS
    from core.bus import Event

    a._watch_bus(Event("agent_update", {"status": "done"}))   # it reported back
    a._update_activity()
    assert a.activity.text == ""
    a.tasks.task = object()
    a._update_activity()
    assert a.activity.text == BACKGROUND_TASK
    a.tasks.task = None


def test_the_page_gets_the_line_in_its_snapshot(assistant) -> None:
    from core.runtime import WebBridge

    a = assistant
    a.activity.set("Reading the page…", "tool")
    assert WebBridge(a).snapshot()["activity"] == "Reading the page…"
