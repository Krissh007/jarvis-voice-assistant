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

    def __init__(self, scripts: list[list[types.LiveServerMessage]], fail_first: int = 0) -> None:
        self._scripts = list(scripts)
        self._fail_first = fail_first
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
