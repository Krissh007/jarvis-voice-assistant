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
        node("hub:memory", "memory", "Remembered facts", True),
        node("fact:1", "memory", "Krishna's friend is called Sanjay.", added="2026-10-19T10:00:00", uses=2),
        node("fact:2", "memory", "<b>Krishna</b> prefers short answers.", added="2026-10-20T10:00:00", uses=0),
        node("hub:conversation", "conversation", "Conversations", True),
        node("conv:5", "conversation", "Call Sanjay tomorrow and search the web", started="2026-10-25T09:00:00",
             lines=6, channels="voice", day="Sun 25 Oct"),
        node("cat:search", "tool", "Search", True, description="Web search"),
        node("tool:search_web", "tool", "search_web", description="Search the web.", read_only=True),
        node("hub:file", "file", "Files used", True),
        node("file:Documents/notes.md", "file", "notes.md", path="Documents/notes.md", last_used="2026-10-25T09:01"),
    ],
    "links": [
        link("jarvis", "hub:memory", "part of JARVIS: remembered facts"),
        link("fact:1", "hub:memory", "remembered"),
        link("fact:2", "hub:memory", "remembered"),
        link("jarvis", "hub:conversation", "part of JARVIS: conversations"),
        link("conv:5", "hub:conversation", "kept conversation"),
        link("conv:5", "fact:1", "close in meaning to “Call Sanjay tomorrow” (0.71)", "meaning"),
        link("jarvis", "cat:search", "search tools"),
        link("tool:search_web", "cat:search", "a search tool"),
        link("conv:5", "tool:search_web", "called 2 times in this conversation", "uses"),
        link("jarvis", "hub:file", "part of JARVIS: files used"),
        link("file:Documents/notes.md", "hub:file", "a file JARVIS used"),
    ],
    "counts": {"memory": 2, "conversation": 1, "tool": 1, "file": 1},
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
    assert "Memory2" in (await legend.inner_text()).replace("\n", "").replace(" ", "")

    await page.get_by_label("Search the brain").fill("sanjay")
    results = page.get_by_label("Search results")
    assert (await results.get_by_role("button").count()) == 2          # the fact and the conversation
    await results.get_by_role("button", name="Krishna's friend is called Sanjay.").click()
    inspector = page.get_by_role("region", name="Inspector")
    await inspector.get_by_text("Remembered fact", exact=True).wait_for()
    await inspector.get_by_text("close in meaning to “Call Sanjay tomorrow” (0.71)").wait_for()
    assert "Recalled" in await inspector.inner_text() and "2 times" in await inspector.inner_text()

    await inspector.get_by_role("button", name="Call Sanjay tomorrow and search the web").click()   # a neighbour
    await inspector.get_by_text("called 2 times in this conversation").wait_for()
    await page.get_by_label("Clear the focus").click()
    await page.get_by_text("Click a node to see what it is").wait_for()


async def test_labels_are_text_not_html(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_label("Search the brain").fill("short answers")
    await page.get_by_label("Search the brain").press("Enter")
    await page.get_by_role("region", name="Inspector").get_by_text("<b>Krishna</b> prefers short answers.").wait_for()
    assert await page.locator(".brain b").count() == 0


async def test_the_legend_hides_a_type_and_shift_click_traces_a_path(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    await settle(page)
    x, y = await screen_xy(page, "fact:2")
    await page.mouse.click(x, y)
    await page.get_by_role("region", name="Inspector").get_by_text("<b>Krishna</b> prefers short answers.").wait_for()
    x, y = await screen_xy(page, "tool:search_web")
    await page.keyboard.down("Shift")
    await page.mouse.click(x, y)
    await page.keyboard.up("Shift")
    trail = page.get_by_role("region", name="Path")
    await trail.wait_for()
    text = await trail.inner_text()
    # fact -> Remembered facts -> fact:1 ... the path avoids JARVIS when there's another way
    assert "called 2 times in this conversation" in text and "part of JARVIS" not in text
    assert text.index("prefers short answers") < text.index("search_web")
    await page.get_by_label("Clear the path").click()

    await page.get_by_role("button", name="Hide tools").click()
    assert await page.get_by_role("button", name="Show tools").get_attribute("aria-pressed") == "false"
    await page.get_by_label("Search the brain").fill("search_web")
    await page.get_by_text("Nothing called that.").wait_for()           # hidden types aren't found either
    hubs = await page.get_by_role("region", name="Top hubs").inner_text()
    assert "Search" not in hubs and "Remembered facts" in hubs


async def test_the_brain_refreshes_on_brain_update_and_keeps_its_layout(served, page) -> None:  # noqa: F811
    bridge, url = served
    bridge.brain_graph = GRAPH
    await page.goto(url + "&s=brain")
    await page.get_by_text("10 nodes · 11 links").wait_for()
    assert await until(lambda: bridge.layouts, timeout=15)              # kept once the layout settles
    positions, fresh = bridge.layouts[0]
    assert fresh and set(positions) == {n["id"] for n in GRAPH["nodes"]}
    assert all(len(xy) == 2 for xy in positions.values())
    before = await screen_xy(page, "fact:1")

    grown = json.loads(json.dumps(GRAPH))
    grown["nodes"].append(node("fact:3", "memory", "Krishna's car is blue."))
    grown["links"].append(link("fact:3", "hub:memory", "remembered"))
    grown["counts"]["memory"] = 3
    bridge.brain_graph = grown
    seen = bridge.brain_requests
    bridge.bus.publish("brain_update", why=["memory"])
    await page.get_by_text("11 nodes · 12 links").wait_for()
    assert bridge.brain_requests == seen + 1
    after = await screen_xy(page, "fact:1")
    assert abs(after[0] - before[0]) < 60 and abs(after[1] - before[1]) < 60   # the brain didn't reshuffle


async def test_the_model_helpers(served, page) -> None:  # noqa: F811
    """brain-model.js on its own: kept positions, chunked layout messages, the path, search."""
    bridge, url = served
    await page.goto(url + "&s=chat")
    result = await page.evaluate("""async (graph) => {
        const m = await import('/static/app/sections/brain-model.js');
        const g = m.prepare(graph, new Map(), { "fact:1": [10, 20] });
        const fact = g.byId.get("fact:1"), conv = g.byId.get("conv:5");
        const many = Array.from({ length: 900 }, (_, i) => ({ id: "file:" + "x".repeat(60) + i, x: i * 1.234, y: -i }));
        const chunks = m.layoutChunks(many);
        const again = m.prepare(graph, new Map([["fact:1", { x: 5, y: 6 }]]), { "fact:1": [10, 20] });
        return {
            kept: [fact.x, fact.y], placedNear: Number.isFinite(conv.x), placed: g.placed,
            previousWins: [again.byId.get("fact:1").x, again.byId.get("fact:1").y],
            chunkSizes: chunks.map((c) => JSON.stringify({ type: "brain_layout", ...c }).length),
            fresh: chunks.map((c) => c.fresh), total: chunks.reduce((n, c) => n + Object.keys(c.positions).length, 0),
            path: m.path(g.byId.get("fact:2"), g.byId.get("tool:search_web")).map((n) => n.id),
            hiddenPath: m.path(g.byId.get("fact:2"), g.byId.get("tool:search_web"), new Set(["conversation"])).map((n) => n.id),
            found: m.search(g.nodes, "SANJAY call").map((n) => n.id),
            degree: g.byId.get("conv:5").degree,
        };
    }""", GRAPH)
    assert result["kept"] == [10, 20] and result["placed"] == 1 and result["placedNear"]
    assert result["previousWins"] == [5, 6]                             # the open page's positions beat the kept ones
    assert max(result["chunkSizes"]) < 8000 and result["total"] == 900
    assert result["fresh"][0] is True and not any(result["fresh"][1:])
    assert result["path"] == ["fact:2", "hub:memory", "fact:1", "conv:5", "tool:search_web"]
    assert result["hiddenPath"] == ["fact:2", "hub:memory", "jarvis", "cat:search", "tool:search_web"]
    assert result["found"] == ["conv:5", "fact:1"] and result["degree"] == 3   # "call" is in "called" too
