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

## Agent Hub bridge (13 October 2026)

- **Agent Hub:** `agent_hub_request` and `agent_hub_status` hand job-search and dashboard tasks to the
  separate Agent Hub (`E:\agent-hub`) over MCP and speak the results when ready, without interrupting.
  Off by default (`features.agent_hub`); needs `AGENT_HUB_TOKEN` in `.env`.
- **Confirmations:** `NeedsConfirmation` has an optional `on_cancel` hook, called when the user says
  no, doesn't answer in time, or supersedes the action.
- **Background tasks:** `Services.background`, `notify`, `can_speak` and `taint` let a module run a loop
  and announce results safely.
- New dependency: `mcp` 2.3.0.

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

## Reasoning (16 October 2026)

- `think_through` (`actions/reasoning.py`, `features.reasoning`): careful answers for maths, logic,
  comparisons and decisions, on the free Flash chain with thinking `high` (`reasoning.thinking`;
  `LLM.generate(thinking=...)` overrides a model's level for one call). With `use_web` it searches
  first (Tavily, else DuckDuckGo) and reasons over the results, which stay untrusted.
- Free tier, checked on this account: Gemini Pro models have a limit of 0, and Google Search
  grounding is refused on every Flash model, so neither is used.
- Tool choice (no router: the Live model picks from the tool descriptions): `agent_hub_request` now
  covers the shortlist, applications waiting for approval, job lists, job search runs, approving or
  skipping an application and recent runs, and no longer claims dashboard work the Hub doesn't have.
  With the Hub on, `job_search_status` is the quick overall summary. `search_web`, `run_plan` and
  `look_at_screen` say when not to use them; the persona has a short tool-choice rule. On the real
  Live model, 17 of 17 test requests picked the intended tool.
- Agent Hub on (`features.agent_hub: true`), with `AGENT_HUB_TOKEN` set. Tested through the real Live
  model and the running Hub: shortlist and approval questions answered by the Hub; "run discovery"
  and "skip application 5" read the Hub's own question and were released on "no".

## Shorter spoken results (16 October 2026)

- `core/condense.py`: results over `condense.over_chars` from tools that opt in (`Tool.condense`:
  `read_app` except `what=controls`, `wait_for_app`, `job_search_status`) are summarised for the
  user's latest request before the Live model sees them, on `gemini-3.5-flash-lite` first
  (`LLM.generate(start_at=...)`). Over `condense.timeout_s`, or with no model, the start and end
  of the text are used. Plan steps get the full text. Live: the Claude window went from 4,310 to
  337 characters; summarising took about 8 s while the models were busy.
- `condense.timeout_s` lowered to 5 s, so a slow summary never holds up a spoken reply for long.

## Claude's latest reply (16 October 2026)

- `core/chat.py` reads chat messages from the accessibility tree using a per-app profile
  (`chat_apps.profiles`; shipped for Claude's chat view): the `feed` of `article`s named "Message N
  of M", roles from the "You said:" / "Claude responded:" label (then dropped, since it repeats the
  opening words), a reply still streaming marked unfinished, and clutter removed (text inside
  buttons and status lines such as "Thought for 13s", Copy and Retry, plus `drop_text`).
- `read_app what=latest` returns only the newest finished reply; `wait_for_app` waits for a new
  finished reply instead of comparing window text. A configured profile that doesn't match logs a
  warning (once per window per session; repeats at debug level) and JARVIS falls back to the whole window. Long replies keep their start when cut.
- Live: "read me Claude's reply" found the reply (423 characters) in 0.4 s, with no summarising
  needed; before, the tool took about 10 s or hit the 5 s summarising limit.

## Typing fixes (16 October 2026)

From a voice session in VS Code, where code went into the built-in terminal, was typed twice, and
missed the editor:

- A terminal inside an app (xterm's `xterm-helper-textarea`, or an IDE field named "Terminal…") is
  treated like a terminal app: `type_in_app` asks first and says so, and never chooses one itself.
- The same text into the same window within `desktop.repeat_guard_s` (10 s) is typed once; a repeat
  gets "I already typed that". A failed attempt doesn't block trying again.
- An app's whole page (Chromium's `RootWebArea`, or a document named like the window) is never a place
  to type. With the cursor outside a text field, JARVIS types only when there's one obvious field (or
  one chat box); otherwise it asks you to click where the text should go.
- Confirmations and plan lines describe long or multi-line text ("6 lines (84 characters) starting
  …") instead of reading it out; field names that are long accessibility notices are said as "the
  text box". `uia.Control` and `uia.Focused` carry the automation id and class name.
- Typing more than once in VS Code: bringing an app to the front tapped Alt even when it was already
  in front, and in VS Code Alt moves the cursor to the File menu, so every typing after the first
  found no text field. `focus_window` now does nothing for a window already in front, switches by
  attaching to the foreground window's input, and taps Alt only as a last resort (into the window
  being left). Checked live: four bring-to-fronts in a row kept VS Code's cursor in the editor.
- Code into the wrong file: in the same session JARVIS pasted HTML into `style.css`, JavaScript into
  `index.html` and page sections into `script.js`, then asked the user to create the right file. In a
  code editor `type_in_app` now needs the file name (`field`) and types only if that file is open
  (the editor field VS Code names after the file, or the file in its title); otherwise it says which
  file is open and types nothing.
- Long code got cut: over 2,000 characters was refused and the model's parts left out the page's
  start and end. Into a code editor the limit is now 10,000 (`desktop.max_typed_editor`), terminals
  keep 2,000 (`desktop.max_typed`), and the refusal says to type the rest in order with nothing left out.
- Claims of work not done ("I have completed … the JavaScript" when none was typed): the result now
  says what was typed into which file ("6 lines (84 characters) starting … into index.html"), and the
  prompt says to report only what tool results say, ask for a new file before typing, and wait.

## Creating, editing and deleting files (16 October 2026)

- New `file_edit` feature (`actions/file_edit.py`): `read_file`, `write_file` (new files, or `overwrite`;
  folders made as needed; `open` in VS Code), `edit_file` (exact find/replace, or append) and
  `delete_file` (CONFIRM, to the Recycle Bin). Only in Desktop, Documents and Downloads (config
  `file_edit:`), never JARVIS's own folder; private names, hidden/system files, executables and
  scripts Windows runs, and secret-looking text are refused; every change is backed up to
  `data/file_backups`. The prompt prefers these over typing into an editor.
- Live: the Live model rebuilt the cleared portfolio in Downloads (three `write_file` calls), changed
  the heading (an `edit_file` guessed without reading was refused, then it read and edited), appended a
  footer style, and created then deleted a scratch file, which asked first and went to the Recycle Bin.

## Shared core for voice and chat (17 October 2026)

First step of turning JARVIS into a general assistant you can talk or type to:
- New event bus (`core/bus.py`). Messages, partial captions, tool start and finish (plan steps
  included), confirmation requests and outcomes, state changes, notices and status lines are
  published once, tagged with the channel (`voice` or `chat`) of the user's latest input. The console
  is now one subscriber and prints exactly what it did before. The web UI will be another.
- New chat channel (`core/channels.py`). The window's typing box and `--text` mode go through one
  thread-safe entry point, into the same Live conversation, tools and confirmations as speech.
- The audit log now hides the value of every `*_KEY`, `*_TOKEN` and `*_SECRET` environment variable.
  Before, it missed `AGENT_HUB_TOKEN`.
- Live (text mode, silent, Agent Hub off): a typed question ran `get_datetime`, a typed request
  asked to confirm `test_write_note`, and a typed "yes" ran it. Every step arrived on the bus as
  `chat`. 793 tests pass in the worktree (the tray shortcut test needs the main folder's .venv).

## The web window (17 October 2026)

Phase 2 of the general-assistant plan. With `features.web_ui` on (now the default; the Tk window is
used only when it's off), JARVIS's window is a local web page:
- **The page** (`ui/static/`): an audio-reactive orb in the tray's state colours; Talk, Sleep, Mute
  and Stop; the chat (typed and spoken lines, live captions, a "Speak replies" switch); a confirmation
  card with Confirm / Cancel; an activity list of tools, plan steps and confirmations; and a vision
  placeholder. Its layout narrows to two columns, then one.
- **The server** (`ui/server.py`): FastAPI and uvicorn inside JARVIS's own loop, on 127.0.0.1 only.
  One WebSocket streams the event bus. It needs the launch's random token, a local Host header (stops
  DNS rebinding) and the page's own Origin, so another website can't drive JARVIS. The page accepts
  only chat and the existing buttons, shows all text with `textContent` under a strict
  Content-Security-Policy, and loads nothing from outside.
- **The window** (`ui/window.py`): pywebview (Edge WebView2) in a child process, because pywebview
  needs its own main thread. It keeps JARVIS's taskbar ID, remembers its size and place, and comes to
  the front for a second launch. Closing it quits JARVIS, or with `web_ui.close_button: hide` keeps it
  running in the tray.
- New dependencies: `fastapi`, `pywebview`. uvicorn, starlette and websockets were already installed.
- Fixed while checking it live: Windows maps `.js` to `text/plain`, and with `nosniff` the page
  wouldn't run, so the server now sets the types itself.
- Checked live in the browser pane against the real runtime (always-on mode, silent, no mic): a typed
  question ran `get_datetime` and showed the reply; a test note asked to confirm in the card, Confirm
  ran it, and the card cleared; Mute turned the orb red; a reload restored the conversation and
  activity. Then the real WebView2 window opened and connected. In push-to-talk mode, typed text that
  led to a tool call made Gemini Live drop the connection (error 1011). Wake-word and always-on modes
  are fine.

## The orb window (17 October 2026)

The web window was redesigned with the user, step by step. They chose a futuristic orb from several
options, then supplied a reference image, and the result works like this:
- **Just the orb:** the window draws nothing but the orb and a typing box under it. The desktop shows
  through everywhere else, and clicks there go to whatever is behind.
- **The orb** (`ui/static/orb.js`): a particle shell, a tunnel of HUD rings, three tilted orbit bands with
  light running along them and lightning crackling over them, a voice wave through the middle, sparks,
  and a bloom glow that blends into the desktop. It's low when asleep, brighter when listening and
  strongest when speaking; red when muted and amber while working on a task. At the user's request it
  has no vertical bars in the voice wave.
- **Sections around the orb:** while talking, only what's needed appears at equal clock positions (8 to
  4 o'clock): the answer, a confirmation with Confirm / Cancel, what JARVIS is doing, a picture
  (`look_at_screen` shows its redacted capture; never kept), what's playing. They stay until the orb is
  double-clicked or JARVIS goes to sleep.
- **Full panel:** a click on the orb opens controls orbiting it (Talk, Sleep, Mute, Stop), the whole
  conversation, activity and vision. Click again to fold it away. Drag the orb to move the window, which
  opens at the top centre and remembers its place.
- **Window technology:** Qt WebEngine (PySide6) instead of pywebview. Checked on this PC: Edge WebView2
  can't do per-pixel transparency on Windows, and colour-key transparency let clicks through the whole
  window, the orb included. Qt draws true per-pixel alpha, and the window switches `WS_EX_TRANSPARENT`
  as the mouse moves on and off the page's clickable shapes.
- **Fixed while checking it:** stopping the window now ends the whole process tree. The venv's
  `python.exe` is a launcher, and the real Qt process would have stayed open after JARVIS quit.
- **Checks:**
  - Live against the real assistant (headless, with a backdrop): an answer, a confirmation confirmed by
    the button, the screen preview, the full panel, and double-click clearing.
  - On the real desktop: see-through, glow blending, clicks going through empty space and landing on
    the orb and the typing box, and a clean close.
  - 816 tests pass in the worktree.
- Dependencies: `pyside6-essentials`, `pyside6-webengine` (about 400 MB in the venv on E:); `pywebview`
  removed.

## The orb as a control centre, and a chat page (17 October 2026)

At the user's request, the orb became a small quick-action control centre, and the full interface moved
to its own chat page:
- **Orb:** idle, it's only the orb, about 160 px (`web_ui.orb_size`). It opens near the top right the
  first time and can be dragged anywhere; it stays where it's left.
- **Tap / double-tap:** a tap talks. A double-tap opens the control centre around the orb: Talk, Mute,
  New chat, Stop and Sleep, a typing box under it, and quick-action chips (`web_ui.quick_actions`).
- **Sections:** the answer, a confirmation, a picture and what's playing appear around the orb while
  talking. Double-tap, Esc or sleep closes it back to the orb. Near a screen edge the orb eases inward
  while open, and goes back when it closes.
- **Removed from the orb:** the conversation, activity and vision panels, and the "working on it"
  section.
- **New chat** starts a fresh conversation (the Live session reconnects without its resumption handle),
  clears every view, and opens the complete chat page (`/chat`) in its own normal window. The page shows
  a small orb and controls, the conversation with confirmations and pictures inline, then activity and
  vision.
- The orb's lines and glows now scale with its size, so it stays crisp small. Before, it washed out into
  a white blob.
- **Checks:**
  - Live (headless): the control centre, sections around the orb, a confirmation from the chat page, and
    New chat starting a fresh conversation.
  - On the real desktop: closed orb at the top right with clicks going around it; double-tap opening and
    keeping the orb on screen; New chat opening the chat window; double-tap closing it back to its
    place.
  - 823 tests pass in the worktree.

## A 3D orb, stacked control centre (17 October 2026)

- **The orb in 3D:** Three.js, one of the libraries JARVIS itself suggested and the user picked, runs
  the orb on the graphics card (`ui/static/orb3d.js`), with the same design and a bloom glow. Version
  0.186.1 is stored in `ui/static/vendor/three` after an integrity check, so nothing loads from the
  internet. The 2D orb remains the fallback.
- **The glow on the desktop:** the last pass turns brightness into alpha. Only the bloom's tighter blur
  layers are used; the widest ones left a foggy disc on the desktop.
- **Layout, at the user's request:** when opened, everything stacks under the orb: the controls bar, the
  sections (answer, confirmation, picture, now playing), the typing box and the quick actions. The open
  window is a 420 x 720 column with the orb at its top.
- **Apple-like styling:** rounded dark glass cards and a segmented controls bar. They're solid enough to
  read over any window.
- **Found on the real desktop:** in the see-through Qt window the browser engine doesn't advance CSS
  animations. Fades stopped halfway, leaving cards half see-through and hard to read. All fades and
  slides now run on animation frames. `backdrop-filter` was dropped too (there's nothing behind the
  page to blur).
- **Click-through:** the orb takes clicks from the moment the window opens, instead of only after the
  page reports its shapes.
- **Checks:**
  - Headless with the graphics card: the 3D orb at 47 fps, sizes 160 to 460 px, no haze.
  - Live against the assistant: the stacked control centre.
  - On the real desktop: a vivid orb, solid cards, clicks through empty space, double-tap open and
    close.
  - 825 tests pass in the worktree.

## Skills in the Agent Hub's tool registry (17 October 2026)

- The Agent Hub (`E:gent-hub`, branch `tool-registry`) now keeps a tool registry: every tool its
  agents, connectors and JARVIS offer, with a category, keywords, read or write access, auth (env
  var names only), parameters and a timeout. Lookups return at most 8 tools, ranked for the request.
- JARVIS sends its 46 skills to it (`core/catalog.py`, `sync_tools` in the Agent Hub poller). This is
  groundwork for the reasoning layer: nothing JARVIS says or does changes yet.

## Routing requests to a few tools (17 October 2026)

- `core/router.py` decides, for each request, between one clear skill, several candidate tools, or
  reasoning, and attaches 1 to 8 tools from the Agent Hub's registry (or JARVIS's own skills when the
  Hub doesn't answer). Questions get read-only tools; areas with no tools yet are reported as missing.
- 60 labelled requests (`tests/fixtures/routing_samples.yaml`) route 100% correctly with the right tool
  attached. A held-out set written after tuning first scored 24/25 routes and 22/25 tools; the three
  misses ("how hot will it be", "put on some AR Rahman", "write me a short poem") were fixed with
  general rules and moved into the tuned set, and the remaining 22 held-out requests all pass.
- Every routing decision is logged to `logs/routing.jsonl` without the request text.
  `python main.py --route "..."` shows a decision. Nothing JARVIS says or does changes yet.

## The reasoning layer: ask_brain (17 October 2026)

- New tool `ask_brain` (`actions/brain.py`, `features.brain`, replaces `think_through`): routes the
  request, then a Groq model (`core/groq.py`, free tier, `GROQ_API_KEY`) reasons and calls only the
  1 to 8 attached tools, at most 6 model calls and 45 s per request, and answers in spoken sentences.
- Anything that changes something asks first through JARVIS's normal confirmation; Agent Hub tools
  run through the Hub's new `call_tool`, which applies the Hub's deny and confirm policies.
- Without Groq it falls back to Gemini Flash with web search, or hands tool work back to the voice model.
- `logs/brain.jsonl` records route, steps, tokens and time per request (no text).
  `python main.py --ask "..."` tries one request without voice.

## Google Drive, read-only (17 October 2026)

- The Agent Hub has its first connector: Google Drive, read-only (search, recent files, read a Doc,
  Sheet, Slides or text file). It needs a one-time setup and sign-in (Agent Hub README, "Google");
  until then its tools are hidden and Drive requests are answered as "not connected yet".
- The router recognises "drive", "gdrive" and "my drive" as Drive requests. 8 new labelled Drive
  requests: first run 8/8 routes and 6/7 tools (the miss was "gdrive"); after the fix, all pass, and
  the earlier 82 requests still all pass with the larger registry.

## Word files from Drive, and Google Calendar, read-only (17 October 2026)

- Drive now reads Word (.docx) files too (standard library only, hostile files refused).
- Second connector: Google Calendar, read-only (events on a day or range, what's coming up). It needs
  the Calendar API enabled and a sign-in with `--calendar` (Agent Hub README, "Google").
- 8 labelled Calendar requests, written before measuring: 8/8 routes and tools on the first run.
  "Not connected yet" is still reported when the Hub doesn't offer Calendar tools.

