"""job_search_status: reads JobPilot's local API (GET only) and speaks a summary."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from actions import job_search
from actions.job_search import JobSearchStatus, make_fetch
from core.registry import ToolError

NOW = datetime(2026, 10, 10, 18, 0)


def utc(hours_ago: float) -> str:
    """An API timestamp (UTC, 'Z') for local time NOW minus hours_ago."""
    local = (NOW - timedelta(hours=hours_ago)).astimezone()
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def job(id, status, score, title, company, applied_hours_ago=None):
    return {"id": id, "status": status, "score": score, "title": title, "company": company,
            "applied_at": utc(applied_hours_ago) if applied_hours_ago is not None else None}


JOBS = [
    job(1, "found", 55, "Business Analyst", "NewCo"),
    job(2, "found", 50, "Product Analyst", "OtherCo"),
    job(3, "shortlisted", 85, "AI Business Analyst", "Infor"),
    job(4, "docs_ready", 92, "Business Analyst", "Techdome Solutions"),
    job(5, "applied", 74, "Product Owner III", "PDI Technologies", applied_hours_ago=3),
    job(6, "applied", 70, "BA", "Old Co", applied_hours_ago=72),
    job(7, "interview", 80, "Senior Product Analyst", "Marriott"),
]


class FakeApi:
    def __init__(self, jobs=JOBS, runs=None, applications=None, questions=None):
        self.data = {
            "/jobs": {"total": len(jobs), "items": jobs},
            "/runs": runs if runs is not None else [
                {"agent": "search", "status": "ok", "started_at": utc(2.5), "finished_at": utc(2), "is_example": False},
                {"agent": "search", "status": "ok", "started_at": utc(30), "finished_at": utc(29), "is_example": False},
                {"agent": "search", "status": "ok", "started_at": utc(0.1), "finished_at": utc(0.05), "is_example": True},
            ],
            "/review/weekly": {"open_questions": questions if questions is not None else [
                {"question": "What is your expected CTC?"}, {"question": "Thank you for your responses."}]},
        }
        self.applications = applications if applications is not None else {
            "pending_review": [{"is_example": False, "job": {"title": "Business Analyst", "company": "Wise"}},
                               {"is_example": True, "job": {"title": "Demo", "company": "Example"}}],
            "blocked": [{"is_example": False, "job": {"title": "BA", "company": "Blocked Co"}}],
        }
        self.calls = []

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        if path == "/applications":
            return self.applications.get((params or {}).get("state"), [])
        return self.data[path]


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def status(api, tmp_path):
    return JobSearchStatus(api, tmp_path, now=lambda: NOW)


def test_overview_is_speakable(status, api):
    text = status.report()
    assert "last job search finished 2 hours ago" in text
    assert "2 new jobs to look at" in text
    assert "2 jobs are shortlisted" in text and "Business Analyst at Techdome Solutions" in text
    assert text.index("Techdome") < text.index("Infor")  # best score first
    assert "1 of them have a tailored resume" in text
    assert "1 application waits for your approval" in text and "Wise" in text and "Example" not in text
    assert "stopped at a sign-in or security check" in text
    assert "2 applied" in text and "1 at interview" in text
    assert "Product Owner III at PDI Technologies" in text and "Old Co" not in text  # only the last day
    assert "expected CTC" in text and "Thank you" not in text
    assert "http" not in text and "*" not in text and "#" not in text
    # demo rows never reach the voice
    assert ("/jobs", {"include_examples": "false", "limit": 2000}) in api.calls


def test_only_get_paths_are_used(status, api):
    for topic in job_search.TOPICS:
        status.report(topic)
    assert {path for path, _ in api.calls} <= {"/jobs", "/runs", "/applications", "/review/weekly"}


def test_stale_and_failed_search_runs(tmp_path):
    stale = FakeApi(runs=[{"agent": "search", "status": "ok", "started_at": utc(41), "finished_at": utc(40)}])
    assert "may not be happening" in JobSearchStatus(stale, tmp_path, now=lambda: NOW).freshness()

    failed = FakeApi(runs=[{"agent": "search", "status": "error", "started_at": utc(3), "finished_at": utc(3)}])
    assert "failed 3 hours ago" in JobSearchStatus(failed, tmp_path, now=lambda: NOW).freshness()

    running = FakeApi(runs=[{"agent": "search", "status": "running", "started_at": utc(0.2)}])
    assert "running right now" in JobSearchStatus(running, tmp_path, now=lambda: NOW).freshness()


def test_empty_jobpilot(tmp_path):
    empty = FakeApi(jobs=[], runs=[], applications={}, questions=[])
    text = JobSearchStatus(empty, tmp_path, now=lambda: NOW).report()
    assert "hasn't run a job search yet" in text
    assert "Nothing is shortlisted" in text and "Nothing is waiting in the review queue" in text
    assert "No applications are recorded yet" in text


def test_resume_upload_still_reads_its_status_file(api, tmp_path):
    (tmp_path / "resume_upload_status.md").write_text(
        "- Last run: 2026-10-10 08:31\n- Result: FAILED\n- Detail: Not signed in.\n", encoding="utf-8")
    text = JobSearchStatus(api, tmp_path, now=lambda: NOW).report("resume")
    assert "failed" in text and "Not signed in" in text


def test_jobpilot_not_running_is_a_spoken_error(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(job_search.httpx, "get", refuse)
    status = JobSearchStatus(make_fetch("http://127.0.0.1:8000/api"), tmp_path, now=lambda: NOW)
    with pytest.raises(ToolError, match="JobPilot isn't running"):
        status.report()


def test_api_must_be_on_this_computer():
    with pytest.raises(ValueError, match="this computer"):
        make_fetch("http://example.com/api")
    make_fetch("http://localhost:8000/api")  # loopback names are fine


def test_time_ago_reads_naturally():
    assert job_search._ago(NOW - timedelta(seconds=20), NOW) == "1 minute ago"
    assert job_search._ago(NOW - timedelta(minutes=5), NOW) == "5 minutes ago"
    assert job_search._ago(NOW - timedelta(hours=1), NOW) == "1 hour ago"
    assert job_search._ago(NOW - timedelta(days=3), NOW) == "3 days ago"
