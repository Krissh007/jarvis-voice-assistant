"""--bench-apps: how fast and how well JARVIS can read each open app. Read-only.

For every open window it times one read of the buttons and fields and one read with text, and
counts what it found. Only counts and timings are printed: never any text from the apps. Windows
JARVIS won't read (browsers, password managers, Settings...) are listed as skipped, without their
titles.
"""

from __future__ import annotations

import time

SLOW_S = 2.0  # a read slower than this makes voice replies feel sluggish
FEW_ITEMS = 5  # fewer than this usually means the app doesn't support UI Automation well


def run_benchmark(config: dict) -> int:
    from actions import Services
    from actions.desktop import Desktop, blocked_reason, is_private_control, is_shell, safe_label
    from core import uia

    desktop = Desktop(config, Services())
    windows = [w for w in uia.top_windows() if not is_shell(w)]
    if not windows:
        print("No open app windows to measure.")
        return 0
    print(f"Measuring {len(windows)} open window(s). Nothing is clicked or typed, and no app text is shown.\n")
    print(f"{'app':24} {'controls':>8} {'secs':>5} {'with text':>9} {'secs':>5} {'fields':>6}  note")
    slow = 0
    for win in windows:
        name = safe_label(win)[:24]
        if why := blocked_reason(win):
            print(f"{name:24} {'-':>8} {'-':>5} {'-':>9} {'-':>5} {'-':>6}  skipped: {why.split(',')[0]}")
            continue
        t = time.perf_counter()
        controls = uia.read(win.hwnd, limit=desktop.max_items, patient=True)
        t_controls = time.perf_counter() - t
        t = time.perf_counter()
        with_text = uia.read(win.hwnd, with_text=True, limit=desktop.max_items, max_chars=desktop.max_chars)
        t_text = time.perf_counter() - t
        fields = sum(1 for c in controls if c.kind in uia.TYPEABLE and not is_private_control(c))
        private = sum(1 for c in controls if is_private_control(c))
        chars = sum(len(c.value) + (len(c.name) if c.kind in {"text", "list item"} else 0) for c in with_text)
        notes = []
        if len(controls) < FEW_ITEMS:
            notes.append("few controls: this app may not support UI Automation well")
        if max(t_controls, t_text) > SLOW_S:
            notes.append("slow")
            slow += 1
        if private:
            notes.append(f"{private} private field(s) never read")
        notes.append(f"{chars} characters of text")
        print(f"{name:24} {len(controls):>8} {t_controls:>5.1f} {len(with_text):>9} {t_text:>5.1f} {fields:>6}  "
              + "; ".join(notes))
    print(f"\n'controls' = buttons, fields, menus and list items found; 'fields' = places JARVIS can type.")
    if slow:
        print(f"{slow} app(s) took over {SLOW_S:.0f} s to read; JARVIS will feel slower there.")
    return 0
