"""play_music via the user's installed YouTube Music app (rung 2) + media controls (rung 2).

How it works, without automating the user's personal Chrome profile:
- the song is found with ytmusicapi (YouTube Music's search, no login, no browser)
- the best match is picked in code (title/artist similarity; covers/remixes avoided unless asked)
- the song's music.youtube.com link is handed to the installed app via its own Start-menu launcher
- playback is confirmed, and pause/next/previous are sent, through Windows' media sessions (SMTC)
If the app isn't installed, songs play in JARVIS's own browser instead (rung 3).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from core.registry import Registry, Tier, Tool

log = logging.getLogger(__name__)

WATCH_URL = "https://music.youtube.com/watch?v={id}"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
VARIANT_WORDS = ["cover", "karaoke", "instrumental", "remix", "live", "sped up", "slowed", "nightcore", "8d",
                 "lofi", "lo-fi", "acoustic", "reverb", "mashup"]
PLAYING, PAUSED = 4, 5  # GlobalSystemMediaTransportControlsSessionPlaybackStatus

DETECT_PS = r"""
$app = Get-StartApps | Where-Object { $_.Name -eq 'YouTube Music' } | Select-Object -First 1
$lnk = Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" -Recurse -Filter 'YouTube Music.lnk' -ErrorAction SilentlyContinue | Select-Object -First 1
$out = @{ aumid = $null; target = $null; args = $null }
if ($app) { $out.aumid = $app.AppID }
if ($lnk) { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk.FullName); $out.target = $s.TargetPath; $out.args = $s.Arguments }
$out | ConvertTo-Json -Compress
"""


@dataclass
class YTMApp:
    """The user's installed YouTube Music app (a Chrome web app in their own profile)."""

    aumid: str
    launcher: str
    args: list[str]

    @property
    def ready(self) -> bool:
        return bool(self.aumid and self.launcher and any(a.startswith("--app-id=") for a in self.args))


def detect_ytm_app() -> YTMApp | None:
    """Read the app's Start-menu shortcut: launcher, --profile-directory, --app-id, and its app id."""
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", DETECT_PS], capture_output=True,
                             text=True, timeout=20).stdout.strip()
        info = json.loads(raw or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not info.get("target") or not info.get("args"):
        return None
    args = re.findall(r'--[\w-]+=(?:"[^"]*"|\S+)', info["args"])
    args = [a.replace('"', "") for a in args if a.startswith(("--profile-directory=", "--app-id="))]
    app = YTMApp(aumid=info.get("aumid") or "", launcher=info["target"], args=args)
    return app if app.ready else None


# ---------------------------------------------------------------- choosing the song

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def parse_request(query: str) -> tuple[str, str]:
    """'Believer by Imagine Dragons' -> ('believer', 'imagine dragons')."""
    q = _norm(query)
    for prefix in ("play ", "the song ", "song "):
        if q.startswith(prefix):
            q = q[len(prefix):]
    if " by " in q:
        title, artist = q.rsplit(" by ", 1)
        return title.strip(), artist.strip()
    return q, ""


def score(result: dict[str, Any], title: str, artist: str, query: str, rank: int) -> float:
    name = _norm(result.get("title", ""))
    artists = [_norm(a.get("name", "")) for a in result.get("artists") or []]
    s = SequenceMatcher(None, title, name).ratio() if title else 0.0
    if title and (title == name or name.startswith(title + " ")):
        s = max(s, 0.95)
    if artist:
        a = max((SequenceMatcher(None, artist, x).ratio() for x in artists), default=0.0)
        s = 0.55 * s + 0.45 * a
    q = _norm(query)
    if any(w in name and w not in q for w in VARIANT_WORDS):
        s -= 0.3  # a cover or remix when the user didn't ask for one
    return s - 0.02 * rank  # YouTube Music's own ranking breaks ties


def choose_song(results: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    songs = [r for r in results if r.get("videoId") and VIDEO_ID.match(r["videoId"])]
    if not songs:
        return None
    title, artist = parse_request(query)
    best = max(enumerate(songs), key=lambda ir: score(ir[1], title, artist, query, ir[0]))[1]
    return best


def describe(song: dict[str, Any]) -> str:
    artists = ", ".join(a.get("name", "") for a in song.get("artists") or [] if a.get("name"))
    return f"{song.get('title', 'that song')}" + (f" by {artists}" if artists else "")


def search_songs(query: str) -> list[dict[str, Any]]:
    from ytmusicapi import YTMusic

    return YTMusic().search(query, filter="songs", limit=10)


# ---------------------------------------------------------------- Windows media sessions

async def media_sessions() -> list[Any]:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    manager = await Manager.request_async()
    return list(manager.get_sessions())


async def session_state(session) -> dict[str, Any]:
    info = await session.try_get_media_properties_async()
    return {"app": session.source_app_user_model_id, "title": info.title or "", "artist": info.artist or "",
            "status": int(session.get_playback_info().playback_status)}


async def find_session(aumid: str | None):
    """The YouTube Music app's session if there is one, else whatever Windows considers current."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    manager = await Manager.request_async()
    if aumid:
        for s in manager.get_sessions():
            if s.source_app_user_model_id == aumid:
                return s
    return manager.get_current_session()


def app_windows() -> list[int]:
    """Windows of the YouTube Music app. Its title ends in 'YouTube Music'; a Chrome tab's ends in 'Google Chrome'."""
    from actions.system import _is_jarvis_browser, _visible_windows

    import psutil

    buf = ctypes.create_unicode_buffer(512)
    hwnds = []
    for hwnd, pid, cls in _visible_windows():
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        if cls != "Chrome_WidgetWin_1" or not (title == "YouTube Music" or title.endswith("| YouTube Music")):
            continue
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() == "chrome.exe" and not _is_jarvis_browser(proc):
                hwnds.append(hwnd)
        except psutil.Error:
            continue
    return hwnds


class Media:
    def __init__(self, config: dict, services) -> None:
        self.source = (config.get("music") or {}).get("source", "youtube_music_app")
        self.services = services
        self._app: YTMApp | None = None
        self._detected = False

    def app(self) -> YTMApp | None:
        if not self._detected and self.source == "youtube_music_app":
            self._app = detect_ytm_app()
            self._detected = True
            log.info("YouTube Music app: %s", "found" if self._app else "not installed")
        return self._app

    # ---------------------------------------------------------------- tools

    async def play_music(self, query: str) -> str:
        """Play a song on YouTube Music: 'Believer by Imagine Dragons'. Rung 2 (installed app + Windows media
        session) or rung 3 (JARVIS's browser). Tier SAFE: playing media."""
        try:
            results = await asyncio.to_thread(search_songs, query)
        except Exception as exc:
            log.warning("YouTube Music search failed: %s", exc)
            return "I couldn't search YouTube Music just now."
        song = choose_song(results, query)
        if song is None:
            return f"I couldn't find {query!r} on YouTube Music."
        url = WATCH_URL.format(id=song["videoId"])
        app = await asyncio.to_thread(self.app)
        if app is not None:
            return await self._play_in_app(app, url, song)
        return await self._play_in_browser(url, song)

    async def media_key(self, action: str) -> str:
        """play, pause, next, previous or stop. Rung 2: Windows media session (YouTube Music app first),
        falling back to the media virtual keys. Tier SAFE."""
        from actions.system import media_key as virtual_key

        action = action.lower().strip()
        app = self._app if self._detected else await asyncio.to_thread(self.app)
        try:
            session = await find_session(app.aumid if app else None)
        except Exception:
            session = None
        if session is None:
            return await asyncio.to_thread(virtual_key, action)
        ops = {"play": session.try_play_async, "resume": session.try_play_async, "pause": session.try_pause_async,
               "stop": session.try_pause_async, "next": session.try_skip_next_async, "skip": session.try_skip_next_async,
               "previous": session.try_skip_previous_async, "back": session.try_skip_previous_async}
        if action not in ops:
            return f"I don't know the media action {action!r}. I can do play, pause, next, previous or stop."
        ok = await ops[action]()
        if not ok:
            return await asyncio.to_thread(virtual_key, action)
        await asyncio.sleep(1.0)
        state = await session_state(session)
        now = f"{state['title']} by {state['artist']}" if state["title"] else "the music"
        return {"pause": f"Paused {now}.", "stop": f"Paused {now}.", "play": f"Playing {now}.",
                "resume": f"Playing {now}."}.get(action, f"Now playing {now}.")

    async def now_playing(self) -> str:
        """What's playing, from Windows' media sessions. Rung 2. Tier SAFE, read-only."""
        try:
            sessions = await media_sessions()
            states = [await session_state(s) for s in sessions]
        except Exception:
            return "I can't see what's playing right now."
        playing = [s for s in states if s["status"] == PLAYING] or [s for s in states if s["status"] == PAUSED]
        if not playing:
            return "Nothing is playing."
        s = playing[0]
        status = "playing" if s["status"] == PLAYING else "paused"
        return f"{s['title']} by {s['artist']} ({status})." if s["artist"] else f"{s['title']} ({status})."

    # ---------------------------------------------------------------- helpers

    async def _play_in_app(self, app: YTMApp, url: str, song: dict) -> str:
        # One song at a time: close the previous app window, or both would play.
        for hwnd in await asyncio.to_thread(app_windows):
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: like clicking X
        await asyncio.sleep(0.5)
        subprocess.Popen([app.launcher, *app.args, f"--app-launch-url-for-shortcuts-menu-item={url}"])
        wanted = _norm(song.get("title", ""))
        for _ in range(24):  # up to ~12 s for the app to load and start
            await asyncio.sleep(0.5)
            try:
                session = await find_session(app.aumid)
                state = await session_state(session) if session else None
            except Exception:
                state = None
            if state and state["app"] == app.aumid and SequenceMatcher(None, wanted, _norm(state["title"])).ratio() > 0.6:
                if state["status"] == PAUSED:
                    await session.try_play_async()
                    continue
                if state["status"] == PLAYING:
                    return f"Playing {describe(song)} in YouTube Music."
        return f"Opened {describe(song)} in YouTube Music; press play if it hasn't started."

    async def _play_in_browser(self, url: str, song: dict) -> str:
        browser = getattr(self.services, "browser", None)
        if browser is None:
            return "YouTube Music isn't installed and the browser module is off, so I can't play music."
        opened = await browser.browser_open(url)
        if opened.startswith(("That page", "Couldn't")):
            return opened
        state = await browser._wait_for_playback(await browser._page())
        if state and state.get("playing"):
            return f"Playing {describe(song)} in YouTube Music (JARVIS's browser)."
        return f"Opened {describe(song)} in JARVIS's browser; it hasn't started playing."


def register(registry: Registry, config: dict, services) -> None:
    media = Media(config, services)
    registry.register(Tool(
        name="play_music",
        description="Play a song on YouTube Music, e.g. 'Believer by Imagine Dragons'. Include the artist if known.",
        func=media.play_music,
        tier=Tier.SAFE,
        self_gated=True,  # can only ever start a YouTube Music song
        rung=2,
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        summarize=lambda a: f"Play {a.get('query')!r} on YouTube Music",
    ))
    registry.register(Tool(
        name="media_key",
        description="Control the music or video that's playing: play, pause, next, previous or stop.",
        func=media.media_key,
        tier=Tier.SAFE,
        rung=2,
        parameters={
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "stop"]}},
            "required": ["action"],
        },
        summarize=lambda a: f"Press {a.get('action')} on the music",
    ))
    registry.register(Tool(
        name="now_playing",
        description="Say which song or video is playing right now.",
        func=media.now_playing,
        tier=Tier.SAFE,
        read_only=True,
        rung=2,
    ))
