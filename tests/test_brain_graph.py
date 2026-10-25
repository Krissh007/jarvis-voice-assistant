"""Brain view phase 1: the graph of what JARVIS knows and did (core/brain_graph.py) and the bridge's brain request."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from core import brain_graph
from core.memory_db import MemoryDB
from tests.test_web_ui import assistant  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 26, 12, 0, 0)


def vec(*xs) -> bytes:
    v = np.array(xs + (0.0,) * (4 - len(xs)), dtype=np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32).tobytes()


def utc(local: datetime) -> str:
    """How the audit log writes a time: UTC with its zone."""
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


class Registry:
    def __init__(self, *names):
        self.tools = [SimpleNamespace(name=n, description=f"The {n} tool.", read_only=n.startswith("read"))
                      for n in names]

    def visible(self):
        return self.tools


def make_db(tmp_path):
    db = MemoryDB(tmp_path / "memory.sqlite")
    return db, SimpleNamespace(db=db)


def add_fact(db, text, embedding=None, uses=0):
    with db.connect() as c:
        return c.execute("INSERT INTO facts (text, added, embedding, uses) VALUES (?, ?, ?, ?)",
                         (text, NOW.isoformat(), embedding, uses)).lastrowid


def add_conversation(db, started, lines, ended=None):
    """lines: (role, text, embedding or None)."""
    with db.connect() as c:
        cid = c.execute("INSERT INTO conversations (started, ended, channels) VALUES (?, ?, 'voice')",
                        (started.isoformat(timespec="seconds"),
                         ended.isoformat(timespec="seconds") if ended else None)).lastrowid
        for i, (role, text, emb) in enumerate(lines):
            c.execute("INSERT INTO messages (conversation, time, role, text, embedding) VALUES (?, ?, ?, ?, ?)",
                      (cid, (started + timedelta(seconds=i)).isoformat(timespec="seconds"), role, text, emb))
    return cid


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
    db, store = make_db(tmp_path)
    fid = add_fact(db, "Krishna's car is a blue Honda", uses=3)
    cid = add_conversation(db, NOW - timedelta(hours=2), [("user", "When is the car service due?", None),
                                                          ("assistant", "Next Tuesday.", None)],
                           ended=NOW - timedelta(hours=1, minutes=50))
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, {"timestamp": utc(NOW - timedelta(hours=1, minutes=55)), "tool": "read_file",
                        "args": {"path": r"C:\Users\krishna\Documents\car.txt"}})
    hub = {"agents": [{"name": "jobpilot", "description": "Job search", "reachable": True, "optional": False,
                       "action_list": [{"name": "run_agent", "description": "Run it", "side_effect": True}]}]}
    reminders = [{"id": "r1", "text": "Book the service", "due": NOW + timedelta(days=1)}]
    g = brain_graph.build(store=store, conversations=store, registry=Registry("read_file", "agent_hub_request"),
                          hub_status=hub, reminders=reminders, audit_path=audit, now=NOW)
    nodes = by_id(g)
    assert nodes["jarvis"]["hub"] and nodes[f"fact:{fid}"]["detail"]["uses"] == 3
    assert nodes[f"conv:{cid}"]["label"] == "When is the car service due?" and nodes[f"conv:{cid}"]["detail"]["lines"] == 2
    assert link(g, f"fact:{fid}", "hub:memory") and link(g, f"conv:{cid}", "hub:conversation")
    assert link(g, "tool:read_file", "cat:files")["reason"] == "a files tool" and nodes["cat:files"]["hub"]
    assert link(g, "tool:agent_hub_request", "cat:core")                 # JARVIS's own controls
    assert link(g, "action:jobpilot.run_agent", "agent:jobpilot") and nodes["action:jobpilot.run_agent"]["detail"]["side_effect"]
    assert link(g, "tool:agent_hub_request", "hub:agents")["reason"] == "hands requests to the Agent Hub"
    assert nodes["reminder:r1"]["detail"]["due"] == "2026-10-27T12:00"
    assert "file:Documents/car.txt" in nodes and nodes["file:Documents/car.txt"]["label"] == "car.txt"
    assert link(g, "file:Documents/car.txt", "tool:read_file")["reason"] == "read_file 1×"
    assert g["counts"] == {"memory": 1, "conversation": 1, "tool": 2, "agent": 2, "reminder": 1, "file": 1}
    assert g["generated"] == "2026-10-26T12:00:00" and g["capped"] == []
    json.dumps(g)                                                         # the page gets it as JSON


def test_no_sources_is_just_jarvis() -> None:
    g = brain_graph.build(now=NOW)
    assert [n["id"] for n in g["nodes"]] == ["jarvis"] and g["links"] == [] and g["counts"] == {}


def test_tool_calls_land_in_the_conversation_they_happened_in(tmp_path) -> None:
    """The audit log's UTC times are matched to conversations kept in local time."""
    db, store = make_db(tmp_path)
    first = add_conversation(db, NOW - timedelta(hours=3), [("user", "Search the news", None)],
                             ended=NOW - timedelta(hours=3) + timedelta(minutes=5))
    second = add_conversation(db, NOW - timedelta(minutes=30), [("user", "Search again", None)])  # still open
    audit = tmp_path / "actions.jsonl"
    in_first = NOW - timedelta(hours=3) + timedelta(minutes=2)
    write_audit(audit,
                {"timestamp": utc(in_first), "tool": "search_web"},
                {"timestamp": utc(in_first + timedelta(seconds=10)), "tool": "search_web"},
                {"timestamp": utc(NOW - timedelta(hours=1)), "tool": "search_web"},          # between the two
                {"timestamp": utc(NOW - timedelta(minutes=10)), "tool": "search_web"},
                {"timestamp": utc(NOW - timedelta(minutes=9)), "tool": "search_web", "confirmation": "cancelled"},
                {"timestamp": utc(NOW - timedelta(minutes=8)), "tool": "search_web", "dry_run": True},
                {"timestamp": utc(NOW - timedelta(minutes=7)), "tool": "not_a_tool_here"})
    with audit.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    g = brain_graph.build(conversations=store, registry=Registry("search_web"), audit_path=audit, now=NOW)
    assert link(g, f"conv:{first}", "tool:search_web")["reason"] == "called 2 times in this conversation"
    assert link(g, f"conv:{second}", "tool:search_web")["reason"] == "called 1 time in this conversation"
    assert "tool:not_a_tool_here" not in by_id(g)


def test_old_conversations_and_calls_are_left_out(tmp_path) -> None:
    db, store = make_db(tmp_path)
    old = add_conversation(db, NOW - timedelta(days=brain_graph.DAYS + 1), [("user", "Long ago", None)])
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, {"timestamp": utc(NOW - timedelta(days=40)), "tool": "read_file", "args": {"path": "E:/old.txt"}})
    g = brain_graph.build(conversations=store, registry=Registry("read_file"), audit_path=audit, now=NOW)
    assert f"conv:{old}" not in by_id(g) and not any(n["type"] == "file" for n in g["nodes"])


def test_facts_link_to_conversations_by_their_closest_line(tmp_path) -> None:
    db, store = make_db(tmp_path)
    close = add_fact(db, "Only ask before deleting things", vec(1, 0.2))
    vague = add_fact(db, "Krishna likes tea", vec(0.5, 0, 1))          # about 0.45 to the matching line
    worded = add_fact(db, "The dentist appointment is in March")      # no vector yet: words decide
    generic = add_fact(db, "Talking about change is something", vec(0, 0, 1))  # shares words, far in meaning
    cid = add_conversation(db, NOW - timedelta(hours=1), [
        ("user", "Don't ask me before every little change", vec(1, 0, 0)),
        ("user", "Talking about something else entirely now", vec(0, 1, 0)),
        ("user", "Move my dentist appointment to April", vec(0, 1, 0)),
        ("assistant", "Done, the dentist appointment is moved.", vec(0, 0, 0, 1)),   # JARVIS's lines don't count
    ])
    g = brain_graph.build(store=store, conversations=store, now=NOW)
    meaning = link(g, f"conv:{cid}", f"fact:{close}")
    assert meaning["kind"] == "meaning" and meaning["reason"].startswith("close in meaning to “Don't ask me before")
    assert meaning["reason"].endswith("(0.98)")
    assert link(g, f"conv:{cid}", f"fact:{vague}") is None             # below MEANING_MIN
    assert link(g, f"conv:{cid}", f"fact:{worded}")["reason"] == "shares: appointment, dentist"
    assert link(g, f"conv:{cid}", f"fact:{generic}") is None


def test_one_conversation_links_to_its_closest_facts_only(tmp_path) -> None:
    db, store = make_db(tmp_path)
    facts = [add_fact(db, f"Rule number {i}", vec(1, 0.05 * i)) for i in range(5)]
    cid = add_conversation(db, NOW - timedelta(hours=1), [("user", "the rule", vec(1, 0))])
    g = brain_graph.build(store=store, conversations=store, now=NOW)
    linked = [f for f in facts if link(g, f"conv:{cid}", f"fact:{f}")]
    assert linked == facts[:brain_graph.MAX_FACT_LINKS]


def test_the_threshold_matches_the_measurement() -> None:
    assert brain_graph.MEANING_MIN == 0.55     # measured on real data: see the comment in core/brain_graph.py


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
                {"timestamp": t, "tool": "read_file", "args": {"query": "C:/x.txt"}})
    g = brain_graph.build(registry=Registry("read_file", "write_file", "search_web"), audit_path=audit, now=NOW)
    files = {n["id"]: n for n in g["nodes"] if n["type"] == "file" and not n["hub"]}
    assert set(files) == {"file:Desktop/a.html", "file:E:/notes/b.md"}
    assert link(g, "file:Desktop/a.html", "tool:write_file")["reason"] == "write_file 2×"
    assert "krishna" not in json.dumps(files).lower()                   # the home folder isn't shown


def test_sizes_are_capped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(brain_graph, "MAX_FILES", 2)
    monkeypatch.setattr(brain_graph, "MAX_CONVERSATIONS", 2)
    db, store = make_db(tmp_path)
    for i in range(4):
        add_conversation(db, NOW - timedelta(hours=i + 1), [("user", f"chat {i}", None)])
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, *({"timestamp": utc(NOW - timedelta(minutes=10 - i)), "tool": "read_file",
                          "args": {"path": f"E:/f{i}.txt"}} for i in range(4)))
    g = brain_graph.build(conversations=store, registry=Registry("read_file"), audit_path=audit, now=NOW)
    assert g["counts"]["conversation"] == 2 and g["counts"]["file"] == 2
    assert {n["label"] for n in g["nodes"] if n["type"] == "file" and not n["hub"]} == {"f2.txt", "f3.txt"}  # newest
    assert sorted(g["capped"]) == ["conversations", "files"]


def test_labels_are_redacted_and_cut(tmp_path) -> None:
    db, store = make_db(tmp_path)
    fid = add_fact(db, "Krishna's email is krishna@example.com and phone 9876543210")
    cid = add_conversation(db, NOW - timedelta(hours=1), [("user", "word " * 100, None)])
    g = brain_graph.build(store=store, conversations=store, now=NOW)
    nodes = by_id(g)
    assert "example.com" not in nodes[f"fact:{fid}"]["label"] and "[email]" in nodes[f"fact:{fid}"]["label"]
    assert "9876543210" not in nodes[f"fact:{fid}"]["label"]
    assert len(nodes[f"conv:{cid}"]["label"]) <= 90 and nodes[f"conv:{cid}"]["label"].endswith("…")


def test_reminders_link_to_the_conversation_that_set_them(tmp_path) -> None:
    db, store = make_db(tmp_path)
    cid = add_conversation(db, NOW - timedelta(minutes=20), [("user", "Remind me to call mum", None)])
    audit = tmp_path / "actions.jsonl"
    write_audit(audit, {"timestamp": utc(NOW - timedelta(minutes=19)), "tool": "set_reminder",
                        "args": {"text": "Call  mum"}})
    reminders = [{"id": "r9", "text": "call mum", "due": NOW + timedelta(hours=1)}]
    g = brain_graph.build(conversations=store, registry=Registry("set_reminder"), reminders=reminders,
                          audit_path=audit, now=NOW)
    assert link(g, "reminder:r9", f"conv:{cid}")["reason"] == "set in this conversation"


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
    a.bus.publish("message", role="user", label="Krishna", text="hi")
    a.bus.publish("partial", text="h")                                  # doesn't change the graph
    await asyncio.sleep(0.15)
    assert [e.data["why"] for e in seen] == [["message", "tool_finished"]]
    bridge.forget_fact(12345)                                           # a page action without a bus event
    await asyncio.sleep(0.15)
    assert [e.data["why"] for e in seen][-1] == ["memory"] and len(seen) == 2


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
