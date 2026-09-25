"""Phase 6: YouTube Music. Search results, app launching and Windows media sessions are all faked."""

import pytest

from actions import Services, media
from actions.media import YTMApp, choose_song, parse_request

APP = YTMApp(aumid="Chrome._crx_x.UserData.Profile15", launcher="C:/chrome_proxy.exe",
             args=["--profile-directory=Profile 15", "--app-id=abc"])


def song(vid, title, *artists):
    return {"videoId": vid, "title": title, "artists": [{"name": a} for a in artists]}


RESULTS = [
    song("aaaaaaaaaaa", "Believer (Cover)", "Some Band"),
    song("Kx7B-XvmFtE", "Believer", "Imagine Dragons"),
    song("bbbbbbbbbbb", "Believer (Lofi Remix)", "Imagine Dragons"),
    song("ccccccccccc", "Thunder", "Imagine Dragons"),
]


@pytest.fixture(autouse=True)
def no_artist_lookup(monkeypatch):
    """Tests never reach the real YouTube Music; artist requests are faked where a test needs them."""
    monkeypatch.setattr(media, "artist_songs", lambda q: [])


# ---------------------------------------------------------------- choosing the song

@pytest.mark.parametrize("query, expected", [
    ("Believer by Imagine Dragons", ("believer", "imagine dragons")),
    ("play Stand By Me by Ben E. King", ("stand by me", "ben e king")),
    ("Shape of You", ("shape of you", "")),
])
def test_parse_request(query, expected) -> None:
    assert parse_request(query) == expected


def test_the_original_beats_covers_and_remixes() -> None:
    assert choose_song(RESULTS, "Believer by Imagine Dragons")["videoId"] == "Kx7B-XvmFtE"
    assert choose_song(RESULTS, "Believer")["videoId"] == "Kx7B-XvmFtE"


def test_a_remix_when_asked_for_one() -> None:
    assert choose_song(RESULTS, "Believer lofi remix by Imagine Dragons")["videoId"] == "bbbbbbbbbbb"


def test_artist_decides_between_same_titles() -> None:
    results = [song("aaaaaaaaaaa", "Hello", "Lionel Richie"), song("bbbbbbbbbbb", "Hello", "Adele")]
    assert choose_song(results, "Hello by Adele")["videoId"] == "bbbbbbbbbbb"


def test_results_without_a_valid_video_id_are_ignored() -> None:
    assert choose_song([{"videoId": "x; rm -rf", "title": "Believer"}], "Believer") is None


# ---------------------------------------------------------------- playing in the installed app

class FakeSession:
    def __init__(self, title="Believer", artist="Imagine Dragons", status=media.PLAYING, app=APP.aumid) -> None:
        self.source_app_user_model_id = app
        self.title, self.artist, self.status = title, artist, status
        self.calls: list[str] = []

    async def try_get_media_properties_async(self):
        return type("P", (), {"title": self.title, "artist": self.artist})()

    def get_playback_info(self):
        return type("I", (), {"playback_status": self.status})()

    async def _op(self, name, status=None):
        self.calls.append(name)
        if status is not None:
            self.status = status
        return True

    def try_play_async(self):
        return self._op("play", media.PLAYING)

    def try_pause_async(self):
        return self._op("pause", media.PAUSED)

    def try_skip_next_async(self):
        return self._op("next")

    def try_skip_previous_async(self):
        return self._op("previous")


@pytest.fixture
def fake_os(monkeypatch):
    """Records launches and window closes; the app's media session is scripted per test."""
    record = {"launched": [], "closed": [], "session": FakeSession()}
    monkeypatch.setattr(media, "search_songs", lambda q: RESULTS)
    monkeypatch.setattr(media, "detect_ytm_app", lambda: APP)
    monkeypatch.setattr(media.subprocess, "Popen", lambda args: record["launched"].append(args))
    monkeypatch.setattr(media, "app_windows", lambda: [501])

    class User32:
        def PostMessageW(self, hwnd, msg, w, l):
            record["closed"].append((hwnd, msg))

    monkeypatch.setattr(media.ctypes, "windll", type("W", (), {"user32": User32()})())

    async def find_session(aumid):
        return record["session"]

    monkeypatch.setattr(media, "find_session", find_session)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(media.asyncio, "sleep", no_sleep)
    return record


async def test_play_music_opens_the_song_in_the_installed_app(fake_os) -> None:
    out = await media.Media({}, Services()).play_music("Believer by Imagine Dragons")
    assert out == "Playing Believer by Imagine Dragons in YouTube Music."
    assert fake_os["launched"] == [[
        "C:/chrome_proxy.exe", "--profile-directory=Profile 15", "--app-id=abc",
        "--app-launch-url-for-shortcuts-menu-item=https://music.youtube.com/watch?v=Kx7B-XvmFtE",
    ]]


async def test_the_previous_app_window_is_closed_first(fake_os) -> None:
    await media.Media({}, Services()).play_music("Believer")
    assert fake_os["closed"] == [(501, 0x0010)]  # polite WM_CLOSE, so two songs never play at once


async def test_a_paused_start_is_resumed(fake_os) -> None:
    fake_os["session"].status = media.PAUSED
    out = await media.Media({}, Services()).play_music("Believer")
    assert "Playing" in out and fake_os["session"].calls == ["play"]


async def test_says_so_when_it_cannot_confirm_playback(fake_os) -> None:
    fake_os["session"] = FakeSession(title="Something else")
    out = await media.Media({}, Services()).play_music("Believer")
    assert "press play if it hasn't started" in out


async def test_search_failure_is_spoken(fake_os, monkeypatch) -> None:
    def boom(q):
        raise ConnectionError("offline")

    monkeypatch.setattr(media, "search_songs", boom)
    assert await media.Media({}, Services()).play_music("x") == "I couldn't search YouTube Music just now."


async def test_falls_back_to_jarvis_browser_without_the_app(fake_os, monkeypatch) -> None:
    monkeypatch.setattr(media, "detect_ytm_app", lambda: None)
    opened = []

    class FakeBrowser:
        async def browser_open(self, url):
            opened.append(url)
            return "Opened Believer (music.youtube.com)."

        async def _page(self):
            return None

        async def _wait_for_playback(self, page):
            return {"playing": True}

    out = await media.Media({}, Services(browser=FakeBrowser())).play_music("Believer")
    assert opened == ["https://music.youtube.com/watch?v=Kx7B-XvmFtE"] and "JARVIS's browser" in out
    assert fake_os["launched"] == []


# ---------------------------------------------------------------- controls and now playing

@pytest.mark.parametrize("action, call, spoken", [
    ("pause", "pause", "Paused Believer by Imagine Dragons."),
    ("play", "play", "Playing Believer by Imagine Dragons."),
    ("next", "next", "Now playing Believer by Imagine Dragons."),
    ("previous", "previous", "Now playing Believer by Imagine Dragons."),
])
async def test_controls_target_the_music_session(fake_os, action, call, spoken) -> None:
    out = await media.Media({}, Services()).media_key(action)
    assert fake_os["session"].calls == [call] and out == spoken


async def test_pause_really_pauses_instead_of_toggling(fake_os) -> None:
    fake_os["session"].status = media.PAUSED
    await media.Media({}, Services()).media_key("pause")
    assert fake_os["session"].status == media.PAUSED  # a toggle key would have started playback


async def test_controls_fall_back_to_media_keys_with_no_session(fake_os, monkeypatch) -> None:
    from actions import system

    async def none(aumid):
        return None

    monkeypatch.setattr(media, "find_session", none)
    monkeypatch.setattr(system, "media_key", lambda action: f"key:{action}")
    assert await media.Media({}, Services()).media_key("next") == "key:next"


async def test_now_playing(fake_os, monkeypatch) -> None:
    async def sessions():
        return [FakeSession(status=media.PAUSED, title="Old"), FakeSession()]

    monkeypatch.setattr(media, "media_sessions", sessions)
    assert await media.Media({}, Services()).now_playing() == "Believer by Imagine Dragons (playing)."


def test_media_module_replaces_the_plain_media_key() -> None:
    from actions import load_actions
    from core.registry import Registry

    reg = load_actions(Registry(), {"features": {"system": True, "media": True}, "apps": {"allowlist": {}}},
                       Services())
    assert reg.get("media_key").func.__self__.__class__ is media.Media


async def test_an_artist_name_plays_that_artists_song(fake_os, monkeypatch) -> None:
    """Live log: 'Rolling Stones' played 'Still Rolling Stones' by someone else."""
    monkeypatch.setattr(media, "artist_songs",
                        lambda q: [song("PaintBlack1", "Paint It Black", "The Rolling Stones")] if "rolling" in q.lower() else [])
    fake_os["session"] = FakeSession("Paint It Black", "The Rolling Stones")
    m = media.Media({"music": {"source": "youtube_music_app"}}, Services())
    said = await m.play_music("The Rolling Stones")
    assert "Paint It Black by The Rolling Stones" in said
    assert "PaintBlack1" in fake_os["launched"][-1][-1]


async def test_close_the_music_app(fake_os) -> None:
    m = media.Media({"music": {"source": "youtube_music_app"}}, Services())
    assert await m.media_key("close") == "Closed YouTube Music."
    assert fake_os["closed"] == [(501, 0x0010)]


async def test_music_volume_needs_the_ducker() -> None:
    m = media.Media({}, Services())
    assert "whole computer" in await m.set_music_volume(30)
