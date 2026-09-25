"""Phase 7: screen and clipboard, with the privacy rules."""

import io

import pytest

from actions import Services, clipboard, screen
from core.privacy import redact_reversible, restore, secret_in_text
from core.registry import NeedsConfirmation
from tests.fakes import fake_live as fl


class FakeLLM:
    def __init__(self, reply="ok") -> None:
        self.reply, self.calls = reply, []

    async def generate(self, prompt, system="", max_tokens=800, json=False, images=None):
        self.calls.append({"prompt": prompt, "images": images})
        return self.reply(prompt) if callable(self.reply) else self.reply


# ---------------------------------------------------------------- secret detection

@pytest.mark.parametrize("text, kind", [
    ("GOOGLE_API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu", "a Google API key"),
    ("key: sk-proj-abcdefghijklmnopqrstuvwx", "an OpenAI-style key"),
    ("TAVILY_API_KEY=tvly-dev-abcdefghijklmnop", "a Tavily key"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "a GitHub token"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "a private key"),
    ("DB_PASSWORD=hunter2hunter2", "a password or key setting"),
    ("pay with 4111 1111 1111 1111 please", "a card number"),
    ("session Zx9Qm2LpVr7Tn4Kw8Yb3Hc6Df1Gs5Ja", "a long random token"),
])
def test_secrets_are_recognised(text, kind) -> None:
    assert secret_in_text(text) == kind


@pytest.mark.parametrize("text", [
    "Meeting at 10am tomorrow, bring the report.", "Order 1234 5678 9012 3456 shipped",
    "https://github.com/pandu/jarvis-agent/blob/main/README.md", "Version 2.3.1, build 20260925",
    "Password reset instructions are in the handbook.", "नमस्ते, आप कैसे हैं?",
])
def test_ordinary_text_is_not_a_secret(text) -> None:
    assert secret_in_text(text) is None


def test_reversible_redaction_round_trip() -> None:
    safe, mapping = redact_reversible("Call +91 98765 43210 or mail a.b@gmail.com today")
    assert "98765" not in safe and "gmail" not in safe
    assert restore(safe, mapping) == "Call +91 98765 43210 or mail a.b@gmail.com today"


# ---------------------------------------------------------------- clipboard

@pytest.fixture
def board(monkeypatch):
    state = {"text": ""}
    monkeypatch.setattr(clipboard, "read_clipboard", lambda: state["text"])
    monkeypatch.setattr(clipboard, "write_clipboard", lambda t: state.update(text=t))
    return state


async def test_translate_sends_placeholders_not_contact_details(board) -> None:
    board["text"] = "Please call me on +91 98765 43210 tomorrow."
    llm = FakeLLM(lambda p: "कृपया मुझे कल [PHONE1] पर कॉल करें।")
    out = await clipboard.Clipboard(Services(llm=llm)).clipboard_action("translate", "Hindi")
    assert out == "कृपया मुझे कल +91 98765 43210 पर कॉल करें।"
    prompt = llm.calls[0]["prompt"]
    assert "98765" not in prompt and "Hindi" in prompt and '<untrusted_content source="clipboard">' in prompt


async def test_secret_clipboard_is_never_sent(board) -> None:
    board["text"] = "GOOGLE_API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu"
    llm = FakeLLM()
    out = await clipboard.Clipboard(Services(llm=llm)).clipboard_action("summarize")
    assert "won't send it anywhere" in out and llm.calls == []


@pytest.mark.parametrize("text, message", [("", "no text"), ("   ", "no text"), ("x" * 20_001, "too long")])
async def test_empty_or_huge_clipboard(board, text, message) -> None:
    board["text"] = text
    assert message in await clipboard.Clipboard(Services(llm=FakeLLM())).clipboard_action("explain")


async def test_fix_previews_and_waits_for_confirmation(board, tmp_path) -> None:
    board["text"] = "their going too the park tomorow"
    llm = FakeLLM("They're going to the park tomorrow.")
    h = fl.SafetyHarness(tmp_path)
    from core.registry import Tier, Tool

    h.registry.register(Tool(name="clipboard_fix", description="x", func=clipboard.Clipboard(Services(llm=llm)).clipboard_fix,
                             tier=Tier.SAFE, self_gated=True, untrusted_output=True))
    resp = await h.model_calls("clipboard_fix")
    assert resp["status"] == "awaiting_confirmation"
    assert resp["summary"] == ('Replace the copied text with the corrected version (6 words), starting: '
                               '"They\'re going to the park tomorrow."')
    assert board["text"] == "their going too the park tomorow"  # unchanged until confirmed
    await h.dispatcher.on_hotkey("confirm")
    assert board["text"] == "They're going to the park tomorrow."


async def test_fix_does_not_overwrite_a_clipboard_that_changed(board) -> None:
    board["text"] = "teh cat"
    paused = await clipboard.Clipboard(Services(llm=FakeLLM("The cat"))).clipboard_fix()
    assert isinstance(paused, NeedsConfirmation)
    board["text"] = "something new the user copied"
    assert "didn't overwrite" in await paused.resume()
    assert board["text"] == "something new the user copied"


async def test_fix_keeps_contact_details(board) -> None:
    board["text"] = "mail me at a.b@gmail.com pls"
    paused = await clipboard.Clipboard(Services(llm=FakeLLM("Please email me at [EMAIL1]."))).clipboard_fix()
    await paused.resume()
    assert board["text"] == "Please email me at a.b@gmail.com."


# ---------------------------------------------------------------- screen

def test_private_windows_are_blacked_out_on_a_real_capture(monkeypatch) -> None:
    from PIL import Image

    monkeypatch.setattr(screen, "private_windows", lambda: [(".env - Notepad", (100, 100, 700, 500))])
    data, hidden = screen.capture()
    assert hidden == [".env - Notepad"]
    img = Image.open(io.BytesIO(data)).convert("L")
    scale = img.width / 1920 if img.width < 1920 else 1
    box = (int(150 * scale), int(150 * scale), int(650 * scale), int(450 * scale))
    pixels = list(img.crop(box).getdata())
    assert sum(pixels) / len(pixels) < 8  # black (JPEG noise aside)


def test_capture_is_shrunk_and_never_written_to_disk(monkeypatch, tmp_path) -> None:
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(screen, "private_windows", lambda: [])
    data, _ = screen.capture()
    assert Image.open(io.BytesIO(data)).width <= screen.MAX_WIDTH
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("title, secret", [(".env - Notepad", True), ("JARVIS API KEY.txt - Notepad", True),
                                           ("Passwords - Bitwarden", True), ("YouTube Music", False)])
def test_secret_window_titles(title, secret) -> None:
    from actions.app_ui import looks_secret

    assert looks_secret(title) is secret


async def test_look_at_screen_sends_one_image_and_mentions_hidden_windows(monkeypatch) -> None:
    monkeypatch.setattr(screen, "capture", lambda: (b"jpeg", [".env - Notepad"]))
    llm = FakeLLM("A code editor and a music player.")
    out = await screen.Screen(Services(llm=llm)).look_at_screen("what's on screen?")
    assert llm.calls[0]["images"] == [b"jpeg"]
    assert out.startswith("A code editor and a music player.") and "blacked out" in out


async def test_active_window_hides_secret_titles(monkeypatch) -> None:
    monkeypatch.setattr(screen, "foreground_title", lambda: ".env - Notepad")
    assert "looks private" in await screen.Screen(Services()).active_window()
    monkeypatch.setattr(screen, "foreground_title", lambda: "Inbox (3) - a.b@gmail.com")
    assert "[email]" in await screen.Screen(Services()).active_window()


async def test_says_when_nothing_new_was_copied(board, monkeypatch) -> None:
    seq = {"n": 7}
    monkeypatch.setattr(clipboard, "clipboard_sequence", lambda: seq["n"])
    board["text"] = "=HYPERLINK(MID(O2,FIND(\"](\",O2)+2))"
    clip = clipboard.Clipboard(Services(llm=FakeLLM("an Excel formula")))
    first = await clip.clipboard_action("explain")
    assert "nothing new has been copied" not in first
    again = await clip.clipboard_action("translate", "Telugu")  # user thinks they copied new text
    assert "nothing new has been copied" in again
    seq["n"] = 8  # a real copy happened
    board["text"] = "Hello"
    assert "nothing new has been copied" not in await clip.clipboard_action("translate", "Telugu")


async def test_echoed_wrapper_tags_never_reach_the_clipboard(board) -> None:
    board["text"] = "helo from jarvis"
    echo = '<untrusted_content source="clipboard">\nHello from JARVIS.\n</untrusted_content>'
    paused = await clipboard.Clipboard(Services(llm=FakeLLM(echo))).clipboard_fix()
    assert "untrusted_content" not in paused.summary
    await paused.resume()
    assert board["text"] == "Hello from JARVIS."
