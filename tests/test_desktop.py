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
    screen["windows"] = [win("winword.exe", "a.docx - Word", 3)]   # Notepad's Enter is only a new line
    screen["controls"][3] = [Control(1, "document", "Text editor", (0, 0, 900, 700))]
    out = run(Desktop(CONFIG, Services()).type_in_app("hello", app="winword", send=True))
    assert isinstance(out, NeedsConfirmation) and keyboard["typed"] == []
    assert out.summary == 'type "hello" into a.docx - Word and press Enter'
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
    ("enter", "claude.exe", "ok"), ("enter", "notepad.exe", "ok"), ("enter", "winword.exe", "ask"), ("ctrl+s", "notepad.exe", "ask"),
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


# ---------------------------------------------------------------- typing fixes: IDE terminals, whole page, repeats

CODE = "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n    <title>Portfolio</title>\n</head>\n</html>"
VSCODE_TITLE = "● Untitled-1 - agent-hub - Visual Studio Code"


def vscode(screen, *, editors=1, terminal=True) -> None:
    screen["windows"] = [win("code.exe", VSCODE_TITLE, 9)]
    controls = [Control(1, "document", VSCODE_TITLE, (0, 0, 1536, 816), auto_id="RootWebArea")]
    for i in range(editors):
        controls.append(Control(2 + i, "text box", "The editor is not accessible at this time. To enable screen "
                                "reader optimized mode, use Shift+Alt+F1", (329, 221 + 400 * i, 1231, 240 + 400 * i),
                                class_name="native-edit-context"))
    if terminal:
        controls.append(Control(9, "text box", "Terminal 1 environment is stale", (353, 751, 360, 769),
                                class_name="xterm-helper-textarea"))
    screen["controls"][9] = controls


def focus_like_the_app(keyboard, screen, monkeypatch) -> None:
    """set_focus reports the control's own class and automation id, as UI Automation would."""
    def set_focus(hwnd, cid):
        c = next(c for c in screen["controls"][hwnd] if c.id == cid)
        keyboard["focused"] = uia.Focused(c.kind, c.name, c.password, c.kind in uia.TYPEABLE, c.auto_id, c.class_name)
        return True
    monkeypatch.setattr(uia, "set_focus", set_focus)


def test_cursor_in_vs_codes_terminal_asks_first_and_says_its_a_terminal(screen, keyboard) -> None:
    vscode(screen)
    keyboard["focused"] = uia.Focused("text box", "Terminal 1 environment is stale", False, True, "",
                                      "xterm-helper-textarea")
    out = run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code"))
    assert isinstance(out, NeedsConfirmation) and keyboard["typed"] == []
    assert "into the terminal in code" in out.summary and "can run commands" in out.summary
    assert run(out.resume()).startswith("Typed") and keyboard["typed"] == [(9, CODE)]


def test_an_ide_field_named_terminal_counts_as_a_terminal_even_without_the_class(screen, keyboard) -> None:
    vscode(screen)
    keyboard["focused"] = uia.Focused("text box", "Terminal input", False, True)
    assert isinstance(run(Desktop(CONFIG, Services()).type_in_app("ls", app="code")), NeedsConfirmation)


def test_cursor_in_the_editor_types_without_asking(screen, keyboard) -> None:
    vscode(screen)
    keyboard["focused"] = uia.Focused("text box", "The editor is not accessible at this time. To enable screen "
                                      "reader optimized mode", False, True, "", "native-edit-context")
    out = run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="Untitled-1"))
    assert keyboard["typed"] == [(9, CODE)] and "into Untitled-1 in code" in out  # the file, not the long notice


def test_the_whole_page_is_never_a_place_to_type(screen, keyboard, monkeypatch) -> None:
    vscode(screen, editors=2)
    focus_like_the_app(keyboard, screen, monkeypatch)
    keyboard["focused"] = uia.Focused("document", VSCODE_TITLE, False, True, "RootWebArea")  # focus on the page
    with pytest.raises(ToolError, match="click inside Untitled-1"):
        run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="Untitled-1"))
    assert keyboard["typed"] == []


def test_with_one_real_field_it_uses_that_one_never_the_page_or_the_terminal(screen, keyboard, monkeypatch) -> None:
    vscode(screen, editors=1, terminal=True)
    focus_like_the_app(keyboard, screen, monkeypatch)
    keyboard["focused"] = uia.Focused("document", VSCODE_TITLE, False, True, "RootWebArea")
    run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="Untitled-1"))
    assert keyboard["focused"].class_name == "native-edit-context" and keyboard["typed"] == [(9, CODE)]


def test_a_document_named_like_the_window_counts_as_the_whole_page() -> None:
    w = win("someapp.exe", "Report - Some App")
    assert desktop.is_whole_page(w, "document", "Report - Some App", "")
    assert not desktop.is_whole_page(w, "document", "Text editor", "")
    assert desktop.is_whole_page(w, "text box", "anything", "RootWebArea")


def test_the_same_text_twice_in_a_row_is_typed_once(screen, keyboard, monkeypatch) -> None:
    vscode(screen)
    keyboard["focused"] = uia.Focused("text box", "editor", False, True, "", "native-edit-context")
    clock = [1000.0]
    monkeypatch.setattr(desktop, "_now", lambda: clock[0])
    d = Desktop(CONFIG, Services())
    run(d.type_in_app(CODE, field="Untitled-1"))
    clock[0] += 6
    again = run(d.type_in_app(CODE, app="code", field="Untitled-1"))  # the model repeats it, naming the app
    assert again.startswith("I already typed that into code 6 seconds ago") and keyboard["typed"] == [(9, CODE)]
    run(d.type_in_app("<p>more</p>", app="code", field="Untitled-1"))  # different text is typed
    clock[0] += 11
    run(d.type_in_app(CODE, app="code", field="Untitled-1"))  # after the guard window, asking again types again
    assert [t for _, t in keyboard["typed"]] == [CODE, "<p>more</p>", CODE]


def test_a_failed_typing_does_not_block_trying_again(screen, keyboard, monkeypatch) -> None:
    vscode(screen)
    keyboard["focused"] = uia.Focused("text box", "editor", False, True, "", "native-edit-context")
    calls = []

    def flaky(hwnd, text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("the app is no longer in front")
        return "pasted"
    monkeypatch.setattr(desktop.textinput, "type_text", flaky)
    d = Desktop(CONFIG, Services())
    with pytest.raises(ToolError, match="no longer in front"):
        run(d.type_in_app("hello", app="code", field="Untitled-1"))
    assert run(d.type_in_app("hello", app="code", field="Untitled-1")).startswith("Typed") and len(calls) == 2


@pytest.mark.parametrize("text,expect", [
    ("see you at six", '"see you at six"'),
    (CODE, f'6 lines ({len(CODE)} characters) starting "<!DOCTYPE html>"'),
])
def test_long_or_multi_line_text_is_described_not_read_out(text, expect) -> None:
    assert desktop.typed_words(text) == expect


def test_confirmations_and_plan_lines_describe_code_instead_of_reading_it(screen, keyboard) -> None:
    screen["windows"] = [win("winword.exe", "a.docx - Word", 3)]
    keyboard["focused"] = uia.Focused("document", "Text editor", False, True)
    out = run(Desktop(CONFIG, Services()).type_in_app(CODE, app="winword", send=True))
    assert out.summary == (f'type 6 lines ({len(CODE)} characters) starting "<!DOCTYPE html>" into a.docx - Word '
                           'and press Enter')
    reg = Registry()
    desktop.register(reg, CONFIG, Services())
    assert reg.get("type_in_app").summary({"text": CODE, "app": "code"}).startswith(f'type 6 lines ({len(CODE)} characters)')


# ---------------------------------------------------------------- code editors: the right file, whole files

def editor_on(screen, keyboard, file: str, *, focused: bool = True) -> None:
    """VS Code with `file` open; its editor is named after the file, as VS Code names it."""
    title = f"{file} - portfolio - Visual Studio Code"
    screen["windows"] = [win("code.exe", title, 9)]
    screen["controls"][9] = [Control(1, "document", title, (0, 0, 1536, 816), auto_id="RootWebArea"),
                             Control(2, "text box", file, (329, 221, 1231, 700), class_name="native-edit-context"),
                             Control(9, "text box", "Terminal 1", (353, 751, 360, 769),
                                     class_name="xterm-helper-textarea")]
    keyboard["focused"] = (uia.Focused("text box", file, False, True, "", "native-edit-context") if focused
                           else uia.Focused("document", title, False, True, "RootWebArea"))


def test_code_goes_only_into_the_file_named(screen, keyboard) -> None:
    editor_on(screen, keyboard, "index.html")
    out = run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="index.html"))
    assert keyboard["typed"] == [(9, CODE)]
    assert out == f'Typed 6 lines ({len(CODE)} characters) starting "<!DOCTYPE html>" into index.html in code.'


def test_code_for_another_file_is_not_typed_into_the_open_one(screen, keyboard) -> None:
    editor_on(screen, keyboard, "style.css")  # the voice session's bug: HTML went into style.css
    with pytest.raises(ToolError, match=r"index.html isn't open in code \(the file open now is style.css\)"):
        run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="index.html"))
    assert keyboard["typed"] == []


def test_in_a_code_editor_the_file_must_be_named(screen, keyboard) -> None:
    editor_on(screen, keyboard, "style.css")
    with pytest.raises(ToolError, match="which file this goes in.*The file open now is style.css"):
        run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code"))
    assert keyboard["typed"] == []


def test_a_file_name_with_folders_or_in_another_case_still_matches(screen, keyboard) -> None:
    editor_on(screen, keyboard, "script.js")
    run(Desktop(CONFIG, Services()).type_in_app("let x = 1;", app="code", field="portfolio/Script.JS"))
    assert keyboard["typed"] == [(9, "let x = 1;")]


def test_with_the_cursor_outside_the_editor_it_puts_it_in_the_named_file(screen, keyboard, monkeypatch) -> None:
    editor_on(screen, keyboard, "index.html", focused=False)
    focus_like_the_app(keyboard, screen, monkeypatch)
    run(Desktop(CONFIG, Services()).type_in_app(CODE, app="code", field="index.html"))
    assert keyboard["focused"].name == "index.html" and keyboard["typed"] == [(9, CODE)]


def test_the_terminal_can_still_be_named_and_asks_first(screen, keyboard, monkeypatch) -> None:
    editor_on(screen, keyboard, "index.html")
    focus_like_the_app(keyboard, screen, monkeypatch)
    out = run(Desktop(CONFIG, Services()).type_in_app("npm start", app="code", field="terminal"))
    assert isinstance(out, NeedsConfirmation) and "into the terminal in code" in out.summary
    assert keyboard["typed"] == []


def test_a_whole_file_fits_in_an_editor_but_not_elsewhere(screen, keyboard) -> None:
    page = "<p>portfolio</p>\n" * 300  # about 5,100 characters, like the session's full page
    editor_on(screen, keyboard, "index.html")
    run(Desktop(CONFIG, Services()).type_in_app(page, app="code", field="index.html"))
    assert keyboard["typed"] == [(9, page)]
    screen["windows"] = [win("notepad.exe", "a.txt - Notepad", 3)]
    keyboard["focused"] = uia.Focused("document", "Text editor", False, True)
    with pytest.raises(ToolError, match="over 2000 characters.*in parts, in order"):
        run(Desktop(CONFIG, Services()).type_in_app(page, app="notepad"))
    editor_on(screen, keyboard, "index.html")
    with pytest.raises(ToolError, match="over 10000 characters"):
        run(Desktop(CONFIG, Services()).type_in_app(page * 3, app="code", field="index.html"))


def test_long_text_is_never_pasted_into_a_terminal(screen, keyboard, monkeypatch) -> None:
    editor_on(screen, keyboard, "index.html")
    focus_like_the_app(keyboard, screen, monkeypatch)
    with pytest.raises(ToolError, match="too long to paste into a terminal"):
        run(Desktop(CONFIG, Services()).type_in_app("echo hi\n" * 400, app="code", field="terminal"))
    assert keyboard["typed"] == []


def test_the_limits_come_from_config(screen, keyboard) -> None:
    editor_on(screen, keyboard, "index.html")
    d = Desktop({**CONFIG, "desktop": {"max_typed_editor": 50}}, Services())
    with pytest.raises(ToolError, match="over 50 characters"):
        run(d.type_in_app(CODE, app="code", field="index.html"))


def test_the_open_file_comes_from_the_editors_title() -> None:
    assert desktop.open_file(win("code.exe", "\u25cf index.html - portfolio - Visual Studio Code")) == "index.html"
    assert desktop.open_file(win("code.exe", "Visual Studio Code")) == ""
    assert desktop.open_file(win("notepad.exe", "a.txt - Notepad")) == ""


def test_after_reading_a_window_a_click_on_what_the_user_named_does_not_ask(screen, hands) -> None:
    """Seen live: after reading Claude's window, "go to JP dashboard" asked "Open the JP Dashboard?"."""
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    screen["controls"][7] = [Control(1, "button", "JP Dashboard", (0, 0, 90, 20)),
                             Control(2, "button", "JARVIS architecture dev", (0, 30, 90, 50)),
                             Control(3, "button", "Code, working", (0, 60, 90, 80)),
                             Control(4, "button", "Delete chat", (0, 90, 90, 110))]
    registry = type("R", (), {"last_user_text": "Can you go to JP dashboard in pinned section?"})()
    services = Services()
    services.dispatcher = type("D", (), {"tainted": True, "registry": registry})()
    d = Desktop(CONFIG, services)
    assert isinstance(run(d.click_in_app("JP Dashboard", app="claude")), str)
    assert isinstance(run(d.click_in_app("JARVIS architecture dev", app="claude")), NeedsConfirmation)  # not named
    registry.last_user_text = "Okay, go to Jarvis architecture tab again."
    assert isinstance(run(d.click_in_app("JARVIS architecture dev", app="claude")), str)
    registry.last_user_text = "No, go to code code"
    assert isinstance(run(d.click_in_app("Code, working", app="claude")), str)
    registry.last_user_text = "delete this chat"
    assert isinstance(run(d.click_in_app("Delete chat", app="claude")), NeedsConfirmation)   # risky still asks


def test_claude_gets_a_draft_until_the_user_says_send(screen, hands) -> None:
    """Seen live: JARVIS sent Claude nine messages in one conversation, including remarks meant for JARVIS."""
    chat_app(screen)
    registry = type("R", (), {"last_user_text": "Instead, why can't we just stick to my titles?"})()
    services = Services()
    services.dispatcher = type("D", (), {"tainted": False, "registry": registry})()
    d = Desktop(CONFIG, services)
    out = run(d.type_in_app("Could we stick to my titles?", app="claude", send=True))
    assert "didn't send it" in out and d.drafts == {7: "Could we stick to my titles?"}
    registry.last_user_text = "also say titles relevant to mine are fine"
    out = run(d.type_in_app("Could we stick to my titles, and ones relevant to them?", app="claude"))
    assert out.startswith('Replaced the unsent draft "Could we stick to my titles?" with') and         (7, "ctrl+a", 1) in hands["keys"]
    registry.last_user_text = "okay send it"
    assert run(d.type_in_app("", app="claude", send=True)) == "Sent the draft in claude." and d.drafts == {}
    registry.last_user_text = "ask Claude what a monad is"
    assert run(d.type_in_app("What is a monad?", app="claude", send=True)).endswith("pressed Enter.")
    with pytest.raises(ToolError, match="no draft"):
        run(d.type_in_app("", app="claude", send=True))


def test_a_sidebar_name_beats_chat_messages_that_quote_it() -> None:
    """Seen live: "JobPilot auto apply" matched two chat messages quoting it, and JARVIS clicked one."""
    controls = [
        Control(1, "list item", "Pinned section: “Switch to JobPilot auto apply in the pinned section.” It should "
                                "click it in Claude’s sidebar.", (0, 0, 900, 40)),
        Control(2, "list item", "Telling it where you are: “I’ve opened JobPilot auto apply.” It should just "
                                "acknowledge.", (0, 50, 900, 90)),
        Control(3, "button", "Job Pilot - Auto Apply", (0, 100, 200, 120)),
        Control(4, "button", "Job Pilot - Job Search", (0, 130, 200, 150)),
    ]
    found, _ = desktop.match_controls("JobPilot auto apply", controls)
    assert [c.id for c in found] == [3]
    found, _ = desktop.match_controls("auto apply", controls)
    assert [c.id for c in found] == [3]                  # a partial match skips long message text


def test_a_click_on_what_the_user_named_a_moment_ago_does_not_ask(screen, hands) -> None:
    screen["windows"] = [win("claude.exe", "Claude", 7)]
    screen["controls"][7] = [Control(41, "button", "Job Pilot - Auto Apply", (0, 0, 90, 20))]
    registry = type("R", (), {"last_user_text": "I'm still in that tab.", "recent_user_texts": [
        "Can you switch to job job pilot auto auto apply? In pin section.", "Okay. I'm still seeing Jarvis "
        "architecture done.", "I'm still in that tab."]})()
    services = Services()
    services.dispatcher = type("D", (), {"tainted": True, "registry": registry})()
    assert isinstance(run(Desktop(CONFIG, services).click_in_app("Job Pilot - Auto Apply", app="claude")), str)
    registry.recent_user_texts = ["I'm still in that tab."]
    assert isinstance(run(Desktop(CONFIG, services).click_in_app("Job Pilot - Auto Apply", app="claude")),
                      NeedsConfirmation)
