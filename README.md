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
