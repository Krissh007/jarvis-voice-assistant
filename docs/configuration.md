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

### Features

Each entry under `features:` switches a whole module on or off: `system`, `web`, `browser`,
`media`, `app_ui`, `screen`, `clipboard`, `memory`, `reminders`, `agent_mode`, `morning_briefing`
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
| `--browser-login` | Open JARVIS's Chrome profile normally, to sign in to YouTube once |
| `--install-shortcut` | Create the desktop icon, which starts JARVIS as an app (no terminal) |

`run.bat` passes any of these through, for example `.\run.bat --mic-test`.
