"""Job search status: "Jarvis, how's my job search?" (reads JobPilot's local API).

JobPilot (E:\\JobPilot) owns the job database, scoring, tailored documents and the apply review
queue, and serves them on http://127.0.0.1:8000/api. This module only sends GET requests to that
API and turns the answers into short spoken sentences. It never changes anything (no status
changes, no approvals), so it is SAFE and read-only. Only a loopback API address is accepted.

The weekday LinkedIn resume re-upload is not part of JobPilot; its status is still read from the
job search agent's project_sync/resume_upload_status.md.

Job titles and company names come from job sites, so the output is marked untrusted.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from core.registry import Registry, Tier, Tool, ToolError

log = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8000/api"
DEFAULT_SYNC_DIR = "E:/job_search_agent/project_sync"
START_HINT = ("JobPilot isn't running. Start it with the run API script in the JobPilot scripts folder, "
              "then ask me again.")
TOPICS = ("overview", "shortlist", "review", "applications", "questions", "resume")
MAX_ITEMS = 5
STALE_AFTER = timedelta(hours=30)
TIMEOUT_SECONDS = 5.0
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

Fetch = Callable[[str, "dict[str, Any] | None"], Any]


# ---------------------------------------------------------------- helpers

def _local(stamp: Any) -> datetime | None:
    """API timestamps are UTC ('...Z' or '+00:00'); return naive local time."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_local(stamp: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(stamp.strip()[:19], fmt)
        except ValueError:
            continue
    return None


def _ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{_plural(max(minutes, 1), 'minute')} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


def _job(job: dict[str, Any]) -> str:
    title = " ".join(str(job.get("title") or "a role").split())[:80]
    company = " ".join(str(job.get("company") or "").split())[:60]
    return f"{title} at {company}" if company else title


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _is_question(text: str) -> bool:
    # The apply agent sometimes records a chatbot's closing line as a question.
    return bool(text) and not re.match(r"\s*(thank you|thanks)\b", text, re.IGNORECASE)


def make_fetch(api_url: str) -> Fetch:
    """GET-only client for JobPilot's API. Refuses anything that isn't this computer."""
    host = urlparse(api_url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"job_search.api_url must point at this computer, not {host!r}")
    base = api_url.rstrip("/")

    def fetch(path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = httpx.get(f"{base}{path}", params=params, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise ToolError(START_HINT) from None
        if response.status_code != 200:
            log.warning("JobPilot API %s returned %s", path, response.status_code)
            raise ToolError("JobPilot answered with an error just now. Try again in a moment.")
        return response.json()

    return fetch


# ---------------------------------------------------------------- the tool

class JobSearchStatus:
    def __init__(self, fetch: Fetch, sync_dir: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self.fetch = fetch
        self.sync_dir = sync_dir
        self.now = now

    # data (JobPilot's demo rows are always left out)

    def jobs(self) -> list[dict[str, Any]]:
        data = self.fetch("/jobs", {"include_examples": "false", "limit": 2000})
        return list(data.get("items", []))

    def applications(self, state: str) -> list[dict[str, Any]]:
        rows = self.fetch("/applications", {"state": state})
        return [row for row in rows if not row.get("is_example")]

    # each part returns one or two speakable sentences, or "" when there is nothing to say

    def freshness(self) -> str:
        runs = [run for run in self.fetch("/runs", None)
                if run.get("agent") == "search" and not run.get("is_example")]
        if not runs:
            return "JobPilot hasn't run a job search yet."
        latest = max(runs, key=lambda run: run.get("started_at") or "")
        if latest.get("status") == "running":
            return "A job search is running right now."
        finished = _local(latest.get("finished_at") or latest.get("started_at"))
        if finished is None:
            return ""
        now = self.now()
        if latest.get("status") == "error":
            return f"The last job search failed {_ago(finished, now)}. The Runs screen in JobPilot has the log."
        text = f"The last job search finished {_ago(finished, now)}."
        if now - finished > STALE_AFTER:
            text += " That's over a day old, so the scheduled runs may not be happening."
        return text

    def shortlist(self, jobs: list[dict[str, Any]] | None = None) -> str:
        jobs = self.jobs() if jobs is None else jobs
        picked = [job for job in jobs if job.get("status") in ("shortlisted", "docs_ready")]
        if not picked:
            return "Nothing is shortlisted right now."
        picked.sort(key=lambda job: -float(job.get("score") or 0))
        ready = sum(1 for job in picked if job.get("status") == "docs_ready")
        names = [_job(job) for job in picked[:MAX_ITEMS]]
        more = f", plus {len(picked) - MAX_ITEMS} more" if len(picked) > MAX_ITEMS else ""
        docs = f" {ready} of them have a tailored resume and cover letter ready." if ready else ""
        verb = "jobs are" if len(picked) != 1 else "job is"
        return f"{len(picked)} {verb} shortlisted. The best: {_join(names)}{more}.{docs}"

    def review(self) -> str:
        waiting = self.applications("pending_review")
        blocked = self.applications("blocked")
        parts = []
        if waiting:
            names = _join([_job(row.get("job") or {}) for row in waiting[:MAX_ITEMS]])
            verb = "waits" if len(waiting) == 1 else "wait"
            parts.append(f"{_plural(len(waiting), 'application')} {verb} for your approval in JobPilot's "
                         f"review queue: {names}.")
        if blocked:
            verb = "is" if len(blocked) == 1 else "are"
            parts.append(f"{_plural(len(blocked), 'application')} {verb} stopped at a sign-in or security "
                         "check and need you.")
        return " ".join(parts) or "Nothing is waiting in the review queue."

    def applied(self, jobs: list[dict[str, Any]] | None = None) -> str:
        jobs = self.jobs() if jobs is None else jobs
        counts = Counter(job.get("status") for job in jobs)
        parts = [f"{counts[status]} {label}" for status, label in
                 (("applied", "applied"), ("interview", "at interview"), ("offer", "with an offer"))
                 if counts[status]]
        if not parts:
            return "No applications are recorded yet."
        now = self.now()
        recent = [job for job in jobs
                  if (when := _local(job.get("applied_at"))) and now - when < timedelta(days=1)]
        text = f"In total: {_join(parts)}."
        if recent:
            text += f" In the last day you applied to {_join([_job(job) for job in recent[:MAX_ITEMS]])}."
        else:
            text += " Nothing was submitted in the last day."
        return text

    def questions(self) -> str:
        weekly = self.fetch("/review/weekly", None)
        items = [str(row.get("question") or "").strip() for row in weekly.get("open_questions", [])]
        items = [item for item in items if _is_question(item)]
        if not items:
            return "No screening questions are waiting for you."
        return (f"{_plural(len(items), 'screening question')} in JobPilot's answer bank need your answer, "
                f"for example: {_join([item[:120] for item in items[:3]])}.")

    def resume(self) -> str:
        path = self.sync_dir / "resume_upload_status.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        status = {m.group(1).strip().lower(): m.group(2).strip()
                  for m in re.finditer(r"^\s*-\s*([^:]+):\s*(.*)$", text, re.MULTILINE)}
        if not status:
            return "The LinkedIn resume upload hasn't run yet."
        last = _parse_local(status.get("last run", ""))
        when = _ago(last, self.now()) if last else "at an unknown time"
        if status.get("result", "").upper() == "OK":
            return f"Your resume was re-uploaded to LinkedIn {when}."
        return f"The LinkedIn resume upload failed {when}. {status.get('detail', '')[:160]}".strip()

    def overview(self) -> str:
        jobs = self.jobs()
        new = sum(1 for job in jobs if job.get("status") == "found")
        found = f"There are {_plural(new, 'new job')} to look at." if new else ""
        parts = [self.freshness(), found, self.shortlist(jobs), self.review(), self.applied(jobs)]
        questions = self.questions()
        if not questions.startswith("No "):
            parts.append(questions)
        return " ".join(part for part in parts if part)

    def report(self, topic: str = "overview") -> str:
        """Speakable job search update from JobPilot. Tier SAFE, read-only (GET requests only)."""
        topic = (topic or "overview").strip().lower()
        if topic not in TOPICS:
            topic = "overview"
        method = self.applied if topic == "applications" else getattr(self, topic)
        try:
            return method().replace("?.", "?")
        except ToolError:
            raise
        except Exception:
            log.exception("job_search_status failed")
            raise ToolError("I couldn't read the job search from JobPilot just now.") from None


def register(registry: Registry, config: dict, services) -> None:
    settings = config.get("job_search") or {}
    status = JobSearchStatus(
        make_fetch(str(settings.get("api_url") or DEFAULT_API_URL)),
        Path(str(settings.get("sync_dir") or DEFAULT_SYNC_DIR)),
    )
    registry.register(Tool(
        name="job_search_status",
        description=(
            "Report on the user's job search from JobPilot: when the last search ran, new jobs, the "
            "shortlist and tailored documents, applications waiting for the user's approval, applications "
            "sent, interviews and offers, open screening questions, and the LinkedIn resume upload. "
            "Read-only: it cannot change statuses, approve or submit anything. Use when asked how the job "
            "search is going, what is shortlisted, what needs approval, or what was applied to. "
            "topic: overview (default), shortlist, review, applications, questions or resume."
        ),
        func=status.report,
        tier=Tier.SAFE,
        read_only=True,
        untrusted_output=True,  # job titles and company names come from job sites
        parameters={
            "type": "object",
            "properties": {"topic": {"type": "string", "enum": list(TOPICS)}},
        },
    ))
