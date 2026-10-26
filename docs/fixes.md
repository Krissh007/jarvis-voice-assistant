# JARVIS fixes

Where JARVIS's bugs and fixes are recorded:

- **Issues** (bugs JARVIS noticed itself): `logs/issues.jsonl`. When a request fails three times
  (a tool error or refusal, or Krishna correcting or repeating it), JARVIS stops retrying, logs an
  issue with what was asked and what it tried, and sends it to Claude in the "JARVIS architecture dev"
  chat (`self_heal` in `config.yaml`). Ask JARVIS "any open issues?" to hear them.
- **Fixes**: this file. When an issue is fixed, add a line naming it (`#7`), and JARVIS counts it as
  fixed. Each line says what was wrong, what changed and the commit.
- **Raw evidence**: `logs/console.txt` (the conversation), `logs/app.log` (technical detail) and
  `logs/actions.jsonl` (every tool call and its outcome).

## Fixed

Format: `- #<issue> (<date>): <what was wrong> -> <what changed> (<commit>)`. Fixes found by reading the
logs before self-healing existed have no issue number.

- #1 (2026-10-27): a false alarm in self-healing itself. "What are the agents doing?", two follow-up
  questions and "No, cancel that" opened an issue although every tool worked, and JARVIS told Krishna the
  check had failed -> follow-ups and soft "no"s count only after a tool has failed, cancelling ends the
  request, and the stop message says honestly whether tools failed (commit 9947389)
- (2026-10-25): "JobPilot auto apply" clicked a chat message quoting it instead of the sidebar chat ->
  names match without spaces/punctuation; partial matches skip long text; recent lines count as naming
  a control (commit 008e782)
- (2026-10-25): answered half-sentences, sent Claude nine messages, used the job agents to move around
  Claude, claimed "stopped the search" -> waits for whole sentences, drafts for Claude, honest results
  (commit 8acb1bd)
- (2026-10-23): clicks on things Krishna named asked for a yes; "close Chrome" missed JARVIS's own
  browser (commit 0eb9ee8)
- (2026-10-23): fell asleep after 15 s (commit e668465)
- (2026-10-23): "Yes, and don't send it" cancelled; sent to Claude on "add"; false wakes at 0.60
  (commit 1b5da74)
