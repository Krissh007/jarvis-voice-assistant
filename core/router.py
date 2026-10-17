"""Intent routing for the reasoning layer: which route a request takes and which few tools it gets.

Every request is sorted into one of three routes:
- "skill": one tool obviously fits ("what's the weather in Pune"). The tool and up to two neighbours
  from its category are attached, so the model can still pick the right one.
- "tools": tools fit, but not one clearly ("find my resume and open it", "anything new in my job
  search"). The best tools from the matching categories are attached.
- "reasoning": no tool fits, or the request asks for thinking ("should I...", "compare", maths). Only
  web search and page reading are attached, for current facts.

Between 3 and 8 tools are attached, never the whole registry. Candidates come from the Agent Hub's tool
registry (`find_tools`, every source) when it answers within a moment, otherwise from JARVIS's own
skills ranked here the same way (BM25 over name, keywords, description and category, as in
hub/tools/catalog.py). Category cue words, reasoning cues and action verbs then shape the choice:
a request with no action verb gets read-only tools when there are any. An optional classifier (a
small LLM, wired in later) is asked only when the keyword scores are in a grey zone.

Each decision is appended to logs/routing.jsonl: a hash of the request (not its text), the route,
categories, tool ids, scores and timing. No request text, tool arguments or keys are logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import catalog
from core.registry import Registry

log = logging.getLogger(__name__)

MIN_TOOLS = 3
MAX_TOOLS = 8
MAX_REQUEST_CHARS = 500
HUB_TIMEOUT_S = 2.5
HUB_RETRY_S = 60.0  # after the Hub fails a lookup, use the local index for this long
CUE_BOOST = 0.3      # a tool's score grows by this much per cue word of its category (at most two)
CONTEXT_TOOLS = 2    # read-only tools kept on the reasoning route when they fit well

# Score thresholds, calibrated on tests/fixtures/routing_samples.yaml (BM25 scores over the registry).
TOOL_MIN = 2.6       # below this no tool really fits
SKILL_MIN = 4.0      # at or above this, with a clear lead, one tool is "the" skill
SKILL_LEAD = 1.25    # the top tool's score must be this many times the next tool's
GREY_LOW, GREY_HIGH = 1.8, 3.4  # ask the optional classifier when the top score is in here

# Same categories as the Hub (hub/tools/schema.py). Cue words say which areas a request is about,
# even when no registered tool covers it yet (mail, calendar...).
CATEGORY_CUES: dict[str, list[str]] = {
    "search": [r"\bweather\b", r"\bforecast\b", r"\brain(?:ing|y)?\b", r"\bhow (?:hot|cold|warm)\b", r"\btemperature\b", r"\bnews\b", r"\blatest\b", r"\bsearch\b",
               r"\blook (?:it |that )?up\b", r"\bgoogle\b", r"\bprice of\b", r"\bwho (?:won|is|was)\b",
               r"\bscore\b", r"\barticle\b"],
    "files": [r"\bfiles?\b", r"\bfolders?\b", r"\bdocuments?\b", r"\bpdf\b", r"\bresume\b", r"\bdownloads\b",
              r"\.(?:txt|html|css|js|py|md|docx)\b"],
    "mail": [r"\be-?mails?\b", r"\binbox\b", r"\bgmail\b", r"\bmail\b"],
    "calendar": [r"\bcalendar\b", r"\bmeetings?\b", r"\bschedule\b", r"\bappointments?\b", r"\bevents?\b"],
    "docs": [r"\bgoogle drive\b", r"\bdrive\b", r"\bnotion\b", r"\bgoogle docs?\b", r"\bspreadsheet\b"],
    "code": [r"\bgithub\b", r"\brepo(?:sitor(?:y|ies))?\b", r"\bpull requests?\b", r"\bissues?\b", r"\bcommits?\b"],
    "browser": [r"\bwebsite\b", r"\bbrowser\b", r"\bchrome\b", r"\bon (?:amazon|flipkart|youtube)\b",
                r"\bthis page\b", r"\.(?:com|in|org)\b"],
    "desktop": [r"\bnotepad\b", r"\bwindow\b", r"\bclick\b", r"\btype\b", r"\bpress\b", r"\bscroll\b",
                r"\bvs code\b", r"\bclaude\b"],
    "screen": [r"\bscreen\b", r"\bclipboard\b", r"\bcopied\b", r"\bwhat am i looking at\b"],
    "media": [r"\bmusic\b", r"\bsongs?\b", r"\bplay\b", r"\bput on\b", r"\bpause\b", r"\bplaylist\b", r"\byoutube\b",
              r"\bvideo\b", r"\bvolume\b", r"\bnext track\b"],
    "system": [r"\bbattery\b", r"\bcpu\b", r"\btime is it\b", r"\bdate\b", r"\bopen (?:the )?app\b",
               r"\bclose (?:the )?app\b", r"\bvolume\b"],
    "memory": [r"\bremember\b", r"\bforget\b", r"\bwhat did i tell you\b", r"\bmy (?:wife|car|birthday)\b"],
    "reminders": [r"\bremind(?:er|ers)?\b", r"\btimer\b", r"\balarm\b"],
    "jobs": [r"\bjobs?\b", r"\bshortlist\b", r"\bapplications?\b", r"\bjob search\b", r"\bdiscovery\b",
             r"\blinkedin\b", r"\bnaukri\b", r"\bscreening\b", r"\breview queue\b"],
}
_CUES = {c: [re.compile(p, re.IGNORECASE) for p in ps] for c, ps in CATEGORY_CUES.items()}

# Clear requests for thinking or writing: always the reasoning route (with any strong read-only tool
# that gives it context, e.g. "explain what's on my screen" keeps look_at_screen).
STRONG_REASONING = re.compile(
    r"\b(why|how come|explain|compare|comparison|versus|difference between|pros and cons|should i|"
    r"which (?:is|one is) better|is it worth|what would happen|what if|help me (?:decide|think|choose)|"
    r"recommend|advice|brainstorm|ideas for|meaning of|fun fact|"
    r"tell me (?:a|an|some) (?:joke|fact|story|riddle)|"
    r"(?:write|compose|make up) (?:me )?(?:a|an|some) (?:\w+ )?(?:poem|haiku|story|joke|limerick|song|lyrics|essay|speech|caption|toast))\b",
    re.IGNORECASE)
# Weaker hints (maths, sizes, plans): reasoning unless one tool clearly fits.
REASONING_CUES = re.compile(
    r"\b(calculate|work out|how (?:much|many|long|far|old) (?:is|are|would|will|does|do)|plan (?:a|my|out)|"
    r"summari[sz]e the idea)\b"
    r"|\d+(?:\.\d+)?\s*(?:%|percent|[-+*/x^]\s*\d)",
    re.IGNORECASE)
FACTS_CUES = re.compile(r"\b(latest|today|tonight|current|currently|news|price|cost|who won|right now|this week|"
                        r"recent|20\d\d)\b", re.IGNORECASE)
# Words that ask JARVIS to change something. Without one, write tools are dropped when read ones exist.
ACT_VERBS = re.compile(
    r"\b(send|reply|forward|delete|remove|trash|create|make|write|save|edit|change|update|rename|move|"
    r"approve|skip|submit|apply|run|start|launch|kick off|play|pause|resume|stop|open|close|set|turn|mute|"
    r"cancel|remind|schedule|book|add|type|click|press|scroll|minimi[sz]e|maximi[sz]e|fix|rewrite|"
    r"translate|remember|forget|go to|search for|navigate)\b",
    re.IGNORECASE)

Classifier = Callable[[str, list[str]], Awaitable[list[str]]]  # (request, categories) -> chosen categories


# ---------------------------------------------------------------- ranking (same scheme as the Hub)

_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its me my of on or please "
    "show tell that the their them this to up use what when where which who will with you your".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        if len(w) < 2 or w in STOPWORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 3 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


@dataclass
class ToolPick:
    id: str
    name: str
    executor: str
    category: str
    access: str
    description: str
    score: float = 0.0

    @classmethod
    def from_entry(cls, d: dict[str, Any], score: float = 0.0) -> ToolPick:
        return cls(id=d["id"], name=d["name"], executor=d["executor"], category=d["category"],
                   access=d["access"], description=d["description"], score=float(d.get("score", score)))


class LocalIndex:
    """JARVIS's own skills, ranked like the Hub ranks its registry. Used when the Hub doesn't answer."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = [{**e, "id": e.get("id") or f"{catalog.JARVIS_SOURCE}.{e['name']}",
                         "executor": e.get("executor", "jarvis")} for e in entries]
        self._docs = []
        self._df: Counter = Counter()
        for e in self.entries:
            name = tokens(e["name"].replace("_", " "))
            toks = (name * 2 + tokens(" ".join(e.get("keywords", []))) * 2 + tokens(e["description"])
                    + tokens(e["category"]) + tokens(catalog.CATEGORIES.get(e["category"], "")))
            self._docs.append((e, Counter(toks), len(toks)))
            self._df.update(set(toks))

    def get(self, tool_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e["id"] == tool_id), None)

    def find(self, query: str, categories: list[str] | None = None, limit: int = MAX_TOOLS) -> list[ToolPick]:
        cats = set(categories or [])
        pool = [(e, tf, n) for e, tf, n in self._docs if not cats or e["category"] in cats]
        q = tokens(query)
        if not q:
            return [ToolPick.from_entry(e) for e, _, _ in pool][:limit] if cats else []
        total = len(self._docs) or 1
        avg = sum(n for _, _, n in self._docs) / total or 1.0
        out = []
        for e, tf, n in pool:
            score = 0.0
            for t in set(q):
                if f := tf.get(t, 0):
                    idf = math.log(1 + (total - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    score += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * n / avg))
            if score > 0:
                out.append(ToolPick.from_entry(e, round(score, 3)))
        out.sort(key=lambda p: (-p.score, p.id))
        return out[:limit]


# ---------------------------------------------------------------- the decision

@dataclass
class Decision:
    route: str                       # "skill", "tools" or "reasoning"
    tools: list[ToolPick]
    categories: list[str]
    confidence: float
    reason: str
    index: str = "local"             # where candidates came from: "hub" or "local"
    ms: float = 0.0
    missing: list[str] = field(default_factory=list)  # categories asked about that have no tools yet

    @property
    def tool_ids(self) -> list[str]:
        return [t.id for t in self.tools]

    def log_record(self, text: str, source: str = "") -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            "chars": len(text),
            "source": source,
            "route": self.route,
            "categories": self.categories,
            "missing": self.missing,
            "tools": [{"id": t.id, "score": t.score} for t in self.tools],
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "index": self.index,
            "ms": round(self.ms, 1),
        }


WEB_SEARCH = "jarvis.search_web"


def boost(hits: list[ToolPick], cues: dict[str, int]) -> list[ToolPick]:
    """Raise tools whose category the request's cue words point at, then re-rank."""
    for h in hits:
        if h.category in cues:
            h.score = round(h.score * (1 + CUE_BOOST * min(cues[h.category], 2)), 3)
    return sorted(hits, key=lambda h: (-h.score, h.id))


def cue_categories(text: str) -> dict[str, int]:
    """Categories whose cue words appear in the request, with how many cues matched."""
    hits = {c: sum(bool(rx.search(text)) for rx in rxs) for c, rxs in _CUES.items()}
    return {c: n for c, n in sorted(hits.items(), key=lambda kv: -kv[1]) if n}


class Router:
    def __init__(self, registry: Registry, hub: Any = None, log_path: Path | None = None,
                 classifier: Classifier | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.registry = registry
        self.hub = hub                     # actions.agent_hub.HubClient, or None
        self.log_path = log_path
        self.classifier = classifier
        self.clock = clock
        self._local: LocalIndex | None = None
        self._hub_down_until = 0.0
        self._has_tools: set[str] = set()  # categories known to have tools (connectors only get added)

    @property
    def local(self) -> LocalIndex:
        if self._local is None:  # built on first use: every skill is registered by then
            self._local = LocalIndex(catalog.export(self.registry))
        return self._local

    # -- candidates

    async def _candidates(self, text: str, categories: list[str] | None = None,
                          limit: int = MAX_TOOLS) -> tuple[list[ToolPick], str]:
        if self.hub is not None and self.clock() >= self._hub_down_until:
            try:
                async with asyncio.timeout(HUB_TIMEOUT_S):
                    rows = await self.hub.find_tools(text, categories, None, limit)
                return [p for p in (ToolPick.from_entry(r) for r in rows) if self._runnable(p)], "hub"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # down, slow, or an older Hub without the registry
                self._hub_down_until = self.clock() + HUB_RETRY_S
                log.info("tool lookup in the Agent Hub failed (%s); using JARVIS's own skills for %.0f s",
                         type(exc).__name__, HUB_RETRY_S)
        return self.local.find(text, categories, limit), "local"

    def _runnable(self, pick: ToolPick) -> bool:
        """A JARVIS skill the Hub lists must still exist here (and not be blocked) to be offered."""
        if pick.executor != "jarvis":
            return True
        return pick.name not in catalog.EXCLUDED and any(t.name == pick.name for t in self.registry.visible())

    # -- routing

    async def route(self, request: str, source: str = "") -> Decision:
        start = self.clock()
        text = " ".join(request.split())[:MAX_REQUEST_CHARS]
        hits, index = await self._candidates(text)
        cues = cue_categories(text)
        hits = boost(hits, cues)
        top = hits[0].score if hits else 0.0
        second = hits[1].score if len(hits) > 1 else 0.0
        missing = await self._missing(cues, hits)
        reason = ""

        chosen: list[str] | None = None
        if self.classifier is not None and GREY_LOW <= top < GREY_HIGH and not STRONG_REASONING.search(text):
            chosen = await self._classify(text)
            reason = "classifier"

        if chosen == []:
            decision = self._reasoning(text, hits, "classifier: no tool area fits")
        elif STRONG_REASONING.search(text):
            decision = self._reasoning(text, hits, "asks for thinking or writing")
        elif cues and next(iter(cues)) in missing:
            decision = self._reasoning(text, [], f"no {next(iter(cues))} tools yet")
        elif REASONING_CUES.search(text) and not (top >= SKILL_MIN and hits[0].category in cues):
            decision = self._reasoning(text, hits, "asks for working out")
        elif top < TOOL_MIN and not chosen:
            decision = self._reasoning(text, [], "no tool fits")
        elif hits[0].id == WEB_SEARCH and not chosen:
            decision = self._reasoning(text, hits, "general facts: think, search the web if needed")
        else:
            decision = await self._tools(text, hits, cues, chosen, top, second, reason)
        decision.missing = missing
        decision.index = index
        decision.ms = (self.clock() - start) * 1000
        self._log(decision, text, source)
        return decision

    async def _missing(self, cues: dict[str, int], hits: list[ToolPick]) -> list[str]:
        """Cue categories with no registered tool at all (e.g. mail before a mail connector exists)."""
        found = {h.category for h in hits}
        out = []
        for c in cues:
            if c in found or c in self._has_tools:
                continue
            extra, _ = await self._candidates("", [c], 1)
            if extra:
                self._has_tools.add(c)
            else:
                out.append(c)
        return out

    async def _classify(self, text: str) -> list[str] | None:
        try:
            async with asyncio.timeout(4):
                cats = await self.classifier(text, list(CATEGORY_CUES))
            return [c for c in cats if c in CATEGORY_CUES]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("routing classifier failed (%s); keeping the keyword decision", type(exc).__name__)
            return None

    async def _tools(self, text: str, hits: list[ToolPick], cues: dict[str, int], chosen: list[str] | None,
                     top: float, second: float, reason: str) -> Decision:
        if chosen:
            cats = chosen
        else:
            cats = []
            for c in [hits[0].category, *(h.category for h in hits[1:3] if h.score >= 0.6 * top), *list(cues)[:2]]:
                if c not in cats:
                    cats.append(c)
        picks = [h for h in hits if h.category in cats]
        skill = (not chosen and top >= SKILL_MIN and top >= SKILL_LEAD * second)
        reads_only = not ACT_VERBS.search(text) and any(p.access == "read" for p in picks)
        if skill:
            picks = [hits[0], *[p for p in picks if p.id != hits[0].id and p.category == hits[0].category][:2]]
        if len(picks) < MIN_TOOLS:  # top up with the rest of the chosen categories' tools
            extra, _ = await self._candidates("", cats[:2], MAX_TOOLS)
            picks += [p for p in extra if p.id not in {q.id for q in picks}]
        if reads_only:  # a question: no tool that changes anything, unless only write tools fit
            picks = [p for p in picks if p.access == "read" or (skill and p.id == hits[0].id)]
        if len(picks) < MIN_TOOLS and not chosen:  # still short: other matches that fit the same rule
            picks += [h for h in hits if h.id not in {q.id for q in picks}
                      and (not reads_only or h.access == "read")][: MIN_TOOLS - len(picks)]
        picks = picks[: MIN_TOOLS if skill else MAX_TOOLS]
        return Decision(
            route="skill" if skill else "tools",
            tools=picks,
            categories=cats,
            confidence=min(1.0, top / (SKILL_MIN * 1.5)),
            reason=reason or (f"clear match: {hits[0].name}" if skill else "several tools fit"),
        )

    def _reasoning(self, text: str, hits: list[ToolPick], reason: str) -> Decision:
        """Thinking, with web search and page reading for current facts (and weather if asked), plus up
        to CONTEXT_TOOLS read-only tools that fit the request well."""
        ids = [WEB_SEARCH, "jarvis.read_page"]
        if "weather" in text.lower() or "forecast" in text.lower():
            ids.append("jarvis.get_weather")
        tools = [ToolPick.from_entry(e) for e in (self.local.get(i) for i in ids) if e]
        context = [h for h in hits if h.access == "read" and h.score >= SKILL_MIN and h.id not in ids]
        tools = [*context[:CONTEXT_TOOLS], *tools]
        return Decision(route="reasoning", tools=tools, categories=["reasoning", "search"],
                        confidence=0.8 if STRONG_REASONING.search(text) or FACTS_CUES.search(text) else 0.5,
                        reason=reason)

    # -- logging

    def _log(self, decision: Decision, text: str, source: str) -> None:
        log.info("route %s: %s (%s, %.0f ms, %s)", decision.route, ", ".join(decision.tool_ids) or "-",
                 decision.reason, decision.ms, decision.index)
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(decision.log_record(text, source)) + "\n")
        except OSError:
            log.warning("couldn't write %s", self.log_path)


def as_dict(decision: Decision) -> dict[str, Any]:
    """For printing (--route): everything except the descriptions."""
    d = asdict(decision)
    d["tools"] = [{k: v for k, v in t.items() if k != "description"} for t in d["tools"]]
    return d
