"""Task mode: JARVIS keeps working on one goal turn after turn, checks its work, then reports."""

import asyncio

import pytest

from actions import Services, file_edit
from actions.file_edit import FileEditor
from core.registry import NeedsConfirmation, Registry, Tier, ToolError
from core.task import TaskMode, register


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "Downloads" / "portfolio").mkdir(parents=True)
    monkeypatch.setattr(file_edit, "PROJECT_DIR", (tmp_path / "jarvis").resolve())
    return tmp_path


@pytest.fixture
def ed(home):
    services = Services(data_dir=home / "data")
    services.dispatcher = type("D", (), {"tainted": False})()
    e = FileEditor({"file_edit": {"editor": ""}}, services)
    e._roots = {"Downloads": (home / "Downloads").resolve()}
    return e


@pytest.fixture
def tasks(ed):
    clock = Clock()
    t = TaskMode({"task": {"max_rounds": 5, "max_minutes": 10, "stall_rounds": 2}}, notify=lambda _: None,
                 resolve_folder=ed.resolve_folder, clock=clock)
    t.clock_ref = clock
    ed.services.task = t
    return t


def test_a_task_keeps_going_after_each_turn_until_finished(tasks) -> None:
    out = tasks.start_task("Build a complete professional portfolio website", "Downloads/portfolio")
    assert out.startswith("Task started.") and "without asking" in out and "deleting still asks" in out
    tasks.tool_called("write_file")
    notice = tasks.next_notice()
    assert notice.startswith("[system notice] Task in progress (round 1 of 5)")
    assert "Build a complete professional portfolio website" in notice and "finish_task" in notice


def test_the_first_finish_is_a_check_the_second_ends_the_task(tasks) -> None:
    finished = []
    tasks.on_finished = lambda: finished.append(True)
    tasks.start_task("Build my portfolio website")
    check = tasks.finish_task("done")
    assert check.startswith("Not finished yet: check first") and "read_file" in check
    assert "lorem ipsum" in check  # a website gets the website checklist
    assert tasks.active and not finished
    out = tasks.finish_task("Built all three pages")
    assert out.startswith("Task finished.") and "Built all three pages" in out
    assert not tasks.active and finished == [True]
    assert tasks.next_notice() is None


def test_non_code_goals_get_no_website_checklist(tasks) -> None:
    tasks.start_task("Write a packing list for my Goa trip")
    assert "lorem ipsum" not in tasks.finish_task()


def test_turns_without_tools_count_as_stalled(tasks) -> None:
    tasks.start_task("Write a packing list for my trip")
    assert tasks.next_notice().startswith("[system notice] Task in progress")  # quiet round 1
    stalled = tasks.next_notice()  # quiet round 2
    assert "stalled" in stalled and not tasks.active


def test_a_tool_call_resets_the_stall_count(tasks) -> None:
    tasks.start_task("Write a packing list for my trip")
    tasks.next_notice()
    tasks.tool_called("edit_file")
    assert "Task in progress (round 2" in tasks.next_notice()
    assert "Task in progress (round 3" in tasks.next_notice()


def test_round_and_time_limits_stop_with_a_report(tasks) -> None:
    tasks.start_task("Write a packing list for my trip")
    for _ in range(5):
        tasks.tool_called("edit_file")
        assert "Task in progress" in tasks.next_notice()
    tasks.tool_called("edit_file")
    assert "reached its time or step limit" in tasks.next_notice() and not tasks.active
    tasks.start_task("Write a packing list for my trip")
    tasks.clock_ref.t += 11 * 60
    tasks.tool_called("edit_file")
    assert "limit" in tasks.next_notice() and not tasks.active


def test_stop_and_status(tasks) -> None:
    assert tasks.stop_task() == "No task was running." and not tasks.stop()
    tasks.start_task("Write a packing list for my trip")
    tasks.clock_ref.t += 125
    assert tasks.status() == 'Working on "Write a packing list for my trip": round 0, 2 minutes so far.'
    assert tasks.stop() and not tasks.active


def test_a_new_task_replaces_the_old_one(tasks) -> None:
    tasks.start_task("Write a packing list for my trip")
    out = tasks.start_task("Build my portfolio website")
    assert 'stopped the previous task: "Write a packing list' in out


def test_a_vague_goal_or_a_folder_outside_the_allowed_ones_is_refused(tasks, home) -> None:
    with pytest.raises(ToolError, match="finished result"):
        tasks.start_task("do")
    with pytest.raises(ToolError):
        tasks.start_task("Build my portfolio website", str(home / "Elsewhere"))
    with pytest.raises(ToolError):
        tasks.start_task("Build my portfolio website", "Downloads/jarvis/../../Elsewhere")
    assert not tasks.active


def test_in_the_task_folder_edits_dont_ask_even_after_untrusted_text(tasks, ed, home) -> None:
    page = home / "Downloads/portfolio/index.html"
    page.write_text("<h1>Hi</h1>\n", encoding="utf-8")
    (home / "Downloads/notes.txt").write_text("x\n", encoding="utf-8")
    ed.services.dispatcher.tainted = True
    ed.read_file("Downloads/notes.txt")  # the last file read is elsewhere
    assert isinstance(ed.edit_file("Downloads/portfolio/index.html", find="Hi", replace="Hello"),
                      NeedsConfirmation)  # no task yet: asks
    tasks.start_task("Build my portfolio website", "Downloads/portfolio")
    ed.read_file("Downloads/notes.txt")
    assert ed.edit_file("Downloads/portfolio/index.html", find="Hi", replace="Hello").startswith("Changed")
    assert ed.write_file("Downloads/portfolio/about.html", "<p>About</p>\n").startswith("Created")
    # outside the task folder it still asks
    ed.read_file("Downloads/portfolio/index.html")
    assert isinstance(ed.write_file("Downloads/other.txt", "y\n"), NeedsConfirmation)
    tasks.stop_task()
    ed.read_file("Downloads/notes.txt")
    assert isinstance(ed.edit_file("Downloads/portfolio/index.html", find="Hello", replace="Hey"), NeedsConfirmation)


def test_tools_are_registered_with_safe_tiers(tasks) -> None:
    reg = Registry()
    register(reg, tasks)
    assert reg.get("start_task").base_tier({}) == Tier.SAFE and not reg.get("start_task").read_only  # asks after untrusted text
    assert all(reg.get(n).read_only for n in ("finish_task", "stop_task", "task_status"))
    summary = reg.get("start_task").summary({"goal": "Build my portfolio", "folder": "Downloads"})
    assert summary == "work on this until it's done: Build my portfolio, changing files in Downloads without asking"


# ---------------------------------------------------------------- runtime


@pytest.fixture
def assistant(monkeypatch):
    import main
    from core.runtime import Assistant, Options

    cfg = main.load_config()
    cfg["listening"]["mode"] = "push_to_talk"
    a = Assistant(cfg, client=None, options=Options())
    notices = []
    monkeypatch.setattr(a, "_notify", notices.append)
    a.tasks.notify = notices.append
    a.tasks.pause_s = 0.01
    a._last_heard = 0.0
    return a, notices


async def test_runtime_sends_keep_going_after_each_turn(assistant) -> None:
    a, notices = assistant
    a.tasks.start_task("Write a packing list for my trip")
    await a.on_tool_call("get_datetime", {}, None)
    a.on_turn_complete()
    await asyncio.wait_for(a._task_next, 2)
    assert len(notices) == 1 and notices[0].startswith("[system notice] Task in progress (round 1")
    assert a._tray_state() == "working"


async def test_runtime_waits_while_the_user_speaks_and_the_stop_key_ends_the_task(assistant) -> None:
    a, notices = assistant
    a.tasks.pause_s = 0.5
    a.tasks.start_task("Write a packing list for my trip")
    a.on_turn_complete()
    a.on_input_transcript("how's it going")
    await asyncio.sleep(0.3)
    assert notices == []  # the user is talking: wait
    a.on_hotkey("stop", True)
    await asyncio.sleep(0.05)
    assert not a.tasks.active and notices == []


async def test_runtime_no_task_no_notice(assistant) -> None:
    a, notices = assistant
    a.on_turn_complete()
    await asyncio.sleep(0.05)
    assert notices == [] and a._task_next is None


GOOD_PAGE = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="style.css"></head><body>
<nav><a href="#about">About</a><a href="#contact">Contact</a></nav>
<section id="about"><h2>About</h2><p>Hi, I'm Krishna.</p></section>
<section id="skills"></section><section id="projects"></section><section id="experience"></section>
<section id="contact"><form><input name="email" type="email"><button type="submit">Send</button></form></section>
<img src="me.png" alt="Krishna"><script src="script.js"></script></body></html>"""


def test_site_check_finds_what_a_quick_self_review_misses(home) -> None:
    from core.task import site_problems

    site = home / "Downloads/portfolio"
    (site / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="style.css"></head><body><h1>Alex Morgan portfolio</h1>'
        '<a href="#about">About</a><section id="projects"><h3>Project 1</h3></section>'
        '<form><input type="text"></form><img src="x.png"><script src="app.js"></script></body></html>',
        encoding="utf-8")
    (site / "style.css").write_text("body { color: red; }", encoding="utf-8")
    problems = site_problems(site, "Krishna")
    assert problems == [
        "index.html links app.js, which doesn't exist",
        'index.html has a link to #about but no element with id="about"',
        'index.html still has placeholder text ("Project 1")',
        "index.html has form fields without a name attribute, so the form sends nothing",
        "index.html has images without alt text",
        "index.html has no viewport meta tag, so phones show it zoomed out",
        "the CSS has no @media rules, so there's no mobile layout",
        "no about, skills, experience, contact sections",
        "it never mentions the user (Krishna); it should be about them, not an invented person, unless they "
        "asked for someone else",
    ]
    (site / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
    (site / "style.css").write_text("@media (max-width: 600px) { nav { display: block; } }", encoding="utf-8")
    (site / "script.js").write_text("", encoding="utf-8")
    assert site_problems(site, "Krishna") == []
    assert site_problems(home / "Downloads", "Krishna") == []  # no pages: nothing to check


def test_finish_is_refused_while_the_site_has_problems_then_reports_them(tasks, home) -> None:
    site = home / "Downloads/portfolio"
    (site / "index.html").write_text("<html><body>portfolio</body></html>", encoding="utf-8")
    tasks.user_name = lambda: "Krishna"
    tasks.start_task("Build my portfolio website", "Downloads/portfolio")
    first = tasks.finish_task()
    assert first.startswith("Not finished yet: check first") and "no viewport meta tag" in first
    for _ in range(3):
        again = tasks.finish_task()
        assert again.startswith("Not finished yet. The app checked the files") and tasks.active
    out = tasks.finish_task("Built it")
    assert out.startswith("Task finished.") and "these are still not fixed: index.html has no viewport" in out
    assert not tasks.active


def test_finish_is_accepted_once_the_site_is_fixed(tasks, home) -> None:
    site = home / "Downloads/portfolio"
    (site / "index.html").write_text(GOOD_PAGE.replace('<link rel="stylesheet" href="style.css">', "<style>"
                                                       "@media (max-width: 600px) {}</style>")
                                     .replace('<script src="script.js"></script>', ""), encoding="utf-8")
    tasks.user_name = lambda: "Krishna"
    tasks.start_task("Build my portfolio website", "Downloads/portfolio")
    assert "The app checked" not in tasks.finish_task()
    out = tasks.finish_task("Built it")
    assert out.startswith("Task finished.") and "still not fixed" not in out
    assert not tasks.active
