# Developer guide

- [Setup](#setup)
- [Running the tests](#running-the-tests)
- [Adding a capability](#adding-a-capability)
- [Conventions](#conventions)
- [Testing a new tool](#testing-a-new-tool)
- [Debugging](#debugging)

## Setup

```bash
pip install --user uv
python -m uv sync --extra wakeword
copy .env.example .env      # add GOOGLE_API_KEY
python -m uv run pytest
```

- The project uses Python 3.12 and a flat layout (`core/`, `actions/`, `tests/`), managed by uv
  with `package = false`.
- Dependencies are locked in `uv.lock`, resolved for Windows only.
- `pyrnnoise` is used only for its bundled `rnnoise.dll`. Its heavy extras are excluded in
  `[tool.uv] override-dependencies`, and `core/denoise.py` loads the DLL directly.

## Running the tests

```bash
python -m uv run pytest            # everything (about a minute)
python -m uv run pytest tests/test_safety.py -q
```

- **Offline:** a fake Gemini Live session (`tests/fakes/fake_live.py`) scripts tool calls,
  transcripts and playback events against the real dispatcher.
- **Browser tests:** these run your installed Chrome headless against local fixture pages,
  including an injection page and a password-field page.
- **Some tests use real Windows APIs.** They create shortcuts in a temp folder, take a named mutex
  and event, and use UI Automation. They never touch your pinned taskbar items or a running JARVIS.
- **Low disk space on C:?** Pass `--basetemp` to put temp files on another drive:
  `pytest -p no:cacheprovider --basetemp E:\tmp`.

## Adding a capability

A capability is one file in `actions/` that registers its tools. Here's a complete example:

```python
# actions/notes.py
from pathlib import Path

from core.registry import Registry, Tier, Tool, ToolError


def register(registry: Registry, config: dict, services) -> None:
    notes = Path(services.data_dir) / "notes.txt"

    def read_notes() -> str:
        """Read the notes file. Rung 2 (local file). Tier SAFE, read-only."""
        return notes.read_text(encoding="utf-8") if notes.exists() else "There are no notes yet."

    def add_note(text: str) -> str:
        """Append a note. Rung 2 (local file). Tier CONFIRM."""
        if not text.strip():
            raise ToolError("What should the note say?")
        with notes.open("a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        return "Added the note."

    registry.register(Tool(
        name="read_notes",
        description="Read the user's notes.",   # the model reads this: say when to use it
        func=read_notes,
        tier=Tier.SAFE,
        read_only=True,                         # changes nothing, so taint never escalates it
    ))
    registry.register(Tool(
        name="add_note",
        description="Add a note for the user.",
        func=add_note,
        tier=Tier.CONFIRM,
        summarize=lambda a: f'Add a note saying: "{str(a.get("text", ""))[:200]}"',  # read aloud before confirming
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    ))
```

Then:
1. Add `"actions.notes": "notes"` to `MODULES` in `actions/__init__.py`, before `agent_mode`,
   which must stay last.
2. Add `notes: true` under `features:` in `config.yaml`.
3. Add tests in `tests/test_notes.py` (see [Testing a new tool](#testing-a-new-tool)).

### Choosing the `Tool` fields

| Field | Use it for |
|---|---|
| `tier` | `SAFE` only if it can't cause harm or cost. Anything that writes, sends, types, closes or deletes is `CONFIRM`. Never register something that should be `BLOCKED`. The tier can be a function of the arguments, e.g. SAFE for allowlisted sites. |
| `read_only=True` | The tool changes nothing at all, so it's never escalated by taint. |
| `untrusted_output=True` | The output contains outside content (web, screen, clipboard, files others wrote). The dispatcher then wraps it and taints the turn. |
| `summarize` | Writes the confirmation text from the real arguments. Without it, a generic summary is used. |
| `self_gated=True` | A multi-step tool that applies its own per-step tiers and returns `NeedsConfirmation` for risky steps (like `browser_task`). |
| `rung` | Documentation of how the tool acts: 1 HTTP, 2 local OS or API, 3 browser, 4 UI Automation. |

**Shared services** (`actions.Services`) give a module:
- `llm`: Flash calls with fallbacks;
- `data_dir`;
- `browser`: JARVIS's Chrome;
- `dispatcher`: used by agent mode;
- `web`, `memory`, `reminders` and `ducker`.

## Conventions

- **Tools never crash the session.** Return a short, *speakable* string, including on failure:
  one or two sentences, no markdown or URLs. `raise ToolError("…")` when the message itself is what
  the user should hear. Log the full error.
- **Blocking work** runs in a thread automatically. Use `async def` for async tools.
- **Docstrings** on every tool, stating the rung and tier.
- **Untrusted text** that goes into a Flash prompt is wrapped with `core.untrusted.wrap()`.
- **No secrets** in code, logs or prompts. `core.privacy.secret_in_text()` and `redact()` help.
- **The model never approves anything.** Anything the user must agree to goes through a CONFIRM
  tier or a `NeedsConfirmation`, never a prompt instruction.
- Type hints and small functions. Match the surrounding style.

## Testing a new tool

Use the safety harness to drive the real dispatcher:

```python
from actions import Services
from tests.fakes import fake_live as fl


async def test_add_note_needs_confirmation(tmp_path):
    h = fl.SafetyHarness(tmp_path, config={"features": {"notes": True}}, services=Services(data_dir=tmp_path))
    resp = await h.model_calls("add_note", text="buy milk")
    assert resp["status"] == "awaiting_confirmation"
    assert resp["summary"] == 'Add a note saying: "buy milk"'
    await h.speak_summary()        # JARVIS reads it; playback ends
    await h.user_says("yes")       # your voice, after playback
    assert (tmp_path / "notes.txt").read_text() == "buy milk\n"
    assert h.audit()[-1]["confirmation"] == "confirmed_voice"
```

The harness also offers:
- `h.advance(seconds)` for timeouts;
- `h.dispatcher.on_hotkey("confirm")` for the confirm key;
- a call to `test_fetch_untrusted` first, to check how the tool behaves in a tainted turn.

## Debugging

- **Run it in a terminal:** `.\run.bat` shows tool calls (`[tool] …`), confirmations
  (`[confirm?] …`), wake and sleep, and near-miss wake words.
- **Type to it:** `.\run.bat --text` makes the conversation reproducible, and it exits when input
  ends, so you can pipe commands in.
- **Logs:**
  - `logs/app.log`: everything, including shutdown timing and button clicks.
  - `logs/actions.jsonl`: the audit log.
  - `logs/console.txt`: terminal output when JARVIS runs from an icon.
- **The microphone:** `.\run.bat --mic-test`, and `--mic NAME` to try another one.
