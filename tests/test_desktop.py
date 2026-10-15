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
    assert out == 'Typed "what is a monad?" into the Prompt box in claude and pressed Enter.'


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


# ---------------------------------------------------------------- clicking, keys, scrolling, windows

@pytest.fixture
def hands(keyboard, screen, monkeypatch):
    """Fake clicks, key presses, scrolling and window moves."""
    state = {"pressed": [], "clicks": [], "keys": [], "scrolls": [], "moves": [], "patterns": True}
    monkeypatch.setattr(uia, "press", lambda h, cid: state["pressed"].append((h, cid)) or "pressed"
                        if state["patterns"] else None)
    monkeypatch.setattr(uia, "current_rect", lambda h, cid: None)
    monkeypatch.setattr(desktop.textinput, "click_at", lambda h, x, y, b="left", d=False:
                        state["clicks"].append((h, x, y, b, d)))
    monkeypatch.setattr(desktop.textinput, "send_combo", lambda h, c, t=1: state["keys"].append((h, c, t)))
    monkeypatch.setattr(desktop.textinput, "scroll_at", lambda h, x, y, n: state["scrolls"].append((h, n)))
    monkeypatch.setattr(desktop, "_move_window", lambda h, a: state["moves"].append((h, a)))
    monkeypatch.setattr(uia._user32, "GetWindowRect", staticmethod(lambda h, r: True), raising=False)
    return state


def toolbar(screen) -> None:
    screen["windows"] = [win("notepad.exe", "a.txt - Notepad", 3)]
    screen["controls"][3] = [Control(1, "menu item", "File", (0, 0, 40, 20)),
                             Control(2, "menu item", "Edit", (40, 0, 80, 20)),
                             Control(3, "button", "Delete", (0, 30, 40, 50)),
                             Control(4, "button", "Account", (40, 30, 80, 50)),
                             Control(5, "button", "Reply", (0, 60, 40, 80)),
                             Control(6, "button", "Reply", (0, 90, 40, 110)),
                             Control(7, "menu item", "Save as", (0, 120, 40, 140)),
                             Control(8, "text box", "Card number", (0, 150, 40, 170))]


def test_click_by_name_presses_without_the_mouse(screen, hands) -> None:
    toolbar(screen)
    out = run(Desktop(CONFIG, Services()).click_in_app("file", app="notepad"))
    assert hands["pressed"] == [(3, 1)] and hands["clicks"] == [] and out == "Pressed File in notepad."


def test_click_falls_back_to_a_real_click_in_the_middle(screen, hands) -> None:
    toolbar(screen)
    hands["patterns"] = False
    run(Desktop(CONFIG, Services()).click_in_app("Edit", app="notepad"))
    assert hands["clicks"] == [(3, 60, 10, "left", False)]


def test_click_by_number_from_the_last_read(screen, hands) -> None:
    toolbar(screen)
    d = Desktop(CONFIG, Services())
    run(d.read_app("notepad"))
    run(d.click_in_app("2"))
    assert hands["pressed"] == [(3, 2)]


def test_did_you_mean_and_duplicates(screen, hands) -> None:
    toolbar(screen)
    d = Desktop(CONFIG, Services())
    with pytest.raises(ToolError, match='Did you mean "Edit"'):
        run(d.click_in_app("Edti", app="notepad"))
    with pytest.raises(ToolError, match="numbers 5, 6"):
        run(d.click_in_app("reply", app="notepad"))
    assert hands["pressed"] == []


@pytest.mark.parametrize("target", ["Delete", "Save as"])
def test_risky_clicks_ask_first(screen, hands, target) -> None:
    toolbar(screen)
    out = run(Desktop(CONFIG, Services()).click_in_app(target, app="notepad"))
    assert isinstance(out, NeedsConfirmation) and hands["pressed"] == []
    assert out.summary.startswith("click ")
    run(out.resume())
    assert len(hands["pressed"]) == 1


def test_account_and_private_controls_are_never_clicked(screen, hands) -> None:
    toolbar(screen)
    d = Desktop(CONFIG, Services())
    with pytest.raises(ToolError, match="accounts or settings"):
        run(d.click_in_app("Account", app="notepad"))
    with pytest.raises(ToolError, match="password or payment"):
        run(d.click_in_app("Card number", app="notepad"))
    assert hands["pressed"] == [] and hands["clicks"] == []


def test_send_in_a_chat_app_does_not_ask(screen, hands) -> None:
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    screen["controls"][7] = [Control(1, "button", "Send message", (0, 0, 40, 20))]
    assert run(Desktop(CONFIG, Services()).click_in_app("send message", app="claude")) == \
        "Pressed Send message in claude."


@pytest.mark.parametrize("keys,expect", [
    ("Control + Shift + T", "ctrl+shift+t"), ("ctrl-n", "ctrl+n"), ("page down", "pagedown"),
    ("Down arrow", "down"), ("Esc", "escape"), ("ctrl s", "ctrl+s"), ("Windows + R", "win+r"),
])
def test_normalize_keys(keys, expect) -> None:
    assert desktop.normalize_keys(keys) == expect


@pytest.mark.parametrize("combo,exe,rule", [
    ("ctrl+n", "notepad.exe", "ok"), ("escape", "notepad.exe", "ok"), ("a", "notepad.exe", "ok"),
    ("enter", "claude.exe", "ok"), ("enter", "notepad.exe", "ask"), ("ctrl+s", "notepad.exe", "ask"),
    ("alt+f4", "notepad.exe", "ask"), ("ctrl+w", "notepad.exe", "ask"), ("delete", "explorer.exe", "ask"),
    ("delete", "notepad.exe", "ok"), ("ctrl+c", "windowsterminal.exe", "ask"),
    ("shift+delete", "explorer.exe", "no"), ("win+r", "notepad.exe", "no"), ("alt+tab", "notepad.exe", "no"),
    ("ctrl+alt+delete", "notepad.exe", "no"), ("ctrl+d", "outlook.exe", "no"), ("ctrl+alt+t", "notepad.exe", "no"),
])
def test_key_rules(combo, exe, rule) -> None:
    assert desktop.key_rule(combo, exe)[0] == rule


def test_press_keys_repeats_only_navigation_and_asks_for_risky(screen, hands) -> None:
    toolbar(screen)
    d = Desktop(CONFIG, Services())
    assert run(d.press_keys("page down", app="notepad", times=3)) == "Pressed Pagedown 3 times in notepad."
    run(d.press_keys("ctrl+n", app="notepad", times=5))
    assert hands["keys"] == [(3, "pagedown", 3), (3, "ctrl+n", 1)]
    out = run(d.press_keys("ctrl+s", app="notepad"))
    assert isinstance(out, NeedsConfirmation) and len(hands["keys"]) == 2
    with pytest.raises(ToolError, match="Windows-key"):
        run(d.press_keys("win+r", app="notepad"))


def test_scroll_and_window_actions(screen, hands) -> None:
    toolbar(screen)
    d = Desktop(CONFIG, Services())
    run(d.scroll_app("down", app="notepad", amount=3))
    assert hands["scrolls"] == [(3, -3)]
    run(d.window_action("left", app="notepad"))
    assert hands["moves"] == [(3, "left half")]
    with pytest.raises(ToolError, match="I can"):
        run(d.window_action("explode", app="notepad"))


def test_browser_windows_can_be_moved_but_not_named(screen, hands) -> None:
    screen["windows"] = [win("chrome.exe", "Private thing - Google Chrome", 9)]
    out = run(Desktop(CONFIG, Services()).window_action("minimise", app="chrome"))
    assert hands["moves"] == [(9, "minimise")] and "Private" not in out
    with pytest.raises(ToolError):
        run(Desktop(CONFIG, Services()).click_in_app("x", app="chrome"))


# ---------------------------------------------------------------- waiting and plans

def test_new_text_keeps_only_what_was_added() -> None:
    assert desktop.new_text("Hello you said hi", "Hello you said hi Claude says forty two") == "Claude says forty two"
    assert desktop.new_text("a b Line 1 Column 3", "a b new words Line 2 Column 9") == "new words"


def test_wait_for_app_returns_the_reply_once_it_stops_changing(screen, monkeypatch) -> None:
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    frames = iter(["Q what is six times seven", "Q what is six times seven Forty", "Q what is six times seven Forty two.",
                   "Q what is six times seven Forty two.", "Q what is six times seven Forty two."])
    d = Desktop(CONFIG, Services())
    monkeypatch.setattr(d, "_text", lambda w: next(frames))
    monkeypatch.setattr(desktop, "WAIT_POLL_S", 0)
    out = run(d.wait_for_app("claude"))
    assert out.startswith("claude finished") and out.endswith("New text:\nForty two.")


def test_wait_for_app_uses_the_text_from_before_the_message_was_sent(screen, keyboard, monkeypatch) -> None:
    chat_app(screen)
    keyboard["focused"] = uia.Focused("text box", "Prompt", False, True)
    d = Desktop(CONFIG, Services())
    texts = ["old chat"]
    monkeypatch.setattr(d, "_text", lambda w: texts[-1])
    monkeypatch.setattr(desktop, "WAIT_POLL_S", 0)
    run(d.type_in_app("hi", app="claude", send=True))
    assert d.baseline == {7: "old chat"}
    texts.append("old chat hi Hello Krishna")  # the reply had already started when waiting began
    assert run(d.wait_for_app("claude")).endswith("hi Hello Krishna")


def test_wait_for_app_until_and_timeout(screen, monkeypatch) -> None:
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    d = Desktop(CONFIG, Services())
    monkeypatch.setattr(desktop, "WAIT_POLL_S", 0)
    frames = iter(["x", "x Done step one", "x Done step one Finished"])
    monkeypatch.setattr(d, "_text", lambda w: next(frames))
    assert run(d.wait_for_app("claude", until="finished")).endswith("Done step one Finished")
    clock = iter(range(0, 1000, 100))
    monkeypatch.setattr(desktop, "_now", lambda: next(clock))
    monkeypatch.setattr(d, "_text", lambda w: "same")
    assert "Nothing changed" in run(d.wait_for_app("claude", timeout=5))


def test_a_just_opened_app_is_waited_for(screen, monkeypatch) -> None:
    d = Desktop(CONFIG, Services())
    monkeypatch.setattr(d, "_launching", lambda app: True)
    polls = []

    def sleep(s):
        polls.append(s)
        if len(polls) == 3:
            screen["windows"] = [win("notepad.exe", "Untitled - Notepad", 3)]
    monkeypatch.setattr(desktop.time, "sleep", sleep)
    assert d.target("notepad").hwnd == 3


def test_an_approved_plan_step_does_not_ask_because_of_taint(screen, keyboard) -> None:
    from core.registry import APPROVED_STEP

    chat_app(screen)
    services = Services()
    services.dispatcher = type("D", (), {"tainted": True})()
    d = Desktop(CONFIG, services)
    assert isinstance(run(d.type_in_app("hi", app="claude")), NeedsConfirmation)
    token = APPROVED_STEP.set(True)
    try:
        assert isinstance(run(d.type_in_app("hi", app="claude")), str)
        toolbar(screen)
        assert isinstance(run(d.click_in_app("Delete", app="notepad")), NeedsConfirmation)  # risky still asks
    finally:
        APPROVED_STEP.reset(token)


def test_plan_lines_show_the_exact_words() -> None:
    reg = Registry()
    desktop.register(reg, CONFIG, Services())
    assert reg.get("type_in_app").summary({"text": "milk\neggs", "app": "notepad"}) == 'type "milk eggs" into notepad'
    assert reg.get("type_in_app").summary({"text": "hi", "app": "claude", "send": True}) == \
        'type "hi" into claude and press Enter'
    assert reg.get("press_keys").summary({"keys": "down", "times": 3}) == "press down 3 times in the current app"


def test_a_desktop_plan_runs_after_one_yes(tmp_path, screen, hands, monkeypatch) -> None:
    """Planner (faked) -> approval -> type, send, wait for the reply, answer: no further questions."""
    from actions import agent_mode
    from tests.fakes import fake_live as fl

    chat_app(screen)
    hands_focus = uia.Focused("text box", "Prompt", False, True)
    monkeypatch.setattr(uia, "focused", lambda: hands_focus)
    frames = iter(["chat", "chat what is a monad? A monad is a pattern.", "chat what is a monad? A monad is a pattern.",
                   "chat what is a monad? A monad is a pattern."])

    class Planner:
        async def generate(self, prompt, **kw):
            if "Write a plan" in prompt:
                return ('{"steps": [{"tool": "type_in_app", "args": {"app": "claude", "text": "what is a monad?", '
                        '"send": true}}, {"tool": "wait_for_app", "args": {"app": "claude"}}, {"tool": "answer"}]}')
            assert "A monad is a pattern" in prompt
            return "Claude says a monad is a pattern."

    services = Services(llm=Planner())
    h = fl.SafetyHarness(tmp_path, services=services, config={"features": {"desktop": True}, **CONFIG})
    services.dispatcher = h.dispatcher
    agent_mode.register(h.registry, {"limits": {"agent_steps_max": 10}}, services)
    monkeypatch.setattr(services.desktop, "_text", lambda w: next(frames))
    monkeypatch.setattr(desktop, "WAIT_POLL_S", 0)

    async def go():
        resp = await h.model_calls("run_plan", goal="ask Claude what a monad is")
        assert resp["summary"] == ('Here\'s the plan: 1. type "what is a monad?" into claude and press Enter '
                                   "2. wait for claude to finish 3. Tell you the answer")
        await h.dispatcher.on_user_input("yes", "text")

    run(go())
    assert hands_focus and h.pending is None
    assert "Claude says a monad is a pattern." in h.notices[-1]


def test_benchmark_prints_counts_but_never_app_text(screen, capsys) -> None:
    from core.desktop_bench import run_benchmark

    screen["windows"] = [win("notepad.exe", "diary.txt - Notepad", 3), win("chrome.exe", "Bank - Google Chrome", 4)]
    screen["controls"][3] = [Control(1, "document", "Text editor", (0, 0, 9, 9), value="my secret diary"),
                             Control(2, "text box", "PIN", (0, 0, 9, 9))]
    assert run_benchmark(CONFIG) == 0
    out = capsys.readouterr().out
    assert "notepad" in out and "skipped" in out and "1 private field" in out
    assert "diary" not in out and "Bank" not in out and "secret" not in out
