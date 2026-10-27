"""job_search_status: reads JobPilot's local API (GET only) and speaks a summary."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from actions import job_search
from actions.job_search import JobSearchStatus, make_fetch
from core.registry import Tier, ToolError

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


# ---- richer status: AI tiers, saved applications, failed runs --------------------------

def tiered(id, status, score, title, company, tier):
    return {**job(id, status, score, title, company), "ai_tier": tier}


def test_shortlist_speaks_the_score_and_ai_tier(tmp_path):
    jobs = [tiered(1, "shortlisted", 92, "Business System Analyst", "Synergy", "AI-adjacent"),
            tiered(2, "shortlisted", 73, "Product Manager", "Nxtwave", "AI-core"),
            job(3, "shortlisted", 61, "Data Analyst", "Old Scorer Co")]            # no tier from an older JobPilot
    text = JobSearchStatus(FakeApi(jobs=jobs), tmp_path, now=lambda: NOW).shortlist()
    assert "Business System Analyst at Synergy (92, AI-adjacent)" in text
    assert "Product Manager at Nxtwave (73, AI-core)" in text
    assert "Data Analyst at Old Scorer Co (61)" in text and "Old Scorer Co (61," not in text
    assert text.index("Synergy") < text.index("Nxtwave") < text.index("Old Scorer")


def saved(title, company, blanks, reason="draft_saved", example=False):
    fields = [{"value_used": "(empty: needs you)"} for _ in range(blanks)] + [{"value_used": "9000000000"}]
    return {"is_example": example, "reason": reason, "job": {"title": title, "company": company}, "fields": fields}


def test_review_separates_ready_for_approval_from_saved_for_you_to_finish(tmp_path):
    apps = {"pending_review": [
        {"is_example": False, "reason": "", "job": {"title": "Data Analyst", "company": "Syntrace"}, "fields": []},
        saved("Technical Product Owner", "Evernorth", 1),
        saved("Team member", "Dr. Reddy's", 2, reason="draft_unsaved"),
        saved("Demo", "Example", 3, example=True)]}
    text = JobSearchStatus(FakeApi(applications=apps), tmp_path, now=lambda: NOW).review()
    assert "1 application waits for your approval" in text and "Syntrace" in text
    assert "2 applications were filled as far as possible and saved for you to finish" in text
    assert "Evernorth (1 blank)" in text and "Reddy's (2 blanks)" in text and "Example" not in text
    assert "review queue shows what is still blank" in text


def test_only_saved_applications_are_not_called_ready_for_approval(tmp_path):
    apps = {"pending_review": [saved("Role", "Co", 1)]}
    text = JobSearchStatus(FakeApi(applications=apps), tmp_path, now=lambda: NOW).review()
    assert "for your approval" not in text and "1 application was filled as far as possible" in text


def run(agent, status, hours_ago, error="", example=False):
    return {"agent": agent, "status": status, "started_at": utc(hours_ago + 0.1), "finished_at": utc(hours_ago),
            "error": error, "is_example": example}


def test_problems_reports_failed_agents_in_the_last_day(tmp_path):
    runs = [run("search", "ok", 3), run("score", "error", 2, "QuotaExhausted: still 429 after retries"),
            run("apply", "error", 5, "Not signed in to https://www.linkedin.com/login please"),
            run("docs", "error", 40, "old failure"), run("apply", "error", 0.2, "demo", example=True)]
    text = JobSearchStatus(FakeApi(runs=runs), tmp_path, now=lambda: NOW).problems()
    assert "The scoring run failed 2 hours ago: QuotaExhausted" in text
    assert "The apply run failed 5 hours ago" in text and "http" not in text
    assert "document" not in text and "demo" not in text                       # too old / example
    assert "job search run failed" not in text


def test_a_later_successful_run_clears_the_problem(tmp_path):
    runs = [run("score", "error", 6, "boom"), run("score", "ok", 1)]
    assert JobSearchStatus(FakeApi(runs=runs), tmp_path, now=lambda: NOW).problems() == "No agent run has failed in the last day."


def test_overview_mentions_problems_but_not_twice_for_search(tmp_path):
    runs = [run("search", "error", 3, "x"), run("docs", "error", 1, "Gemini quota")]
    text = JobSearchStatus(FakeApi(runs=runs), tmp_path, now=lambda: NOW).report()
    assert text.count("job search failed") == 1 and "The document run failed 1 hour ago: Gemini quota." in text


def test_overview_stays_quiet_when_nothing_failed(status):
    assert "failed" not in status.report() or "stopped at a sign-in" in status.report()
    assert "No agent run" not in status.report()


# ---- blanks, and saving a dictated answer ------------------------------------------------------

def _parked(company, fields):
    return {"is_example": False, "reason": "draft_saved", "job": {"title": "PO", "company": company}, "fields": fields}


def test_blanks_lists_unanswered_questions_and_consent_boxes(tmp_path):
    api = FakeApi(applications={"pending_review": [
        _parked("Evernorth", [{"field_name": "Do you hold a CSPO certification?*", "value_used": "(empty, left for you)"},
                              {"field_name": "Phone", "value_used": "7989"}]),
        _parked("Nxtwave", [{"field_name": "Data consent*", "value_used": "(not ticked: consent box)"}]),
        _parked("Done Co", [{"field_name": "Phone", "value_used": "7989"}]),
    ]})
    text = JobSearchStatus(api, tmp_path, now=lambda: NOW).report("blanks")
    assert "Evernorth" in text and "Do you hold a CSPO certification?" in text and "*" not in text
    assert "Nxtwave" in text and "1 consent box" in text and "Done Co" not in text


def test_blanks_says_so_when_none_are_named(tmp_path):
    api = FakeApi(applications={"pending_review": [_parked("Evernorth", [])]})
    assert "Re-run apply" in JobSearchStatus(api, tmp_path, now=lambda: NOW).report("blanks")


class FakeBank:
    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def fetch(self, path, params=None):
        assert path == "/answers"
        return self.rows

    def send(self, method, path, body=None):
        self.sent.append((method, path, body))
        return {}


def saver(rows):
    bank = FakeBank(rows)
    return job_search.AnswerSaver(bank.fetch, bank.send), bank


def test_new_answer_is_posted_tagged_voice():
    s, bank = saver([])
    out = s.save("What is your notice period?", "30 days")
    assert bank.sent == [("POST", "/answers", {"question": "What is your notice period?", "answer": "30 days", "tags": "voice"})]
    assert out.startswith("Saved")


def test_open_question_is_filled_not_duplicated():
    s, bank = saver([{"id": 9, "question": "Do you hold a CSPO certification?", "answer": "", "is_example": False}])
    s.save("do you hold a cspo certification", "No")
    assert bank.sent == [("PUT", "/answers/9", {"answer": "No"})]


def test_same_answer_is_not_rewritten():
    s, bank = saver([{"id": 9, "question": "Notice period", "answer": "30 days", "is_example": False}])
    assert "already" in s.save("notice period", "30 Days")
    assert bank.sent == []


def test_demo_rows_never_match():
    s, bank = saver([{"id": 3, "question": "Notice period", "answer": "x", "is_example": True}])
    s.save("notice period", "30 days")
    assert bank.sent[0][0] == "POST"


def test_missing_parts_save_nothing():
    s, bank = saver([])
    with pytest.raises(ToolError):
        s.save("Notice period", "  ")
    assert bank.sent == []


def test_summary_reads_back_question_and_answer():
    s, _ = saver([])
    text = s.summary({"question": "Notice  period?", "answer": "30 days"})
    assert '"Notice period?"' in text and '"30 days"' in text


def test_writer_refuses_a_remote_api():
    with pytest.raises(ValueError):
        job_search.make_writer("http://example.com/api")


def test_save_tool_is_registered_as_confirm_tier():
    from core.registry import Registry, Tier
    registry = Registry()
    job_search.register(registry, {}, None)
    tool = registry.get("save_job_answer") if hasattr(registry, "get") else None
    assert tool is not None and tool.tier == Tier.CONFIRM and not tool.read_only


def test_with_the_agent_hub_on_status_points_shortlist_and_approvals_to_the_hub() -> None:
    from actions import Services, job_search
    from core.registry import Registry

    def description(hub: bool) -> str:
        reg = Registry()
        job_search.register(reg, {"features": {"agent_hub": hub}}, Services())
        return reg.get("job_search_status").description

    assert "agent_hub_request" in description(True) and "quick overall summary" in description(True)
    assert "agent_hub_request" not in description(False) and "shortlist" in description(False)


def test_runs_under_jobpilots_new_agent_names_count(tmp_path):
    # JobPilot renamed search/score/docs to discovery/scoring/documents; runs started through the Hub use them.
    runs = [run("search", "ok", 72), run("discovery", "ok", 8), run("scoring", "error", 2, "QuotaExhausted"),
            run("documents", "error", 1, "Gemini quota")]
    status = JobSearchStatus(FakeApi(runs=runs), tmp_path, now=lambda: NOW)
    assert status.freshness() == "The last job search finished 8 hours ago."
    text = status.problems()
    assert "The scoring run failed 2 hours ago: QuotaExhausted" in text
    assert "The document run failed 1 hour ago: Gemini quota" in text


# ---- going through the blanks one at a time (next_job_question) ----------------------------------

def test_next_question_skips_answered_and_shared_questions(tmp_path):
    """Seen live: "residing in Hyderabad?" asked five times, and "26 pending" that never went down."""
    empty = "(empty, left for you)"
    api = FakeApi(applications={"pending_review": [
        _parked("CGI", [{"field_name": "Are you residing in Hyderabad?*", "value_used": empty},
                        {"field_name": "Expected CTC", "value_used": empty}]),
        _parked("Wipro", [{"field_name": "Are you  residing in Hyderabad?", "value_used": empty},
                          {"field_name": "Do you speak Telugu?", "value_used": empty}]),
        _parked("Mediamint", [{"field_name": "Notice period", "value_used": "30 days"}]),
    ]})
    api.data["/answers"] = [{"question": "Expected CTC", "answer": "16 LPA", "is_example": False},
                            {"question": "Do you speak Telugu?", "answer": "", "is_example": False},   # still open
                            {"question": "Are you residing in Hyderabad", "answer": "x", "is_example": True}]
    s = JobSearchStatus(api, tmp_path, now=lambda: NOW)
    text = s.next_question()
    assert '"Are you residing in Hyderabad?"' in text and "CGI" in text and "1 more after this one" in text
    api.data["/answers"].append({"question": "Are you residing in Hyderabad?", "answer": "Yes", "is_example": False})
    text = s.next_question()                                                   # answered: gone for every job
    assert '"Do you speak Telugu?"' in text and "Wipro" in text and "0 more" in text
    text = s.next_question(skip="Do you speak Telugu?")                        # skipped: not asked again
    assert text.startswith("No blank question is left") and "1 question skipped" in text


def test_next_question_tool_is_read_only_and_short(tmp_path):
    from actions import Services, job_search
    from core.registry import Registry

    registry = Registry()
    services = Services()
    services.data_dir = tmp_path
    job_search.register(registry, {}, services)
    tool = registry.get("next_job_question")
    assert tool.tier == Tier.SAFE and tool.read_only and tool.untrusted_output and not tool.condense
