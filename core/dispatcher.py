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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.confirm import Confirmer, PendingAction, UtteranceTracker, is_accept
from core.registry import NeedsConfirmation, Registry, Tier, Tool, ToolError
from core.untrusted import wrap

log = logging.getLogger(__name__)

CONFIRM_INSTRUCTION = (
    "Read the summary to the user word for word, then ask whether to go ahead. "
    "Do not call this tool again, and do not say it is done. "
    "The app will send a [system notice] with the outcome."
)
_SECRET_KEYS = ("password", "passwd", "token", "secret", "api_key", "apikey", "card", "cvv")
_SECRET_ENV = ("GOOGLE_API_KEY", "OPENROUTER_API_KEY", "TAVILY_API_KEY")
TRUNCATE = 200


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > TRUNCATE:
        return value[:TRUNCATE] + "…"
    return value


class AuditLog:
    """Appends one JSON line per tool outcome to logs/actions.jsonl (§0.7)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _redact(self, args: dict[str, Any]) -> dict[str, Any]:
        secrets = {v for k in _SECRET_ENV if (v := os.environ.get(k))}
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
        self._pending_meta: dict[int, dict[str, Any]] = {}
        self._bg: set[asyncio.Task] = set()

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
            return {**response, "status": "awaiting_confirmation", "summary": summary, "instruction": CONFIRM_INSTRUCTION}

        result = await self._run(tool, args, call_id)
        if isinstance(result, NeedsConfirmation):
            self._audit(name, args, base.name, effective.name, "not_required", f"paused: {result.summary}",
                        tainted=was_tainted)
            pending = self._park(tool, result, call_id)
            return {**response, "status": "awaiting_confirmation", "summary": pending.summary,
                    "progress": result.progress, "instruction": CONFIRM_INSTRUCTION}
        self._audit(name, args, base.name, effective.name, "not_required", result, tainted=was_tainted)
        return {**response, "status": "ok", "result": result}

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
        pending = self.confirmer.pending
        if pending is None:
            self.registry.user_turn()
            return False
        if source == "voice" and not self.confirmer.armed:
            return False  # spoken before the summary finished: not an answer yet
        if is_accept(text, self.accept_phrases):
            await self._confirm(f"confirmed_{source}")
        else:
            reason = f'the user answered "{text[:80]}", which is not a confirmation'
            self._finish_cancel(self.confirmer.take(), reason, "cancelled")
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
        pending = self.confirmer.create(tool.name, needs.step, needs.summary, call_id)
        self._pending_meta[pending.id] = {"tier": "CONFIRM", "effective_tier": "CONFIRM",
                                          "tainted": self.tainted, "resume": needs.resume}
        log.info("pending #%d (step): %s", pending.id, needs.summary)
        return pending

    async def _confirm(self, outcome: str) -> None:
        pending = self.confirmer.take()
        tool = self.registry.get(pending.tool)
        meta = self._pending_meta.pop(pending.id, {})
        result = await self._run(tool, pending.args, pending.call_id, resume=meta.get("resume"))
        if isinstance(result, NeedsConfirmation):
            self._audit(pending.tool, pending.args, "CONFIRM", "CONFIRM", outcome, f"next: {result.summary}",
                        tainted=meta.get("tainted", False))
            nxt = self._park(tool, result, pending.call_id)
            self.notify(
                f'[system notice] The user confirmed "{pending.summary}" and it was done. {result.progress}\n'
                "The next step also needs confirmation. Read this to the user word for word and ask whether "
                f'to go ahead: "{nxt.summary}"'
            )
            return
        self._audit(pending.tool, pending.args, meta.get("tier", "CONFIRM"), meta.get("effective_tier", "CONFIRM"),
                    outcome, result, tainted=meta.get("tainted", False))
        self.notify(
            f'[system notice] The user confirmed "{pending.summary}". Result: {result}\n'
            "Tell the user the outcome in one short sentence."
        )

    def _finish_cancel(self, pending: PendingAction, reason: str, outcome: str, notify: bool = True) -> None:
        meta = self._pending_meta.pop(pending.id, {})
        self._audit(pending.tool, pending.args, meta.get("tier", "CONFIRM"), meta.get("effective_tier", "CONFIRM"),
                    outcome, reason, tainted=meta.get("tainted", False))
        log.info("pending #%d %s: %s", pending.id, outcome, reason)
        if notify:
            self.notify(
                f'[system notice] Not done: "{pending.summary}" was cancelled ({reason}). '
                "Tell the user briefly."
            )

    async def _run(self, tool: Tool, args: dict[str, Any], call_id: str | None,
                   resume=None) -> str | NeedsConfirmation:
        """Execute a tool (or resume a paused step), returning a short speakable string."""
        if self.dry_run and not tool.read_only:
            return f"[dry run] Would {tool.summary(args)}."
        if resume is not None:
            task = asyncio.ensure_future(resume())
        elif inspect.iscoroutinefunction(tool.func):
            task = asyncio.ensure_future(tool.func(**args))
        else:
            task = asyncio.ensure_future(asyncio.to_thread(tool.func, **args))
        self._running = (task, call_id)
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
            result = "Stopped."
        except ToolError as exc:
            log.warning("tool %s: %s", tool.name, exc)
            result = str(exc)
        except Exception as exc:
            log.exception("tool %s failed", tool.name)
            result = f"That didn't work ({type(exc).__name__})."
        finally:
            self._running = None
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

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
