"""The Brain view's graph: what JARVIS knows and has done, as nodes and links (docs/brain-ui-plan.md).

Everything comes from JARVIS itself, read-only: its tools (the registry, grouped by core/catalog.py's
categories, with how often each was called), the Agent Hub's agents and actions (agent_status) and the
requests they are working on now, the projects JARVIS looks after (brain_view.projects in config.yaml),
pending reminders, and the files JARVIS actually used (the audit log's path arguments, never the whole file
index). Nothing is invented and nothing leaves the PC.

Remembered facts and kept conversations aren't drawn: hundreds of them swamped the view (Krishna, 28 Oct
2026). The Memory section still lists both.

Every link carries a reason the inspector can show ("read_file 3×", "in this project"). Sizes are capped so
a long history can't swamp the view. Labels go through privacy.redact (emails, long numbers); the page
shows them as text, never HTML.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import catalog
from core.privacy import redact

log = logging.getLogger(__name__)

TYPES = {  # type -> the hub every node of that type hangs from (and its label; not a tool category's name)
    "tool": (None, None),            # tools hang from their category's hub (cat:<category>)
    "agent": ("hub:agents", "Active agents"),
    "project": ("hub:projects", "Projects"),
    "reminder": ("hub:reminder", "Reminders"),
    "file": ("hub:file", "Files used"),
}
MAX_FILES = 150
MAX_PROJECTS = 20
MAX_AUDIT_LINES = 8000          # newest lines of logs/actions.jsonl read for tool calls and files
DAYS = 30                       # tool calls older than this aren't counted
FILE_TOOLS = {"read_file", "write_file", "edit_file", "delete_file", "open_file"}


def _label(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    text = redact(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _local(ts: str) -> datetime | None:
    """The audit log's UTC timestamp as local time without a zone."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


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


def _slug(name: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())


def _under(path: str, folder: str) -> bool:
    p = str(path).replace("\\", "/").lower().rstrip("/")
    f = str(folder).replace("\\", "/").lower().rstrip("/")
    return bool(f) and (p == f or p.startswith(f + "/"))


def build(*, registry=None, hub_status: dict | None = None, working: list[dict] | None = None,
          projects: list[dict] | None = None, reminders: list[dict] | None = None, audit_path: Path | None = None,
          now: datetime | None = None) -> dict:
    """The whole graph: {"nodes", "links", "counts", "generated", "capped"}. Every source is optional.

    working: the Agent Hub requests still out ({id, text, minutes}); projects: {name, folder, agents, tools}."""
    now = now or datetime.now()
    since = now - timedelta(days=DAYS)
    g = Graph()
    g.node("jarvis", "core", "JARVIS", hub=True)
    capped: list[str] = []
    calls = read_audit(audit_path, since)

    # ---- tools, by category, with how often each was called lately
    names = set()
    used = Counter(c["tool"] for c in calls)
    last: dict[str, datetime] = {}
    for c in calls:
        last[c["tool"]] = max(c["at"], last.get(c["tool"], c["at"]))
    if registry is not None:
        for t in registry.visible():
            names.add(t.name)
            cat = catalog.CATALOG.get(t.name, ("core", []))[0] if t.name not in catalog.EXCLUDED else "core"
            cat_id = f"cat:{cat}"
            g.node(cat_id, "tool", cat.capitalize() if cat != "core" else "Core", hub=True,
                   description=catalog.CATEGORIES.get(cat, "JARVIS's own controls: plans, tasks, the Agent Hub."))
            g.link("jarvis", cat_id, f"{cat} tools", "structure")
            nid = g.node(f"tool:{t.name}", "tool", t.name, description=_label(t.description, 220),
                         read_only=bool(t.read_only), calls=used.get(t.name, 0),
                         last_used=last[t.name].isoformat(timespec="minutes") if t.name in last else "")
            g.link(nid, cat_id, f"a {cat} tool", "structure")

    # ---- the Agent Hub's agents and actions, and the requests they're working on now
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
    for w in working or []:
        g.hub("agent")
        minutes = int(w.get("minutes") or 0)
        nid = g.node(f"work:{w.get('id')}", "agent", _label(w.get("text"), 90), minutes=minutes, working=True)
        g.link(nid, "hub:agents", f"working on it now ({minutes} min)" if minutes else "working on it now", "uses")
    if (agents or working) and "agent_hub_request" in names:
        g.link("tool:agent_hub_request", "hub:agents", "hands requests to the Agent Hub", "uses")

    # ---- the projects JARVIS looks after
    proj_ids: list[tuple[str, str]] = []   # (node id, folder)
    for p in (projects or [])[:MAX_PROJECTS]:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        g.hub("project")
        folder = str(p.get("folder") or "").strip()
        pid = g.node(f"project:{_slug(name)}", "project", _label(name, 60), hub=True, folder=_label(folder, 160))
        g.link(pid, "hub:projects", "a project JARVIS looks after", "structure")
        proj_ids.append((pid, folder))
        for agent in p.get("agents") or []:
            g.link(pid, f"agent:{agent}", f"{agent} works on {name}", "uses")
        for cat in p.get("tools") or []:
            g.link(pid, f"cat:{cat}", f"the {cat} tools work on {name}", "uses")
    if len(projects or []) > MAX_PROJECTS:
        capped.append("projects")

    # ---- pending reminders
    for r in reminders or []:
        g.hub("reminder")
        due = r.get("due")
        nid = g.node(f"reminder:{r.get('id')}", "reminder", _label(r.get("text"), 90),
                     due=due.isoformat(timespec="minutes") if isinstance(due, datetime) else str(due or ""))
        g.link(nid, "hub:reminder", "pending reminder", "structure")

    # ---- files JARVIS used (the audit log), and the project each one is in
    file_uses: dict[str, Counter] = defaultdict(Counter)
    file_last: dict[str, datetime] = {}
    file_full: dict[str, str] = {}
    for call in calls:
        if call["tool"] in FILE_TOOLS and call["path"]:
            p = _short_path(call["path"])
            file_uses[p][call["tool"]] += 1
            file_last[p] = max(call["at"], file_last.get(p, call["at"]))
            file_full[p] = call["path"]
    recent_files = sorted(file_last, key=file_last.get, reverse=True)
    if len(recent_files) > MAX_FILES:
        recent_files = recent_files[:MAX_FILES]
        capped.append("files")
    in_project: Counter = Counter()
    for p in recent_files:
        g.hub("file")
        nid = g.node(f"file:{p}", "file", _label(p.rsplit("/", 1)[-1], 80), path=_label(p, 160),
                     last_used=file_last[p].isoformat(timespec="minutes"))
        g.link(nid, "hub:file", "a file JARVIS used", "structure")
        for tool, n in file_uses[p].items():
            g.link(nid, f"tool:{tool}", f"{tool} {n}×", "uses")
        for pid, folder in proj_ids:
            if _under(file_full[p], folder):
                g.link(nid, pid, "in this project", "uses")
                in_project[pid] += 1
    for pid, _ in proj_ids:
        g.nodes[pid]["detail"]["files"] = in_project.get(pid, 0)

    # agents and projects are hubs for what hangs off them but still things shown; the other hubs only group
    counts = Counter(n["type"] for n in g.nodes.values()
                     if not n["hub"] or n["id"].startswith(("agent:", "project:")))
    return {"nodes": list(g.nodes.values()), "links": list(g.links.values()), "counts": dict(counts),
            "generated": now.isoformat(timespec="seconds"), "capped": capped}
