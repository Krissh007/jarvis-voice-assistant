"""The web UI's server: the page, and one WebSocket that streams the event bus and takes chat and buttons.

It runs inside JARVIS's own asyncio loop (uvicorn, on 127.0.0.1 only) and is the only way a page reaches
the assistant. Because a WebSocket isn't covered by the browser's same-origin rules, every connection
must pass three checks, or any website open in the user's browser could drive JARVIS:
- the per-launch token (random, 32 bytes, from the URL's #fragment, so it never sits in a request line
  for the page itself), compared in constant time;
- the Host header is 127.0.0.1 or localhost on this port (stops DNS rebinding);
- the Origin is this server's own page.

What the page sends is limited to: a chat message (through ChatChannel, like the window's box) and the
buttons the tray and hotkeys already have (talk, sleep, mute, stop, confirm, cancel). Confirm is the
user's click in JARVIS's own window, the same as the confirm hotkey; nothing the model or a tool
produces can press it. The page shows every text with textContent (never as HTML) under a strict
Content-Security-Policy, so text from web pages can't run as code there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import secrets
import socket
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from core.bus import EventBus

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
# Windows' registry often maps .js to text/plain, and with nosniff the browser then won't run it.
for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("text/html", ".html")):
    mimetypes.add_type(_type, _ext)
ACTIONS = {"talk", "sleep", "mute", "stop", "confirm", "cancel", "new_chat", "open_chat", "announce", "voice",
           "open_app", "open_settings"}
REQUESTS = {"settings", "facts", "conversations", "conversation"}  # read-only, answered to the asking page only
MAX_MESSAGE_BYTES = 8000  # one WebSocket message from the page (chat is limited to 2000 characters)
LEVEL_FPS = 15  # orb updates
HISTORY_KINDS = {"message", "tool_started", "tool_finished", "confirm_requested", "confirm_resolved",
                 "sources", "memory_used"}
HISTORY_MAX = 150
STREAM_KINDS = HISTORY_KINDS | {"partial", "state", "status", "focus", "media", "conversation", "announce", "voice",
                                "look"}


class Bridge(Protocol):
    """What the server needs from the assistant (core.runtime.WebBridge)."""

    bus: EventBus

    def snapshot(self) -> dict[str, Any]: ...  # state, level, user, speak, muted, pending
    def chat(self, text: str, speak: bool) -> bool: ...
    def control(self, action: str) -> bool: ...
    def set_look(self, values: dict) -> dict: ...  # the orb's size and brightness (Customize)
    def request(self, what: str, arg: Any = None) -> dict | None: ...  # the app's read-only data
    def delete_conversation(self, conversation_id: Any) -> dict: ...  # the Memory page's Delete button
    def regenerate(self, speak: bool) -> bool: ...  # the chat's Regenerate
    def pin(self, text: Any) -> dict: ...  # the chat's Pin to memory
    async def snap_screen(self) -> dict: ...  # "attach my screen" (private windows blacked out)
    attachments: Any  # core.attachments.AttachmentStore: uploaded pictures until their message is sent
    def change_setting(self, key: str, value: Any) -> dict: ...  # one change from the Settings page
    def forget_fact(self, fact_id: Any) -> dict: ...  # the Memory settings' Forget button


def security_headers(port: int) -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def create_app(bridge: Bridge, token: str, port: int) -> FastAPI:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    headers = security_headers(port)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return PlainTextResponse("Not here.", status_code=400)
        response = await call_next(request)
        response.headers.update(headers)
        return response

    @app.get("/")
    async def index():  # the orb: JARVIS's control centre
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/chat")
    async def chat():  # the complete chat page
        return FileResponse(STATIC / "chat.html", media_type="text/html")

    @app.get("/app")
    async def app_page():  # the app: sidebar with Chat, Command centre, Memory, Settings
        return FileResponse(STATIC / "app" / "index.html", media_type="text/html")

    def _page_only(request: Request) -> bool:
        """Uploads come from JARVIS's own page: its Origin and the launch token (in a header, never the URL)."""
        given = request.headers.get("x-jarvis-token", "")
        return (request.headers.get("origin") in origins
                and secrets.compare_digest(given.encode(), token.encode()))

    @app.post("/upload")
    async def upload(request: Request):  # a picture attached in the chat
        from core.attachments import MAX_UPLOAD_BYTES, AttachmentError

        if not _page_only(request):
            log.warning("web UI: refused an upload (origin %r)", request.headers.get("origin"))
            return JSONResponse({"ok": False, "message": "Not here."}, status_code=403)
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if not 0 < size <= MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "message": "Attach a picture of 10 MB at most."}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "message": "Attach a picture of 10 MB at most."}, status_code=413)
        name = request.headers.get("x-file-name", "picture")[:80]
        try:
            item = await asyncio.to_thread(bridge.attachments.add, raw, name)
        except AttachmentError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **item})

    @app.post("/snap")
    async def snap(request: Request):  # "attach my screen"
        if not _page_only(request):
            log.warning("web UI: refused a screen snapshot (origin %r)", request.headers.get("origin"))
            return JSONResponse({"ok": False, "message": "Not here."}, status_code=403)
        try:
            return JSONResponse({"ok": True, **(await bridge.snap_screen())})
        except Exception:
            log.exception("screen snapshot failed")
            return JSONResponse({"ok": False, "message": "Couldn't take a picture of the screen."}, status_code=500)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.websocket("/ws")
    async def ws(socket_: WebSocket):
        given = socket_.query_params.get("t", "")
        if (socket_.headers.get("host") not in hosts or socket_.headers.get("origin") not in origins
                or not secrets.compare_digest(given.encode(), token.encode())):
            log.warning("web UI: refused a connection (host %r, origin %r)", socket_.headers.get("host"),
                        socket_.headers.get("origin"))
            await socket_.close(code=1008)
            return
        await socket_.accept()
        log.info("web UI connected")
        stream = bridge.bus.stream(kinds=STREAM_KINDS)
        history = [e.to_dict() for e in current_conversation(bridge.bus)][-HISTORY_MAX:]
        await socket_.send_text(_dumps({"kind": "hello", "snapshot": bridge.snapshot(), "history": history}))
        tasks = [asyncio.create_task(_send_events(socket_, stream)),
                 asyncio.create_task(_send_levels(socket_, bridge))]
        try:
            while True:
                text = await socket_.receive_text()
                reply = _handle(bridge, text)
                if reply is not None:
                    await socket_.send_text(_dumps(reply))
        except WebSocketDisconnect:
            pass
        finally:
            stream.close()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("web UI disconnected")

    return app


def current_conversation(bus: EventBus) -> list:
    """The history since the last "new chat" (earlier conversations aren't shown again)."""
    events = bus.recent(HISTORY_KINDS | {"conversation"})
    starts = [i for i, e in enumerate(events) if e.kind == "conversation"]
    return events[starts[-1] + 1:] if starts else events


def _handle(bridge: Bridge, text: str) -> dict[str, Any] | None:
    """One message from the page. Anything malformed is dropped (and logged without its content).
    Requests get a reply for the page that sent them: {"kind": "reply", "id": <its id>, ...}."""
    if len(text.encode("utf-8", "replace")) > MAX_MESSAGE_BYTES:
        log.warning("web UI: dropped an oversized message (%d characters)", len(text))
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        log.warning("web UI: dropped a message that isn't JSON")
        return None
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    rid = msg.get("id") if isinstance(msg.get("id"), (int, str)) else None
    if kind == "chat" and isinstance(msg.get("text"), str):
        extras = {}
        if msg.get("web") in ("on", "off"):
            extras["web"] = msg["web"]
        if isinstance(msg.get("attachments"), list) and msg["attachments"]:
            extras["attachments"] = [a for a in msg["attachments"] if isinstance(a, str)][:4]
        bridge.chat(msg["text"], bool(msg.get("speak", True)), **extras)
    elif kind == "regenerate":
        bridge.regenerate(bool(msg.get("speak", True)))
    elif kind == "pin":
        return {"kind": "reply", "id": rid, "what": "pin", "data": bridge.pin(msg.get("text"))}
    elif kind == "control" and msg.get("action") in ACTIONS:
        bridge.control(msg["action"])
    elif kind == "look" and isinstance(msg.get("look"), dict):
        bridge.set_look(msg["look"])
    elif kind == "request" and msg.get("what") in REQUESTS:
        data = bridge.request(msg["what"], msg.get("arg")) if "arg" in msg else bridge.request(msg["what"])
        return {"kind": "reply", "id": rid, "what": msg["what"], "data": data}
    elif kind == "delete_conversation":
        return {"kind": "reply", "id": rid, "what": "delete_conversation",
                "data": bridge.delete_conversation(msg.get("conversation"))}
    elif kind == "setting" and isinstance(msg.get("key"), str) and "value" in msg:
        return {"kind": "reply", "id": rid, "what": "setting", "data": bridge.change_setting(msg["key"], msg["value"])}
    elif kind == "forget":
        return {"kind": "reply", "id": rid, "what": "forget", "data": bridge.forget_fact(msg.get("fact"))}
    else:
        log.warning("web UI: dropped an unknown message type %r", str(kind)[:20])
    return None


async def _send_events(socket_, stream) -> None:
    async for event in stream:
        await socket_.send_text(_dumps(event.to_dict()))


async def _send_levels(socket_, bridge: Bridge) -> None:
    """The orb's input: state and loudness, sent only when they change."""
    last = None
    while True:
        snap = bridge.snapshot()
        now = (snap["state"], round(float(snap.get("level") or 0.0), 2))
        if now != last:
            last = now
            await socket_.send_text(_dumps({"kind": "level", "state": now[0], "level": now[1]}))
        await asyncio.sleep(1 / LEVEL_FPS)


class _HideToken(logging.Filter):
    """Replaces the launch's token with *** in a log line (it's in every WebSocket's request line)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token in message:
            record.msg, record.args = message.replace(self.token, "***"), ()
        return True


class WebUI:
    """Owns the server's socket, token and lifetime, inside the assistant's loop."""

    def __init__(self, bridge: Bridge, port: int = 0) -> None:
        self.bridge = bridge
        self.token = secrets.token_urlsafe(32)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", int(port or 0)))  # never another interface
        self.port: int = self._sock.getsockname()[1]
        self._server = None

    @property
    def url(self) -> str:
        """The orb page's address, with the token in the #fragment (pass it only to JARVIS's own windows)."""
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    @property
    def chat_url(self) -> str:
        """The complete chat page's address (same token)."""
        return f"http://127.0.0.1:{self.port}/chat#t={self.token}"

    @property
    def app_url(self) -> str:
        """The app's address (same token); add "&s=<section>" to open it on a section."""
        return f"http://127.0.0.1:{self.port}/app#t={self.token}"

    async def serve(self) -> None:
        import uvicorn

        class _Server(uvicorn.Server):
            def capture_signals(self):  # JARVIS owns Ctrl+C and its own shutdown
                return contextlib.nullcontext()

        # uvicorn logs each WebSocket's request line, token included: keep only its warnings, and hide the
        # token in anything it does log.
        hide = _HideToken(self.token)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.addFilter(hide)
            logger.setLevel(logging.WARNING)
        config = uvicorn.Config(create_app(self.bridge, self.token, self.port), log_config=None, access_log=False,
                                lifespan="off", ws_max_size=MAX_MESSAGE_BYTES * 2, timeout_graceful_shutdown=1)
        self._server = _Server(config)
        log.info("web UI on 127.0.0.1:%d", self.port)
        await self._server.serve(sockets=[self._sock])

    def stop(self) -> None:
        """Ask the server to close its connections and finish; the serve() task then ends by itself
        (await it). Closes the socket if the server never started."""
        if self._server is not None:
            self._server.should_exit = True
        else:
            with contextlib.suppress(OSError):
                self._sock.close()
