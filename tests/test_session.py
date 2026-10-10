"""LiveSession against the fake Live API: events, resumption, GoAway, reconnect policy."""

import asyncio

import pytest

from core.session import LiveSession, ReconnectPolicy, build_live_config
from tests.fakes import fake_live as fl


def make_session(client, events=None, policy=None) -> LiveSession:
    return LiveSession(
        client,
        "gemini-test-live",
        lambda handle: build_live_config(
            system_instruction="test", voice="Charon", push_to_talk=False, resume_handle=handle
        ),
        events or fl.RecordingEvents(),
        policy=policy,
    )


async def run_briefly(session: LiveSession, seconds: float = 0.2) -> None:
    task = asyncio.create_task(session.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_config_enables_transcription_compression_and_resumption() -> None:
    cfg = build_live_config(system_instruction="x", voice="Charon", push_to_talk=False, resume_handle="h1")
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None
    assert cfg.context_window_compression.sliding_window is not None
    assert cfg.session_resumption.handle == "h1"
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is False
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"


def test_push_to_talk_disables_server_vad() -> None:
    cfg = build_live_config(system_instruction="x", voice=None, push_to_talk=True, resume_handle=None,
                            end_pause_ms=1000)
    assert cfg.realtime_input_config.automatic_activity_detection.disabled is True
    assert cfg.speech_config is None


async def test_events_are_dispatched_in_order() -> None:
    events = fl.RecordingEvents()
    client = fl.FakeClient([[
        fl.input_transcript("hello"),
        fl.audio(b"\x02\x00"),
        fl.output_transcript("Good evening."),
        fl.interrupted(),
        fl.turn_complete(),
    ]])
    await run_briefly(make_session(client, events))
    assert [c[0] for c in events.calls] == ["input", "audio", "output", "interrupted", "turn_complete"]
    assert events.calls[1] == ("audio", b"\x02\x00")


async def test_go_away_reconnects_with_latest_resumption_handle() -> None:
    client = fl.FakeClient([
        [fl.resumption("h1"), fl.resumption("h2"), fl.go_away()],
        [fl.turn_complete()],
    ])
    session = make_session(client)
    await run_briefly(session)
    assert len(client.connects) == 2
    assert client.connects[0]["config"].session_resumption.handle is None
    assert client.connects[1]["config"].session_resumption.handle == "h2"


async def test_outgoing_audio_text_and_activity_reach_the_session() -> None:
    client = fl.FakeClient([[]])
    session = make_session(client)
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(session.connected.wait(), 1)
    session.send_audio(b"\x00\x00")
    session.send_text("hi")
    session.activity_start()
    session.activity_end()
    await asyncio.sleep(0.05)
    task.cancel()
    sent = client.sessions[0].sent
    assert sent[0]["audio"].mime_type == "audio/pcm;rate=16000"
    assert sent[1] == {"text": "hi"}
    assert "activity_start" in sent[2] and "activity_end" in sent[3]


async def test_audio_is_dropped_while_disconnected() -> None:
    session = make_session(fl.FakeClient([]))
    session.send_audio(b"\x00\x00")
    assert session._outgoing.qsize() == 0


async def test_failures_retry_then_connect(monkeypatch) -> None:
    monkeypatch.setattr("core.session.random.uniform", lambda a, b: 0)
    policy = ReconnectPolicy()
    monkeypatch.setattr(policy, "record_failure", lambda: (0.01, False))
    client = fl.FakeClient([[fl.turn_complete()]], fail_first=2)
    await run_briefly(make_session(client, policy=policy))
    assert len(client.connects) == 3


def test_reconnect_policy_backs_off_after_three_failures_in_five_minutes() -> None:
    now = [0.0]
    policy = ReconnectPolicy(clock=lambda: now[0])
    d1, n1 = policy.record_failure()
    now[0] = 10
    d2, n2 = policy.record_failure()
    assert not n1 and not n2 and d2 > d1
    now[0] = 20
    delay, notice = policy.record_failure()
    assert notice and delay == 60


def test_reconnect_policy_forgets_old_failures() -> None:
    now = [0.0]
    policy = ReconnectPolicy(clock=lambda: now[0])
    policy.record_failure()
    policy.record_failure()
    now[0] = 400  # outside the 5-minute window
    _, notice = policy.record_failure()
    assert not notice


async def test_repeated_failures_tell_the_user() -> None:
    events = fl.RecordingEvents()
    policy = ReconnectPolicy(long_backoff_s=0.01, base_delay_s=0.001)
    client = fl.FakeClient([], fail_first=3)
    await run_briefly(make_session(client, events, policy), 0.3)
    assert any(kind == "status" and "backing off" in msg for kind, msg in events.calls)


def test_config_passes_transcription_language_and_vocabulary() -> None:
    cfg = build_live_config(system_instruction="x", voice=None, push_to_talk=False, resume_handle=None,
                            languages=["en-IN"], vocabulary=["Jarvis", "confirm"])
    assert cfg.input_audio_transcription.language_codes == ["en-IN"]
    assert cfg.input_audio_transcription.custom_vocabulary == ["Jarvis", "confirm"]


async def test_bad_resumption_handle_is_dropped_after_two_failures() -> None:
    client = fl.FakeClient([[fl.resumption("h1"), fl.go_away()]], fail_after=1)
    policy = ReconnectPolicy(base_delay_s=0.001, long_backoff_s=0.001)
    await run_briefly(make_session(client, policy=policy), 0.2)
    handles = [c["config"].session_resumption.handle for c in client.connects]
    assert handles[:3] == [None, "h1", "h1"] and handles[3] is None


async def test_a_failing_event_handler_does_not_drop_the_connection() -> None:
    class Grumpy(fl.RecordingEvents):
        def on_output_transcript(self, text: str) -> None:
            raise UnicodeEncodeError("charmap", text, 0, 1, "can't print")

    events = Grumpy()
    client = fl.FakeClient([[fl.output_transcript("नमस्ते"), fl.audio(b"\x01\x00"), fl.turn_complete()]])
    await run_briefly(make_session(client, events))
    assert len(client.connects) == 1  # no reconnect
    assert [c[0] for c in events.calls] == ["audio", "turn_complete"]


def test_a_pause_mid_sentence_does_not_end_the_turn() -> None:
    """Live log: "tell me an efficient way where I can... build auto apply... feature" arrived as three requests."""
    from google.genai import types

    vad = build_live_config(system_instruction="x", voice=None, push_to_talk=False, resume_handle=None,
                            end_pause_ms=1000).realtime_input_config.automatic_activity_detection
    assert vad.silence_duration_ms == 1000 and not vad.disabled
    assert vad.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW
    default = build_live_config(system_instruction="x", voice=None, push_to_talk=False, resume_handle=None)
    assert default.realtime_input_config.automatic_activity_detection.silence_duration_ms is None
