"""Planner/executor with plan freeze (spec Phase 8, §0.8).

1. Flash drafts a numbered plan using only registered, non-BLOCKED tools. Code validates it.
2. The plan is read to the user (each line written by code from the tool's own summary) and needs
   one approval (CONFIRM).
3. On approval the plan is FROZEN: the executor runs its own immutable copy. Nothing that comes back
   from a web page, the screen or the clipboard can add, remove, reorder or change steps.
4. Values that can only be known later ("the product page found in step 3") are declared as named
   blanks in the approved plan. Filling a blank picks a value only, and that value still goes through
   the step tool's own tier (a site off the trusted list still asks).
5. Per-step tiers and taint still apply; the first failure stops the plan and asks the user.
   A step with no blanks runs with exactly the arguments the user approved, so self-gated tools (the
   desktop tools) don't ask again just because an earlier step read untrusted text (APPROVED_STEP);
   their own risky-step checks (Save, Delete, Enter outside chat apps...) still ask.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from actions.browser import parse_action
from actions.system import normalize_url
from core.llm import USAGE_LIMIT_MESSAGE, LLMUnavailable
from core.registry import APPROVED_STEP, NeedsConfirmation, Registry, Tier, Tool, ToolError
from core.untrusted import unwrap, wrap

log = logging.getLogger(__name__)

BLANK = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.DOTALL)  # a blank anywhere in a value
NOT_PLANNABLE = {"run_plan", "go_to_sleep", "test_write_note", "test_fetch_untrusted"}
FAILURE = re.compile(r"^(That didn't work|Couldn't|I couldn't|I won't|I can't|I stopped|That page is blocked|"
                     r"Page limit|There's no|No results|I don't know|.*isn't on my list|Several things match|"
                     r"There are \d+ called|Number \d+ isn't|That's a password|That's too long|That text looks|"
                     r"Which key|That's my own|.*is minimised|"
                     + re.escape(USAGE_LIMIT_MESSAGE) + ")", re.IGNORECASE)
URL_KEYS = {"url", "start_url"}
MAX_RESULT_CHARS = 1500
BLANK_RETRIES = 2  # a blank whose value failed (e.g. a site that blocks readers) gets 2 more tries


@dataclass(frozen=True)
class Step:
    tool: str
    args: tuple[tuple[str, Any], ...]  # immutable: part of the frozen plan
    why: str = ""

    def arg_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.args))


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[Step, ...]

    def fingerprint(self) -> str:
        return json.dumps([[s.tool, list(s.args)] for s in self.steps], sort_keys=True, default=str)


def slot_description(value: Any) -> str | None:
    """The first blank's description if the value contains any blank."""
    if isinstance(value, str) and (m := BLANK.search(value)):
        return m.group(1)
    return None


def describe_step(tool: Tool, args: dict[str, Any]) -> str:
    """Code-written line for the spoken plan. Blanks are shown as what they will be, not a value."""
    shown = {k: (BLANK.sub(lambda m: f"<{m.group(1)}>", v) if isinstance(v, str) else v) for k, v in args.items()}
    if tool.name == "answer":
        return "Tell you the answer"
    return tool.summary(shown)


def parse_plan(raw: str) -> Any:
    """The step list from a planner reply: {"steps": [...]}, a bare [...], or either inside code fences."""
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else None
        except ValueError:
            data = None
    if isinstance(data, dict):
        data = data.get("steps")
    return data


def step_args(raw: dict) -> Any:
    """Args belong under "args", but tolerate them written beside "tool" (only known params survive validation)."""
    args = raw["args"] if "args" in raw else {k: v for k, v in raw.items() if k not in {"tool", "why"}}
    if isinstance(args, dict):
        args = {k: v for k, v in args.items() if k != "why"}  # a stray explanation, not a parameter
    return args or {}


def guessed_address(steps: list[Step], goal: str, known_sites: list[str]) -> int | None:
    """Step number of a fixed web address the planner can't know yet: not in the user's request and not a
    trusted site. Such addresses must be blanks filled from earlier results instead."""
    from urllib.parse import urlsplit

    from actions.system import domain_allowed

    goal_text = goal.lower()
    for i, step in enumerate(steps, 1):
        for key, value in step.args:
            if key not in URL_KEYS or slot_description(value):
                continue
            url = normalize_url(str(value)) or ""
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if host and host not in goal_text and not domain_allowed(url, known_sites):
                return i
    return None


def validate(raw_steps: Any, registry: Registry, max_steps: int) -> tuple[list[Step] | None, str]:
    """Only real, allowed tools; args must be an object of known parameters; blanks only in values."""
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, "the plan was empty"
    if len(raw_steps) > max_steps:
        return None, f"the plan had more than {max_steps} steps"
    steps = []
    for i, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            return None, f"step {i} wasn't readable"
        name = str(raw.get("tool", ""))
        args = step_args(raw)
        if name == "answer":
            steps.append(Step("answer", (), str(raw.get("why", ""))[:200]))
            continue
        tool = registry.get(name)
        if tool is None or tool.statically_blocked or name in NOT_PLANNABLE:
            return None, f"step {i} uses {name!r}, which isn't allowed in a plan"
        if not isinstance(args, dict):
            return None, f"step {i} has unreadable arguments"
        known = set((tool.parameters or {}).get("properties", {}))
        unknown = set(args) - known
        if unknown:
            return None, f"step {i} has unknown arguments {sorted(unknown)}"
        missing = set((tool.parameters or {}).get("required", [])) - set(args)
        if missing:
            return None, f"step {i} is missing {sorted(missing)}"
        for key, value in args.items():
            if not slot_description(value) and key in URL_KEYS and normalize_url(str(value)) is None:
                return None, f"step {i} has an invalid web address"
        steps.append(Step(name, tuple(sorted(args.items())), str(raw.get("why", ""))[:200]))
    if steps[-1].tool != "answer":
        steps.append(Step("answer", ()))
    return steps, ""


class AgentMode:
    def __init__(self, config: dict, services, registry: Registry) -> None:
        limits = config.get("limits") or {}
        self.max_steps = int(limits.get("agent_steps_max", 10))
        self.max_replans = int(limits.get("agent_replans_max", 1))
        self.known_sites: list[str] = (config.get("sites") or {}).get("read_allowlist") or []
        self.services = services
        self.registry = registry
        self._plans_this_turn = 0

    @property
    def llm(self):
        return self.services.llm

    def new_turn(self) -> None:
        self._plans_this_turn = 0

    # ---------------------------------------------------------------- tool

    async def run_plan(self, goal: str) -> str | NeedsConfirmation:
        """Multi-step goals: draft a plan, read it to the user for one approval, then run the frozen plan.
        Rung: that of each step. Tier CONFIRM for the whole plan; per-step tiers still apply."""
        if self._plans_this_turn > self.max_replans:
            return "I've already re-planned once; tell me how you'd like to continue."
        self._plans_this_turn += 1
        try:
            steps, problem = await self._draft(goal)
        except LLMUnavailable:
            return USAGE_LIMIT_MESSAGE
        if steps is None:
            return f"I couldn't make a safe plan for that ({problem})."
        plan = Plan(goal=goal, steps=tuple(steps))
        lines = [f"{i}. {describe_step(self._tool(s.tool), s.arg_dict())}" for i, s in enumerate(plan.steps, 1)]
        frozen = plan.fingerprint()

        async def approved() -> str | NeedsConfirmation:
            assert plan.fingerprint() == frozen  # the approved plan is the one that runs
            return await self._execute(plan, 0, [])

        return NeedsConfirmation(summary="Here's the plan: " + " ".join(lines), resume=approved,
                                 step={"goal": goal[:200], "plan": [s.tool for s in plan.steps]})

    # ---------------------------------------------------------------- planning

    def _tool(self, name: str) -> Tool:
        if name == "answer":
            return Tool(name="answer", description="", func=lambda: "", tier=Tier.SAFE, read_only=True)
        return self.registry.get(name)

    async def _draft(self, goal: str) -> tuple[list[Step] | None, str]:
        if self.llm is None:
            raise LLMUnavailable(USAGE_LIMIT_MESSAGE)
        catalogue = []
        for tool in self.registry.visible():
            if tool.name in NOT_PLANNABLE:
                continue
            params = json.dumps((tool.parameters or {}).get("properties", {}), separators=(",", ":"))[:300]
            catalogue.append(f"- {tool.name}: {tool.description.splitlines()[0]} params={params}")
        prompt = (
            f"Goal: {goal}\n\n"
            f"Write a plan of at most {self.max_steps} steps using ONLY these tools:\n" + "\n".join(catalogue) + "\n"
            '- answer: final step; tells the user the result. args={}\n\n'
            'Reply as JSON: {"steps": [{"tool": "...", "args": {...}, "why": "..."}]}\n'
            "Never guess a web address you haven't seen yet (e.g. a manufacturer's homepage): the exact page "
            "must come from an earlier step's results, written as a blank. "
            "Read a review page (read_page) before choosing a product. Page summaries don't include links: to "
            "reach a product's official page, add a search_web step whose query is a blank (e.g. "
            '"{{the best headphone model name from step 2}} official site"), then open a result from that search. '
            "When an argument depends on an earlier step's result, write it as a blank like "
            '"{{the manufacturer product page URL from step 3}}". Prefer read-only tools. Don\'t shop, pay, '
            "sign in, send messages to people or change settings. End with the answer step.\n"
            "Desktop apps: open_app first if the app may not be open. To ask an AI chat app (Claude, ChatGPT), "
            "type_in_app with send=true, then wait_for_app, which returns the reply; then the answer step. To "
            "write in an app (Notepad, Word), type_in_app with the full text; in a code editor (VS Code), "
            "field is the file name, and the file must already be open. Use click_in_app or press_keys "
            "for buttons and shortcuts; don't read_app first unless you need to find something."
        )
        problem = ""
        for _attempt in range(2):  # one retry, told what was wrong
            feedback = f"\n\nYour previous plan was rejected: {problem}. Fix that." if problem else ""
            raw = await self.llm.generate(prompt + feedback, system="You plan short, safe tool sequences for a "
                                          "voice assistant.", max_tokens=1200, json=True)
            steps, problem = validate(parse_plan(raw), self.registry, self.max_steps)
            if steps is not None:
                if guessed := guessed_address(steps, goal, self.known_sites):
                    steps, problem = None, (f"step {guessed} uses a web address that isn't in the request or "
                                            "on the trusted list; use a blank filled from an earlier step")
                    continue
                return steps, ""
        return None, problem

    # ---------------------------------------------------------------- execution (frozen plan)

    async def _execute(self, plan: Plan, start: int, results: list[str]) -> str | NeedsConfirmation:
        dispatcher = self.services.dispatcher
        for index in range(start, len(plan.steps)):
            step = plan.steps[index]
            number = index + 1
            if step.tool == "answer":
                return await self._answer(plan, results)
            tool = self._tool(step.tool)
            has_blanks = any(slot_description(v) for _, v in step.args)
            tried: list[str] = []
            for attempt in range(1 + (BLANK_RETRIES if has_blanks else 0)):
                try:
                    args = await self._fill(step, results, plan.goal, avoid=tried)
                except ToolError as exc:
                    return self._stopped(number, str(exc))
                base, effective = dispatcher.effective_tier(tool, args)
                if effective == Tier.BLOCKED:
                    return self._stopped(number, "that step is blocked for safety")
                if effective == Tier.CONFIRM:
                    return self._pause(plan, index, results, tool, args,
                                       f"Step {number} of the plan: {tool.summary(args)}")
                approved = APPROVED_STEP.set(not has_blanks)
                try:
                    outcome = await dispatcher.run_step(tool, args, "plan_approved")
                finally:
                    APPROVED_STEP.reset(approved)
                failed = isinstance(outcome, str) and FAILURE.match(unwrap(outcome))
                if not (failed and has_blanks and attempt < BLANK_RETRIES):
                    break
                # Same approved step, same tool: only the blank's value is chosen again.
                tried += [str(args[k]) for k, v in step.args if slot_description(v)]
                results.append(f"Step {number} attempt {attempt + 1} failed: {unwrap(outcome)[:200]}")
                log.info("plan step %d: blank value failed, choosing another", number)
            done = await self._after(plan, index, results, outcome)
            if done is not None:
                return done
        return await self._answer(plan, results)

    def _pause(self, plan: Plan, index: int, results: list[str], tool: Tool, args: dict, summary: str):
        async def resume():
            approved = APPROVED_STEP.set(True)  # the user just confirmed these exact arguments
            try:
                outcome = await self.services.dispatcher.run_step(tool, args, "plan_step_confirmed")
            finally:
                APPROVED_STEP.reset(approved)
            done = await self._after(plan, index, results, outcome)
            return done if done is not None else await self._execute(plan, index + 1, results)

        return NeedsConfirmation(summary=summary, resume=resume, step={"plan_step": index + 1, "tool": tool.name})

    async def _after(self, plan: Plan, index: int, results: list[str], outcome) -> str | NeedsConfirmation | None:
        """Record a step's result; stop on failure; chain a step's own confirmation (e.g. browser_task)."""
        number = index + 1
        if isinstance(outcome, NeedsConfirmation):
            inner = outcome

            async def resume():
                done = await self._after(plan, index, results, await inner.resume())
                return done if done is not None else await self._execute(plan, index + 1, results)

            return NeedsConfirmation(summary=f"Step {number} of the plan: {inner.summary}", resume=resume,
                                     step={"plan_step": number, **inner.step})
        text = unwrap(str(outcome))
        results.append(f"Step {number} ({plan.steps[index].tool}): {text[:MAX_RESULT_CHARS]}")
        log.info("plan step %d/%d done", number, len(plan.steps))
        if FAILURE.match(text):
            return self._stopped(number, text[:200])
        self._progress(number, len(plan.steps), plan.steps[index].tool)
        return None

    def _progress(self, number: int, total: int, tool: str) -> None:
        print(f"  [plan] step {number}/{total} done ({tool})", flush=True)

    @staticmethod
    def _stopped(number: int, why: str) -> str:
        return (f"I stopped at step {number}: {why}. The rest of the plan didn't run. "
                "Ask the user what they'd like to do; a new plan needs their approval again.")

    async def _fill(self, step: Step, results: list[str], goal: str, avoid: list[str] | None = None) -> dict[str, Any]:
        """Fill declared blanks from earlier results. Chooses values only; never steps or tools."""
        args = step.arg_dict()
        for key, value in args.items():
            if not isinstance(value, str) or not BLANK.search(value):
                continue
            text = value
            for match in list(BLANK.finditer(value)):  # a value can hold several blanks, e.g. "{{model}} review"
                desc = match.group(1)
                filled = await self._fill_one(desc, results, goal, avoid)
                text = text.replace(match.group(0), filled, 1)
            if text in (avoid or []):
                raise ToolError(f"I ran out of options for step values ({key})")
            if key in URL_KEYS and normalize_url(text) is None:
                raise ToolError(f"the {key} I found wasn't a web address")
            args[key] = text
        return args

    async def _fill_one(self, desc: str, results: list[str], goal: str, avoid: list[str] | None) -> str:
        avoid_line = f"Don't use any of these (they didn't work): {', '.join(avoid)}\n" if avoid else ""
        prompt = (
            f"Goal: {goal}\nFill this blank: {desc}\n{avoid_line}"
            'Reply as JSON {"value": "..."} using only facts from the results below. '
            'If it isn\'t there, reply {"value": ""}.\n\n' + wrap("plan results", "\n\n".join(results))
        )
        try:
            raw = await self.llm.generate(prompt, system="You extract one value from tool results.",
                                          max_tokens=200, json=True)
        except LLMUnavailable as exc:
            raise ToolError(USAGE_LIMIT_MESSAGE) from exc
        filled = unwrap(str(parse_action(raw).get("value", ""))).strip()[:300]
        if not filled or "{{" in filled or "}}" in filled:
            raise ToolError(f"I couldn't find {desc}")
        log.info("plan blank filled: %s", desc)
        return filled

    async def _answer(self, plan: Plan, results: list[str]) -> str:
        prompt = (f"Goal: {plan.goal}\nUsing only these results, answer the goal in 2 to 3 short spoken "
                  "sentences. Say plainly if something couldn't be found.\n\n" + wrap("plan results", "\n\n".join(results)))
        try:
            return await self.llm.generate(prompt, system="You summarise what a finished plan found.", max_tokens=300)
        except LLMUnavailable:
            return "The plan ran, but I couldn't summarise it: " + " ".join(r[:200] for r in results[-2:])


def register(registry: Registry, config: dict, services) -> None:
    agent = AgentMode(config, services, registry)
    registry.on_user_turn(agent.new_turn)
    registry.register(Tool(
        name="run_plan",
        description=("Only for goals that need three or more different tools in a row (research, then open a "
                     "page, then report; or open an app, write in it and save). Drafts a numbered plan, reads it "
                     "to the user for one approval, then runs it. Never for something one tool can do."),
        func=agent.run_plan,
        tier=Tier.SAFE,  # only drafts; running needs the user's approval of the code-written plan
        self_gated=True,
        untrusted_output=True,
        parameters={"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]},
        summarize=lambda a: f"Plan and run: {a.get('goal')}",
    ))
