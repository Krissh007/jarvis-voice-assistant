"""Execute tools, enforce tiers, taint escalation, PendingAction/confirmation, audit log.

The model asks; this module decides. Flow for a CONFIRM tool:
  1. model calls the tool -> a PendingAction is created with a summary written by code
  2. the call is answered at once with {"status": "awaiting_confirmation", "summary": ...}
  3. the user answers by voice (after the summary has finished playing), typed text in
     --text mode, or hotkey -> the tool runs (or not)
  4. the outcome is injected back into the session as a [system notice]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.confirm import Confirmer, PendingAction, UtteranceTracker, answer_kind, is_accept

RECENT_USER_LINES = 3   # "switch to X" ... "I'm still in that tab": the click that follows names X
from core.registry import NeedsConfirmation, Registry, Tier, Tool, ToolError
from core.untrusted import wrap

log = logging.getLogger(__name__)

CONFIRM_INSTRUCTION = (
    "Ask the user a short yes-or-no question in their own words that names the action and what it acts on, "
    "e.g. \"Open Figma?\" or \"Delete notes.txt?\". Don't read web addresses or the whole summary aloud (it's "
    "on the screen), don't explain, and never make it sound smaller than the summary. "
    "Do not call this tool again, and do not say it is done. "
    "The app will send a [system notice] with the outcome."
)
_SECRET_KEYS = ("password", "passwd", "token", "secret", "api_key", "apikey", "card", "cvv")
_SECRET_ENV_NAME = re.compile(r"(_KEY|_TOKEN|_SECRET)$")  # GOOGLE_API_KEY, AGENT_HUB_TOKEN...
TRUNCATE = 200


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > TRUNCATE:
        return value[:TRUNCATE] + "…"
    return value


def _plain_step(step: Any) -> dict[str, Any]:
    """A paused step's details for the views' plan cards: short plain values only (goal, the plan's tool
    names, which step this is), never results or arbitrary objects."""
    if not isinstance(step, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in step.items():
        if isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:200]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[key] = [v[:60] for v in value[:12]]
    return out


def _secret_values() -> set[str]:
    """Values of the keys and tokens in the environment (.env is loaded into it), never to be logged."""
    return {v for k, v in os.environ.items() if _SECRET_ENV_NAME.search(k.upper()) and len(v) >= 8}


class AuditLog:
    """Appends one JSON line per tool outcome to logs/actions.jsonl (§0.7)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _redact(self, args: dict[str, Any]) -> dict[str, Any]:
        secrets = _secret_values()
        out = {}
        for k, v in args.items():
            if any(s in k.lower() for s in _SECRET_KEYS) or (isinstance(v, str) and v in secrets):
                out[k] = "***"
            else:
                out[k] = _truncate(v)
        return out

    def write(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        tier: str,
        effective_tier: str,
        tainted: bool,
        confirmation: str,
        result: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "args": self._redact(args),
            "tier": tier,
            "effective_tier": effective_tier,
            "tainted": tainted,
            "confirmation": confirmation,
            "result": _truncate(result),
            "dry_run": dry_run,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


class Dispatcher:
    def __init__(
        self,
        registry: Registry,
        confirmer: Confirmer,
        tracker: UtteranceTracker,
        audit: AuditLog,
        notify: Callable[[str], None],
        accept_phrases: list[str],
        dry_run: bool = False,
    ) -> None:
        self.registry = registry
        self.confirmer = confirmer
        self.tracker = tracker
        self.audit = audit
        self.notify = notify
        self.accept_phrases = accept_phrases
        self.dry_run = dry_run
        self.tainted = False
        self._running: tuple[asyncio.Task, str | None] | None = None
        self._confirmed_running: str | None = None  # tool the user just confirmed, while it runs
        self._pending_meta: dict[int, dict[str, Any]] = {}
        self._bg: set[asyncio.Task] = set()
        self._inflight: dict[str, asyncio.Future] = {}  # identical read-only calls share one run
        self.condenser = None  # core.condense.Condenser, set by the runtime: long results are shortened
        # events(kind, **data): tool and confirmation progress for every view (core.bus); set by the runtime
        self.events: Callable[..., Any] | None = None
        # Tools the user turned off for their current message (the chat's web search: Off), with the reason
        # the model is told; the runtime sets it for each new message.
        self.turn_blocked: dict[str, str] = {}
        self.hold = ""   # set while the user's words stop mid-sentence: no tool runs on half a request
        self.on_user_line: Callable[[str], None] | None = None   # each finished line that isn't a yes/no
        self._failed = False  # the last _execute ended in an error or was stopped
        self.last_failed = False  # the same, for the caller of handle_call

    # ------------------------------------------------------------------ model side

    async def handle_call(self, name: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
        """Answer one function call from the model. Never raises."""
        # A user's spoken "yes" must be judged before a new call can supersede the pending action.
        if text := self.tracker.finalize():
            await self.on_user_input(text, "voice")

        was_tainted = self.tainted
        tool = self.registry.get(name)
        if tool is None or tool.statically_blocked:
            self._audit(name, args, "BLOCKED" if tool else "UNKNOWN", "BLOCKED", "refused", "not available")
            return {"status": "refused", "message": f"{name} is not something I can do."}
        if self.hold:
            self._audit(name, args, "-", "-", "refused", "the user hadn't finished speaking")
            return {"status": "waiting", "message": self.hold}
        if name in self.turn_blocked:
            self._audit(name, args, "-", "-", "refused", "turned off for this message")
            return {"status": "refused", "message": self.turn_blocked[name]}

        if name == self._confirmed_running:
            # The model re-issuing the call it just had confirmed would queue a second copy.
            self._audit(name, args, "-", "-", "refused", "already running")
            return {"status": "already_running",
                    "message": "The user already confirmed this and it is running now. Do not call it again; "
                               "wait for the result notice."}

        base = tool.base_tier(args)
        effective = base
        if self.tainted and not tool.read_only and not tool.self_gated:
            effective = max(base, Tier.CONFIRM)
        if effective == Tier.BLOCKED:
            self._audit(name, args, base.name, effective.name, "refused", "blocked for safety")
            return {"status": "refused", "message": "That action is blocked for safety."}

        response: dict[str, Any] = {}
        if self.confirmer.pending:
            old = self.confirmer.take()
            self._finish_cancel(old, "superseded by a new request", "cancelled", notify=False)
            response["previous_action_cancelled"] = old.summary

        if effective == Tier.CONFIRM:
            summary = tool.summary(args)
            pending = self.confirmer.create(name, args, summary, call_id)
            self._pending_meta[pending.id] = {"tier": base.name, "effective_tier": effective.name, "tainted": self.tainted}
            log.info("pending #%d: %s", pending.id, summary)
            self._emit("confirm_requested", id=pending.id, tool=name, summary=summary)
            return {**response, "status": "awaiting_confirmation", "summary": summary, "instruction": CONFIRM_INSTRUCTION}

        key = self._share_key(tool, args)
        if key in self._inflight:
            # Speech split into pieces ("read my screen"... "specific") makes the model repeat a call while
            # the first is still running: share its result instead of capturing and asking again.
            try:
                result = await asyncio.shield(self._inflight[key])
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                result = "Stopped."
            self._audit(name, args, base.name, effective.name, "not_required", "same call already running: "
                        "shared its result", tainted=was_tainted)
            return {**response, "status": "ok", "result": result}
        if key is not None:
            self._inflight[key] = asyncio.get_running_loop().create_future()
        result = None
        try:
            result = await self._run(tool, args, call_id, condense=True)
        finally:
            if key is not None:
                shared = self._inflight.pop(key)
                if result is not None:
                    shared.set_result(result)
                else:
                    shared.cancel()  # we're shutting down
        if isinstance(result, NeedsConfirmation):
            self._audit(name, args, base.name, effective.name, "not_required", f"paused: {result.summary}",
                        tainted=was_tainted)
            pending = self._park(tool, result, call_id)
            return {**response, "status": "awaiting_confirmation", "summary": pending.summary,
                    "progress": result.progress, "instruction": CONFIRM_INSTRUCTION}
        self._audit(name, args, base.name, effective.name, "not_required", result, tainted=was_tainted)
        return {**response, "status": "ok", "result": result}

    @staticmethod
    def _share_key(tool: Tool, args: dict[str, Any]) -> str | None:
        """Identical calls to a read-only tool can share one run; anything that acts never does."""
        if not tool.read_only or tool.self_gated:
            return None
        return tool.name + json.dumps(args, sort_keys=True, default=str)

    # ------------------------------------------------------------------ agent-mode steps

    def effective_tier(self, tool: Tool, args: dict[str, Any]) -> tuple[Tier, Tier]:
        """(base, effective) tier for a step right now, with the same taint rule as a direct call."""
        base = Tier.BLOCKED if tool.statically_blocked else tool.base_tier(args)
        effective = base
        if self.tainted and not tool.read_only and not tool.self_gated:
            effective = max(base, Tier.CONFIRM)
        return base, effective

    async def run_step(self, tool: Tool, args: dict[str, Any], outcome: str) -> str | NeedsConfirmation:
        """Run one approved plan step: same execution, untrusted wrapping, taint and audit as a tool call."""
        base, effective = self.effective_tier(tool, args)
        was_tainted = self.tainted
        result = await self._run(tool, args, None, step=True)
        if isinstance(result, NeedsConfirmation):
            self._audit(tool.name, args, base.name, effective.name, outcome, f"paused: {result.summary}",
                        tainted=was_tainted)
            return result
        self._audit(tool.name, args, base.name, effective.name, outcome, result, tainted=was_tainted)
        return result

    def on_turn_complete(self) -> None:
        self.confirmer.on_turn_complete()

    def on_tool_cancellation(self, ids: list[str]) -> None:
        """The server withdrew calls (e.g. the user interrupted): abort a matching running tool."""
        if self._running and self._running[1] in ids:
            self._running[0].cancel()

    # ------------------------------------------------------------------ user side

    def on_input_fragment(self, text: str) -> bool:
        """Transcript of mic audio. Dropped while the speaker plays (echo can't confirm)."""
        return self.tracker.add(text)

    def flush_utterance(self) -> None:
        """Close the current utterance now (the model has started to respond)."""
        if text := self.tracker.finalize():
            self._spawn(self.on_user_input(text, "voice"))

    async def on_user_input(self, text: str, source: str) -> bool:
        """A finished utterance ('voice') or typed line ('text'). True if it answered a pending action."""
        self.tainted = False  # a new user turn clears taint
        self.registry.last_user_text = text
        self.registry.recent_user_texts = [*self.registry.recent_user_texts, text][-RECENT_USER_LINES:]
        pending = self.confirmer.pending
        if pending is None:
            self.registry.user_turn()
            if self.on_user_line is not None:
                self.on_user_line(text)
            return False
        if source == "voice" and not self.confirmer.armed:
            return False  # spoken before the summary finished: not an answer yet
        kind = answer_kind(text, self.accept_phrases, pending.summary)
        if kind == "yes":
            await self._confirm(f"confirmed_{source}")
        elif kind == "unclear" and pending.unclear < 2:
            # Seen live: "yeah" came back as "Ja." or "anfangen" and every such reply cancelled the
            # action. A short reply that is neither yes nor no is asked again, and the timer restarts.
            pending.unclear += 1
            pending.armed_at = self.confirmer.clock()
            log.info("pending #%d: unclear answer (%d), asking again", pending.id, pending.unclear)
            self.notify(
                f'[system notice] The user\'s reply "{text[:60]}" wasn\'t a clear yes or no. "{pending.summary}" '
                "is still waiting: ask them briefly to say yes or no. Don't call the tool again."
            )
        else:
            reason = f'the user answered "{text[:80]}", which is not a confirmation'
            self._finish_cancel(self.confirmer.take(), reason, "cancelled", answered=True)
        return True

    async def on_hotkey(self, name: str) -> None:
        if name == "confirm" and self.confirmer.pending:
            await self._confirm("confirmed_hotkey")
        elif name == "cancel" and self.confirmer.pending:
            self._finish_cancel(self.confirmer.take(), "cancel hotkey", "cancelled")
        elif name == "stop":
            self.stop()

    def stop(self) -> None:
        """Stop hotkey: abort the running tool and cancel anything pending."""
        if self._running:
            self._running[0].cancel()
        if self.confirmer.pending:
            self._finish_cancel(self.confirmer.take(), "stopped", "cancelled")

    async def tick(self) -> None:
        """Call a few times a second: closes quiet utterances, arms and times out pending actions."""
        if text := self.tracker.finalize(force=False):
            await self.on_user_input(text, "voice")
        if self.confirmer.poll():
            self._finish_cancel(self.confirmer.take(), "no answer within the time limit", "timeout")

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmer.pending is not None

    # ------------------------------------------------------------------ internals

    def _park(self, tool: Tool, needs: NeedsConfirmation, call_id: str | None) -> PendingAction:
        """A multi-step tool paused on a step that needs confirmation."""
        if self.confirmer.pending:
            self._finish_cancel(self.confirmer.take(), "superseded by a paused step", "cancelled", notify=False)
        pending = self.confirmer.create(tool.name, needs.step, needs.summary, call_id)
        self._pending_meta[pending.id] = {"tier": "CONFIRM", "effective_tier": "CONFIRM",
                                          "tainted": self.tainted, "resume": needs.resume,
                                          "on_cancel": needs.on_cancel}
        log.info("pending #%d (step): %s", pending.id, needs.summary)
        self._emit("confirm_requested", id=pending.id, tool=tool.name, summary=needs.summary,
                   step=_plain_step(needs.step))
        return pending

    async def _confirm(self, outcome: str) -> None:
        pending = self.confirmer.take()
        tool = self.registry.get(pending.tool)
        meta = self._pending_meta.pop(pending.id, {})
        self._confirmed_running = pending.tool
        try:
            result = await self._run(tool, pending.args, pending.call_id, resume=meta.get("resume"), condense=True)
        finally:
            self._confirmed_running = None
        self._emit("confirm_resolved", id=pending.id, tool=pending.tool, outcome=outcome,
                   result=_truncate(result.summary if isinstance(result, NeedsConfirmation) else result))
        if isinstance(result, NeedsConfirmation):
            self._audit(pending.tool, pending.args, "CONFIRM", "CONFIRM", outcome, f"next: {result.summary}",
                        tainted=meta.get("tainted", False))
            nxt = self._park(tool, result, pending.call_id)
            self.notify(
                f'[system notice] The user confirmed "{pending.summary}" and it was done. {result.progress}\n'
                "The next step also needs confirmation. Ask the user a short yes-or-no question in their own "
                f'words that names its action and target (it is on the screen): "{nxt.summary}"'
            )
            return
        self._audit(pending.tool, pending.args, meta.get("tier", "CONFIRM"), meta.get("effective_tier", "CONFIRM"),
                    outcome, result, tainted=meta.get("tainted", False))
        self.notify(
            f'[system notice] The user confirmed "{pending.summary}". Result: {result}\n'
            "Tell the user the outcome in one short sentence."
        )

    def _finish_cancel(self, pending: PendingAction, reason: str, outcome: str, notify: bool = True,
                       answered: bool = False) -> None:
        meta = self._pending_meta.pop(pending.id, {})
        self._audit(pending.tool, pending.args, meta.get("tier", "CONFIRM"), meta.get("effective_tier", "CONFIRM"),
                    outcome, reason, tainted=meta.get("tainted", False))
        log.info("pending #%d %s: %s", pending.id, outcome, reason)
        self._emit("confirm_resolved", id=pending.id, tool=pending.tool, outcome=outcome, result=reason)
        if on_cancel := meta.get("on_cancel"):
            self._spawn(self._release(on_cancel))
        if notify:
            # Seen live: after "yes, and don't send it" was cancelled, JARVIS said "One moment" and did
            # nothing. If the user still wants it (maybe changed), the tool has to be called again.
            again = (" If their answer asks for it done differently (for example typed but not sent), call "
                     "the tool again with that change: the app will ask them. Never say \"One moment\" "
                     "unless a confirmation is waiting.") if answered else ""
            self.notify(
                f'[system notice] Not done: "{pending.summary}" was cancelled ({reason}). '
                "Tell the user briefly." + again
            )

    @staticmethod
    async def _release(on_cancel: Callable[[], Any]) -> None:
        try:
            await on_cancel()
        except Exception:
            log.exception("cancel hook failed")

    async def _run(self, tool: Tool, args: dict[str, Any], call_id: str | None,
                   resume=None, condense: bool = False, step: bool = False) -> str | NeedsConfirmation:
        """Execute a tool (or resume a paused step), returning a short speakable string, and publish
        its start and end. `step`: one step of an approved plan."""
        self._emit("tool_started", tool=tool.name, args=self.audit._redact(args), step=step)
        result = await self._execute(tool, args, call_id, resume, condense)
        paused = isinstance(result, NeedsConfirmation)
        self._emit("tool_finished", tool=tool.name, step=step, paused=paused, ok=not self._failed,
                   result=_truncate(result.summary if paused else result))
        return result

    async def _execute(self, tool: Tool, args: dict[str, Any], call_id: str | None,
                       resume, condense: bool) -> str | NeedsConfirmation:
        """With `condense`, a long result from a tool that opts in is shortened first (not for plan
        steps: the plan's answer step summarises them, and blanks are filled from the full text)."""
        if self.dry_run and not tool.read_only:
            return f"[dry run] Would {tool.summary(args)}."
        if resume is not None:
            task = asyncio.ensure_future(resume())
        elif inspect.iscoroutinefunction(tool.func):
            task = asyncio.ensure_future(tool.func(**args))
        else:
            task = asyncio.ensure_future(asyncio.to_thread(tool.func, **args))
        self._running = (task, call_id)
        self._failed = False
        try:
            raw = await task
            if isinstance(raw, NeedsConfirmation):
                if tool.untrusted_output:
                    self.tainted = True
                    raw.progress = wrap(tool.name, raw.progress) if raw.progress else ""
                return raw
            result = str(raw)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # we are being shut down, not stopped by the user
            result, self._failed = "Stopped.", True
        except ToolError as exc:
            log.warning("tool %s: %s", tool.name, exc)
            result, self._failed = str(exc), True
        except Exception as exc:
            log.exception("tool %s failed", tool.name)
            result, self._failed = f"That didn't work ({type(exc).__name__}).", True
        finally:
            self._running = None
            self.last_failed = self._failed
        if condense and self.condenser is not None and tool.wants_condense(args) and self.condenser.needed(result):
            result = await self.condenser.condense(tool.name, result, self.registry.last_user_text,
                                                   keep=tool.condense_keep(args))
        if tool.untrusted_output:
            self.tainted = True
            result = wrap(tool.name, result)
        return result

    def _audit(self, tool: str, args: dict[str, Any], tier: str, effective: str, outcome: str, result: str,
               tainted: bool | None = None) -> None:
        try:
            self.audit.write(
                tool=tool, args=args, tier=tier, effective_tier=effective,
                tainted=self.tainted if tainted is None else tainted,
                confirmation=outcome, result=result, dry_run=self.dry_run,
            )
        except OSError:
            log.exception("audit log write failed")

    def _emit(self, kind: str, **data: Any) -> None:
        """Publish progress for the views; a broken view never affects the action."""
        if self.events is None:
            return
        try:
            self.events(kind, **data)
        except Exception:
            log.exception("publishing %s failed", kind)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
