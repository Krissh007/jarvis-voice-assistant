# Configuration

Settings live in `config.yaml`, which is commented throughout, and secrets live in `.env`, which is
never committed. Restart JARVIS after changing either file.

The app's **Settings** page (orb → App, or the chat window's Settings button) changes a fixed set of
them without editing `config.yaml`: the reply voice, listening mode and timing, the models, memory
limits, the hotkeys and the orb's look. Voice, Announce and the orb's look apply at once; the rest are
saved in `data/settings.json`, laid over `config.yaml` at the next start, and marked "Restart to apply"
until then. Each value is checked (range, model-id shape, hotkey clashes) before it's kept, and nothing
else in `config.yaml` can be changed that way. Delete `data/settings.json` to go back to `config.yaml`
alone. Keys and tokens are shown there only as set or not set.

## `.env`

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | yes | Gemini Live and Gemini Flash |
| `TAVILY_API_KEY` | no | Tavily web search (falls back to DuckDuckGo) |
| `OPENROUTER_API_KEY` | no | Fallback text model; set `models.openrouter` too |
| `GROQ_API_KEY` | no | The reasoning layer's model (`ask_brain`), free at console.groq.com; without it `ask_brain` uses Gemini Flash |
| `AGENT_HUB_TOKEN` | with `features.agent_hub` | Must equal `HUB_TOKEN` in the Agent Hub's own `.env` |

Values that are left as comments or blank are ignored.

## `config.yaml`

### You and the voice

| Setting | Default | Meaning |
|---|---|---|
| `user.name` | `Sir` | How JARVIS addresses you. A remembered "call me …" fact overrides it. |
| `voice.name` | `Charon` | JARVIS's speaking voice, one of Gemini Live's 30 voices. Change it in Settings (with a Preview button) or by voice ("switch to Kore", "let me hear Puck"): it applies at once, the conversation carries on, and it's kept for the next start. Previews are cached in `data/voice_previews/` |

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
| `wake_word.strict_margin` | `0.15` | Extra score the voice model needs when the stock model barely reacts (under 0.05): with `0.15` it needs 0.65 (raised from 0.10 on 23 October 2026, after normal speech scored 0.63–0.66 and woke JARVIS). Lower wakes on more first tries but wakes more often on other speech; see the table below |
| `wake_word.gain` | `1.0` | Boosts the mic for wake detection only (`--mic-test` suggests a value) |
| `wake_word.active_window_s` | `60` | After waking, JARVIS keeps listening until this many seconds pass with nothing from you (speech or typing; the count waits while JARVIS is still talking). "Go to sleep", "stop listening", "that's all" or "goodbye" end it at once, and so does a bare "stop" when JARVIS isn't talking or working. Also in Settings, applied at once |
| `wake_word.chime_volume` | `0.06` | Wake beep loudness (`0` is silent) |
| `listening.stall_restart_s` | `10` | You said something (3+ words) but Gemini stayed silent this long, with no action running or waiting for your yes: JARVIS reconnects with a recap and repeats your request. `0` turns it off |
| `browser.step_timeout_s` | `20` | One step in JARVIS's browser (scroll, click, read) gives up after this many seconds |
| `browser.task_timeout_s` | `120` | A whole multi-step `browser_task` gives up after this many seconds. While a browser job runs, a new browser request is answered "still busy" at once instead of waiting |
| `wake_word.skip_after_wake_ms` | `500` | Mic audio not sent to Gemini right after waking. It's the end of "hey jarvis", which on its own was transcribed as foreign words |
| `noise_suppression.enabled` | `true` | Clean background noise from what Gemini hears |
| `noise_suppression.strength` | `1.0` | 0 to 1; lower keeps more of the original sound |
| `ducking.enabled` | `true` | Lower other apps while JARVIS is awake |
| `ducking.level` | `0.3` | Other apps play at this fraction of their normal volume |
| `audio.input_device` | `null` | Windows default, or part of a device name |
| `audio.echo_mode` | `auto` | `auto`, `headphones` or `speakers` |
| `audio.transcription_languages` | `[en-IN]` | Language hints for transcribing your speech |
| `audio.mute_mic_while_speaking` | `false` | Force the speaker-style echo gate |
| `audio.echo_tail_ms` | `400` | Audio ignored after JARVIS stops speaking |

**Choosing `wake_word.strict_margin`.** Measured on 17 October 2026 with the saved recordings in
`data/wake/` (48 "hey jarvis", 132 s of other speech). "Unseen" uses voice models trained without
the clip being tested (4-fold, as `--train-wake` reports); "current model" uses the trained
verifier, which has heard every clip, so it flatters both columns. False wakes are counted in 132 s
of nonstop talking, a harsher test than a normal room. With `verifier_threshold: 0.5`:

| `strict_margin` | Needs (stock ~0) | Unseen: wakes | Unseen: false wakes | Current model: wakes | Current model: false wakes |
|---|---|---|---|---|---|
| `0.15` | 0.65 | 41 of 48 | 7 | 46 of 48 | 3 |
| `0.10` | 0.60 | 42 of 48 | 11 | 46 of 48 | 4 |
| `0.05` | 0.55 | 43 of 48 | 12 | 47 of 48 | 5 |
| `0` | 0.50 | 43 of 48 | 12 | 47 of 48 | 8 |

In live use on 17 October, genuine first tries that missed at `0.15` scored 0.53, 0.57, 0.61 and
0.64: `0.10` would have woken on two of them, `0.05` on three. Raising `verifier_threshold` instead
does no better at the same false-wake count. Recording more of your own speech with `--train-wake`
is what lowers both columns.

### Window, tray and hotkeys

| Setting | Default | Meaning |
|---|---|---|
| `web_ui.port` | `0` | The web window's port on 127.0.0.1 (`0` = any free port). Every connection needs this launch's random token |
| `web_ui.window` | `true` | Show the orb: tap = menu (Chat, Talk, Command centre), hold = talk, double-tap = mute or unmute, drag anywhere. Its colour: blue listening, amber working, grey muted, red when something JARVIS needs isn't working (the reason shows under it); `false` = tray only |
| `web_ui.orb_size` | `112` | How big the orb looks, in pixels (70–260) |
| `web_ui.core_size` | `0.83` | The arc reactor core's size as a share of the cloud's (0.4–1.0) |
| `web_ui.orb_brightness` | `0.55` | How bright the orb's arc reactor core and its nucleus glow, 0.2 (faint) to 1.0 (bright) |
| `web_ui.cloud_brightness` | `0.45` | How bright the cloud of light points around the core glows, on its own, 0.2 to 1.0 |
| `web_ui.ribbon_brightness` | `0.5` | How bright the glowing Möbius ribbons around the cloud are, 0.2 to 1.0 |

The five orb settings above are defaults. The **Customize** button in the control centre has a slider for
each; they change the orb live, and your choices are kept in `data/orb_look.json`, which wins over
`config.yaml`. Reset goes back to the `config.yaml` values.
| `web_ui.caption_seconds` | `8` | Voice mode: a reply's caption under the orb fades after this many seconds (2–60) |
| `web_ui.quick_actions` | 4 examples | Buttons in the control centre; each sends its text to JARVIS (up to 8, 80 characters) |
| `web_ui.chat_width` / `web_ui.chat_height` | `1100` / `760` | The chat page's window the first time; then it remembers |
| `web_ui.remote.enabled` | `false` | The phone app through Tailscale ([mobile.md](mobile.md)). On = the server uses `web_ui.remote.port` and accepts `web_ui.remote.host` |
| `web_ui.remote.host` | `""` | This PC's Tailscale name (MagicDNS + HTTPS on), e.g. `mypc.tail1234.ts.net`; a plain host name, no scheme, port or path |
| `web_ui.remote.port` | `47821` | The fixed local port `tailscale serve` forwards HTTPS to (still 127.0.0.1 only) |
| `web_ui.app_width` / `web_ui.app_height` | `1240` / `800` | The app's window (sidebar: Chat, Command centre, Memory, Settings) the first time; then it remembers |
| `web_ui.voice_replies` | `true` | Starting value of the **Voice** button (off = text-only replies); the button's choice is kept in `data/prefs.json` |
| `web_ui.announce` | `true` | Starting value of the **Announce** button (off = agent results and reminders shown, not spoken); kept in `data/prefs.json` |
| `web_ui.always_on_top` | `true` | Keep the orb above other windows |
| `web_ui.close_button` | `quit` | Closing the orb (Alt+F4) quits JARVIS, or `hide` keeps it running in the tray |
| `window.always_on_top` | `true` | The older Tk window (`features.window`, used only when `web_ui` is off): keep it above others |
| `window.opacity` | `1.0` | Below 1 lets windows behind show through |
| `window.close_button` | `quit` | The X quits JARVIS, or `hide` keeps it running in the tray |
| `window.speak_typed_replies` | `true` | Answers to typed messages are also spoken (the starting value of "Speak replies" in either window) |
| `hotkeys.*` | see file | `show_orb` (Ctrl+Alt+Space: show or hide the orb), `push_to_talk` (Ctrl+Alt+T), `mute`, `stop`, `confirm`, `cancel`. Change them on the app's Settings page (after a restart) or here |

### Confirmation

| Setting | Default | Meaning |
|---|---|---|
| `confirm.accept_phrases` | yes, yeah, sure, okay, confirm, go ahead, do it, haan, avunu… | What counts as "yes". Echoing the action also counts ("open it" for "Open figma.com"). A clear no, "wait", or a longer sentence cancels; a short reply that is neither (often a mis-heard "yeah") is asked again, up to twice |
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
| `file_edit.folders` | Folders whose files JARVIS may create, change and delete (default `desktop`, `documents`, `downloads`) |
| `file_edit.extra_folders` | More, e.g. `["E:/Projects"]` (JARVIS's own folder is always excluded) |
| `file_edit.max_kb` | Largest file read or written (default `200`) |
| `file_edit.backups` | Keep each file's old version in `data/file_backups` before changing it (default `true`) |
| `file_edit.editor` | `code` opens files in VS Code when `write_file` has `open`; empty = the file's usual app |
| `chat_apps.max_chars` | Longest chat reply `read_app what=latest` and `wait_for_app` return; its start is kept (default `6000`) |
| `chat_apps.profiles.<exe>` | Where an app's messages are in its accessibility tree: `container_role`, `message_role`, `message_name` (number and total), `streaming_name`, `roles` (text label → role), `reply_role`, `skip_inside` (`button`, `status`, `menu`, `toolbar`) and `drop_text`. Shipped for `claude.exe` (Claude's chat view); a profile that doesn't match logs a warning (once per window per session) and JARVIS reads the whole window |
| `condense.over_chars` | Results from `read_app`, `wait_for_app` and `job_search_status` longer than this (default `1500` characters) are shortened before the Live model sees them |
| `condense.max_words` | Length of that summary (default `120` words) |
| `condense.model` | Model tried first for it (default `gemini-3.5-flash-lite`, to spare the main Flash model's daily quota) |
| `condense.timeout_s` | If summarising takes longer (default `5` s), the start and end of the text are used instead |
| `groq.models` | Groq models for `ask_brain`, tried in order (default `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, then `openai/gpt-oss-20b`, which also classifies unclear requests; free tier: 1000 requests a day and 8000 tokens a minute each). A model Groq retires is skipped for an hour and logged |
| `groq.timeout_s` / `groq.max_tokens` | Per model call: time limit (default `15` s) and longest reply (default `700` tokens) |
| `groq.rpm` | Requests per minute per model, kept under on this side (free tier: 30) |
| `brain.max_steps` | Model calls per request (default `6`); then one last answer without tools |
| `brain.budget_s` | Time for a whole request (default `45` s), not counting time waiting for your yes |
| `brain.max_tool_calls_per_step` | Tool calls the model may make at once (default `4`) |
| `brain.memory` | Offer read-only `recall` on every `ask_brain` run (default `true`); what it finds is sent to the Groq model answering |
| `brain.result_chars` | Longest tool result the model sees (default `3000` characters) |
| `reasoning.thinking` | Thinking level for `think_through` calls only (default `high`); the models are the Flash chain |
| `reasoning.max_tokens` | Longest `think_through` answer before it's cut (default `2000`) |
| `desktop.max_items` | Buttons, fields and lines `read_app` reads from one window (default `150`) |
| `desktop.max_typed` | Longest text `type_in_app` types in one go (default `2000`); also the limit for any terminal |
| `desktop.max_typed_editor` | The limit into a code editor such as VS Code (default `10000`), so a whole file goes in one paste |
| `desktop.repeat_guard_s` | The same text into the same window again within this many seconds (default `10`) is a repeated call, so it isn't typed twice |
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
| `memory.embeddings` | `true` | Search memory by meaning with the on-device model from `--setup-memory`; `false` (or no model) = keyword search only |
| `memory.log_conversations` | `true` | Keep what you and JARVIS said (finished lines only; never tool output, web pages, screen or clipboard text, or a line that looks like a password, PIN or card number), so `recall` can answer "what did we talk about yesterday?". "Don't remember this conversation" pauses it until the next new chat |
| `memory.keep_conversations_days` | `30` | Conversation lines older than this are deleted at start and once a day |
| `briefing.news_query` | `top news today` | Headline search for the morning briefing |
| `agent_hub.url` | `http://127.0.0.1:8765/mcp` | The Agent Hub's MCP address (this computer only). The bearer token is `AGENT_HUB_TOKEN` in `.env` and must equal the Hub's `HUB_TOKEN` |
| `agent_hub.poll_s` | `5` | How often JARVIS looks for finished results |
| `agent_hub.request_timeout_s` | `8` | Per call to the Hub |

### Features

Each entry under `features:` switches a whole module on or off: `system`, `web`, `browser`,
`reasoning` (`think_through`; off, `brain` replaces it), `brain` (`ask_brain`, the reasoning layer), `media`, `app_ui` (off by default), `screen`, `clipboard`, `files`, `desktop`, `memory`, `reminders`, `job_search`, `agent_hub` (needs the Hub running), `agent_mode`, `morning_briefing`
(off by default), `tray`, `web_ui` (the orb control centre and the chat page), `window` (the
older Tk window, off; used only when `web_ui` is off) and `wake_word`. `test_tools` exposes two tools used only to
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
| `--route "REQUEST"` | Show which route and tools the reasoning layer would pick (no model is called) |
| `--setup-memory` | Download the on-device memory model (all-MiniLM-L6-v2, ~90 MB, pinned and hash-checked) into `data/models/` and embed your saved facts |
| `--ask "REQUEST"` | Run one request through `ask_brain` in the terminal; confirmations are shown, never given |
| `--install-shortcut` | Create the desktop icon, which starts JARVIS as an app (no terminal) |

`run.bat` passes any of these through, for example `.\run.bat --mic-test`.
