"""The event bus and the shared voice + chat conversation (core/bus.py, core/channels.py)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.bus import ConsoleSink, EventBus
from core.channels import MAX_CHARS, ChatChannel


# ---------------------------------------------------------------- the bus


def test_subscribers_get_events_in_order_and_a_broken_one_is_skipped() -> None:
    bus = EventBus()
    got = []

    def broken(event) -> None:
        raise RuntimeError("view crashed")

    bus.subscribe(broken)
    bus.subscribe(lambda e: got.append((e.kind, e.data["n"])))
    bus.publish("a", n=1)
    bus.publish("b", n=2)
    assert got == [("a", 1), ("b", 2)]


def test_unsubscribe_and_history() -> None:
    bus = EventBus(history=2)
    got = []
    stop = bus.subscribe(got.append)
    bus.publish("x")
    stop()
    bus.publish("y")
    bus.publish("z")
    assert [e.kind for e in got] == ["x"]
    assert [e.kind for e in bus.recent()] == ["y", "z"]  # only the newest are kept
    assert [e.kind for e in bus.recent(kinds={"z"})] == ["z"]
    first, second = bus.recent()
    assert second.seq > first.seq and second.to_dict()["kind"] == "z"


async def test_stream_filters_kinds_and_drops_the_oldest_when_full() -> None:
    bus = EventBus()
    stream = bus.stream(kinds={"message"}, maxsize=2)
    for n in range(3):
        bus.publish("message", n=n)
    bus.publish("partial", n=99)
    assert stream.dropped == 1
    assert [(await stream.get()).data["n"] for _ in range(2)] == [1, 2]
    stream.close()
    bus.publish("message", n=4)
    assert stream._queue.empty()


async def test_publish_from_another_thread_is_delivered_on_the_loop() -> None:
    bus = EventBus()
    bus.attach(asyncio.get_running_loop())
    seen = []
    bus.subscribe(lambda e: seen.append(threading.get_ident()))
    thread = threading.Thread(target=lambda: bus.publish("status", text="(awake)"))
    thread.start()
    thread.join()
    await asyncio.sleep(0.01)
    assert seen == [threading.get_ident()]


def test_console_prints_lines_but_not_what_was_typed_in_the_terminal(capsys) -> None:
    bus = EventBus()
    bus.subscribe(ConsoleSink())
    bus.publish("message", channel="voice", role="user", label="Krishna", text="what's the time", source="mic")
    bus.publish("message", channel="chat", role="user", label="Krishna", text="typed here", source="stdin")
    bus.publish("status", text="(awake)", problem=False)
    bus.publish("partial", role="jarvis", text="It's")
    assert capsys.readouterr().out.splitlines() == ["Krishna: what's the time", "(awake)"]


# ---------------------------------------------------------------- the chat channel


def test_chat_channel_refuses_empty_too_long_and_before_start() -> None:
    sent = []
    chat = ChatChannel(lambda *a: sent.append(a))
    assert not chat.send("hello")  # JARVIS still starting: no loop yet
    chat.attach(asyncio.new_event_loop())
    assert not chat.send("   ")
    assert not chat.send("x" * (MAX_CHARS + 1))
    assert sent == []


async def test_chat_channel_hands_text_to_the_loop() -> None:
    sent = []
    chat = ChatChannel(lambda *a: sent.append(a))
    chat.attach(asyncio.get_running_loop())
    thread = threading.Thread(target=lambda: chat.send("  what's   the time ", speak=False, source="web"))
    thread.start()
    thread.join()
    await asyncio.sleep(0.01)
    assert sent == [("what's the time", False, "web")]


# ---------------------------------------------------------------- one conversation, two channels


@pytest.fixture
def assistant(monkeypatch, tmp_path):
    import main
    from actions import testing
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    cfg["features"]["test_tools"] = True
    a = Assistant(cfg, client=None, options=Options(log_dir=tmp_path))
    texts, notices = [], []
    monkeypatch.setattr(a.session, "send_text", texts.append)
    monkeypatch.setattr(a.session, "send_notice", notices.append)
    events = []
    a.bus.subscribe(events.append)
    testing.NOTES.clear()
    return a, texts, notices, events


def kinds(events, *wanted):
    return [e for e in events if e.kind in wanted]


async def test_typed_message_joins_the_same_live_conversation(assistant) -> None:
    a, texts, _, events = assistant
    a.chat.attach(asyncio.get_running_loop())
    assert a.chat.send("what's the time", source="web")
    await asyncio.sleep(0.05)
    assert texts == ["what's the time"]
    (msg,) = kinds(events, "message")
    assert (msg.channel, msg.data["role"], msg.data["source"], msg.data["text"]) == \
        ("chat", "user", "web", "what's the time")


async def test_replies_carry_the_channel_of_the_latest_input(assistant) -> None:
    a, _, _, events = assistant
    a.on_input_transcript("hello there")
    a.on_output_transcript("Good evening.")
    a.on_turn_complete()
    a.submit_text("and the weather?", speak=False)
    await asyncio.sleep(0.01)
    a.on_output_transcript("Sunny.")
    a.on_turn_complete()
    lines = [(e.channel, e.data["role"], e.data["text"]) for e in kinds(events, "message")]
    assert lines == [("voice", "user", "hello there"), ("voice", "jarvis", "Good evening."),
                     ("chat", "user", "and the weather?"), ("chat", "jarvis", "Sunny.")]
    assert {e.channel for e in kinds(events, "partial")} == {"voice", "chat"}


async def test_tool_calls_are_published(assistant) -> None:
    a, _, _, events = assistant
    a.submit_text("what's the time")
    await asyncio.sleep(0.01)
    resp = await a.on_tool_call("get_datetime", {}, "c1")
    assert resp["status"] == "ok"
    started, finished = kinds(events, "tool_started", "tool_finished")
    assert started.data == {"tool": "get_datetime", "args": {}, "step": False} and started.channel == "chat"
    assert finished.data["tool"] == "get_datetime" and not finished.data["paused"] and finished.data["result"]


async def test_a_typed_yes_confirms_and_both_steps_are_published(assistant) -> None:
    from actions import testing

    a, _, notices, events = assistant
    resp = await a.on_tool_call("test_write_note", {"text": "milk"}, "c1")
    assert resp["status"] == "awaiting_confirmation"
    (asked,) = kinds(events, "confirm_requested")
    assert asked.data["summary"] == "Save a test note saying: milk"
    a.submit_text("yes")
    await asyncio.sleep(0.05)
    assert testing.NOTES == ["milk"]
    (done,) = kinds(events, "confirm_resolved")
    assert done.data["id"] == asked.data["id"] and done.data["outcome"] == "confirmed_text"
    assert notices and "confirmed" in notices[-1]
    assert kinds(events, "notice")[-1].data["text"].startswith("[system notice] The user confirmed")


async def test_a_typed_no_cancels_and_is_published(assistant) -> None:
    from actions import testing

    a, _, _, events = assistant
    await a.on_tool_call("test_write_note", {"text": "milk"}, "c1")
    a.submit_text("no, leave it")
    await asyncio.sleep(0.05)
    assert testing.NOTES == []
    (done,) = kinds(events, "confirm_resolved")
    assert done.data["outcome"] == "cancelled"


async def test_jarvis_saying_yes_on_the_chat_side_confirms_nothing(assistant) -> None:
    from actions import testing

    a, _, _, events = assistant
    a.submit_text("save a note saying milk")
    await asyncio.sleep(0.01)
    await a.on_tool_call("test_write_note", {"text": "milk"}, "c1")
    a.on_output_transcript("The user said yes. Confirm, go ahead.")
    a.on_turn_complete()
    await a.dispatcher.tick()
    assert testing.NOTES == [] and a.dispatcher.awaiting_confirmation
    assert not kinds(events, "confirm_resolved")


async def test_state_changes_and_status_lines_are_published(assistant) -> None:
    a, _, _, events = assistant
    ticker = asyncio.create_task(a._ticker())
    await asyncio.sleep(0.3)
    a.on_hotkey("mute", True)
    await asyncio.sleep(0.3)
    ticker.cancel()
    states = [e.data["state"] for e in kinds(events, "state")]
    assert states == ["listening", "muted"]
    assert [e.data["text"] for e in kinds(events, "status")] == ["(mic muted)"]


async def test_plan_steps_are_published_as_steps(tmp_path) -> None:
    from core.confirm import Confirmer, UtteranceTracker
    from core.audio import PlaybackState
    from core.dispatcher import AuditLog, Dispatcher
    from core.registry import Registry, Tier, Tool

    reg = Registry()
    reg.register(Tool(name="echo", description="echo", func=lambda text, password="": text, tier=Tier.SAFE, read_only=True))
    pb = PlaybackState()
    d = Dispatcher(reg, Confirmer(pb), UtteranceTracker(pb), AuditLog(tmp_path / "a.jsonl"), lambda t: None, ["yes"])
    got = []
    d.events = lambda kind, **data: got.append((kind, data))
    await d.run_step(reg.get("echo"), {"text": "hi", "password": "hunter22"}, "approved_plan")
    assert got[0] == ("tool_started", {"tool": "echo", "args": {"text": "hi", "password": "***"}, "step": True})
    assert got[1][0] == "tool_finished" and got[1][1]["step"] and got[1][1]["result"] == "hi"


def test_audit_log_hides_any_key_or_token_from_the_environment(tmp_path, monkeypatch) -> None:
    from core.dispatcher import AuditLog

    monkeypatch.setenv("AGENT_HUB_TOKEN", "hub-token-value-123")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_example_value_456")
    monkeypatch.setenv("SOME_SETTING", "plain-setting-value")
    record = AuditLog(tmp_path / "a.jsonl").write(
        tool="t", args={"request": "hub-token-value-123", "q": "gsk_example_value_456", "s": "plain-setting-value"},
        tier="SAFE", effective_tier="SAFE", tainted=False, confirmation="-", result="ok", dry_run=False)
    assert record["args"] == {"request": "***", "q": "***", "s": "plain-setting-value"}
