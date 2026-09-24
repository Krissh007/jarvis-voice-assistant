"""Global hotkeys via pynput, with press *and* release events (needed for push-to-talk).

Keys are compared by Windows virtual-key code rather than character, because with
Ctrl held pynput reports control characters (Ctrl+M arrives as '\\r').
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MODIFIERS = {"ctrl", "alt", "shift", "cmd"}
_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}

KeyId = str | int  # modifier name, or virtual-key code


def parse_combo(combo: str) -> frozenset[KeyId]:
    """'ctrl+alt+y' -> {'ctrl', 'alt', 0x59}."""
    keys: set[KeyId] = set()
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _MODIFIERS:
            keys.add(part)
        elif part in _NAMED_VK:
            keys.add(_NAMED_VK[part])
        elif len(part) == 1 and part.isalnum():
            keys.add(ord(part.upper()))
        else:
            raise ValueError(f"unsupported key {part!r} in hotkey {combo!r}")
    if not keys - _MODIFIERS:
        raise ValueError(f"hotkey {combo!r} needs a non-modifier key")
    return frozenset(keys)


@dataclass
class HotkeyMatcher:
    """Pure state machine: feed key ids down/up, get (name, pressed) events back."""

    combos: dict[str, frozenset[KeyId]]
    _down: set[KeyId] = field(default_factory=set)
    _active: set[str] = field(default_factory=set)

    def press(self, key: KeyId) -> list[tuple[str, bool]]:
        self._down.add(key)
        events = []
        for name, combo in self.combos.items():
            if name not in self._active and combo <= self._down:
                self._active.add(name)
                events.append((name, True))
        return events

    def release(self, key: KeyId) -> list[tuple[str, bool]]:
        self._down.discard(key)
        events = []
        for name in list(self._active):
            if not self.combos[name] <= self._down:
                self._active.discard(name)
                events.append((name, False))
        return events


def _key_id(key) -> KeyId | None:
    """Normalise a pynput key to a modifier name or virtual-key code."""
    from pynput import keyboard

    name = getattr(key, "name", None)
    if isinstance(key, keyboard.Key) and name:
        base = name.split("_")[0]  # ctrl_l -> ctrl, alt_gr -> alt
        if base in _MODIFIERS:
            return base
        if key.value.vk is not None:
            return key.value.vk
        return None
    vk = getattr(key, "vk", None)
    if vk is not None:
        return vk
    char = getattr(key, "char", None)
    return ord(char.upper()) if char and char.isalnum() else None


class Hotkeys:
    """Runs a pynput listener thread and calls `handler(name, pressed)` from it."""

    def __init__(self, bindings: dict[str, str], handler: Callable[[str, bool], None]) -> None:
        self._matcher = HotkeyMatcher({name: parse_combo(c) for name, c in bindings.items()})
        self._handler = handler
        self._listener = None

    def start(self) -> None:
        from pynput import keyboard

        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key) -> None:
        if (k := _key_id(key)) is not None:
            self._emit(self._matcher.press(k))

    def _on_release(self, key) -> None:
        if (k := _key_id(key)) is not None:
            self._emit(self._matcher.release(k))

    def _emit(self, events: list[tuple[str, bool]]) -> None:
        for name, pressed in events:
            try:
                self._handler(name, pressed)
            except Exception:
                log.exception("hotkey handler failed for %s", name)
