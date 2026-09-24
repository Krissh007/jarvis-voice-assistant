"""Fake Live session: scripted server messages + recorded client sends, no network.

Usage:
    client = FakeClient([[msg1, msg2, go_away()], [msg3]])   # one script per connection
    session = LiveSession(client, "model", factory, events)
Each `connect()` consumes the next script. When a script runs out the fake
blocks (like an idle socket) until the test cancels it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from google.genai import types


# ---- message builders ----

def audio(data: bytes = b"\x01\x00" * 10) -> types.LiveServerMessage:
    part = types.Part(inline_data=types.Blob(data=data, mime_type="audio/pcm;rate=24000"))
    return types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(parts=[part])))


def input_transcript(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(input_transcription=types.Transcription(text=text))
    )


def output_transcript(text: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(output_transcription=types.Transcription(text=text))
    )


def interrupted() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))


def turn_complete() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def resumption(handle: str) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle=handle, resumable=True)
    )


def go_away() -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s"))


# ---- fake SDK objects ----

class FakeSession:
    def __init__(self, script: list[types.LiveServerMessage]) -> None:
        self._script = list(script)
        self.sent: list[dict[str, Any]] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def send_tool_response(self, *, function_responses: Any) -> None:
        self.sent.append({"tool_response": function_responses})

    async def send_client_content(self, *, turns: Any = None, turn_complete: bool = True) -> None:
        self.sent.append({"client_content": turns, "turn_complete": turn_complete})

    async def receive(self):
        """Mirrors the SDK: yields until the end of one turn, then stops."""
        if not self._script:
            await asyncio.Event().wait()  # idle connection
        while self._script:
            msg = self._script.pop(0)
            yield msg
            if msg.server_content and msg.server_content.turn_complete:
                return
            if msg.go_away:
                return


class FakeClient:
    """Stands in for `genai.Client`; `client.aio.live.connect(...)` returns scripted sessions."""

    def __init__(
        self, scripts: list[list[types.LiveServerMessage]], fail_first: int = 0, fail_after: int | None = None
    ) -> None:
        self._scripts = list(scripts)
        self._fail_first = fail_first
        self._fail_after = fail_after  # connections after this many successful ones all fail
        self.connects: list[dict[str, Any]] = []
        self.sessions: list[FakeSession] = []
        self.aio = self
        self.live = self

    @asynccontextmanager
    async def connect(self, *, model: str, config: types.LiveConnectConfig):
        self.connects.append({"model": model, "config": config})
        if self._fail_first > 0:
            self._fail_first -= 1
            raise ConnectionError("fake connection failure")
        if self._fail_after is not None and len(self.sessions) >= self._fail_after:
            raise ConnectionError("fake connection failure")
        session = FakeSession(self._scripts.pop(0) if self._scripts else [])
        self.sessions.append(session)
        yield session


class RecordingEvents:
    """SessionEvents that records every callback."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def on_audio(self, pcm: bytes) -> None:
        self.calls.append(("audio", pcm))

    def on_interrupted(self) -> None:
        self.calls.append(("interrupted", None))

    def on_input_transcript(self, text: str) -> None:
        self.calls.append(("input", text))

    def on_output_transcript(self, text: str) -> None:
        self.calls.append(("output", text))

    def on_turn_complete(self) -> None:
        self.calls.append(("turn_complete", None))

    def on_status(self, message: str, *, problem: bool = False) -> None:
        self.calls.append(("status", message))

    async def on_tool_call(self, name: str, args: dict[str, Any], call_id: str | None) -> dict[str, Any]:
        self.calls.append(("tool_call", (name, args, call_id)))
        return {"status": "ok", "result": f"ran {name}"}

    def on_tool_cancellation(self, ids: list[str]) -> None:
        self.calls.append(("tool_cancellation", ids))


def tool_call(name: str, call_id: str = "call-1", **args: Any) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id=call_id, name=name, args=args)])
    )


# ---- safety harness: dispatcher + confirmation + echo gating on a fake clock ----

class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class SafetyHarness:
    """Drives the real Dispatcher with scripted model calls, transcripts and playback events."""

    ACCEPT = ["confirm", "yes", "yes do it", "go ahead"]

    def __init__(self, log_dir, dry_run: bool = False, extra_tools=(), config: dict | None = None) -> None:
        from actions import load_actions
        from actions import testing
        from core.audio import PlaybackState
        from core.confirm import Confirmer, UtteranceTracker
        from core.dispatcher import AuditLog, Dispatcher
        from core.registry import Registry

        testing.NOTES.clear()
        self.notes = testing.NOTES
        self.clock = FakeClock()
        self.playback = PlaybackState(tail_s=0.4, clock=self.clock)
        config = config or {}
        config.setdefault("features", {}).update({"system": True, "test_tools": True})
        self.registry = load_actions(Registry(), config)
        for tool in extra_tools:
            self.registry.register(tool)
        self.notices: list[str] = []
        self.audit_path = log_dir / "actions.jsonl"
        self.dispatcher = Dispatcher(
            self.registry,
            Confirmer(self.playback, timeout_s=20, clock=self.clock),
            UtteranceTracker(self.playback, idle_s=1.0, clock=self.clock),
            AuditLog(self.audit_path),
            notify=self.notices.append,
            accept_phrases=self.ACCEPT,
            dry_run=dry_run,
        )

    async def model_calls(self, tool: str, /, **args: Any) -> dict:
        return await self.dispatcher.handle_call(tool, args, call_id=f"id-{tool}")

    async def advance(self, seconds: float) -> None:
        self.clock.t += seconds
        await self.dispatcher.tick()

    async def speak_summary(self, seconds: float = 3.0) -> None:
        """The model reads the summary aloud, finishes its turn, and playback + echo tail end."""
        self.playback.set_playing(True)
        self.clock.t += seconds
        self.playback.set_playing(False)
        self.dispatcher.on_turn_complete()
        await self.advance(0.5)

    async def user_says(self, text: str) -> None:
        """Mic transcript fragment(s), then the user goes quiet."""
        self.dispatcher.on_input_fragment(text)
        await self.advance(1.1)

    @property
    def pending(self):
        return self.dispatcher.confirmer.pending

    def audit(self) -> list[dict]:
        import json

        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
