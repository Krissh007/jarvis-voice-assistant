"""read_app: privacy blocks, target choice, scrubbing (UI Automation itself is faked)."""

import asyncio
import os

import pytest

from actions import Services, desktop
from actions.desktop import Desktop, blocked_reason, describe, is_shell
from core import uia
from core.privacy import hide_secrets
from core.registry import NeedsConfirmation, Registry, ToolError
from core.uia import Control, Window

CONFIG = {"apps": {"allowlist": {"claude": "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude",
                                 "notepad": "notepad.exe"},
                   "aliases": {"cloud": "claude"}}}


def win(exe: str, title: str, hwnd: int = 1, pid: int = 4242) -> Window:
    return Window(hwnd, title, pid, exe)


@pytest.fixture
def screen(monkeypatch):
    """A fake desktop: windows front to back, and what each one shows."""
    state = {"windows": [], "controls": {}, "minimised": set()}
    monkeypatch.setattr(uia, "top_windows", lambda: [w for w in state["windows"] if w.hwnd not in state["minimised"]])
    monkeypatch.setattr(uia, "all_windows", lambda: list(state["windows"]))
    monkeypatch.setattr(uia, "window", lambda h: next((w for w in state["windows"] if w.hwnd == h), None))
    monkeypatch.setattr(uia, "read", lambda h, **kw: [Control(c.id, c.kind, c.name, c.rect, c.password, c.value)
                                                     for c in state["controls"].get(h, [])])

    class User32:
        @staticmethod
        def IsIconic(h):
            return h in state["minimised"]

    monkeypatch.setattr(uia, "_user32", User32)
    return state


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("exe,title", [
    ("keepassxc.exe", "Database"), ("1password.exe", "1Password"),
    ("chrome.exe", "YouTube - Google Chrome"), ("msedge.exe", "New tab"), ("firefox.exe", "Mozilla Firefox"),
    ("systemsettings.exe", "Settings"), ("sechealthui.exe", "Windows Security"),
    ("credentialuibroker.exe", "Windows Security"), ("consent.exe", "User Account Control"),
    ("regedit.exe", "Registry Editor"), ("taskmgr.exe", "Task Manager"),
    ("applicationframehost.exe", "Settings"), ("notepad.exe", ".env - Notepad"),
    ("notepad.exe", "passwords.txt - Notepad"), ("python.exe", "JARVIS"),
])
def test_blocked_apps(exe, title) -> None:
    assert blocked_reason(win(exe, title))


def test_own_process_is_blocked_whatever_the_title() -> None:
    assert blocked_reason(win("python.exe", "anything", pid=os.getpid()))


@pytest.mark.parametrize("exe,title", [("notepad.exe", "notes.txt - Notepad"), ("claude.exe", "Claude"),
                                       ("winword.exe", "Report.docx - Word"), ("calculatorapp.exe", "Calculator")])
def test_ordinary_apps_are_allowed(exe, title) -> None:
    assert blocked_reason(win(exe, title)) is None


def test_shell_windows_are_not_apps() -> None:
    assert is_shell(win("textinputhost.exe", "Windows Input Experience"))
    assert is_shell(win("explorer.exe", "Program Manager"))
    assert not is_shell(win("explorer.exe", "Downloads - File Explorer"))


def test_reads_the_app_in_front_skipping_shell_and_jarvis(screen) -> None:
    screen["windows"] = [win("textinputhost.exe", "Windows Input Experience", 1),
                         win("python.exe", "JARVIS", 2, pid=os.getpid()),
                         win("notepad.exe", "todo.txt - Notepad", 3)]
    screen["controls"][3] = [Control(1, "document", "Text editor", (0, 0, 9, 9), value="buy milk")]
    d = Desktop(CONFIG, Services())
    out = run(d.read_app())
    assert "todo.txt - Notepad (notepad.exe)" in out and "[1] document \"Text editor\": buy milk" in out
    assert d.current.hwnd == 3


def test_named_app_by_allowlist_alias_then_current_app_is_remembered(screen) -> None:
    screen["windows"] = [win("notepad.exe", "a.txt - Notepad", 1), win("claude.exe", "Claude", 2)]
    screen["controls"][2] = [Control(1, "text", "Hello Krishna", (0, 0, 9, 9))]
    d = Desktop(CONFIG, Services())
    assert "Hello Krishna" in run(d.read_app("cloud"))
    screen["windows"].reverse()  # Notepad comes to the front; "read it again" still means Claude
    assert "App: Claude" in run(d.read_app())


def test_named_app_by_title_words(screen) -> None:
    screen["windows"] = [win("winword.exe", "Report.docx - Word", 5)]
    screen["controls"][5] = [Control(1, "button", "Bold", (0, 0, 9, 9))]
    assert "[1] button \"Bold\"" in run(Desktop(CONFIG, Services()).read_app("word"))


def test_unknown_app(screen) -> None:
    with pytest.raises(ToolError, match="can't find"):
        run(Desktop(CONFIG, Services()).read_app("photoshop"))


def test_blocked_window_is_refused_without_saying_its_title(screen) -> None:
    screen["windows"] = [win("chrome.exe", "API keys | Google AI Studio - Google Chrome", 1)]
    with pytest.raises(ToolError) as err:
        run(Desktop(CONFIG, Services()).read_app("chrome"))
    assert "your browser" in str(err.value) and "API keys" not in str(err.value)


def test_secret_titled_front_window_is_refused(screen) -> None:
    screen["windows"] = [win("notepad.exe", ".env - Notepad", 1)]
    with pytest.raises(ToolError) as err:
        run(Desktop(CONFIG, Services()).read_app())
    assert ".env" not in str(err.value)


def test_minimised_app(screen) -> None:
    screen["windows"] = [win("notepad.exe", "a.txt - Notepad", 1)]
    screen["minimised"] = {1}
    with pytest.raises(ToolError, match="minimised"):
        run(Desktop(CONFIG, Services()).read_app("notepad"))


def test_password_and_payment_fields_are_never_read() -> None:
    snap = desktop.Snapshot(win("app.exe", "Checkout"), [
        Control(1, "text box", "Password", (0, 0, 9, 9), password=True),
        Control(2, "text box", "Card number", (0, 0, 9, 9), value="4111 1111 1111 1111"),
        Control(3, "text box", "CVV", (0, 0, 9, 9), value="123"),
        Control(4, "text box", "Name on order", (0, 0, 9, 9), value="Krishna"),
    ])
    text, _ = describe(snap, "all")
    assert "4111" not in text and "123" not in text
    assert "[1] text box \"Password\" (private: not read)" in text
    assert "[2] text box \"Card number\" (private: not read)" in text
    assert "Krishna" in text


def test_text_is_scrubbed_of_emails_numbers_and_keys() -> None:
    key = "AIza" + "x" * 35
    snap = desktop.Snapshot(win("notepad.exe", "n.txt"), [
        Control(1, "document", "Text editor", (0, 0, 9, 9),
                value=f"mail me at krishna@example.com or +91 98765 43210, key {key}"),
    ])
    text, hidden = describe(snap, "all")
    assert "krishna@example.com" not in text and "98765" not in text and key not in text
    assert "[email]" in text and "[hidden]" in text and hidden == 1


def test_what_filters() -> None:
    snap = desktop.Snapshot(win("a.exe", "A"), [Control(1, "button", "Send", (0, 0, 9, 9)),
                                                 Control(2, "text", "A message", (0, 0, 9, 9))])
    assert "Send" not in describe(snap, "text")[0] and "A message" in describe(snap, "text")[0]
    assert "Send" in describe(snap, "controls")[0] and "A message" not in describe(snap, "controls")[0]


def test_hide_secrets() -> None:
    text, n = hide_secrets("OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz\nhello 4111 1111 1111 1111 and 1234 5678")
    assert "sk-" not in text and "4111" not in text and "[hidden] and 1234 5678" in text and n >= 2


def test_read_app_is_read_only_untrusted_and_registered() -> None:
    reg = Registry()
    services = Services()
    desktop.register(reg, CONFIG, services)
    tool = reg.get("read_app")
    assert tool.read_only and tool.untrusted_output and services.desktop is not None


# ---------------------------------------------------------------- typing

@pytest.fixture
def keyboard(screen, monkeypatch):
    """Fake focus, cursor and keyboard on top of the fake screen."""
    state = {"front": None, "focused": None, "typed": [], "enter": 0, "focus_ok": True}
    monkeypatch.setattr(desktop, "focus_window", lambda h: state.update(front=h if state["focus_ok"] else None))
    monkeypatch.setattr(desktop.textinput, "foreground_root", lambda: state["front"])
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(uia, "focused", lambda: state["focused"])

    def set_focus(hwnd, cid):
        c = next(c for c in screen["controls"][hwnd] if c.id == cid)
        state["focused"] = uia.Focused(c.kind, c.name, c.password, c.kind in uia.TYPEABLE)
        return True

    monkeypatch.setattr(uia, "set_focus", set_focus)
    monkeypatch.setattr(desktop.textinput, "type_text", lambda h, t: state["typed"].append((h, t)) or "pasted")
    monkeypatch.setattr(desktop.textinput, "press_enter", lambda h: state.update(enter=state["enter"] + 1))
    return state


def chat_app(screen) -> None:
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    screen["controls"][7] = [Control(1, "document", "Conversation", (0, 0, 900, 700)),
                             Control(2, "text box", "Search", (0, 0, 200, 30)),
                             Control(3, "text box", "Prompt", (0, 700, 900, 740))]


def test_types_into_the_focused_box_and_sends_in_a_chat_app_without_asking(screen, keyboard) -> None:
    chat_app(screen)
    keyboard["focused"] = uia.Focused("text box", "Prompt", False, True)
    out = run(Desktop(CONFIG, Services()).type_in_app("what is a monad?", app="claude", send=True))
    assert keyboard["typed"] == [(7, "what is a monad?")] and keyboard["enter"] == 1
    assert out == "Typed into the Prompt box in claude and pressed Enter."


def test_without_a_focused_box_it_picks_the_chat_box(screen, keyboard) -> None:
    chat_app(screen)
    run(Desktop(CONFIG, Services()).type_in_app("hi", app="claude"))
    assert keyboard["focused"].name == "Prompt" and keyboard["enter"] == 0


def test_named_field_and_numbered_field(screen, keyboard) -> None:
    chat_app(screen)
    d = Desktop(CONFIG, Services())
    run(d.type_in_app("jarvis", app="claude", field="search"))
    assert keyboard["focused"].name == "Search"
    run(d.read_app("claude"))
    run(d.type_in_app("x", field="1"))
    assert keyboard["focused"].name == "Conversation"
    with pytest.raises(ToolError, match="can't find a Budget field"):
        run(d.type_in_app("x", field="Budget"))


def test_enter_outside_chat_apps_asks_first_then_types(screen, keyboard) -> None:
    screen["windows"] = [win("notepad.exe", "a.txt - Notepad", 3)]
    screen["controls"][3] = [Control(1, "document", "Text editor", (0, 0, 900, 700))]
    out = run(Desktop(CONFIG, Services()).type_in_app("hello", app="notepad", send=True))
    assert isinstance(out, NeedsConfirmation) and keyboard["typed"] == []
    assert out.summary == 'type "hello" into a.txt - Notepad and press Enter'
    run(out.resume())
    assert keyboard["typed"] == [(3, "hello")] and keyboard["enter"] == 1


def test_terminals_always_ask(screen, keyboard) -> None:
    screen["windows"] = [win("windowsterminal.exe", "PowerShell", 4)]
    screen["controls"][4] = [Control(1, "document", "", (0, 0, 900, 700))]
    out = run(Desktop(CONFIG, Services()).type_in_app("dir", app="powershell"))
    assert isinstance(out, NeedsConfirmation) and keyboard["typed"] == []


def test_after_reading_untrusted_text_typing_asks(screen, keyboard) -> None:
    chat_app(screen)
    services = Services()
    services.dispatcher = type("D", (), {"tainted": True})()
    out = run(Desktop(CONFIG, services).type_in_app("hi", app="claude", send=True))
    assert isinstance(out, NeedsConfirmation)


def test_never_types_secrets_or_into_private_fields(screen, keyboard) -> None:
    screen["windows"] = [win("shop.exe", "Checkout", 5)]
    screen["controls"][5] = [Control(1, "text box", "Card number", (0, 0, 300, 30)),
                             Control(2, "text box", "Password", (0, 40, 300, 70), password=True)]
    d = Desktop(CONFIG, Services())
    with pytest.raises(ToolError, match="looks like it contains"):
        run(d.type_in_app("4111 1111 1111 1111", app="shop"))
    with pytest.raises(ToolError, match="anywhere to type"):
        run(d.type_in_app("hello", app="shop"))
    keyboard["focused"] = uia.Focused("text box", "", True, True)  # the cursor is in a password box
    with pytest.raises(ToolError, match="password or payment"):
        run(d.type_in_app("hello", app="shop"))
    assert keyboard["typed"] == []


def test_blocked_apps_and_window_not_in_front(screen, keyboard) -> None:
    screen["windows"] = [win("chrome.exe", "Gmail - Google Chrome", 6), win("notepad.exe", "n - Notepad", 3)]
    screen["controls"][3] = [Control(1, "document", "Text editor", (0, 0, 900, 700))]
    d = Desktop(CONFIG, Services())
    with pytest.raises(ToolError, match="won't type into your browser"):
        run(d.type_in_app("hi", app="chrome"))
    keyboard["focus_ok"] = False
    with pytest.raises(ToolError, match="couldn't bring notepad to the front"):
        run(d.type_in_app("hi", app="notepad"))
    assert keyboard["typed"] == []


def test_type_in_app_is_registered_self_gated() -> None:
    reg = Registry()
    desktop.register(reg, CONFIG, Services())
    tool = reg.get("type_in_app")
    assert tool.self_gated and not tool.read_only
