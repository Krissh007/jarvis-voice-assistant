"""One stream of events for everything JARVIS says and does, shared by every channel.

Voice (Gemini Live) and chat (the window's box, --text, the web UI later) feed one conversation. What
comes out of it (messages, tool calls, confirmations, state changes) is published here once, and every
surface subscribes: the console, the window, a WebSocket. Nothing here decides anything; the dispatcher
still owns the safety rules.

Events are delivered in order on the asyncio loop. publish() from another thread is handed to the loop,
and a subscriber that raises is logged and skipped, so a broken view can never stop the assistant.

Kinds (data fields):
- partial: role ("user" | "jarvis"), text, a fragment of what's being said or written right now
- message: role, label, text, source, one finished line of the conversation
- tool_started: tool, args (secrets hidden, long values cut), step (part of an approved plan)
- tool_finished: tool, result (cut), paused (a step now waits for a yes)
- confirm_requested: id, tool, summary
- confirm_resolved: id, tool, outcome ("confirmed_voice", "cancelled", "timeout"...), result
- state: state ("starting", "asleep", "listening", "speaking", "working", "muted")
- notice: text, the first line of a [system notice] sent to the model
- status: text, problem, a line for the console such as "(awake)"

`channel` says which conversation side an event belongs to: "voice", "chat", or None for the app itself.
Replies and tool calls carry the channel of the user's latest input.

Text in events can come from web pages, apps and mail (tool results keep their untrusted-content tags):
a view shows it as plain text, never as HTML, and nothing a view receives can confirm an action.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

STREAM_MAX = 500  # events a slow stream (e.g. a WebSocket) may fall behind before the oldest are dropped


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any]
    channel: str | None = None
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "channel": self.channel, "seq": self.seq, "ts": self.ts, **self.data}


Handler = Callable[[Event], None]


class Stream:
    """Events as an async iterator, for a consumer that runs on the loop (a WebSocket). Bounded: if it
    falls behind, the oldest events are dropped and counted, so it can never hold up the assistant."""

    def __init__(self, bus: EventBus, maxsize: int, kinds: set[str] | None) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)
        self._kinds = kinds
        self.dropped = 0

    def _put(self, event: Event) -> None:
        if self._kinds is not None and event.kind not in self._kinds:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def close(self) -> None:
        self._bus._unsubscribe(self._put)


class EventBus:
    def __init__(self, history: int = 300) -> None:
        self._handlers: list[Handler] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._seq = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Deliver on this loop from now on (publish() from other threads is handed to it)."""
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """Call `handler(event)` for every event, on the loop. Returns a function that unsubscribes."""
        self._handlers.append(handler)
        return lambda: self._unsubscribe(handler)

    def stream(self, kinds: Iterable[str] | None = None, maxsize: int = STREAM_MAX) -> Stream:
        """A bounded async stream of events (only `kinds`, if given). Call on the loop; close() when done."""
        stream = Stream(self, maxsize, set(kinds) if kinds is not None else None)
        self._handlers.append(stream._put)
        return stream

    def _unsubscribe(self, handler: Handler) -> None:
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def recent(self, kinds: Iterable[str] | None = None) -> list[Event]:
        """The latest events, oldest first, so a view that opens late can catch up."""
        wanted = set(kinds) if kinds is not None else None
        return [e for e in self._history if wanted is None or e.kind in wanted]

    def publish(self, kind: str, channel: str | None = None, **data: Any) -> Event:
        """Never raises. Delivered at once on the loop's thread (or before a loop is attached)."""
        event = Event(kind, data, channel, next(self._seq))
        loop = self._loop
        if loop is not None and threading.get_ident() != self._loop_thread:
            try:
                loop.call_soon_threadsafe(self._deliver, event)
            except RuntimeError:  # the loop has closed: we're shutting down
                pass
            return event
        self._deliver(event)
        return event

    def _deliver(self, event: Event) -> None:
        self._history.append(event)
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.kind)


class ConsoleSink:
    """Prints the conversation and status lines to the terminal, as JARVIS always has. Lines the user
    typed in the terminal itself (source "stdin") are already on screen, so they aren't repeated."""

    def __call__(self, event: Event) -> None:
        if event.kind == "message":
            if event.data.get("source") != "stdin":
                print(f"{event.data['label']}: {event.data['text']}", flush=True)
        elif event.kind == "status":
            print(event.data["text"], flush=True)
