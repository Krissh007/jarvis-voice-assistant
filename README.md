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
