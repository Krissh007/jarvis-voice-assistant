# Changelog

How JARVIS was built, from the phases in the build spec to the finishing touches (24–25 September 2026).

## Phases

| Phase | What it added |
|---|---|
| 0: Skeleton | uv project, config, smoke tests |
| 1: Voice loop | Gemini Live session: audio in and out, transcription, resumption and reconnects, hotkeys, optional offline wake word |
| 2: Safety layer | Tool registry and dispatcher: SAFE, CONFIRM and BLOCKED tiers, the code-written confirmation protocol, echo-safe listening, untrusted-content wrapping and taint, audit log |
| 3: System basics | Date and time, system status, allowlisted apps, websites, volume and media keys |
| 4: Web research | Search (Tavily or DuckDuckGo), page reading and summaries, weather, saving research notes; Flash fallbacks and quota handling |
| 5: Browser control | JARVIS's own Chrome profile with privacy rules: no password or payment fields, account pages blocked, no account switching |
| 6: Music and apps | Songs in the installed YouTube Music app, Windows media sessions, closing and focusing apps, UI Automation for other apps |
| 7: Screen and clipboard | On-demand screenshots with private windows blacked out; summarise, translate, explain and fix copied text; secret detection |
| 8: Agent mode | Multi-step plans approved once and then frozen, blanks filled only from earlier results, per-step tiers |
| 9: Memory and reminders | Explicit-only memory, reminders that survive restarts with notifications, optional morning briefing |
| 10: Polish | Full README and documentation |

## After the phases

- **Wake word:** a mic test that compares gains, and a personal wake-word verifier trained on the
  user's voice (`--train-wake`), with near-miss hints.
- **Desktop app:**
  - a tray icon (click to talk, Stop, Mute, Quit) and a desktop shortcut that starts JARVIS
    without a terminal;
  - one instance at a time, where a second launch brings the window forward and relaunching after
    Quit is instant;
  - error pop-ups, and `logs/console.txt` for the terminal output.
- **The window:**
  - an animated orb with the full conversation and Talk, Sleep, Mute and Stop buttons;
  - a normal Windows window with a dark title bar that minimises to its taskbar button and groups
    with a pin made from the desktop icon;
  - it appears at once while JARVIS is starting.
- **Audio:**
  - RNNoise background-noise suppression;
  - music ducking while JARVIS is awake, and a music-only volume control;
  - following Windows audio-device changes, and an echo mode for speakers.
- **Smaller fixes:**
  - artist-only music requests;
  - closing the music app;
  - saving research from search results;
  - "Yes?" when woken while already awake;
  - staying awake while a tool is still working;
  - never re-running a call that was already confirmed;
  - shutdown timing in the log.
