# Changelog

How JARVIS was built: the phases in the build spec and the finishing touches (24–25 September 2026), then the features added since.

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

## After the phases (24–25 September 2026)

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

## 26–29 September 2026

- **Your files:** "open my resume", "what did I download today?", "show it in its folder". Searches
  file names only, in your own folders, and never opens programs or private-looking files.
- **Type to JARVIS:** a typing box in the window, with spoken or silent replies.
- **No falling asleep mid-sentence:** JARVIS now notices you talking on your PC (RNNoise's voice
  detection, louder than the room) instead of waiting for Gemini's transcript, which can lag.
- **Pausing mid-sentence:** Gemini now waits for a second of quiet (and is less eager to decide
  you've finished) before answering, so one request no longer arrives as several pieces.
- **Wake word hears you more often:** your personal voice model is now always asked, with a
  stricter bar when the stock model barely reacts (it scored about 1 in 3 of your own "hey jarvis"
  near 0, so they never woke JARVIS). On your recordings: 7 of 8 wake, up from 5, with no false wakes.
  Every attempt is logged with both models' scores.
- **Stop leaves your music alone:** it stops JARVIS; a second press, with JARVIS quiet, pauses the music.
- **Claude app:** "open Claude" (the Microsoft Store app; "cloud" is understood too).
- **No repeated lookups:** when split-up speech makes JARVIS ask for the same screen reading or
  search again while the first is still running, the repeats share its result.

## Job search status (5 October 2026)

- `job_search_status` (`actions/job_search.py`, `features.job_search`): a SAFE, read-only tool that
  answers "how's my job search?" from the job search agent's `project_sync` folder: data freshness,
  open jobs, Claude's shortlist, applications, the LinkedIn resume upload and waiting questions.

## Job search from JobPilot (10 October 2026)

- `job_search_status` now reads JobPilot's local API (`job_search.api_url`, GET only, loopback
  addresses only) instead of the job search agent's `project_sync` files, which JobPilot no longer
  updates. New `review` topic: applications waiting for approval or blocked at a security check.
  Demo rows are excluded; a stopped JobPilot is a spoken error. The resume upload topic still reads
  `project_sync/resume_upload_status.md`.

## Wake-word training (11 October 2026)

- `--train-wake` records 20 "hey jarvis" (normal, softer, further away, quick) and keeps every
  earlier recording, so each run adds to the voice model instead of replacing it.
- It reports how often it woke on recordings it did not learn from, and tries a second method
  that also learns from the clips the stock model misses, keeping whichever does better.
- It refuses to start while JARVIS is running (JARVIS would answer every recording).

## Reading and controlling apps (15 October 2026)

- `read_app` (`actions/desktop.py`, `core/uia.py`, `features.desktop`): reads the text, buttons and
  fields of any open app through Windows UI Automation, with no screenshot. A SAFE, read-only tool
  whose output is untrusted. It remembers the current app, so "read it again" needs no name.
- Never read: password managers, web browsers, Windows Settings and Security, sign-in and admin
  prompts, Registry Editor, Task Manager, JARVIS itself and windows with private-looking titles.
  Password and payment fields are listed as private. Emails, numbers, keys and card numbers are
  replaced (`privacy.hide_secrets`).
- `type_in_app`: types into any open app that isn't off-limits, where the cursor is or into a named
  or numbered field, optionally pressing Enter. Text is pasted through the clipboard
  (`core/textinput.py`, kept out of clipboard history) and the clipboard is put back. It asks first in
  terminals, for Enter outside AI chat apps, and after untrusted text. It never types secrets or into
  password or payment fields. UI Automation now runs on one thread (`uia.call`).
- `click_in_app`, `press_keys`, `scroll_app` and `window_action`: click by name or number (UI
  Automation first, a real mouse click only on that app's window), "did you mean" and "which one",
  a fixed key list with risky keys confirmed and Windows-level shortcuts refused, mouse-wheel
  scrolling, and window minimise, maximise, restore, front and half-screen snapping.
- The older `app_ui_action` (allowlisted apps, every step planned and confirmed) is off by default
  (`features.app_ui: false`).
- Multi-step app tasks through `run_plan`: `wait_for_app` waits until an app stops changing (or shows
  some text) and returns only the new text since the message was sent. Plan lines show the exact
  words to be typed. Fixed-argument steps of an approved plan don't ask again because of earlier
  untrusted text (`registry.APPROVED_STEP`). Apps still opening are waited for (up to 10 seconds).
  "Which one" and "did you mean" answers stop a plan.
- `--bench-apps` (`core/desktop_bench.py`): for each open app, the number of controls and typeable
  fields JARVIS finds and how long reads take. Read-only, and it prints no app text.
