"""Self-healing: notice when a request keeps failing, try another way, and log an issue for Claude to fix.

Seen live (23-25 Oct): "switch to JobPilot auto apply" took five tries; the job agents failed the same
navigation three times with "unable to safely"; "close Chrome" failed four times. Nothing recorded these,
so a bug was found only when Krishna complained and someone read the logs.

One episode is one thing the user wants. A failed attempt is a tool error or refusal, or the user
correcting JARVIS or asking for the same thing again. After each failure JARVIS gets a hint to try a
different way; after `attempts` (3) failures it stops retrying, tells the user, and opens an issue:
one JSON line in logs/issues.jsonl, which is the place to look for JARVIS's bugs. The runtime then hands
the issue to Claude (the JARVIS chat) to find and fix the cause; docs/fixes.md records what was fixed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ATTEMPTS = 3
# The user telling JARVIS it went wrong.
CORRECTION = re.compile(
    r"^(no|nope|not that|wrong)\b|\b(still|you didn'?t|didn'?t work|not working|doesn'?t work|isn'?t working|"
    r"that'?s not|that is not|i can'?t see|can'?t see it|try again|again|nothing happened|it'?s not)\b",
    re.IGNORECASE)
# Tool results that are failures even though the tool ran ("did you mean ...").
FAILED_RESULT = re.compile(
    r"several things match|did you mean|can'?t find|couldn'?t|isn'?t open|not found|i stopped|that didn'?t work|"
    r"isn'?t on my list|unable to|wasn'?t able", re.IGNORECASE)
STOP_WORDS = {"the", "a", "an", "to", "in", "on", "of", "and", "is", "it", "my", "me", "please", "can", "you",
              "could", "would", "jarvis", "hey", "okay", "ok", "yeah", "so", "just", "now", "this", "that", "for"}
# What to try instead, per tool. Said to the model, never to the user.
OTHER_WAY = {
    "click_in_app": "read_app with what=controls, then click_in_app by its number; or ask the user which one",
    "type_in_app": "read_app to find the right box, then type_in_app with that field",
    "open_app": "open it as a website with browser_open, or ask the user for its exact name",
    "close_app": "if it's JARVIS's own browser use browser_close; otherwise focus_app and press_keys alt+f4",
    "focus_app": "open_app it, or window_action to restore it",
    "agent_hub_request": "do it yourself with read_app, click_in_app or the browser tools, or job_search_status",
    "browser_open": "check the address with search_web, then browser_open the exact page",
    "browser_click": "browser_show and read_current_page, then click by the link's exact words",
    "browser_task": "do it in single steps: browser_open, browser_click, browser_scroll",
    "find_files": "search with fewer or different words, or another kind",
    "read_app": "look_at_screen",
}
MAX_TEXT = 240


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS and len(w) > 1}


def similar(a: str, b: str) -> bool:
    """The same request again, in other words ("switch to job pilot auto apply" / "can you switch it")."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


@dataclass
class Episode:
    request: str
    started: float
    attempts: list[dict] = field(default_factory=list)
    failures: int = 0
    reported: bool = False


@dataclass
class Issue:
    id: int
    request: str
    attempts: list[dict]
    opened: str

    def report(self) -> str:
        """The message for Claude: what the user asked and what JARVIS tried. Tool output from apps and
        websites is never included (it could carry instructions), only JARVIS's own words about it."""
        lines = [f"JARVIS self-heal issue #{self.id}, filed automatically after {len(self.attempts)} failed "
                 f"attempts ({self.opened}).", f'Krishna asked: "{_short(self.request)}"', "What happened:"]
        for n, a in enumerate(self.attempts, 1):
            if a["kind"] == "tool":
                lines.append(f"{n}. {a['tool']}({_short(a['args'])}) -> {a['result']}")
            else:
                lines.append(f'{n}. Krishna: "{a["text"]}"')
        lines.append(f"Please find the root cause in E:\\JARVIS Agent\\jarvis (logs/issues.jsonl #{self.id}, "
                     "logs/app.log around that time), fix it with a test, run all tests, commit on main without "
                     f"pushing, and add a line for issue #{self.id} to docs/fixes.md.")
        return "\n".join(lines)


class IssueTracker:
    def __init__(self, path: Path, attempts: int = ATTEMPTS, clock: Callable[[], float] = time.monotonic,
                 fixes_path: Path | None = None) -> None:
        self.path = Path(path)
        self.fixes_path = fixes_path
        self.attempts = attempts
        self.clock = clock
        self.episode: Episode | None = None

    # ---- what happens ----

    def user_line(self, text: str) -> None:
        """A finished line from the user (not an answer to a confirmation)."""
        text = " ".join(text.split())
        if not text:
            return
        ep = self.episode
        if ep is not None and not ep.reported and (CORRECTION.search(text) or similar(text, ep.request)):
            ep.attempts.append({"kind": "user", "text": _short(text)})
            ep.failures += 1   # they had to say it again, or tell JARVIS it was wrong
            return
        self.episode = Episode(request=text, started=self.clock())

    def tool_result(self, tool: str, args: dict, ok: bool, result: str, trusted: bool = True) -> None:
        ep = self.episode
        if ep is None or ep.reported:
            return
        failed = not ok or (trusted and bool(FAILED_RESULT.search(result or "")))
        if not failed:
            return
        said = _short(result) if trusted else "(failed; its output came from an app or website and is left out)"
        ep.attempts.append({"kind": "tool", "tool": tool, "args": {k: _short(v) for k, v in (args or {}).items()},
                            "result": said})
        ep.failures += 1

    # ---- what JARVIS does about it ----

    def due(self) -> bool:
        ep = self.episode
        return ep is not None and not ep.reported and ep.failures >= self.attempts

    def hint(self, tool: str) -> str:
        """For the model, after a failure: try another way, or stop and say so."""
        ep = self.episode
        if ep is None or not ep.failures or ep.reported:
            return ""
        if ep.failures >= self.attempts:
            return ("This request has now failed " f"{ep.failures} times. Stop retrying. Tell the user in one "
                    "sentence what you tried and that you've logged it and sent it to Claude to fix.")
        other = OTHER_WAY.get(tool, "a different tool, or ask the user one short question")
        return (f"That didn't work (attempt {ep.failures} of {self.attempts}). Don't repeat the same call: "
                f"try another way: {other}.")

    def open_issue(self) -> Issue | None:
        if not self.due():
            return None
        ep = self.episode
        ep.reported = True
        issue = Issue(id=self._next_id(), request=ep.request, attempts=list(ep.attempts),
                      opened=datetime.now().strftime("%Y-%m-%d %H:%M"))
        self._write({"id": issue.id, "opened": issue.opened, "status": "open", "request": _short(ep.request),
                     "attempts": issue.attempts})
        log.warning("self-heal: issue #%d opened after %d failed attempts", issue.id, len(issue.attempts))
        return issue

    def mark(self, issue_id: int, status: str) -> None:
        self._write({"id": issue_id, "status": status, "at": datetime.now().strftime("%Y-%m-%d %H:%M")})

    # ---- the log ----

    def issues(self) -> list[dict]:
        """Every issue with its latest status; ones named in docs/fixes.md count as fixed."""
        found: dict[int, dict] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            found.setdefault(row["id"], {}).update(row)
        fixed = self._fixed_ids()
        for issue_id, row in found.items():
            if issue_id in fixed:
                row["status"] = "fixed"
        return [found[k] for k in sorted(found)]

    def open_issues(self) -> list[dict]:
        return [row for row in self.issues() if row.get("status") != "fixed"]

    def _fixed_ids(self) -> set[int]:
        if self.fixes_path is None:
            return set()
        try:
            text = self.fixes_path.read_text(encoding="utf-8")
        except OSError:
            return set()
        return {int(n) for n in re.findall(r"#(\d+)", text)}

    def _next_id(self) -> int:
        return max((row["id"] for row in self.issues()), default=0) + 1

    def _write(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("couldn't write the issue log")
