"""The app's Settings page: what may be shown and changed, and where changes are kept.

config.yaml stays the hand-written source (its comments are documentation, so nothing rewrites it).
Changes made in the app are kept in data/settings.json as dotted keys ("hotkeys.mute": "ctrl+alt+m")
and laid over config.yaml when JARVIS starts (apply_overlay, called from main.py). Only the keys in
SETTINGS can be changed, each checked by kind and range, so the file can't switch on a feature or
anything else the page doesn't offer.

Two groups take effect at once and are kept where they already live: the Voice and Announce buttons
(data/prefs.json, "prefs.*") and the orb's look (data/orb_look.json, "look.*"); the runtime applies those.

Keys and tokens are never read out: the page only learns whether each one is set (secret_status).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CATEGORIES = [  # (id, label, Lucide icon), in the page's order
    ("voice", "Voice", "mic"),
    ("models", "Models", "cpu"),
    ("memory", "Memory", "brain"),
    ("hotkeys", "Hotkeys", "keyboard"),
    ("connections", "Agents and connections", "plug"),
    ("appearance", "Appearance", "palette"),
]
# Env names the page may report as set / not set. Their values never leave the backend.
SECRET_NAMES = ("GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY", "AGENT_HUB_TOKEN", "OPENROUTER_API_KEY")
# The Live API's prebuilt voices (docs, checked 2026-10-19).
LIVE_VOICES = ("Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe", "Callirrhoe", "Charon",
               "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus",
               "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
               "Vindemiatrix", "Zephyr", "Zubenelgenubi")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,79}$")
MAX_LIST = 6


@dataclass(frozen=True)
class Setting:
    key: str              # dotted path in config.yaml; "prefs.*" and "look.*" are applied by the runtime
    category: str
    label: str
    kind: str             # bool | int | float | choice | hotkey | model | models | info
    help: str = ""
    applies: str = "restart"  # "now" or "restart"
    choices: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    step: float | None = None
    editable: bool = True


SETTINGS: tuple[Setting, ...] = (
    # Voice
    Setting("prefs.voice", "voice", "Voice replies", "bool", "Off: JARVIS answers in text only and never speaks.", "now"),
    Setting("prefs.announce", "voice", "Announce", "bool",
            "Speak agent results, reminders and the morning briefing. Off: shown here and as a notification.", "now"),
    Setting("voice.name", "voice", "Reply voice", "choice", "One of Gemini Live's voices.", choices=LIVE_VOICES),
    Setting("listening.mode", "voice", "Listening", "choice",
            "Wake word: say \"hey Jarvis\". Push to talk: hold the key. Always on: everything you say.",
            choices=("wake_word", "push_to_talk", "always_on")),
    Setting("listening.end_pause_ms", "voice", "Pause before replying (ms)", "int",
            "How long a quiet moment means you've finished. Shorter replies faster but can cut you off.",
            low=400, high=3000, step=100),
    Setting("wake_word.chime_volume", "voice", "Wake chime volume", "float", "0 = silent.", low=0, high=0.25, step=0.01),
    # Models
    Setting("models.live", "models", "Voice model (Gemini Live)", "model", "Talks with you and calls tools."),
    Setting("models.flash", "models", "Text model (Gemini Flash)", "model",
            "Screen reading, summaries and the fallback when the reasoning models are busy."),
    Setting("models.flash_fallbacks", "models", "Text model fallbacks", "models",
            "Tried in order when the text model is overloaded."),
    Setting("groq.models", "models", "Reasoning models (Groq)", "models",
            "ask_brain tries these in order; the last one also sorts unclear requests."),
    Setting("condense.model", "models", "Shortening model", "model", "Shortens long tool results before the voice model reads them."),
    # Memory
    Setting("memory.max_facts", "memory", "Most facts to remember", "int",
            "remember refuses new facts once this many are kept.", low=10, high=500, step=10),
    Setting("memory.embeddings", "memory", "Search memory by meaning", "bool",
            "Off: keyword search only (uses less memory)."),
    Setting("memory.log_conversations", "memory", "Keep conversations", "bool",
            "What you and JARVIS said, so you can ask about it later. Never tool output, web pages or secrets."),
    Setting("memory.keep_conversations_days", "memory", "Keep conversations for (days)", "int",
            "Older lines are deleted automatically.", low=1, high=365, step=1),
    # Hotkeys
    Setting("hotkeys.push_to_talk", "hotkeys", "Talk", "hotkey", "Wake JARVIS, or hold to talk in push-to-talk mode."),
    Setting("hotkeys.mute", "hotkeys", "Mute the microphone", "hotkey"),
    Setting("hotkeys.stop", "hotkeys", "Stop", "hotkey", "Stops talking and the current action; again to pause music."),
    Setting("hotkeys.confirm", "hotkeys", "Confirm", "hotkey", "Says yes to the action JARVIS is asking about."),
    Setting("hotkeys.cancel", "hotkeys", "Cancel", "hotkey", "Says no to the action JARVIS is asking about."),
    # Agents and connections (shown, not changed here)
    Setting("agent_hub.url", "connections", "Agent Hub address", "info", editable=False),
    Setting("features.agent_hub", "connections", "Agent Hub", "info", "Shortlist, approvals and job-search runs.",
            editable=False),
    Setting("features.job_search", "connections", "JobPilot status", "info", "\"How's my job search?\"", editable=False),
    Setting("features.brain", "connections", "Reasoning (ask_brain)", "info", editable=False),
    # Appearance
    Setting("look.orb_size", "appearance", "Orb size", "int", applies="now", low=70, high=260, step=2),
    Setting("look.core_size", "appearance", "Core size", "float", "Share of the cloud.", "now", low=0.4, high=1.0, step=0.01),
    Setting("look.orb_brightness", "appearance", "Core brightness", "float", applies="now", low=0.2, high=1.0, step=0.01),
    Setting("look.cloud_brightness", "appearance", "Cloud brightness", "float", applies="now", low=0.2, high=1.0, step=0.01),
    Setting("look.ribbon_brightness", "appearance", "Ribbon brightness", "float", applies="now", low=0.2, high=1.0,
            step=0.01),
    Setting("web_ui.caption_seconds", "appearance", "Caption time (s)", "int",
            "How long a reply stays under the closed orb.", low=2, high=60, step=1),
    Setting("web_ui.always_on_top", "appearance", "Orb always on top", "bool"),
)
BY_KEY = {s.key: s for s in SETTINGS}


def _get(config: dict, key: str) -> Any:
    node: Any = config
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(config: dict, key: str, value: Any) -> None:
    *path, last = key.split(".")
    node = config
    for part in path:
        node = node.setdefault(part, {})
    node[last] = value


def check(setting: Setting, value: Any, config: dict | None = None, overlay: dict | None = None) -> Any:
    """The value to keep, normalised; raises ValueError with a sentence for the page when it isn't allowed."""
    if not setting.editable:
        raise ValueError(f"{setting.label} can't be changed here.")
    kind = setting.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{setting.label} is on or off.")
        return value
    if kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{setting.label} needs a number.")
        if setting.low is not None and value < setting.low or setting.high is not None and value > setting.high:
            raise ValueError(f"{setting.label} goes from {setting.low:g} to {setting.high:g}.")
        return int(round(value)) if kind == "int" else round(float(value), 3)
    if kind == "choice":
        if value not in setting.choices:
            raise ValueError(f"{setting.label} must be one of the listed options.")
        return value
    if kind == "model":
        if not isinstance(value, str) or not MODEL_ID.match(value.strip()):
            raise ValueError(f"{setting.label}: that doesn't look like a model id.")
        return value.strip()
    if kind == "models":
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_LIST:
            raise ValueError(f"{setting.label}: give 1 to {MAX_LIST} model ids.")
        out = [v.strip() for v in value if isinstance(v, str)]
        if len(out) != len(value) or not all(MODEL_ID.match(v) for v in out):
            raise ValueError(f"{setting.label}: one of those doesn't look like a model id.")
        return out
    if kind == "hotkey":
        from core.hotkeys import parse_combo

        if not isinstance(value, str):
            raise ValueError(f"{setting.label} needs a key combination like ctrl+alt+m.")
        combo = "+".join(p for p in value.lower().replace(" ", "").split("+") if p)
        try:
            keys = parse_combo(combo)
        except ValueError:
            raise ValueError(f"{setting.label}: JARVIS can't listen for {value!r}. Try something like ctrl+alt+m.") from None
        if not keys & {"ctrl", "alt", "cmd"}:  # cmd = the Windows key (pynput's name)
            raise ValueError(f"{setting.label} needs Ctrl, Alt or the Windows key, so it doesn't fire while you type.")
        for other in SETTINGS:
            if other.kind != "hotkey" or other.key == setting.key:
                continue
            current = (overlay or {}).get(other.key, _get(config or {}, other.key))
            try:
                taken = parse_combo(str(current)) if current else None
            except ValueError:
                taken = None
            if taken == keys:
                raise ValueError(f"{combo} is already {other.label}.")
        return combo
    raise ValueError(f"{setting.label} can't be changed here.")


def load_overlay(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        log.warning("couldn't read %s; using config.yaml only", path)
        return {}
    return data if isinstance(data, dict) else {}


def apply_overlay(config: dict, path: Path) -> list[str]:
    """Lay the app's saved changes over config.yaml (at start-up). Keys not in SETTINGS, or values that
    no longer pass their check, are skipped. Returns the keys applied."""
    overlay = load_overlay(path)
    applied = []
    for key, value in overlay.items():
        setting = BY_KEY.get(key)
        if setting is None or key.startswith(("prefs.", "look.")):
            log.warning("settings.json: ignored %s (not a setting the app may change)", key)
            continue
        try:
            value = check(setting, value, config, {k: v for k, v in overlay.items() if k != key})
        except ValueError as exc:
            log.warning("settings.json: ignored %s: %s", key, exc)
            continue
        _set(config, key, value)
        applied.append(key)
    if applied:
        log.info("settings from the app: %s", ", ".join(sorted(applied)))
    return applied


def secret_status(env_key) -> list[dict[str, Any]]:
    """Each key's name and whether it's set. Never its value."""
    return [{"name": name, "set": bool(env_key(name))} for name in SECRET_NAMES]


class SettingsStore:
    """The running config (as loaded at start) plus the saved changes in data/settings.json."""

    def __init__(self, config: dict, path: Path) -> None:
        self.config = config
        self.path = path
        self.started = json.loads(json.dumps({s.key: _get(config, s.key) for s in SETTINGS}))  # as running now

    def saved(self) -> dict[str, Any]:
        return load_overlay(self.path)

    def value(self, key: str, live: dict[str, Any]) -> Any:
        if key in live:
            return live[key]
        return self.saved().get(key, self.started.get(key))

    def view(self, live: dict[str, Any]) -> dict[str, Any]:
        """Every category with its settings, current values and whether a restart is waiting.
        `live` holds the prefs.* and look.* values, which the runtime owns."""
        saved = self.saved()
        cats = []
        for cid, label, icon in CATEGORIES:
            items = []
            for s in SETTINGS:
                if s.category != cid:
                    continue
                value = self.value(s.key, live)
                items.append({
                    "key": s.key, "label": s.label, "kind": s.kind, "help": s.help, "applies": s.applies,
                    "editable": s.editable, "choices": list(s.choices), "low": s.low, "high": s.high,
                    "step": s.step, "value": value,
                    "restart": s.applies == "restart" and s.key in saved and saved[s.key] != self.started.get(s.key),
                })
            cats.append({"id": cid, "label": label, "icon": icon, "items": items})
        return {"categories": cats}

    def change(self, key: str, value: Any) -> Any:
        """Check and keep a restart-applied setting; returns the normalised value. prefs.* and look.*
        go to the runtime instead (it calls check() itself)."""
        setting = BY_KEY.get(key)
        if setting is None:
            raise ValueError("That isn't a setting JARVIS knows.")
        saved = self.saved()
        value = check(setting, value, self.config, saved)
        if value == self.started.get(key):
            saved.pop(key, None)  # back to what's running: nothing to apply
        else:
            saved[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        log.info("settings: kept %s for the next start", key)
        return value

    def pending_restart(self) -> list[str]:
        saved = self.saved()
        return sorted(k for k, v in saved.items() if k in BY_KEY and v != self.started.get(k))
