"""The Brain view's graph: what JARVIS knows and has done, as nodes and links (docs/brain-ui-plan.md).

Everything comes from JARVIS itself, read-only: remembered facts and kept conversations
(data/memory.sqlite), its tools (the registry, grouped by core/catalog.py's categories), the Agent Hub's
agents and actions (agent_status), pending reminders, and the files JARVIS actually used (the audit log's
path arguments, never the whole file index). Nothing is invented and nothing leaves the PC.

Every link carries a reason the inspector can show ("called 3 times in this conversation", "close in
meaning", "shares: car, service"). Sizes are capped so a long history can't swamp the view.

The labels are the facts and lines themselves, which already passed the secret filter when they were kept;
they go through privacy.redact as well (emails, long numbers). The page shows them as text, never HTML.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core import catalog
from core.privacy import redact

log = logging.getLogger(__name__)

TYPES = {  # type -> the hub every node of that type hangs from (and its label; not a tool category's name)
    "memory": ("hub:memory", "Remembered facts"),
    "conversation": ("hub:conversation", "Conversations"),
    "tool": (None, None),            # tools hang from their category's hub (cat:<category>)
    "agent": ("hub:agents", "Agent Hub"),
    "reminder": ("hub:reminder", "Reminders"),
    "file": ("hub:file", "Files used"),
}
MAX_CONVERSATIONS = 200
MAX_FILES = 150
MAX_AUDIT_LINES = 8000          # newest lines of logs/actions.jsonl read for tool calls and files
DAYS = 30                       # conversations and tool calls older than this aren't drawn
# A fact and a conversation are linked when the fact's meaning is this close (cosine) to one of the user's
# lines in it. Measured 26 Oct 2026 on Krishna's real data (14 facts x 21 conversations, 337 lines): the
# best single line separates far better than the conversation's average (averages of long chats were all
# "close" to everything). From 0.55 the pairs were right (the confirmation rule <-> a talk about
# confirmations; screen permission <-> "what do you see on the screen?"); below it they turned vague
# ("Claude replies summary" <-> "Open Claude.").
MEANING_MIN = 0.55
MAX_FACT_LINKS = 3              # facts linked to one conversation, closest first
SHARED_WORDS_MIN = 2            # or, without meaning vectors to compare, this many shared words
FILE_TOOLS = {"read_file", "write_file", "edit_file", "delete_file", "open_file"}
WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{2,}")
STOP = {"the", "and", "for", "you", "your", "are", "was", "what", "that", "this", "with", "have", "can", "about",
        "from", "please", "jarvis", "tell", "show", "open", "how", "who", "when", "where", "why", "did", "does",
        "will", "would", "could", "should", "just", "now", "today", "like", "want", "need", "know", "call", "user",
        "krishna", "its", "it's", "i'm", "don't", "there", "they", "them", "then", "than", "also", "into", "out"}


def _words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "")} - STOP


def _label(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    text = redact(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _local(ts: str) -> datetime | None:
    """The audit log's UTC timestamp as local time without a zone (how conversations are stored)."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _vector(blob: bytes | None):
    if not blob:
        return None
    import numpy as np

    v = np.frombuffer(blob, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else None


def read_audit(path: Path | None, since: datetime) -> list[dict[str, Any]]:
    """Tool calls from the newest audit lines: {tool, at (local), path?, text?}. Unreadable lines are skipped."""
    if path is None or not Path(path).is_file():
        return []
    try:
        with Path(path).open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - MAX_AUDIT_LINES * 600))
            lines = f.read().decode("utf-8", "replace").splitlines()[-MAX_AUDIT_LINES:]
    except OSError:
        log.warning("brain: couldn't read %s", path)
        return []
    calls = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        at = _local(rec.get("timestamp", ""))
        if at is None or at < since or rec.get("dry_run"):
            continue
        if str(rec.get("confirmation", "")).startswith(("cancelled", "refused", "timeout")):
            continue
        args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
        calls.append({"tool": str(rec.get("tool") or ""), "at": at,
                      "path": args.get("path") if isinstance(args.get("path"), str) else None,
                      "text": args.get("text") if isinstance(args.get("text"), str) else None})
    return calls


def _short_path(p: str) -> str:
    """A path as the user knows it: from their home folder when it's under it, forward slashes."""
    home = os.environ.get("USERPROFILE", "")
    s = str(p).replace("\\", "/")
    h = home.replace("\\", "/")
    if h and s.lower().startswith(h.lower() + "/"):
        s = s[len(h) + 1:]
    return s[-160:]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.links: dict[tuple[str, str], dict[str, Any]] = {}

    def node(self, nid: str, type_: str, label: str, hub: bool = False, **detail: Any) -> str:
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, "type": type_, "label": label, "hub": hub, "detail": detail}
        return nid

    def link(self, a: str, b: str, reason: str, kind: str) -> None:
        if a == b or a not in self.nodes or b not in self.nodes:
            return
        key = (a, b) if a < b else (b, a)
        if key not in self.links:
            self.links[key] = {"source": a, "target": b, "reason": reason, "kind": kind}

    def hub(self, type_: str) -> str:
        nid, label = TYPES[type_]
        self.node(nid, type_, label, hub=True)
        self.link("jarvis", nid, f"part of JARVIS: {label.lower()}", "structure")
        return nid


def build(*, store=None, conversations=None, registry=None, hub_status: dict | None = None,
          reminders: list[dict] | None = None, audit_path: Path | None = None, now: datetime | None = None) -> dict:
    """The whole graph: {"nodes", "links", "counts", "generated", "capped"}. Every source is optional."""
    now = now or datetime.now()
    since = now - timedelta(days=DAYS)
    g = Graph()
    g.node("jarvis", "core", "JARVIS", hub=True)
    capped: list[str] = []

    # ---- remembered facts
    facts = []
    if store is not None:
        try:
            with store.db.connect() as db:
                facts = [dict(r) for r in db.execute("SELECT id, text, added, uses, embedding FROM facts ORDER BY id")]
        except Exception:
            log.exception("brain: facts unreadable")
    for f in facts:
        g.hub("memory")
        nid = g.node(f"fact:{f['id']}", "memory", _label(f["text"], 140), added=f["added"], uses=f["uses"])
        g.link(nid, "hub:memory", "remembered", "structure")

    # ---- kept conversations (with a meaning vector from their user lines)
    convs: list[dict] = []
    if conversations is not None:
        try:
            with conversations.db.connect() as db:
                rows = db.execute(
                    "SELECT c.id, c.started, c.ended, c.channels, COUNT(m.id) AS lines, "
                    "(SELECT text FROM messages WHERE conversation = c.id AND role = 'user' ORDER BY id LIMIT 1) AS first "
                    "FROM conversations c JOIN messages m ON m.conversation = c.id WHERE c.started >= ? "
                    "GROUP BY c.id ORDER BY c.started DESC LIMIT ?", (since.isoformat(timespec="seconds"),
                                                                     MAX_CONVERSATIONS + 1)).fetchall()
                convs = [dict(r) for r in rows]
                if len(convs) > MAX_CONVERSATIONS:
                    convs = convs[:MAX_CONVERSATIONS]
                    capped.append("conversations")
                ids = [c["id"] for c in convs]
                texts: dict[int, list[str]] = defaultdict(list)
                vecs: dict[int, list] = defaultdict(list)
                if ids:
                    q = ",".join("?" * len(ids))
                    for r in db.execute(f"SELECT conversation, text, embedding FROM messages WHERE role = 'user' "
                                        f"AND conversation IN ({q})", ids):
                        texts[r["conversation"]].append(r["text"])
                        v = _vector(r["embedding"])
                        if v is not None:
                            vecs[r["conversation"]].append((v, r["text"]))
        except Exception:
            log.exception("brain: conversations unreadable")
            convs = []
        for c in convs:
            c["words"] = _words(" ".join(texts.get(c["id"], [])))
            c["said"] = vecs.get(c["id"], [])   # (meaning vector, text) of each line the user said
    for c in convs:
        g.hub("conversation")
        when = _parse(c["started"])
        nid = g.node(f"conv:{c['id']}", "conversation", _label(c["first"] or "(JARVIS spoke first)", 90),
                     started=c["started"], ended=c["ended"], lines=c["lines"], channels=c["channels"],
                     day=when.strftime("%a %d %b") if when else "")
        g.link(nid, "hub:conversation", "kept conversation", "structure")

    # ---- facts <-> conversations: close in meaning, or sharing words
    fact_words = {f["id"]: _words(f["text"]) for f in facts}
    fact_vecs = {f["id"]: _vector(f.get("embedding")) for f in facts}
    for c in convs:
        scored = []
        for f in facts:
            fv = fact_vecs.get(f["id"])
            sim, line = 0.0, ""
            if fv is not None:
                for v, text in c["said"]:
                    s = float(fv @ v)
                    if s > sim:
                        sim, line = s, text
            shared = sorted(fact_words[f["id"]] & c["words"])
            if sim >= MEANING_MIN:
                scored.append((sim + 0.01 * len(shared), f["id"],
                               f"close in meaning to “{_label(line, 60)}” ({sim:.2f})"))
            # words only when meaning can't be compared: on real data they mostly matched on "asking, not"
            elif (fv is None or not c["said"]) and len(shared) >= SHARED_WORDS_MIN:
                scored.append((0.3 + 0.01 * len(shared), f["id"], "shares: " + ", ".join(shared[:4])))
        for _, fid, reason in sorted(scored, reverse=True)[:MAX_FACT_LINKS]:
            g.link(f"conv:{c['id']}", f"fact:{fid}", reason, "meaning")

    # ---- tools, by category
    names = set()
    if registry is not None:
        for t in registry.visible():
            names.add(t.name)
            cat = catalog.CATALOG.get(t.name, ("core", []))[0] if t.name not in catalog.EXCLUDED else "core"
            cat_id = f"cat:{cat}"
            g.node(cat_id, "tool", cat.capitalize() if cat != "core" else "Core", hub=True,
                   description=catalog.CATEGORIES.get(cat, "JARVIS's own controls: plans, tasks, the Agent Hub."))
            g.link("jarvis", cat_id, f"{cat} tools", "structure")
            nid = g.node(f"tool:{t.name}", "tool", t.name, description=_label(t.description, 220),
                         read_only=bool(t.read_only))
            g.link(nid, cat_id, f"a {cat} tool", "structure")

    # ---- the Agent Hub's agents and actions
    agents = (hub_status or {}).get("agents") or []
    for a in agents:
        g.hub("agent")
        aid = g.node(f"agent:{a.get('name')}", "agent", str(a.get("name")), hub=True,
                     description=_label(a.get("description"), 200), reachable=a.get("reachable"),
                     optional=a.get("optional"))
        g.link(aid, "hub:agents", "an agent of the Agent Hub", "structure")
        for act in a.get("action_list") or []:
            nid = g.node(f"action:{a.get('name')}.{act.get('name')}", "agent", str(act.get("name")),
                         description=_label(act.get("description"), 200), side_effect=bool(act.get("side_effect")))
            g.link(nid, aid, f"an action of {a.get('name')}", "structure")
    if agents and "agent_hub_request" in names:
        g.link("tool:agent_hub_request", "hub:agents", "hands requests to the Agent Hub", "uses")

    # ---- pending reminders
    for r in reminders or []:
        g.hub("reminder")
        due = r.get("due")
        nid = g.node(f"reminder:{r.get('id')}", "reminder", _label(r.get("text"), 90),
                     due=due.isoformat(timespec="minutes") if isinstance(due, datetime) else str(due or ""))
        g.link(nid, "hub:reminder", "pending reminder", "structure")

    # ---- what the audit log says JARVIS did: tools in conversations, files it used, reminders it set
    calls = read_audit(audit_path, since)
    spans = []
    for c in sorted(convs, key=lambda c: c["started"]):
        start = _parse(c["started"])
        end = _parse(c["ended"]) if c["ended"] else None
        if start:
            spans.append((start, end, c["id"]))
    spans = [(s, e or (spans[i + 1][0] if i + 1 < len(spans) else now), cid) for i, (s, e, cid) in enumerate(spans)]

    def conv_at(at: datetime) -> int | None:
        for s, e, cid in spans:
            if s - timedelta(seconds=5) <= at <= e + timedelta(seconds=60):
                return cid
        return None

    per_conv: dict[tuple[int, str], int] = Counter()
    file_uses: dict[str, Counter] = defaultdict(Counter)
    file_last: dict[str, datetime] = {}
    for call in calls:
        cid = conv_at(call["at"])
        if cid is not None and call["tool"] in names:
            per_conv[(cid, call["tool"])] += 1
        if call["tool"] in FILE_TOOLS and call["path"]:
            p = _short_path(call["path"])
            file_uses[p][call["tool"]] += 1
            file_last[p] = max(call["at"], file_last.get(p, call["at"]))
        if call["tool"] == "set_reminder" and call["text"] and cid is not None:
            for r in reminders or []:
                if " ".join(str(r.get("text", "")).split()).lower() == " ".join(call["text"].split()).lower():
                    g.link(f"reminder:{r.get('id')}", f"conv:{cid}", "set in this conversation", "uses")
    for (cid, tool), n in per_conv.items():
        g.link(f"conv:{cid}", f"tool:{tool}", f"called {n} time{'s' if n != 1 else ''} in this conversation", "uses")
    recent_files = sorted(file_last, key=file_last.get, reverse=True)
    if len(recent_files) > MAX_FILES:
        recent_files = recent_files[:MAX_FILES]
        capped.append("files")
    for p in recent_files:
        g.hub("file")
        nid = g.node(f"file:{p}", "file", _label(p.rsplit("/", 1)[-1], 80), path=_label(p, 160),
                     last_used=file_last[p].isoformat(timespec="minutes"))
        g.link(nid, "hub:file", "a file JARVIS used", "structure")
        for tool, n in file_uses[p].items():
            g.link(nid, f"tool:{tool}", f"{tool} {n}×", "uses")

    # an agent is a hub for its actions but still one of the things shown; the rest of the hubs are just grouping
    counts = Counter(n["type"] for n in g.nodes.values() if not n["hub"] or n["id"].startswith("agent:"))
    return {"nodes": list(g.nodes.values()), "links": list(g.links.values()), "counts": dict(counts),
            "generated": now.isoformat(timespec="seconds"), "capped": capped}
