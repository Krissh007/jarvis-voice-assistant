"""Finding and opening the user's own files: matching, privacy filters, never running programs, taint."""

import os
import time
from datetime import datetime

import pytest

from actions import files
from actions.files import FileTools, clear_winner, is_hidden_name, make_entry, scan, search, spoken_age, tokens

NOW = time.time()
DAY = 86400


def entry(name, root="Documents", rel="", age_days=0.0, is_dir=False):
    return make_entry(os.path.join("C:\\Users\\me", root, rel, name), root, rel, NOW - age_days * DAY, is_dir)


def test_names_split_into_words() -> None:
    assert tokens("MyResume_2024-final.PDF") == ["my", "resume", "2024", "final", "pdf"]
    assert tokens("Résumé (1).docx") == ["resume", "1", "docx"]
    assert tokens("taxReturn2023") == ["tax", "return", "2023"]


@pytest.mark.parametrize("name", ["passwords.txt", "My Password List.xlsx", "Aadhaar card.pdf", "aadhar.jpg",
                                  "PAN card scan.png", "passport front.jpg", "google-backup-codes.txt", "id_rsa",
                                  ".env", "prod.env", "server.pem", "vault.kdbx", "credit card.png",
                                  "github recovery codes.txt", "seed phrase.txt", "api_key.txt"])
def test_private_looking_names_are_hidden(name) -> None:
    assert is_hidden_name(name)


def test_ordinary_names_are_not_hidden_and_extra_patterns_work() -> None:
    for name in ["Resume.pdf", "Invoice March.pdf", "holiday.jpg", "company.pptx", "keynote.txt"]:
        assert not is_hidden_name(name), name
    assert is_hidden_name("HDFC bank statement.pdf", ["*bank*"])


def test_best_match_first_and_newest_breaks_ties() -> None:
    entries = [entry("old resume.docx", age_days=400), entry("Resume.pdf", age_days=3), entry("resume draft.pdf", age_days=1),
               entry("notes.txt")]
    found = search(entries, "resume", now=NOW)
    assert [e.name for e in found] == ["Resume.pdf", "resume draft.pdf", "old resume.docx"]  # exact name wins
    assert clear_winner("resume", found) is found[0]  # "Resume" is the whole name: open it without asking


def test_several_equally_good_matches_are_not_opened_blindly() -> None:
    found = search([entry("resume google.pdf"), entry("resume amazon.pdf")], "resume", now=NOW)
    assert len(found) == 2 and clear_winner("resume", found) is None


def test_filters_kind_date_and_folder() -> None:
    entries = [entry("trip.jpg", "Pictures", age_days=10), entry("trip.pdf", "Downloads", age_days=0.2),
               entry("trip", "Documents", is_dir=True), entry("setup.exe", "Downloads", age_days=0.1)]
    assert [e.name for e in search(entries, "trip", kind="image", now=NOW)] == ["trip.jpg"]
    assert [e.name for e in search(entries, "trip", kind="folder", now=NOW)] == ["trip"]
    assert [e.name for e in search(entries, "", within_days=1, now=NOW)] == ["setup.exe", "trip.pdf"]  # newest first
    assert [e.name for e in search(entries, "", folder="downloads", now=NOW)] == ["setup.exe", "trip.pdf"]
    assert search(entries, "trip", kind="spreadsheet", now=NOW) == []


def test_small_typos_and_extra_words_still_match() -> None:
    entries = [entry("Invoice March 2026.pdf"), entry("holiday photos", is_dir=True)]
    assert [e.name for e in search(entries, "invioce", now=NOW)] == ["Invoice March 2026.pdf"]
    assert [e.name for e in search(entries, "march invoice thing stuff", now=NOW)] == []  # two words missing
    assert [e.name for e in search(entries, "march invoice document", now=NOW)] == ["Invoice March 2026.pdf"]


def test_spoken_age() -> None:
    now = datetime(2026, 9, 26, 12)
    assert spoken_age(datetime(2026, 9, 26, 1).timestamp(), now) == "today"
    assert spoken_age(datetime(2026, 9, 25, 23).timestamp(), now) == "yesterday"
    assert spoken_age(datetime(2026, 9, 22).timestamp(), now) == "4 days ago"
    assert spoken_age(datetime(2026, 3, 2).timestamp(), now) == "on 2 March"
    assert spoken_age(datetime(2024, 3, 2).timestamp(), now) == "in March 2024"


def _hide(path) -> None:
    import ctypes

    ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN


def test_scan_skips_hidden_private_and_tool_folders(tmp_path) -> None:
    root = tmp_path / "Stuff"
    (root / "work" / "node_modules" / "lib").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "work" / "report.docx").write_text("x")
    (root / "work" / "node_modules" / "lib" / "report.js").write_text("x")
    (root / ".git" / "config").write_text("x")
    (root / "passwords.txt").write_text("x")
    (root / "secret-ish hidden.txt").write_text("x")
    (root / "desktop.ini").write_text("x")
    _hide(root / "desktop.ini")
    entries, complete = scan({"Stuff": root})
    names = sorted(e.name for e in entries)
    assert complete and names == ["Stuff", "report.docx", "work"]
    report = next(e for e in entries if e.name == "report.docx")
    assert report.root == "Stuff" and report.rel_dir == "work"


@pytest.fixture
def tools(tmp_path, monkeypatch):
    opened, revealed = [], []
    monkeypatch.setattr(files, "_launch", opened.append)
    monkeypatch.setattr(files, "_reveal", revealed.append)
    root = tmp_path / "Docs"
    (root / "cv").mkdir(parents=True)
    for name in ["Resume.pdf", "cv/resume old.docx", "budget march.xlsx", "budget april.xlsx", "installer.exe"]:
        (root / name).write_text("x")
    t = FileTools({"files": {"folders": [], "extra_folders": [str(root)]}})
    t.opened, t.revealed, t.root = opened, revealed, root
    return t


def test_find_then_open_by_number(tools) -> None:
    out = tools.find_files("budget")
    assert out.startswith("Found 2:") and '"budget march" (Excel sheet)' in out and "Excel sheet" in out
    assert "1. " in out and "open_file" in out
    number = 1 + [e.name for e in tools.last].index("budget april.xlsx")
    assert tools.open_file(number=number).startswith('Opened "budget april" (Excel sheet)')
    assert tools.opened == [str(tools.root / "budget april.xlsx")]
    assert "don't have a result" in tools.open_file(number=9)


def test_open_by_name_opens_a_clear_match_and_asks_otherwise(tools) -> None:
    assert tools.open_file(query="resume").startswith('Opened "Resume" (PDF)')
    out = tools.open_file(query="budget")
    assert out.startswith("I found 2") and "which one" in out and len(tools.opened) == 1
    assert len(tools.last) == 2  # "the second one" works next


def test_programs_are_never_opened_but_can_be_shown(tools) -> None:
    out = tools.open_file(query="installer")
    assert "won't open it" in out and tools.opened == []
    assert tools.open_file(query="installer", show_in_folder=True) == "Showing installer.exe in File Explorer."
    assert tools.revealed == [tools.root / "installer.exe"] and tools.opened == []


def test_new_downloads_show_up_without_waiting_for_the_index(tools) -> None:
    assert "couldn't find" in tools.find_files("ticket")
    (tools.root / "train ticket.pdf").write_text("x")  # arrives after the index was built
    assert '"train ticket" (PDF)' in tools.find_files("ticket")
    (tools.root / "train ticket.pdf").unlink()
    assert "couldn't find" in tools.find_files("ticket")


def test_a_file_that_vanished_is_not_opened(tools) -> None:
    tools.find_files("resume")
    (tools.root / "Resume.pdf").unlink()
    assert "isn't there any more" in tools.open_file(number=1) and tools.opened == []


def test_unknown_folder_or_kind_is_refused(tools) -> None:
    from core.registry import ToolError

    with pytest.raises(ToolError):
        tools.find_files("x", folder="Windows")
    with pytest.raises(ToolError):
        tools.find_files("x", kind="executable")


async def test_file_names_are_untrusted_so_opening_in_the_same_turn_is_confirmed(tmp_path, monkeypatch) -> None:
    from tests.fakes import fake_live as fl

    opened = []
    monkeypatch.setattr(files, "_launch", opened.append)
    root = tmp_path / "Docs"
    root.mkdir()
    (root / "a report.pdf").write_text("x")
    (root / "b report.pdf").write_text("x")
    h = fl.SafetyHarness(tmp_path, config={"features": {"files": True},
                                           "files": {"folders": [], "extra_folders": [str(root)]}})
    found = await h.model_calls("find_files", query="report")
    assert found["status"] == "ok" and "untrusted_content" in str(found)
    resp = await h.model_calls("open_file", number=1)
    assert resp["status"] == "awaiting_confirmation" and opened == []
    assert resp["summary"].startswith('Open "') and 'report" (PDF)' in resp["summary"]
    await h.speak_summary()
    await h.user_says("yes")
    assert len(opened) == 1

    await h.user_says("now open the other one")  # a new turn from the user: no taint, no question
    resp = await h.model_calls("open_file", number=2)
    assert resp["status"] == "ok" and len(opened) == 2
