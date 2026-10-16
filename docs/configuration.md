# Configuration

Settings live in `config.yaml`, which is commented throughout, and secrets live in `.env`, which is
never committed. Restart JARVIS after changing either file.

## `.env`

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | yes | Gemini Live and Gemini Flash |
| `TAVILY_API_KEY` | no | Tavily web search (falls back to DuckDuckGo) |
| `OPENROUTER_API_KEY` | no | Fallback text model; set `models.openrouter` too |

Values that are left as comments or blank are ignored.

## `config.yaml`

### You and the voice

| Setting | Default | Meaning |
|---|---|---|
| `user.name` | `Sir` | How JARVIS addresses you. A remembered "call me …" fact overrides it. |
| `voice.name` | `Charon` | Gemini prebuilt voice (e.g. Puck, Kore, Charon) |

### Models

| Setting | Meaning |
|---|---|
| `models.live` | Gemini Live model for the conversation |
| `models.flash` | Text model for summaries, planning and vision |
| `models.flash_fallbacks` | Tried in order when `flash` is overloaded or out of quota |
| `models.thinking` | Thinking level per text model (lower is faster) |
| `models.openrouter` | OpenRouter model id, used only with `OPENROUTER_API_KEY` |

### Listening and audio

| Setting | Default | Meaning |
|---|---|---|
| `listening.mode` | `wake_word` | `wake_word`, `always_on` or `push_to_talk` |
| `listening.end_pause_ms` | `1000` | How long a pause means you've finished speaking. Lower answers sooner, but a mid-sentence pause may cut you off; `null` uses Gemini's default |
| `wake_word.threshold` | `0.25` | Stock-model score needed to wake (lower wakes more easily) |
| `wake_word.personal_verifier` | `true` | Use `data/wake/…_verifier.pkl` from `--train-wake` when it exists |
| `wake_word.verifier_threshold` | `0.5` | Score needed when the personal verifier is in use |
| `wake_word.gain` | `1.0` | Boosts the mic for wake detection only (`--mic-test` suggests a value) |
| `wake_word.active_window_s` | `15` | Seconds of quiet before JARVIS goes back to sleep |
| `wake_word.chime_volume` | `0.06` | Wake beep loudness (`0` is silent) |
| `noise_suppression.enabled` | `true` | Clean background noise from what Gemini hears |
| `noise_suppression.strength` | `1.0` | 0 to 1; lower keeps more of the original sound |
| `ducking.enabled` | `true` | Lower other apps while JARVIS is awake |
| `ducking.level` | `0.3` | Other apps play at this fraction of their normal volume |
| `audio.input_device` | `null` | Windows default, or part of a device name |
| `audio.echo_mode` | `auto` | `auto`, `headphones` or `speakers` |
| `audio.transcription_languages` | `[en-IN]` | Language hints for transcribing your speech |
| `audio.mute_mic_while_speaking` | `false` | Force the speaker-style echo gate |
| `audio.echo_tail_ms` | `400` | Audio ignored after JARVIS stops speaking |

### Window, tray and hotkeys

| Setting | Default | Meaning |
|---|---|---|
| `window.always_on_top` | `true` | Keep the window above others |
| `window.opacity` | `1.0` | Below 1 lets windows behind show through |
| `window.close_button` | `quit` | The X quits JARVIS, or `hide` keeps it running in the tray |
| `window.speak_typed_replies` | `true` | Answers to typed messages are also spoken (the right-click menu changes it until restart) |
| `hotkeys.*` | see file | `mute`, `push_to_talk`, `stop`, `confirm`, `cancel` |

### Confirmation

| Setting | Default | Meaning |
|---|---|---|
| `confirm.accept_phrases` | yes, yeah, sure, okay, confirm, go ahead… | What counts as "yes" |
| `confirm.timeout_s` | `20` | Seconds to answer, counted from when JARVIS finished reading the summary |

### What JARVIS may use

| Setting | Meaning |
|---|---|
| `apps.allowlist` | Friendly name to program (App Paths name or URL). Only these can be opened. |
| `apps.aliases` | Other names for the same apps (exact match; nothing is guessed) |
| `sites.read_allowlist` | Sites opened without asking |
| `sites.action_allowlist` | Sites where browser tasks may type |
| `browser.channel` / `browser.headless` | `chrome` (your installed Chrome) / show the window |
| `music.source` | `youtube_music_app`, falling back to JARVIS's browser |
| `paths.research_dir` | Where research notes are saved |
| `weather.default_city` | City used when you don't name one (also used by the briefing) |
| `files.folders` | Windows folders searched by name: `desktop`, `documents`, `downloads`, `pictures`, `music`, `videos` |
| `files.extra_folders` | More folders to search, e.g. `["E:/Projects"]` |
| `files.also_hide` | More name patterns never listed or opened, e.g. `["*bank*"]` (private-looking names are always hidden) |
| `files.max_results` | Matches listed per search (default `5`) |
| `chat_apps.max_chars` | Longest chat reply `read_app what=latest` and `wait_for_app` return; its start is kept (default `6000`) |
| `chat_apps.profiles.<exe>` | Where an app's messages are in its accessibility tree: `container_role`, `message_role`, `message_name` (number and total), `streaming_name`, `roles` (text label → role), `reply_role`, `skip_inside` (`button`, `status`, `menu`, `toolbar`) and `drop_text`. Shipped for `claude.exe` (Claude's chat view); a profile that doesn't match logs a warning (once per window per session) and JARVIS reads the whole window |
| `condense.over_chars` | Results from `read_app`, `wait_for_app` and `job_search_status` longer than this (default `1500` characters) are shortened before the Live model sees them |
| `condense.max_words` | Length of that summary (default `120` words) |
| `condense.model` | Model tried first for it (default `gemini-3.5-flash-lite`, to spare the main Flash model's daily quota) |
| `condense.timeout_s` | If summarising takes longer (default `5` s), the start and end of the text are used instead |
| `reasoning.thinking` | Thinking level for `think_through` calls only (default `high`); the models are the Flash chain |
| `reasoning.max_tokens` | Longest `think_through` answer before it's cut (default `2000`) |
| `desktop.max_items` | Buttons, fields and lines `read_app` reads from one window (default `150`) |
| `desktop.max_chars` | Text read from documents and text boxes, and again from long labels (default `4000`) |

### Limits

| Setting | Default | Meaning |
|---|---|---|
| `limits.flash.rpd` | `20` | Requests a day for the Flash model (from your account's limits) |
| `limits.research_pages_max` | `3` | Pages read per question |
| `limits.agent_steps_max` / `agent_replans_max` | `10` / `1` | Plan size and re-plans |
| `limits.browser_task_steps_max` | `10` | Steps per browser task |
| `limits.page_cache_ttl_h` | `24` | How long page summaries are cached |
| `memory.max_facts` | `100` | Memory size |
| `briefing.news_query` | `top news today` | Headline search for the morning briefing |
| `agent_hub.url` | `http://127.0.0.1:8765/mcp` | The Agent Hub's MCP address (this computer only). The bearer token is `AGENT_HUB_TOKEN` in `.env` and must equal the Hub's `HUB_TOKEN` |
| `agent_hub.poll_s` | `5` | How often JARVIS looks for finished results |
| `agent_hub.request_timeout_s` | `8` | Per call to the Hub |

### Features

Each entry under `features:` switches a whole module on or off: `system`, `web`, `browser`,
`reasoning`, `media`, `app_ui` (off by default), `screen`, `clipboard`, `files`, `desktop`, `memory`, `reminders`, `job_search`, `agent_hub` (needs the Hub running), `agent_mode`, `morning_briefing`
(off by default), `tray`, `window` and `wake_word`. `test_tools` exposes two tools used only to
test the safety layer; it's off, and the test suite turns it on itself.

## Command-line options

| Option | Does |
|---|---|
| *(none)* | Start JARVIS |
| `--text` | Type instead of talk |
| `--dry-run` | Log actions instead of performing them |
| `--mic-test` | Show mic level, wake-word scores at several gains, and background noise before and after cleaning |
| `--mic NAME` | Use a different microphone for this run (part of its name) |
| `--train-wake` | Record your voice and train the personal wake-word verifier |
| `--bench-apps` | Measure how fast JARVIS reads each open app, and how many buttons and fields it finds (read-only; shows no app text) |
| `--browser-login` | Open JARVIS's Chrome profile normally, to sign in to YouTube once |
| `--install-shortcut` | Create the desktop icon, which starts JARVIS as an app (no terminal) |

`run.bat` passes any of these through, for example `.\run.bat --mic-test`.
