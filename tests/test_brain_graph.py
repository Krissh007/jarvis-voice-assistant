"""The Brain view's graph of what JARVIS can do and is doing (core/brain_graph.py), and the bridge's brain request."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core import brain_graph
from core.memory_db import MemoryDB
from tests.test_web_ui import assistant  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 26, 12, 0, 0)


def utc(local: datetime) -> str:
    """How the audit log writes a time: UTC with its zone."""
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


class Registry:
    def __init__(self, *names):
        self.tools = [SimpleNamespace(name=n, description=f"The {n} tool.", read_only=n.startswith("read"))
                      for n in names]

    def visible(self):
        return self.tools


def write_audit(path, *records):
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({"tier": "read", "confirmation": "not_needed", "dry_run": False, "args": {}, **r}) + "\n")


def by_id(graph):
    return {n["id"]: n for n in graph["nodes"]}


def link(graph, a, b):
    for item in graph["links"]:
        if {item["source"], item["target"]} == {a, b}:
            return item
    return None


def test_every_source_becomes_nodes_under_its_hub(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", r"C:\Users\krishna")
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, {"timestamp": utc(NOW - timedelta(hours=1, minutes=55)), "tool": "read_file",
                        "args": {"path": r"C:\Users\krishna\Documents\car.txt"}})
    hub = {"agents": [{"name": "jobpilot", "description": "Job search", "reachable": True, "optional": False,
                       "action_list": [{"name": "run_agent", "description": "Run it", "side_effect": True}]}]}
    working = [{"id": "r7", "text": "Find Wipro jobs", "minutes": 6}]
    projects = [{"name": "JobPilot", "folder": "E:/JobPilot", "agents": ["jobpilot"], "tools": ["jobs"]}]
    reminders = [{"id": "r1", "text": "Book the service", "due": NOW + timedelta(days=1)}]
    g = brain_graph.build(registry=Registry("read_file", "agent_hub_request"), hub_status=hub, working=working,
                          projects=projects, reminders=reminders, audit_path=audit, now=NOW)
    nodes = by_id(g)
    assert nodes["jarvis"]["hub"]
    assert link(g, "tool:read_file", "cat:files")["reason"] == "a files tool" and nodes["cat:files"]["hub"]
    assert link(g, "tool:agent_hub_request", "cat:core")                 # JARVIS's own controls
    assert nodes["hub:agents"]["label"] == "Active agents" and nodes["hub:projects"]["label"] == "Projects"
    assert link(g, "action:jobpilot.run_agent", "agent:jobpilot") and nodes["action:jobpilot.run_agent"]["detail"]["side_effect"]
    assert link(g, "tool:agent_hub_request", "hub:agents")["reason"] == "hands requests to the Agent Hub"
    assert link(g, "work:r7", "hub:agents")["reason"] == "working on it now (6 min)"
    assert link(g, "project:jobpilot", "hub:projects") and link(g, "project:jobpilot", "agent:jobpilot")
    assert nodes["reminder:r1"]["detail"]["due"] == "2026-10-27T12:00"
    assert "file:Documents/car.txt" in nodes and nodes["file:Documents/car.txt"]["label"] == "car.txt"
    assert link(g, "file:Documents/car.txt", "tool:read_file")["reason"] == "read_file 1×"
    assert not any(n["type"] in ("memory", "conversation") for n in g["nodes"])   # facts and talks aren't drawn
    assert g["counts"] == {"tool": 2, "agent": 3, "project": 1, "reminder": 1, "file": 1}
    assert g["generated"] == "2026-10-26T12:00:00" and g["capped"] == []
    json.dumps(g)                                                         # the page gets it as JSON


def test_no_sources_is_just_jarvis() -> None:
    g = brain_graph.build(now=NOW)
    assert [n["id"] for n in g["nodes"]] == ["jarvis"] and g["links"] == [] and g["counts"] == {}


def test_tools_say_how_often_they_were_called(tmp_path) -> None:
    audit = tmp_path / "actions.jsonl"
    write_audit(audit,
                {"timestamp": utc(NOW - timedelta(hours=3)), "tool": "search_web"},
                {"timestamp": utc(NOW - timedelta(minutes=10)), "tool": "search_web"},
                {"timestamp": utc(NOW - timedelta(minutes=9)), "tool": "search_web", "confirmation": "cancelled"},
                {"timestamp": utc(NOW - timedelta(minutes=8)), "tool": "search_web", "dry_run": True},
                {"timestamp": utc(NOW - timedelta(days=brain_graph.DAYS + 1)), "tool": "search_web"},
                {"timestamp": utc(NOW - timedelta(minutes=7)), "tool": "not_a_tool_here"})
    with audit.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    g = brain_graph.build(registry=Registry("search_web", "read_file"), audit_path=audit, now=NOW)
    nodes = by_id(g)
    assert nodes["tool:search_web"]["detail"]["calls"] == 2                # cancelled, dry runs and old calls don't count
    assert nodes["tool:search_web"]["detail"]["last_used"] == "2026-10-26T11:50"   # local time, from the UTC log
    assert nodes["tool:read_file"]["detail"]["calls"] == 0 and nodes["tool:read_file"]["detail"]["last_used"] == ""
    assert "tool:not_a_tool_here" not in nodes


def test_running_requests_hang_from_the_active_agents(tmp_path) -> None:
    working = [{"id": "r1", "text": "Score the new jobs", "minutes": 0},
               {"id": "r2", "text": "Mail me at krishna@example.com " + "word " * 40, "minutes": 12}]
    g = brain_graph.build(registry=Registry("agent_hub_request"), working=working, now=NOW)   # Hub unreachable
    nodes = by_id(g)
    assert link(g, "work:r1", "hub:agents")["reason"] == "working on it now"
    assert link(g, "work:r2", "hub:agents")["reason"] == "working on it now (12 min)"
    assert "example.com" not in nodes["work:r2"]["label"] and len(nodes["work:r2"]["label"]) <= 90
    assert link(g, "tool:agent_hub_request", "hub:agents") and g["counts"]["agent"] == 2


def test_projects_gather_their_files_agents_and_tools(tmp_path) -> None:
    audit = tmp_path / "actions.jsonl"
    t = utc(NOW - timedelta(minutes=5))
    write_audit(audit,
                {"timestamp": t, "tool": "read_file", "args": {"path": r"E:\JobPilot\app\main.py"}},
                {"timestamp": t, "tool": "edit_file", "args": {"path": "e:/jobpilot/README.md"}},
                {"timestamp": t, "tool": "read_file", "args": {"path": "E:/JobPilotOld/x.py"}},   # a different folder
                {"timestamp": t, "tool": "read_file", "args": {"path": "E:/JARVIS Agent/jarvis/config.yaml"}})
    projects = [{"name": "JobPilot", "folder": "E:/JobPilot/", "agents": ["jobpilot", "not_an_agent"], "tools": ["jobs"]},
                {"name": "JARVIS", "folder": "E:/JARVIS Agent"},
                {"name": "Vision 2.0"},                                   # no folder: nothing to match
                {"name": "  "}]                                           # no name: left out
    hub = {"agents": [{"name": "jobpilot", "reachable": True}]}
    g = brain_graph.build(registry=Registry("read_file", "edit_file", "job_search_status"), hub_status=hub,
                          projects=projects, audit_path=audit, now=NOW)
    nodes = by_id(g)
    jp = nodes["project:jobpilot"]
    assert jp["hub"] and jp["label"] == "JobPilot" and jp["detail"]["folder"] == "E:/JobPilot/"
    assert jp["detail"]["files"] == 2 and nodes["project:jarvis"]["detail"]["files"] == 1
    assert nodes["project:vision-2-0"]["detail"]["files"] == 0
    assert link(g, "file:E:/JobPilot/app/main.py", "project:jobpilot")["reason"] == "in this project"
    assert link(g, "file:E:/JobPilotOld/x.py", "project:jobpilot") is None
    assert link(g, "project:jobpilot", "agent:jobpilot")["reason"] == "jobpilot works on JobPilot"
    assert link(g, "project:jobpilot", "cat:jobs")["reason"] == "the jobs tools work on JobPilot"
    assert g["counts"]["project"] == 3 and not any(n["id"] == "project:" for n in g["nodes"])


def test_files_come_only_from_file_tools_path_arguments(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", r"C:\Users\krishna")
    audit = tmp_path / "actions.jsonl"
    t = utc(NOW - timedelta(minutes=5))
    write_audit(audit,
                {"timestamp": t, "tool": "write_file", "args": {"path": r"C:\Users\krishna\Desktop\a.html"}},
                {"timestamp": t, "tool": "write_file", "args": {"path": r"C:\Users\krishna\Desktop\a.html"}},
                {"timestamp": t, "tool": "read_file", "args": {"path": "E:/notes/b.md"}},
                {"timestamp": t, "tool": "search_web", "args": {"path": "C:/secret/plans.txt"}},   # not a file tool
                {"timestamp": t, "tool": "read_file", "args": {"path": 42}},
                {"timestamp": t, "tool": "read_file", "args": {"query": "C:/x.txt"}},
                {"timestamp": utc(NOW - timedelta(days=40)), "tool": "read_file", "args": {"path": "E:/old.txt"}})
    g = brain_graph.build(registry=Registry("read_file", "write_file", "search_web"), audit_path=audit, now=NOW)
    files = {n["id"]: n for n in g["nodes"] if n["type"] == "file" and not n["hub"]}
    assert set(files) == {"file:Desktop/a.html", "file:E:/notes/b.md"}
    assert link(g, "file:Desktop/a.html", "tool:write_file")["reason"] == "write_file 2×"
    assert "krishna" not in json.dumps(files).lower()                   # the home folder isn't shown


def test_sizes_are_capped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(brain_graph, "MAX_FILES", 2)
    monkeypatch.setattr(brain_graph, "MAX_PROJECTS", 2)
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, *({"timestamp": utc(NOW - timedelta(minutes=10 - i)), "tool": "read_file",
                          "args": {"path": f"E:/f{i}.txt"}} for i in range(4)))
    projects = [{"name": f"P{i}", "folder": f"E:/p{i}"} for i in range(3)]
    g = brain_graph.build(registry=Registry("read_file"), projects=projects, audit_path=audit, now=NOW)
    assert g["counts"]["project"] == 2 and g["counts"]["file"] == 2
    assert {n["label"] for n in g["nodes"] if n["type"] == "file" and not n["hub"]} == {"f2.txt", "f3.txt"}  # newest
    assert sorted(g["capped"]) == ["files", "projects"]


def test_labels_are_redacted_and_cut(tmp_path) -> None:
    reminders = [{"id": "r1", "text": "Email krishna@example.com and call 9876543210", "due": NOW},
                 {"id": "r2", "text": "word " * 100, "due": NOW}]
    g = brain_graph.build(reminders=reminders, now=NOW)
    nodes = by_id(g)
    assert "example.com" not in nodes["reminder:r1"]["label"] and "[email]" in nodes["reminder:r1"]["label"]
    assert "9876543210" not in nodes["reminder:r1"]["label"]
    assert len(nodes["reminder:r2"]["label"]) <= 90 and nodes["reminder:r2"]["label"].endswith("…")


# ---- the bridge: the app's brain request and brain_update events


class StatusHub:
    def __init__(self):
        self.calls = 0

    async def agent_status(self):
        self.calls += 1
        return {"agents": [{"name": "jobpilot", "description": "Jobs", "reachable": True,
                            "action_list": [{"name": "get_jobs", "description": "List jobs", "side_effect": False}]}]}


async def test_brain_request_builds_the_graph_from_the_running_assistant(assistant) -> None:  # noqa: F811
    from core.runtime import WebBridge

    a = assistant
    a.config["features"]["agent_hub"] = True
    a.services.hub = hub = StatusHub()
    bridge = WebBridge(a)
    g = await bridge.brain()
    nodes = by_id(g)
    assert g["counts"]["tool"] == len(a.registry.visible())
    assert "action:jobpilot.get_jobs" in nodes and hub.calls == 1
    await bridge.brain()
    assert hub.calls == 1                       # the agent list is kept for a while, not asked for every time
    await bridge.status()
    assert hub.calls == 2                       # the command centre's check refreshes it


async def test_brain_updates_are_debounced(assistant, monkeypatch) -> None:  # noqa: F811
    from core import runtime

    monkeypatch.setattr(runtime, "BRAIN_DEBOUNCE_S", 0.05)
    a = assistant
    a.bus.attach(asyncio.get_running_loop())
    bridge = a.web.bridge                                              # the running one (one per assistant)
    seen = []
    a.bus.subscribe(lambda e: seen.append(e) if e.kind == "brain_update" else None)
    a.bus.publish("tool_finished", tool="search_web", step=1, paused=False, ok=True)
    a.bus.publish("agent_update", request_id="r1", status="done", say="Done.")
    a.bus.publish("message", role="user", label="Krishna", text="hi")   # facts and talks aren't drawn any more
    a.bus.publish("partial", text="h")                                  # doesn't change the graph
    await asyncio.sleep(0.15)
    assert [e.data["why"] for e in seen] == [["agent_update", "tool_finished"]]
    bridge.forget_fact(12345)                                           # nor does the Memory page
    await asyncio.sleep(0.15)
    assert len(seen) == 1


def test_brain_request_and_updates_reach_the_page(assistant) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from core.runtime import WebBridge
    from tests.test_web_ui import PORT, TOKEN, connect
    from ui.server import STREAM_KINDS, create_app

    assert "brain_update" in STREAM_KINDS
    bridge = WebBridge(assistant)

    async def brain():
        return {"nodes": [{"id": "jarvis"}], "links": []}

    bridge.brain = brain
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())       # hello
        ws.send_text(json.dumps({"type": "request", "what": "brain", "id": 7}))
        while True:
            msg = json.loads(ws.receive_text())
            if msg["kind"] == "reply":
                break
    assert msg["id"] == 7 and msg["what"] == "brain" and msg["data"]["nodes"] == [{"id": "jarvis"}]


def test_the_brain_layout_is_kept_checked_and_merged(assistant, tmp_path) -> None:  # noqa: F811
    from core import runtime
    from core.runtime import WebBridge

    bridge = WebBridge(assistant, look_path=tmp_path / "orb_look.json")
    assert bridge.layout_path == tmp_path / "brain_layout.json" and bridge._brain_layout() == {}
    assert bridge.save_brain_layout({"jarvis": [1.234, -5], "fact:1": [3, 4], "bad": [1], "nan": [float("nan"), 1],
                                     "big": [1e9, 0], "bool": [True, 2], "": [1, 2], 7: [1, 2], "x" * 300: [1, 2]})
    assert bridge._brain_layout() == {"jarvis": [1.2, -5.0], "fact:1": [3.0, 4.0]}
    assert bridge.save_brain_layout({"conv:2": [9, 9], "fact:1": [0, 0]}, fresh=False)   # the next chunk adds
    assert bridge._brain_layout() == {"jarvis": [1.2, -5.0], "fact:1": [0.0, 0.0], "conv:2": [9.0, 9.0]}
    assert bridge.save_brain_layout({"tool:x": [1, 1]})                                   # a new layout replaces
    assert bridge._brain_layout() == {"tool:x": [1.0, 1.0]}
    assert bridge.save_brain_layout("not a dict") is False

    many = {f"n{i}": [i, i] for i in range(runtime.BRAIN_LAYOUT_MAX + 50)}
    bridge.save_brain_layout(many)
    assert len(bridge._brain_layout()) == runtime.BRAIN_LAYOUT_MAX
    bridge.layout_path.write_text("[1, 2]", encoding="utf-8")                             # not ours: ignored
    assert bridge._brain_layout() == {}


async def test_the_brain_request_carries_the_kept_layout(assistant, tmp_path) -> None:  # noqa: F811
    from core.runtime import WebBridge

    bridge = WebBridge(assistant, look_path=tmp_path / "orb_look.json")
    bridge.save_brain_layout({"jarvis": [10, 20]})
    assert (await bridge.brain())["layout"] == {"jarvis": [10.0, 20.0]}


def test_layout_messages_reach_the_bridge(assistant) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from core.runtime import WebBridge
    from tests.test_web_ui import PORT, TOKEN, connect
    from ui.server import create_app

    bridge = WebBridge(assistant)
    got = []
    bridge.save_brain_layout = lambda positions, fresh=True: got.append((positions, fresh)) or True
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())       # hello
        ws.send_text(json.dumps({"type": "brain_layout", "fresh": True, "positions": {"jarvis": [1, 2]}}))
        ws.send_text(json.dumps({"type": "brain_layout", "fresh": False, "positions": {"fact:1": [3, 4]}}))
        ws.send_text(json.dumps({"type": "brain_layout", "positions": "nope"}))         # dropped
        ws.send_text(json.dumps({"type": "request", "what": "facts", "id": 1}))         # after the others
        while json.loads(ws.receive_text())["kind"] != "reply":
            pass
    assert got == [({"jarvis": [1, 2]}, True), ({"fact:1": [3, 4]}, False)]


# ---- phase 3: the HUD's facts and the Memory switch


def test_the_memory_switch_pauses_and_resumes_the_conversation_log(tmp_path) -> None:
    from core.bus import EventBus
    from core.conversation_log import ConversationLog

    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e.data) if e.kind == "memory_keep" else None)
    log = ConversationLog(MemoryDB(tmp_path / "m.sqlite"))
    log.attach(bus)
    log.pause()
    assert log.paused and seen[-1] == {"on": False, "enabled": True}
    assert log.add("user", "this isn't kept") is False
    log.resume()
    assert not log.paused and seen[-1] == {"on": True, "enabled": True}
    assert log.add("user", "this is kept") is True
    log.pause()
    log.new_conversation()                       # a new chat keeps again, and says so
    assert seen[-1] == {"on": True, "enabled": True} and len(seen) == 4
    log.new_conversation()                       # nothing changed: nothing said
    assert len(seen) == 4


def test_the_hud_facts_and_the_memory_control(assistant, tmp_path) -> None:  # noqa: F811
    from core.conversation_log import ConversationLog
    from core.runtime import WebBridge

    a = assistant
    a.ui = lambda label, fn, *args: fn(*args)
    a.services.conversations = log = ConversationLog(MemoryDB(tmp_path / "m.sqlite"))
    bridge = WebBridge(a, look_path=tmp_path / "orb_look.json")
    snap = bridge.snapshot()
    assert snap["keep"] == {"enabled": True, "on": True}
    assert snap["voice_model"] == a.config["models"]["live"]
    assert snap["language"] == (a.config["audio"].get("transcription_languages") or ["auto"])[0]
    assert set(snap["hub"]) == {"enabled", "reachable"}
    assert bridge.control("keep_memory") and log.paused and bridge.snapshot()["keep"]["on"] is False
    assert bridge.control("keep_memory") and not log.paused
    log.enabled = False
    assert bridge.control("keep_memory") and not log.paused    # conversations aren't kept: nothing to switch
    a.services.conversations = None
    assert bridge.snapshot()["keep"] == {"enabled": False, "on": False}
    bridge.control("keep_memory")                               # no log: does nothing, doesn't fail


def test_the_app_opens_on_the_brain() -> None:
    from core.runtime import Assistant
    from ui.server import ACTIONS, STREAM_KINDS

    assert Assistant.APP_SECTIONS[0] == "brain"
    assert {"keep_memory", "open_brain"} <= ACTIONS and "memory_keep" in STREAM_KINDS


# ---- phase 4: Show in folder


async def test_show_file_opens_only_files_from_the_audit_log(assistant, tmp_path, monkeypatch) -> None:  # noqa: F811
    from actions import files
    from core.runtime import WebBridge

    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    used = tmp_path / "home" / "Documents" / "notes.md"
    used.parent.mkdir(parents=True)
    used.write_text("x", encoding="utf-8")
    gone = tmp_path / "home" / "Documents" / "old.md"                   # the folder is there, the file isn't
    t = utc(datetime.now() - timedelta(minutes=5))
    write_audit(assistant.options.log_dir / "actions.jsonl",
                {"timestamp": t, "tool": "read_file", "args": {"path": str(used)}},
                {"timestamp": t, "tool": "write_file", "args": {"path": str(gone)}},
                {"timestamp": t, "tool": "search_web", "args": {"path": str(tmp_path / "secret.txt")}})
    revealed = []
    monkeypatch.setattr(files, "_reveal", revealed.append)
    bridge = WebBridge(assistant, look_path=tmp_path / "orb_look.json")
    ok = await bridge.show_file("file:Documents/notes.md")
    assert ok["ok"] and revealed == [used]
    moved = await bridge.show_file("file:Documents/old.md")
    assert moved["ok"] and "folder" in moved["message"] and revealed[-1] == gone.parent
    for bad in ("file:secret.txt", f"file:{tmp_path / 'secret.txt'}", "fact:1", "C:/Windows", 7, None):
        refused = await bridge.show_file(bad)
        assert refused["ok"] is False, bad
    assert len(revealed) == 2


def test_show_file_requests_get_a_reply(assistant) -> None:  # noqa: F811
    from fastapi.testclient import TestClient

    from core.runtime import WebBridge
    from tests.test_web_ui import PORT, TOKEN, connect
    from ui.server import create_app

    bridge = WebBridge(assistant)

    async def show_file(node_id):
        return {"ok": True, "message": f"shown {node_id}"}

    bridge.show_file = show_file
    client = TestClient(create_app(bridge, TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    with connect(client) as ws:
        json.loads(ws.receive_text())       # hello
        ws.send_text(json.dumps({"type": "show_file", "node": "file:a.txt", "id": 4}))
        while (msg := json.loads(ws.receive_text()))["kind"] != "reply":
            pass
    assert msg["id"] == 4 and msg["data"]["message"] == "shown file:a.txt"
