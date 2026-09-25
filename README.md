# JARVIS

A personal voice assistant for Windows, built on the Gemini Live API. The full README comes in Phase 10.

## Setup

1. Python 3.12 and [uv](https://docs.astral.sh/uv/).
2. `copy .env.example .env` and add your `GOOGLE_API_KEY` (free from Google AI Studio).
3. `uv sync` (add `--extra wakeword` if you want "hey jarvis").
4. `uv run pytest`
5. `uv run main.py` to talk, or `uv run main.py --text` to type.

## Listening modes (`listening.mode` in config.yaml)

| Mode | How it works |
|---|---|
| `always_on` | Mic streams all the time; Gemini detects when you speak. Interrupt it just by talking. |
| `push_to_talk` | Hold `ctrl+alt+space` while speaking. Pressing it while JARVIS talks interrupts it. |
| `wake_word` | Say "hey jarvis" (detected offline). Needs `features.wake_word: true` and `uv sync --extra wakeword`. Goes back to sleep after 15 s of quiet. |

Hotkeys: `ctrl+alt+m` mute/unmute, `ctrl+alt+s` stop it talking. Change them under `hotkeys:`.

## Why headphones

With speakers, the mic hears JARVIS's own voice. In `always_on` mode that sounds like you
interrupting, so it cuts itself off. Headphones fix it. Without them, use `push_to_talk`
or set `audio.mute_mic_while_speaking: true` (you then can't interrupt by voice).

## Safety model (Phase 2)

Every tool declares a tier; `core/dispatcher.py` enforces it, not the prompt.

- **SAFE** runs at once. **CONFIRM** waits for you. **BLOCKED** is never shown to the model.
- For a CONFIRM action, the app writes the summary from the real arguments and JARVIS reads it out.
  It runs only if **you** say `confirm` / `yes` / `yes do it` / `go ahead` after JARVIS has finished
  speaking, type it in `--text` mode, or press `ctrl+alt+y`. Anything else, `ctrl+alt+n`, the stop
  key, or 20 s of silence cancels it. Speech heard while JARVIS talks (plus 400 ms) is ignored, so its
  own voice can't confirm.
- Web, clipboard and screen content is wrapped as `<untrusted_content>`. After such a tool runs,
  every tool that changes anything needs confirmation until you speak again.
- Every tool call is logged to `logs/actions.jsonl` (secrets redacted, long values cut to 200 chars).
- `--dry-run` logs actions instead of doing them.

Adding a capability: create `actions/<name>.py` with `register(registry, config)` that registers
`Tool(name, description, func, tier, parameters, read_only=..., untrusted_output=..., summarize=...)`,
and list it in `actions/__init__.py`.

## System controls (Phase 3)

- **Apps:** "open calculator", "open VS Code"... Only names under `apps.allowlist` (or its `aliases`)
  in config.yaml can be opened; anything else is refused, never guessed. Add an app by adding
  `friendly name: program.exe` (a name Windows knows, or a URL).
- **Websites:** sites on `sites.read_allowlist` open straight away; any other site asks first.
  Only http/https links can be opened.
- **Status:** "what's my battery?" gives battery, CPU and memory.
- **Volume and media:** "volume to 30", "pause the music", "next song".

## Web research (Phase 4)

- "Research X": JARVIS searches, reads up to 3 pages, answers in a few sentences, and offers to save
  longer notes to `Documents\jarvis\` (asks first; never overwrites).
- Search uses DuckDuckGo with no key, or Tavily if `TAVILY_API_KEY` is set (falls back to DuckDuckGo
  if Tavily fails). Page summaries are cached for 24 h in `data/cache/`.
- "What's the weather in <city>?" uses Open-Meteo (no key). Set `weather.default_city` to skip the city.
- Text sub-tasks use `models.flash`, then `models.flash_fallbacks`, then OpenRouter if
  `OPENROUTER_API_KEY` and `models.openrouter` are set. If all are busy JARVIS says
  "I've hit my usage limit". Free-tier limits are per account: see aistudio.google.com/rate-limit.
- Local and private addresses (localhost, 192.168.x.x) are never fetched.

## Browser control (Phase 5)

JARVIS drives your installed Google Chrome with **its own profile** (`data/browser_profile`), never
your everyday Chrome profile.

**One-time sign-in (for YouTube Premium):** run `run.bat --browser-login`, sign in with your own
Google account, pick your own YouTube profile, choose **No thanks** to sync and to saving passwords,
then close that window. Close it before starting JARVIS: one profile can't be open twice.

Tools: `play_youtube` (search and play the Nth video, no confirmation), `site_search`,
`browser_open`, `read_current_page`, and `browser_task` for short multi-step jobs.

Privacy rules, enforced in code (`core/privacy.py`):
- Password, payment, card, CVV, OTP, PIN, UPI and IBAN fields are never typed into, clicked or read;
  the planner model never even sees them. Chrome's password saving and card/address autofill are off
  in this profile.
- Account pages can't be opened: Google account, account chooser, Families, Pay, Gmail, Drive,
  Photos, Passwords, YouTube Studio, YouTube account settings, channel switcher, memberships.
  Navigation to them is aborted inside the browser, whoever triggers it.
- "Switch account", "Add account", "Sign out", "Manage your Google Account" and similar controls
  are never clicked, so other Family-plan members' profiles stay out of reach.
- Emails and phone numbers are scrubbed from page text before any model sees it; form values are
  never read.
- In `browser_task`: links and play buttons on the same site run straight away; typing (only on
  `sites.action_allowlist`), other clicks and leaving the site ask first.

## Music and app control (Phase 6)

- **"Play Believer by Imagine Dragons"**: JARVIS finds the song on YouTube Music (ytmusicapi, no login),
  picks the best title/artist match (originals over covers and remixes unless you ask), and opens it
  in your installed **YouTube Music app** (found automatically from its Start-menu shortcut). The
  previous app window is closed first so two songs never play at once. Playback is confirmed through
  Windows' media sessions. Without the app, songs play in JARVIS's browser instead.
- **"Pause" / "next" / "previous" / "what's playing?"** control the YouTube Music session directly
  (so "pause" really pauses, instead of toggling), falling back to the media keys.
- **"Close Notepad"**: `close_app` asks first, then closes like clicking X (apps can ask to save).
- **app_ui_action** (rung 4, UI Automation) for apps with no other tool: JARVIS plans the exact steps,
  reads them to you, and runs them only after you confirm. It never reads text inside apps, never
  touches password boxes, account, settings or destructive controls, and won't type into a document
  that looks like it holds secrets (`.env`, keys, passwords...) unless the plan opens a new tab first.

## Screen and clipboard (Phase 7)

- **"What's on my screen?"**: one frame, only when you ask, shrunk and sent to Flash. It is never
  saved or logged. Windows that look private (`.env`, keys, passwords, banking, password managers)
  are **blacked out** before the image leaves your PC, and JARVIS tells you when it hid something.
  "What window is in front?" reads the title (hidden if it looks private).
- **Copied text**: "summarise / explain / translate what I copied into Hindi". Emails and phone
  numbers are replaced with placeholders before the model sees the text, then put back.
  **"Fix what I copied"** writes the corrected text, reads you a preview, and only replaces your
  clipboard after you confirm (and not if you've copied something else meanwhile).
- If the clipboard looks like it holds a secret (API keys, private keys, `KEY=value` lines, card
  numbers, long random tokens), JARVIS refuses to send it anywhere.

## Agent mode (Phase 8)

For goals that need several tools in a row ("research three budget headphones, open the best one's
product page and tell me the price"):

1. JARVIS drafts a plan (max 10 steps) using only its real, allowed tools; code checks every step.
   Web addresses it can't know yet must be **blanks** ("the product page found in step 3"); a guessed
   address is rejected.
2. It reads the numbered plan to you and waits for **one approval**.
3. The approved plan is **frozen**: pages, the screen or the clipboard can't add, remove, reorder or
   change steps. Blanks are filled from earlier results (values only), and if a filled value fails
   (e.g. a site that blocks readers) it may pick another value up to twice, for the same step.
4. Every step keeps its own safety rules: a site off your trusted list, saving a file, or typing
   still asks you mid-plan.
5. The first failure stops the plan and JARVIS asks what to do. A new plan needs a new approval
   (one re-plan per request).

Sites that refuse plain downloads (403) are retried once in a throwaway headless Chrome with no
profile; some sites block all automated readers, and JARVIS says so.

## Memory, reminders and briefing (Phase 9)

- **Memory** (`data/memory.json`, max 100 facts): "remember that…", "what do you remember about…",
  "forget…". JARVIS stores only what you explicitly ask. If your words didn't ask it to remember, or
  it has just read a page, the screen or the clipboard, it asks you first. Forgetting always asks and
  names the exact fact. Passwords, PINs and payment details are refused. Remembered facts are added to
  JARVIS's instructions at each start; "call me Krishna" style facts set the name it uses. The Flash
  planner and browser never see memory.
- **Reminders** (`data/reminders.sqlite`): "remind me in 20 minutes to…" or "at 5:30 pm…". When due,
  JARVIS says it aloud (waking up if asleep) and shows a Windows notification. They survive a
  restart; one that came due while JARVIS was closed is delivered at the next start and flagged as
  late. "What reminders do I have?" and "cancel the dentist reminder" (asks first). A reminder's
  text is only read out: it can't trigger an action without your confirmation.
- **Morning briefing** (off by default: `features.morning_briefing: true`): on the first launch of
  the day, a spoken summary under 45 seconds of the date, weather (needs `weather.default_city`),
  three headlines (`briefing.news_query`) and today's reminders.

Wake word: `--mic-test` scores your voice at several gains (`wake_word.gain`) and suggests one;
`--mic "Nord Buds"` tests or uses another microphone. `wake_word.chime_volume` sets the wake beep.
