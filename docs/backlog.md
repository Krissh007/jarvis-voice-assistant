# Backlog

Agreed work that hasn't been done yet, with what it needs. Newest first.

## Job search status through the Agent Hub

**Now:** `job_search_status` reads JobPilot's local API directly (`job_search.api_url`, GET only). The
Agent Hub already handles the action side through `agent_hub_request`: the shortlist, applications
waiting for approval, job search runs, and approving or skipping an application.

**Why it isn't routed through the Hub yet:** the Hub gives JARVIS only `submit_request`, which returns
a one-line spoken summary (`say`), not data. Its JobPilot actions (`config/agents.yaml`) are
`get_jobs`, `get_shortlist`, `get_review_queue`, `get_run_logs`, `run_agent`, `approve_application`
and `skip_application`. Nothing covers the overview, the answer bank's open questions, the LinkedIn
resume upload, or interview and offer counts. Routing now would lose those, including the blank
questions that `save_job_answer` depends on.

**To do (option 3, agreed 16 October 2026):**

1. **JobPilot** (`E:\JobPilot`, its MCP server `jobpilot.mcp_server`): add read-only status actions,
   each returning structured data plus a short spoken summary:
   - `get_overview`: last run time and outcome, new jobs, shortlist size, applications sent;
   - `get_open_questions`: answer-bank screening questions still blank, with their text;
   - `get_resume_upload`: the LinkedIn resume upload status;
   - `get_outcomes`: interview and offer counts.
2. **Agent Hub** (`E:\agent-hub`): register them under `jobpilot.actions` in `config/agents.yaml`
   (no `side_effect`), add rules in `config/rules.yaml` for the common phrasings, and pass the
   structured data back to JARVIS, not only `say`.
3. **JARVIS:** route `job_search_status` through the Hub: one topic per action, keeping today's
   topics (`overview`, `shortlist`, `review`, `applications`, `questions`, `blanks`, `problems`,
   `resume`) and its untrusted-output wrapping. Keep the direct API as a fallback while the Hub is
   down. Update `save_job_answer` to take the blank questions from the Hub, and update the tests.
