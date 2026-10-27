"""Brain view phase 2: the graph view in a real (headless) browser, against JARVIS's web server with a fake
assistant (tests/test_app_browser.py). Skipped when Playwright's Chromium isn't installed."""

from __future__ import annotations

import json

from tests.test_app_browser import page, served, until  # noqa: F401  (fixtures)


def node(nid, type_, label, hub=False, **detail):
    return {"id": nid, "type": type_, "label": label, "hub": hub, "detail": detail}


def link(a, b, reason, kind="structure"):
    return {"source": a, "target": b, "reason": reason, "kind": kind}


GRAPH = {
    "nodes": [
        node("jarvis", "core", "JARVIS", True),
        node("cat:search", "tool", "Search", True, description="Web search"),
        node("tool:search_web", "tool", "search_web", description="Search the web.", read_only=True, calls=2,
             last_used="2026-10-25T09:01"),
        node("hub:agents", "agent", "Active agents", True),
        node("agent:jobpilot", "agent", "jobpilot", True, description="Jobs", reachable=True),
        node("work:r1", "agent", "Find <b>Wipro</b> jobs", minutes=6, working=True),
        node("hub:projects", "project", "Projects", True),
        node("project:jobpilot", "project", "JobPilot", True, folder="E:/JobPilot", files=1),
        node("hub:file", "file", "Files used", True),
        node("file:Documents/notes.md", "file", "notes.md", path="Documents/notes.md", last_used="2026-10-25T09:01"),
    ],
    "links": [
        link("jarvis", "cat:search", "search tools"),
        link("tool:search_web", "cat:search", "a search tool"),
        link("jarvis", "hub:agents", "part of JARVIS: active agents"),
        link("agent:jobpilot", "hub:agents", "an agent of the Agent Hub"),
        link("work:r1", "hub:agents", "working on it now (6 min)", "uses"),
        link("jarvis", "hub:projects", "part of JARVIS: projects"),
        link("project:jobpilot", "hub:projects", "a project JARVIS looks after"),
        link("project:jobpilot", "agent:jobpilot", "jobpilot works on JobPilot", "uses"),
        link("jarvis", "hub:file", "part of JARVIS: files used"),
        link("file:Documents/notes.md", "hub:file", "a file JARVIS used"),
        link("file:Documents/notes.md", "project:jobpilot", "in this project", "uses"),
    ],
    "counts": {"tool": 1, "agent": 2, "project": 1, "file": 1},
    "capped": [],
    "layout": {},
}


async def screen_xy(page, node_id):
    return await page.evaluate("""(id) => {
        const cv = document.querySelector('.brain-canvas'); const v = cv.brainView;
        const n = v.nodes.find((x) => x.id === id); const r = cv.getBoundingClientRect();
        const [x, y] = v.toScreen(n); return [r.left + x, r.top + y];
    }""", node_id)


async def settle(page):
    await page.evaluate("() => { const v = document.querySelector('.brain-canvas').brainView; v.sim.stop(); v.fit(false); }")


async def test_the_brain_draws_jarvis_data_and_inspects_a_node(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_role("button", name="Brain").wait_for()
    await page.get_by_text("10 nodes · 11 links").wait_for()
    legend = page.get_by_label("Legend: click a type to hide or show it")
    shown = (await legend.inner_text()).replace("\n", "").replace(" ", "")
    assert "Projects1" in shown and "Agents2" in shown
    assert "Memory" not in shown and "Conversations" not in shown       # facts and talks aren't drawn any more

    await page.get_by_label("Search the brain").fill("jobpilot")
    results = page.get_by_label("Search results")
    assert (await results.get_by_role("button").count()) == 2          # the project and the agent
    await results.get_by_role("button", name="JobPilot Project").click()
    inspector = page.get_by_role("region", name="Inspector")
    await inspector.get_by_text("Project", exact=True).wait_for()
    await inspector.get_by_text("jobpilot works on JobPilot").wait_for()
    text = await inspector.inner_text()
    assert "E:/JobPilot" in text and "Files used" in text

    await inspector.get_by_role("button", name="notes.md").click()      # a neighbour
    await inspector.get_by_text("in this project").wait_for()
    await focus(page, "search_web")
    assert "2 times in 30 days" in await inspector.inner_text()
    await page.get_by_label("Clear the focus").click()
    await page.get_by_text("Click a node to see what it is").wait_for()


async def test_labels_are_text_not_html(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_label("Search the brain").fill("wipro")
    await page.get_by_label("Search the brain").press("Enter")
    inspector = page.get_by_role("region", name="Inspector")
    await inspector.get_by_text("Find <b>Wipro</b> jobs").wait_for()
    assert await page.locator(".brain b").count() == 0
    text = (await inspector.inner_text()).lower()
    assert "running request" in text and "6 min" in text


async def test_the_legend_hides_a_type_and_shift_click_traces_a_path(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    await settle(page)
    x, y = await screen_xy(page, "work:r1")
    await page.mouse.click(x, y)
    await page.get_by_role("region", name="Inspector").get_by_text("Find <b>Wipro</b> jobs").wait_for()
    x, y = await screen_xy(page, "file:Documents/notes.md")
    await page.keyboard.down("Shift")
    await page.mouse.click(x, y)
    await page.keyboard.up("Shift")
    trail = page.get_by_role("region", name="Path")
    await trail.wait_for()
    text = await trail.inner_text()
    # request -> Active agents -> jobpilot -> JobPilot -> notes.md: the path avoids JARVIS when there's another way
    assert "jobpilot works on JobPilot" in text and "in this project" in text and "part of JARVIS" not in text
    assert text.index("Wipro") < text.index("notes.md")
    await page.get_by_label("Clear the path").click()

    await page.get_by_role("button", name="Hide tools").click()
    assert await page.get_by_role("button", name="Show tools").get_attribute("aria-pressed") == "false"
    await page.get_by_label("Search the brain").fill("search_web")
    await page.get_by_text("Nothing called that.").wait_for()           # hidden types aren't found either
    hubs = await page.get_by_role("region", name="Top hubs").inner_text()
    assert "Search" not in hubs and "Active agents" in hubs


async def test_the_brain_refreshes_on_brain_update_and_keeps_its_layout(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    assert await until(lambda: bridge.layouts, timeout=15)              # kept once the layout settles
    positions, fresh = bridge.layouts[0]
    assert fresh and set(positions) == {n["id"] for n in GRAPH["nodes"]}
    assert all(len(xy) == 2 for xy in positions.values())
    before = await screen_xy(page, "agent:jobpilot")

    grown = json.loads(json.dumps(GRAPH))
    grown["nodes"].append(node("work:r2", "agent", "Score the new jobs", minutes=0, working=True))
    grown["links"].append(link("work:r2", "hub:agents", "working on it now", "uses"))
    grown["counts"]["agent"] = 3
    bridge.brain_graph = grown
    seen = bridge.brain_requests
    bridge.bus.publish("brain_update", why=["agent_update"])
    await page.get_by_text("11 nodes · 12 links").wait_for()
    assert bridge.brain_requests == seen + 1
    after = await screen_xy(page, "agent:jobpilot")
    assert abs(after[0] - before[0]) < 60 and abs(after[1] - before[1]) < 60   # the brain didn't reshuffle


async def test_the_model_helpers(served, page) -> None:  # noqa: F811
    """brain-model.js on its own: kept positions, chunked layout messages, the path, search."""
    bridge, url = served
    await page.goto(url + "&s=chat")
    result = await page.evaluate("""async (graph) => {
        const m = await import('/static/app/sections/brain-model.js');
        const g = m.prepare(graph, new Map(), { "agent:jobpilot": [10, 20] });
        const agent = g.byId.get("agent:jobpilot"), work = g.byId.get("work:r1");
        const many = Array.from({ length: 900 }, (_, i) => ({ id: "file:" + "x".repeat(60) + i, x: i * 1.234, y: -i }));
        const chunks = m.layoutChunks(many);
        const again = m.prepare(graph, new Map([["agent:jobpilot", { x: 5, y: 6 }]]), { "agent:jobpilot": [10, 20] });
        return {
            kept: [agent.x, agent.y], placedNear: Number.isFinite(work.x), placed: g.placed,
            previousWins: [again.byId.get("agent:jobpilot").x, again.byId.get("agent:jobpilot").y],
            chunkSizes: chunks.map((c) => JSON.stringify({ type: "brain_layout", ...c }).length),
            fresh: chunks.map((c) => c.fresh), total: chunks.reduce((n, c) => n + Object.keys(c.positions).length, 0),
            path: m.path(g.byId.get("work:r1"), g.byId.get("file:Documents/notes.md")).map((n) => n.id),
            hiddenPath: m.path(g.byId.get("work:r1"), g.byId.get("file:Documents/notes.md"), new Set(["project"])).map((n) => n.id),
            found: m.search(g.nodes, "JOBPILOT e:/").map((n) => n.id),
            degree: g.byId.get("project:jobpilot").degree,
            labels: ["work:r1", "project:jobpilot", "hub:projects", "agent:jobpilot"].map((id) => m.typeLabel(g.byId.get(id))),
        };
    }""", GRAPH)
    assert result["kept"] == [10, 20] and result["placed"] == 1 and result["placedNear"]
    assert result["previousWins"] == [5, 6]                             # the open page's positions beat the kept ones
    assert max(result["chunkSizes"]) < 8000 and result["total"] == 900
    assert result["fresh"][0] is True and not any(result["fresh"][1:])
    assert result["path"] == ["work:r1", "hub:agents", "agent:jobpilot", "project:jobpilot", "file:Documents/notes.md"]
    assert result["hiddenPath"] == ["work:r1", "hub:agents", "jarvis", "hub:file", "file:Documents/notes.md"]
    assert result["found"] == ["project:jobpilot"] and result["degree"] == 3   # its folder is searched too
    assert result["labels"] == ["Running request", "Project", "Group", "Agent"]


# ---- phase 3: the HUD, the switches, the command bar and live highlights


async def test_the_app_opens_on_the_brain_with_the_hud(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url)                                               # no section asked for
    await page.get_by_role("heading", name="Brain").wait_for()
    hud = page.get_by_role("region", name="JARVIS", exact=True)
    await hud.get_by_role("img", name="JARVIS: Listening").wait_for()
    assert "Listening" not in await hud.inner_text()                   # the state is said once, in the header
    await page.locator("header .channel").get_by_text("Listening").wait_for()
    await hud.get_by_text("Details").click()                           # the model and language are folded away
    text = await hud.inner_text()
    assert "gemini-3.8-live" in text and "en-IN" in text
    caps = page.get_by_role("group", name="Capabilities")
    for name in ("Mic", "Voice", "Announce", "Memory", "Agents"):
        assert await caps.get_by_role("switch", name=name).get_attribute("aria-checked") == "true", name

    bridge.bus.publish("state", state="working")
    await hud.get_by_role("img", name="JARVIS: Working").wait_for()
    assert "tone-working" in await page.locator(".hud-ring").get_attribute("class")
    bridge.bus.publish("state", state="muted")
    await caps.get_by_role("switch", name="Mic").and_(page.locator("[aria-checked=false]")).wait_for()
    await hud.get_by_role("button", name="Orb").click()                # the 3D orb (or nothing without WebGL)
    assert await page.locator(".hud-ring").count() == 0
    await hud.get_by_role("button", name="Ring").click()
    await page.locator(".hud-ring").wait_for()


async def test_the_switches_are_jarvis_controls(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    caps = page.get_by_role("group", name="Capabilities")
    for name in ("Mic", "Voice", "Announce", "Memory", "Agents"):
        await caps.get_by_role("switch", name=name).click()
    assert await until(lambda: bridge.controls == ["mute", "voice", "announce", "keep_memory", "open_command"])
    bridge.bus.publish("memory_keep", on=False, enabled=True)
    bridge.bus.publish("voice", on=False)
    await caps.get_by_role("switch", name="Memory").and_(page.locator("[aria-checked=false]")).wait_for()
    await caps.get_by_role("switch", name="Voice").and_(page.locator("[aria-checked=false]")).wait_for()
    await page.locator("header .channel [title^='Replies are text only']").wait_for()
    bridge.bus.publish("memory_keep", on=False, enabled=False)
    assert await caps.get_by_role("switch", name="Memory").is_disabled()


async def test_the_command_bar_sends_and_shows_the_caption_and_plans(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_role("button", name="What's on my screen?").click()   # an example
    assert await until(lambda: bridge.chats and bridge.chats[-1][0] == "What's on my screen?")
    box = page.get_by_label("Message to JARVIS")
    await box.fill("Call Sanjay tomorrow")
    await box.press("Enter")
    assert await until(lambda: bridge.chats[-1][0] == "Call Sanjay tomorrow")

    b = bridge.bus
    b.publish("message", channel="chat", role="user", label="Krishna", text="Call Sanjay tomorrow")
    b.publish("partial", channel="voice", role="jarvis", text="Setting a ")
    caption = page.locator(".caption")
    await caption.get_by_text("Setting a").wait_for()
    assert "live" in await caption.get_attribute("class")
    b.publish("message", channel="chat", role="jarvis", label="JARVIS", text="Setting a reminder for tomorrow.")
    await caption.get_by_text("Setting a reminder for tomorrow.").wait_for()
    assert await page.get_by_role("button", name="What's on my screen?").count() == 0   # examples step aside

    b.publish("confirm_requested", channel="voice", id=3, tool="set_reminder", summary="Remind you to call Sanjay at 9?",
              step={})
    await page.get_by_text("Remind you to call Sanjay at 9?").wait_for()
    await page.locator(".ask").get_by_role("button", name="Approve").click()
    assert await until(lambda: "confirm" in bridge.controls)
    b.publish("confirm_resolved", channel="voice", id=3, tool="set_reminder", outcome="confirmed_typed")
    await page.locator(".ask-plan").wait_for(state="detached")         # done: it leaves the command bar


async def test_what_jarvis_uses_lights_up(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    pulses = "() => [...document.querySelector('.brain-canvas').brainView.pulses.keys()].sort()"
    bridge.bus.publish("tool_started", channel="voice", tool="search_web", args={}, step=False)
    bridge.bus.publish("tool_started", channel="voice", tool="not_in_the_graph", args={}, step=False)
    bridge.bus.publish("agent_update", request_id="r1", status="done", say="Found 3 jobs.")
    await page.wait_for_function(f"({pulses})().length === 2")
    assert await page.evaluate(pulses) == ["hub:agents", "tool:search_web"]


# ---- phase 4: the inspector's actions


async def focus(page, name):
    await page.get_by_label("Search the brain").fill(name)
    await page.get_by_label("Search results").get_by_role("button", name=name).first.click()
    await page.get_by_role("region", name="Inspector").wait_for()


async def test_the_inspector_actions(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    inspector = page.get_by_role("region", name="Inspector")

    await focus(page, "notes.md")
    await inspector.get_by_role("button", name="Show in folder").click()
    await inspector.get_by_text("Opened in File Explorer.").wait_for()
    assert bridge.shown == ["file:Documents/notes.md"]                  # the node, never a path

    await page.get_by_label("Search the brain").fill("jobpilot")
    await page.get_by_label("Search results").get_by_role("button", name="jobpilot Agent").click()
    await inspector.get_by_role("button", name="Command centre").click()
    assert await until(lambda: "open_command" in bridge.controls)

    await focus(page, "search_web")                                     # a tool: nothing to do with it
    assert await inspector.locator(".brain-actions").count() == 0
