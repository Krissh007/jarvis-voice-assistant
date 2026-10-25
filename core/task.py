"""Task mode: keep working on one goal until it's done, then tell the user.

A Live turn ends after a few tool calls, and the model then waits for the user. For a task like
"build my portfolio, keep going until it looks professional", code keeps it going instead:

1. The model calls start_task(goal, folder) when the user hands over a whole job.
2. Each time a turn ends while the task is open, the app sends a [system notice] saying "keep going"
   (after a short pause, so the user can speak first; never while a confirmation is waiting).
3. The first finish_task is answered with a check: read the work back against the goal and carry on if
   anything is missing. The second ends the task, and JARVIS tells the user (waking up if asleep).
4. Limits: max_rounds and max_minutes, and a stop after stall_rounds turns that used no tool. The Stop
   key, or the user asking, ends it (stop_task).

Within the task's folder, creating and changing files (not deleting) doesn't ask again: the user handed
over that folder for this job. Every other tool keeps its usual tier and taint rule.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.registry import Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

CONTINUE = (
    "[system notice] Task in progress (round {round} of {max_rounds}): {goal}\n"
    "Keep working on it now with your tools; the user is waiting for the finished result, not for "
    "questions. Look at what's done so far, pick the next most important missing or weak part, and "
    "do it. Prefer whole-file write_file (overwrite=true) for big changes. Don't talk between steps "
    "(at most a few words). If something was declined or timed out, don't retry it; work around it. "
    "Only ask the user if you truly can't continue without them. When the goal is fully met, call "
    "finish_task with a one-sentence summary."
)
REVIEW = (
    "Not finished yet: check first. Read back every file you made or changed (read_file) and compare "
    "it with the goal: \"{goal}\". List to yourself what's missing, empty, placeholder or unpolished. "
    "{quality}If anything is, keep working on it now. Call finish_task again only when nothing is."
)
CODE_QUALITY = (
    "For a website, every section needs real content (no 'Project 1' or lorem ipsum), consistent "
    "styling, navigation, a mobile layout (@media rules) and working links between the files. A "
    "portfolio is about the user (use their name and what you know about them, not an invented "
    "person) and has about, skills, projects, experience and contact sections. "
)
CODE_WORDS = ("website", "web site", "portfolio", "page", "html", "css", "app", "code", "landing")
PLACEHOLDERS = re.compile(r"lorem ipsum|\bproject [1-9]\b|description of project|your[-_ ]?(name|email)@|"
                          r"example\.com|\btodo\b|coming soon|insert .{1,20} here", re.IGNORECASE)
SECTIONS = ("about", "skills", "projects", "experience", "contact")
MAX_REJECTIONS = 3  # finish_task refused this many times for site problems, then it ends and says what's left


def site_problems(folder: Path, user_name: str = "") -> list[str]:
    """Plain checks of a website in `folder` that a quick self-review misses. Empty if it looks complete."""
    pages = sorted(folder.glob("*.html"))[:10]
    if not pages:
        return []
    problems: list[str] = []
    css = "".join(sheet.read_text(encoding="utf-8", errors="replace") for sheet in sorted(folder.glob("*.css"))[:10])
    all_html = ""
    for page in pages:
        html = page.read_text(encoding="utf-8", errors="replace")
        all_html += html
        name = page.name
        for ref in re.findall(r'(?:href|src)="([^"#:?]+\.(?:css|js|html))"', html):
            if not (folder / ref).is_file():
                problems.append(f"{name} links {ref}, which doesn't exist")
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        for anchor in sorted(set(re.findall(r'href="#([^"]+)"', html))):
            if anchor not in ids:
                problems.append(f'{name} has a link to #{anchor} but no element with id="{anchor}"')
        if m := PLACEHOLDERS.search(html):
            problems.append(f'{name} still has placeholder text ("{m.group(0)}")')
        fields = [f.lower() for f in re.findall(r"<(?:input|textarea|select)\b[^>]*>", html, re.IGNORECASE)]
        if any("name=" not in f for f in fields if 'type="submit"' not in f):
            problems.append(f"{name} has form fields without a name attribute, so the form sends nothing")
        if re.search(r"<img\b(?![^>]*\balt=)[^>]*>", html, re.IGNORECASE):
            problems.append(f"{name} has images without alt text")
        if 'name="viewport"' not in html:
            problems.append(f"{name} has no viewport meta tag, so phones show it zoomed out")
    text = all_html.lower()
    if css and "@media" not in css:
        problems.append("the CSS has no @media rules, so there's no mobile layout")
    if not css and "<style" not in text:
        problems.append("there's no styling at all")
    if "portfolio" in text or any(f'id="{s}"' in text for s in SECTIONS):
        missing = [s for s in SECTIONS if f'id="{s}"' not in text
                   and not re.search(rf"<h[1-4][^>]*>[^<]*\b{s}", text)]  # a heading, not just a nav link
        if missing:
            problems.append(f"no {', '.join(missing)} section{'s' if len(missing) > 1 else ''}")
        first = user_name.split()[0] if user_name.strip() else ""
        if first and first.lower() not in {"sir", "madam", "you"} and first.lower() not in text:
            problems.append(f"it never mentions the user ({first}); it should be about them, not an invented "
                            "person, unless they asked for someone else")
    return problems


@dataclass
class Task:
    goal: str
    folder: Path | None
    started: float
    rounds: int = 0
    reviewed: bool = False
    rejections: int = 0
    quiet_rounds: int = 0  # turns in a row without a tool call
    tools_this_round: int = 0


class TaskMode:
    def __init__(self, config: dict, notify: Callable[[str], None], resolve_folder=None,
                 clock: Callable[[], float] = time.monotonic, user_name: Callable[[], str] | None = None) -> None:
        cfg = config.get("task") or {}
        self.max_rounds = int(cfg.get("max_rounds", 30))
        self.max_s = float(cfg.get("max_minutes", 30)) * 60
        self.stall_rounds = int(cfg.get("stall_rounds", 3))
        self.pause_s = float(cfg.get("pause_s", 2.0))
        self.notify = notify
        self.resolve_folder = resolve_folder  # str -> Path inside the file_edit folders, or ToolError
        self.clock = clock
        self.user_name = user_name or (lambda: "")
        self.task: Task | None = None
        self.on_finished: Callable[[], None] | None = None  # the runtime wakes up to tell the user

    @property
    def active(self) -> bool:
        return self.task is not None

    def covers(self, path: Path) -> bool:
        """A file the user handed over for the current task: inside its folder."""
        task = self.task
        if task is None or task.folder is None:
            return False
        try:
            return path.resolve().is_relative_to(task.folder)
        except (OSError, ValueError):
            return False

    # ---------------------------------------------------------------- tools

    def start_task(self, goal: str, folder: str = "") -> str:
        goal = " ".join(str(goal).split())[:500]
        if len(goal) < 5:
            raise ToolError("Say what the finished result should be.")
        where = None
        if folder.strip():
            if self.resolve_folder is None:
                raise ToolError("I can't work in folders here: file editing is switched off.")
            where = self.resolve_folder(folder)
        replaced = f' (stopped the previous task: "{self.task.goal[:80]}")' if self.task else ""
        self.task = Task(goal=goal, folder=where, started=self.clock())
        log.info("task started: %s (folder %s)", goal, where)
        scope = (f" Files in {where} can be created and changed without asking during this task; deleting "
                 "still asks." if where else "")
        return (f"Task started{replaced}.{scope} Say in one short sentence that you're on it and will say "
                "when it's done, then start working right away with your tools. The app keeps you going "
                "after each turn.")

    def finish_task(self, summary: str = "") -> str:
        task = self.task
        if task is None:
            return "No task is open. Just tell the user the result."
        problems = self._problems()
        found = (" The app checked the files and found: " + "; ".join(problems) + ". Fix all of these."
                 if problems else "")
        if not task.reviewed:
            task.reviewed = True
            words = task.goal.lower()
            quality = CODE_QUALITY if any(w in words for w in CODE_WORDS) or problems else ""
            return REVIEW.format(goal=task.goal, quality=quality) + found
        if problems and task.rejections < MAX_REJECTIONS:
            task.rejections += 1
            return "Not finished yet." + found + " Then call finish_task again."
        self._end("finished")
        left = (" Also say plainly that these are still not fixed: " + "; ".join(problems) + ".") if problems else ""
        return (f"Task finished. Tell the user it's done in one or two sentences: {summary[:300]}. "
                f"Only mention what tool results showed was really done.{left}")

    def _problems(self) -> list[str]:
        task = self.task
        if task is None or task.folder is None or not task.folder.is_dir():
            return []
        try:
            return site_problems(task.folder, self.user_name())[:8]
        except OSError:
            log.exception("couldn't check %s", task.folder)
            return []

    def stop_task(self) -> str:
        if self.task is None:
            return "No task was running."
        goal = self.task.goal
        self._end("stopped by the user")
        return f'Stopped the task "{goal[:120]}". Tell the user in a few words.'

    def status(self) -> str:
        task = self.task
        if task is None:
            return "No task is running."
        minutes = int((self.clock() - task.started) // 60)
        return f'Working on "{task.goal}": round {task.rounds}, {minutes} minute{"s" if minutes != 1 else ""} so far.'

    # ---------------------------------------------------------------- runtime hooks

    def tool_called(self, name: str) -> None:
        if self.task is not None and name not in {"start_task", "finish_task", "task_status"}:
            self.task.tools_this_round += 1

    def stop(self) -> bool:
        """Stop key. True if a task was running."""
        if self.task is None:
            return False
        self._end("Stop key")
        return True

    def next_notice(self) -> str | None:
        """After a turn ends: the "keep going" notice, a wrap-up notice at a limit, or None (no task)."""
        task = self.task
        if task is None:
            return None
        task.quiet_rounds = 0 if task.tools_this_round else task.quiet_rounds + 1
        task.tools_this_round = 0
        if task.quiet_rounds >= self.stall_rounds:
            self._end("stalled")
            return ("[system notice] The task has stalled: no tool was used for several turns. Stop working on "
                    f'"{task.goal}" and tell the user in two sentences what is done and what still needs them.')
        if task.rounds >= self.max_rounds or self.clock() - task.started >= self.max_s:
            self._end("limit reached")
            return ("[system notice] The task has reached its time or step limit, so stop working now. Tell the "
                    f'user in two sentences what is done for "{task.goal}" and what is left; they can say '
                    '"carry on" to start another round.')
        task.rounds += 1
        return CONTINUE.format(round=task.rounds, max_rounds=self.max_rounds, goal=task.goal)

    def _end(self, why: str) -> None:
        task, self.task = self.task, None
        if task is not None:
            log.info("task ended (%s) after %d rounds: %s", why, task.rounds, task.goal)
            print(f"(task {why}: {task.goal[:80]})", flush=True)
            if self.on_finished is not None:
                self.on_finished()


def register(registry: Registry, tasks: TaskMode) -> None:
    registry.register(Tool(
        name="start_task",
        description=(
            "Hand yourself a whole job to finish without the user: use it when the user asks you to build, "
            "write or fix something that takes many steps, or says 'keep going until it's done' / 'don't "
            "stop'. After this, the app keeps you working turn after turn until you call finish_task. "
            "goal: the finished result in one sentence, including the user's quality bar. folder: where "
            "the files are, e.g. 'Downloads' or 'Documents/portfolio' (creating and changing files there "
            "won't ask during the task)."
        ),
        func=tasks.start_task,
        tier=Tier.SAFE,  # after untrusted text, the dispatcher asks first (not read_only)
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What 'done' looks like, in one sentence."},
                "folder": {"type": "string", "description": "Folder the work is in, e.g. Downloads/portfolio."},
            },
            "required": ["goal"],
        },
        summarize=lambda a: (f"work on this until it's done: {str(a.get('goal', ''))[:200]}"
                             + (f", changing files in {a['folder']} without asking" if a.get("folder") else "")),
    ))
    registry.register(Tool(
        name="finish_task",
        description="The task from start_task is fully done. summary: one sentence on what was done.",
        func=tasks.finish_task,
        tier=Tier.SAFE,
        read_only=True,
        parameters={"type": "object", "properties": {"summary": {"type": "string"}}},
    ))
    registry.register(Tool(
        name="stop_task",
        description=("Stop your own running task now (the user said stop, cancel or 'that's enough'). It only "
                     "stops task mode: it doesn't stop JobPilot searches or agent runs."),
        func=tasks.stop_task,
        tier=Tier.SAFE,
        read_only=True,
    ))
    registry.register(Tool(
        name="task_status",
        description="How the running task is going (the user asks 'how's it going?').",
        func=tasks.status,
        tier=Tier.SAFE,
        read_only=True,
    ))
