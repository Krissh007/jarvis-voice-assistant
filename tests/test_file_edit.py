"""Creating, changing and deleting the user's files: only in allowed folders, never private files."""

import asyncio
from pathlib import Path

import pytest

from actions import Services
from actions import file_edit
from actions.file_edit import FileEditor
from actions.files import make_entry
from core.registry import NeedsConfirmation, Registry, Tier, ToolError

PAGE = "<!DOCTYPE html>\n<html>\n<body>\n  <h1>Hello</h1>\n</body>\n</html>\n"


def run(x):
    return asyncio.run(x) if asyncio.iscoroutine(x) else x


class FakeDispatcher:
    tainted = False


@pytest.fixture
def home(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (tmp_path / "Elsewhere").mkdir()
    monkeypatch.setattr(file_edit, "PROJECT_DIR", (downloads / "jarvis").resolve())
    return tmp_path


@pytest.fixture
def ed(home):
    services = Services(data_dir=home / "data")
    services.dispatcher = FakeDispatcher()
    e = FileEditor({"file_edit": {"editor": ""}}, services)
    e._roots = {"Downloads": (home / "Downloads").resolve()}
    return e


def test_creates_a_new_file_and_its_folders(ed, home) -> None:
    out = ed.write_file("Downloads/portfolio/index.html", PAGE)
    assert (home / "Downloads/portfolio/index.html").read_text(encoding="utf-8") == PAGE
    assert out == f"Created index.html in Downloads\\portfolio (6 lines, {len(PAGE)} characters)."


def test_an_existing_file_is_only_replaced_with_overwrite_and_its_old_version_is_kept(ed, home) -> None:
    f = home / "Downloads/style.css"
    f.write_bytes(b"body {}\r\n")
    with pytest.raises(ToolError, match="already exists in Downloads. Use edit_file"):
        ed.write_file("Downloads/style.css", "h1 {}\n")
    out = ed.write_file("Downloads/style.css", "h1 {}\nh2 {}\n", overwrite=True)
    assert out.startswith("Replaced everything in style.css") and f.read_bytes() == b"h1 {}\r\nh2 {}\r\n"  # CRLF kept
    backups = list((home / "data/file_backups").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == b"body {}\r\n"


def test_edit_replaces_exact_text_once(ed, home) -> None:
    (home / "Downloads/index.html").write_text(PAGE, encoding="utf-8")
    out = ed.edit_file("Downloads/index.html", find="<h1>Hello</h1>", replace="<h1>Krishna</h1>")
    assert "<h1>Krishna</h1>" in (home / "Downloads/index.html").read_text(encoding="utf-8")
    assert out == f"Changed index.html in Downloads: replaced 1 place (now 6 lines, {len(PAGE) + 2} characters)."


def test_edit_refuses_text_it_cant_find_or_that_is_ambiguous(ed, home) -> None:
    (home / "Downloads/a.css").write_text("p {}\np {}\n", encoding="utf-8")
    with pytest.raises(ToolError, match="couldn't find that text"):
        ed.edit_file("Downloads/a.css", find="h1 {}", replace="h2 {}")
    with pytest.raises(ToolError, match="2 times"):
        ed.edit_file("Downloads/a.css", find="p {}", replace="div {}")
    assert ed.edit_file("Downloads/a.css", find="p {}", replace="div {}", replace_all=True).endswith(
        "replaced 2 places (now 2 lines, 14 characters).")


def test_edit_can_append(ed, home) -> None:
    (home / "Downloads/a.css").write_text("p {}", encoding="utf-8")
    ed.edit_file("Downloads/a.css", append="footer {}\n")
    assert (home / "Downloads/a.css").read_text(encoding="utf-8") == "p {}\nfooter {}\n"


def test_a_bare_name_goes_in_the_folder_last_used(ed, home) -> None:
    ed.write_file("Downloads/portfolio/index.html", PAGE)
    ed.write_file("script.js", "console.log(1);\n")
    assert (home / "Downloads/portfolio/script.js").exists()


def test_a_bare_name_is_found_by_name_when_theres_no_folder_yet(ed, home) -> None:
    f = home / "Downloads/site/index.html"
    f.parent.mkdir()
    f.write_text(PAGE, encoding="utf-8")

    class Index:
        def entries(self):
            return [make_entry(f, "Downloads", "site", 0, False)]
    ed.services.files = Index()
    assert "index.html in Downloads\\site" in ed.read_file("index.html")


def test_a_bare_name_with_no_folder_asks_which_folder(ed) -> None:
    with pytest.raises(ToolError, match="Which folder should notes.txt be in"):
        ed.write_file("notes.txt", "hi")


@pytest.mark.parametrize("path", ["Elsewhere/x.txt", "Downloads/../Elsewhere/x.txt", "Downloads/jarvis/config.yaml"])
def test_nothing_outside_the_allowed_folders_or_in_jarvis_itself(ed, home, path) -> None:
    with pytest.raises(ToolError):
        ed.write_file(str(home / path) if path.startswith("Elsewhere") else path, "hi")
    assert not (home / "Elsewhere/x.txt").exists() and not (home / "Downloads/jarvis/config.yaml").exists()


@pytest.mark.parametrize("name", [".env", "passwords.txt", "secrets/notes.txt", "my_api_key.txt"])
def test_private_files_are_never_touched(ed, name) -> None:
    with pytest.raises(ToolError, match="private file"):
        ed.write_file(f"Downloads/{name}", "hi")


@pytest.mark.parametrize("name", ["run.bat", "setup.ps1", "x.vbs", "link.lnk", "app.exe"])
def test_programs_and_scripts_windows_runs_are_never_written(ed, name) -> None:
    with pytest.raises(ToolError, match="program, script or shortcut"):
        ed.write_file(f"Downloads/{name}", "echo hi")


@pytest.mark.parametrize("name", ["con.txt", "notes.txt.", "a:stream.txt"])
def test_names_windows_would_mangle_are_refused(ed, name) -> None:
    with pytest.raises(ToolError, match="isn't a name I can use"):
        ed.write_file(f"Downloads/{name}", "hi")


def test_web_code_and_dotfiles_are_fine(ed, home) -> None:
    ed.write_file("Downloads/p/script.js", "let a = 1;\n")
    ed.write_file("Downloads/p/.gitignore", "node_modules\n")
    assert (home / "Downloads/p/.gitignore").exists() and (home / "Downloads/p/script.js").exists()


def test_secrets_are_never_written(ed) -> None:
    with pytest.raises(ToolError, match="looks like it contains"):
        ed.write_file("Downloads/a.txt", "card 4111 1111 1111 1111")


def test_read_hides_secrets_and_refuses_binary_or_huge_files(ed, home) -> None:
    (home / "Downloads/a.txt").write_text("card 4111 1111 1111 1111\nhello\n", encoding="utf-8")
    out = ed.read_file("Downloads/a.txt")
    assert "4111" not in out and "[hidden]" in out and "hello" in out
    (home / "Downloads/b.dat").write_bytes(b"\0\1\2")
    with pytest.raises(ToolError, match="isn't a text file"):
        ed.read_file("Downloads/b.dat")
    (home / "Downloads/big.txt").write_text("x" * (201 * 1024), encoding="utf-8")
    with pytest.raises(ToolError, match="over 200 KB"):
        ed.read_file("Downloads/big.txt")


def test_delete_moves_to_the_recycle_bin_and_always_asks(ed, home, monkeypatch) -> None:
    f = home / "Downloads/old.js"
    f.write_text("x", encoding="utf-8")
    binned = []
    monkeypatch.setattr(file_edit, "recycle", lambda p: (binned.append(p), p.unlink()))
    assert ed.delete_file("Downloads/old.js") == "Moved old.js from Downloads to the Recycle Bin."
    assert binned == [f.resolve()]
    reg = Registry()
    file_edit.register(reg, {"file_edit": {}}, ed.services)
    tool = reg.get("delete_file")
    assert tool.base_tier({}) == Tier.CONFIRM and not tool.self_gated
    with pytest.raises(ToolError, match="no missing.txt"):
        ed.delete_file("Downloads/missing.txt")


def test_after_untrusted_text_only_the_file_just_read_changes_without_asking(ed, home) -> None:
    (home / "Downloads/index.html").write_text(PAGE, encoding="utf-8")
    ed.read_file("Downloads/index.html")
    ed.services.dispatcher.tainted = True
    out = ed.edit_file("Downloads/index.html", find="Hello", replace="Hi")
    assert isinstance(out, str) and "Hi" in (home / "Downloads/index.html").read_text(encoding="utf-8")
    other = ed.write_file("Downloads/other.html", PAGE)
    assert isinstance(other, NeedsConfirmation) and not (home / "Downloads/other.html").exists()
    assert run(other.resume()).startswith("Created other.html")


def test_open_opens_it_in_vs_code(ed, home, monkeypatch) -> None:
    exe = home / "Code.exe"
    exe.write_text("")
    launched = []
    monkeypatch.setattr(file_edit, "CODE_EXE", exe)
    monkeypatch.setattr(file_edit.subprocess, "Popen", lambda args, **kw: launched.append(args))
    ed.editor = "code"
    out = ed.write_file("Downloads/a.html", PAGE, open=True)
    assert out.endswith("and opened it in VS Code.")
    assert launched == [[str(exe), "--reuse-window", str((home / "Downloads/a.html").resolve())]]


def test_the_tools_register_and_reading_is_untrusted() -> None:
    reg = Registry()
    file_edit.register(reg, {"file_edit": {}}, Services())
    assert reg.get("read_file").untrusted_output and reg.get("read_file").read_only
    assert reg.get("write_file").self_gated and reg.get("edit_file").self_gated
