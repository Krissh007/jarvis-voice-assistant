"""Phase 6: app_ui_action safety rules and flow (UI Automation itself is faked)."""

import pytest

from actions import Services, app_ui
from actions.app_ui import Control, Snapshot, classify_steps
from core.registry import NeedsConfirmation, Tier
from tests.fakes import fake_live as fl


def notepad(active: str = "Untitled") -> Snapshot:
    return Snapshot(title=f"{active} - Notepad", controls=[
        Control(1, "Document", "Text editor", ""),
        Control(2, "TabItem", active, "", selected=True),
        Control(3, "Button", "Add New Tab", "AddButton"),
        Control(4, "MenuItem", "File", "File"),
        Control(5, "Button", "Settings", "SettingsButton"),
        Control(6, "Button", "User avatar", "AvatarButton"),
        Control(7, "Edit", "Password", "", password=True),
        Control(8, "Button", "Delete", ""),
    ])


def test_typing_in_a_normal_document_is_confirm_with_exact_steps() -> None:
    tier, lines, _ = classify_steps([{"action": "type", "id": 1, "text": "Hello"}], notepad(), "notepad")
    assert tier == Tier.CONFIRM and lines == ["type \"Hello\" into 'Text editor'"]


def test_secret_document_needs_a_new_tab_first() -> None:
    snap = notepad(".env. Unmodified.")
    tier, _, why = classify_steps([{"action": "type", "id": 1, "text": "x"}], snap, "notepad")
    assert tier == Tier.BLOCKED and "secrets" in why
    tier, lines, _ = classify_steps([{"action": "click", "id": 3}, {"action": "type", "id": 1, "text": "x"}],
                                    snap, "notepad")
    assert tier == Tier.CONFIRM and lines[0] == "click 'Add New Tab'"


@pytest.mark.parametrize("active", ["JARVIS API KEY.txt", "passwords.txt", "bank details", "id_rsa.pem", "tokens.json"])
def test_secret_looking_documents(active) -> None:
    assert app_ui.looks_secret(active)


@pytest.mark.parametrize("step, why", [
    ({"action": "click", "id": 5}, "settings"), ({"action": "click", "id": 6}, "account"),
    ({"action": "type", "id": 7, "text": "hunter2"}, "password"), ({"action": "click", "id": 8}, "destructive"),
    ({"action": "type", "id": 3, "text": "x"}, "text box"), ({"action": "key", "key": "ctrl+s"}, "isn't allowed"),
    ({"action": "click", "id": 99}, "isn't on screen"), ({"action": "run", "id": 1}, "isn't on screen"),
])
def test_blocked_steps(step, why) -> None:
    tier, _, reason = classify_steps([step], notepad(), "notepad")
    assert tier == Tier.BLOCKED and why in reason


def test_too_many_or_no_steps() -> None:
    assert classify_steps([], notepad(), "n")[0] == Tier.BLOCKED
    assert classify_steps([{"action": "key", "key": "enter"}] * 6, notepad(), "n")[0] == Tier.BLOCKED


class Planner:
    def __init__(self, steps):
        self.steps, self.prompts = steps, []

    async def generate(self, prompt, system="", max_tokens=800, json=False):
        import json as _j

        self.prompts.append(prompt)
        return _j.dumps({"steps": self.steps})


@pytest.fixture
def fake_uia(monkeypatch):
    ran = []
    monkeypatch.setattr(app_ui, "take_snapshot", lambda command: notepad(".env. Unmodified."))
    monkeypatch.setattr(app_ui, "run_steps", lambda command, steps, snap: ran.append(steps))
    return ran


CONFIG = {"apps": {"allowlist": {"notepad": "notepad.exe"}}}


async def test_plan_is_confirmed_before_anything_runs(tmp_path, fake_uia) -> None:
    planner = Planner([{"action": "click", "id": 3}, {"action": "type", "id": 1, "text": "Hello from JARVIS"}])
    ui = app_ui.AppUI(CONFIG, Services(llm=planner))
    h = fl.SafetyHarness(tmp_path)
    from core.registry import Tool

    h.registry.register(Tool(name="app_ui_action", description="x", func=ui.app_ui_action, tier=Tier.SAFE,
                             self_gated=True))
    resp = await h.model_calls("app_ui_action", app="Notepad", goal="type hello")
    assert resp["status"] == "awaiting_confirmation"
    assert resp["summary"] == "In notepad: click 'Add New Tab'; then type \"Hello from JARVIS\" into 'Text editor'"
    assert fake_uia == []
    await h.dispatcher.on_hotkey("confirm")
    assert fake_uia == [[{"action": "click", "id": 3}, {"action": "type", "id": 1, "text": "Hello from JARVIS"}]]
    assert "Done in notepad" in h.notices[-1]


async def test_planner_never_sees_document_text_or_password_boxes(fake_uia) -> None:
    planner = Planner([{"action": "click", "id": 3}])
    await app_ui.AppUI(CONFIG, Services(llm=planner)).app_ui_action("notepad", "x")
    prompt = planner.prompts[0]
    assert "Password" not in prompt and "looks like it holds secrets" in prompt
    assert '<untrusted_content source="notepad">' in prompt


async def test_blocked_plan_is_refused_without_asking(fake_uia) -> None:
    out = await app_ui.AppUI(CONFIG, Services(llm=Planner([{"action": "type", "id": 1, "text": "x"}]))) \
        .app_ui_action("notepad", "type x")
    assert isinstance(out, str) and "secrets" in out and fake_uia == []


async def test_unlisted_app_is_refused(fake_uia) -> None:
    assert "isn't on my list" in await app_ui.AppUI(CONFIG, Services(llm=Planner([]))).app_ui_action("regedit", "x")
