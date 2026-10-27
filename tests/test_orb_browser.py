"""The orb page in a real browser (Playwright): the states it shows, the muted badge, and the same orb and
gestures docked in the full-screen app (double-click mutes; a docked orb isn't dragged out of the app's dot)."""

from __future__ import annotations

from tests.test_app_browser import page, served, until  # noqa: F401  (fixtures)


async def open_orb(served, page):  # noqa: F811
    bridge, url = served
    lines: list[str] = []
    page.on("console", lambda m: lines.append(m.text) if m.text.startswith("jarvis:") else None)
    await page.goto(url.replace("/app#", "/#") + "&preview=1&orb=2d")
    orb = page.locator("#orb")
    await page.locator('#orb[data-state="listening"]').wait_for(state="attached")
    box = await orb.bounding_box()
    return bridge, lines, orb, (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


async def test_the_orb_shows_idle_listening_thinking_speaking_and_muted(served, page) -> None:  # noqa: F811
    bridge, _, orb, _ = await open_orb(served, page)
    badge = page.locator("#mutedBadge")
    assert await badge.is_hidden()

    async def shows(state):
        await page.locator(f'#orb[data-state="{state}"]').wait_for(state="attached")

    bridge.bus.publish("state", state="asleep")
    await shows("asleep")
    assert "idle" in await orb.get_attribute("aria-label")
    bridge.bus.publish("state", state="listening")
    bridge.bus.publish("activity", text="Thinking…", what="thinking")       # an answer on its way
    await shows("thinking")
    bridge.bus.publish("state", state="speaking")                           # speaking wins over thinking
    await shows("speaking")
    bridge.bus.publish("state", state="listening")
    bridge.bus.publish("activity", text="", what="")
    await shows("listening")
    bridge.bus.publish("activity", text="Discovery is running in the background", what="background")
    await shows("listening")                                                # background work isn't "thinking"
    bridge.bus.publish("state", state="muted")
    await shows("muted")
    assert await badge.is_visible() and "unmute" in await orb.get_attribute("aria-label")
    bridge.bus.publish("state", state="listening")
    await shows("listening")
    assert await badge.is_hidden()


async def test_double_click_mutes_floating_and_docked_and_a_docked_orb_stays_put(served, page) -> None:  # noqa: F811
    bridge, lines, orb, (x, y) = await open_orb(served, page)
    await page.mouse.dblclick(x, y)
    assert await until(lambda: bridge.controls == ["mute"])                 # floating: double-click mutes
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + 40, y + 30, steps=4)
    await page.mouse.up()
    assert await until(lambda: "jarvis:drag" in lines)                      # floating: a drag moves the window

    bridge.bus.publish("orb_dock", docked=True, x=1880, y=60, size=40)
    assert await until(lambda: any(line.startswith('jarvis:dock:{"docked":true') for line in lines))
    assert "drag to move" not in await orb.get_attribute("aria-label")
    lines.clear()
    await page.wait_for_timeout(350)                                        # past the double-click window
    await page.mouse.dblclick(x, y)
    assert await until(lambda: bridge.controls == ["mute", "mute"])         # docked: the same gesture
    bridge.bus.publish("state", state="muted")
    await page.locator("#mutedBadge").wait_for(state="visible")             # and the same muted look
    await page.wait_for_timeout(350)
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + 40, y + 30, steps=4)
    await page.mouse.up()
    await page.wait_for_timeout(400)
    assert "jarvis:drag" not in lines and bridge.controls == ["mute", "mute"]   # docked: not dragged, no tap


# ---- the radial menu: hover bloom, click, flick, the Dashboards ring

MAIN = ["chat", "command", "hub", "dashboards", "settings"]


async def bloom(page, x, y) -> None:  # noqa: F811
    """Mouse away (the menu folds), then onto the orb: the menu blooms."""
    await page.mouse.move(5, 5)
    await page.locator('#radial [data-go="chat"]').wait_for(state="hidden")
    await page.wait_for_timeout(50)
    await page.mouse.move(x - 2, y - 2)
    await page.mouse.move(x, y)
    await page.locator('#radial [data-go="chat"]').wait_for(state="visible")
    await page.wait_for_timeout(450)                                       # the buttons have faded in


async def centre_of(page, selector):
    box = await page.locator(selector).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


async def flick(page, x, y, selector, far=0.45) -> None:
    """Press the orb, drag a little toward the item (less than half way), let go."""
    tx, ty = await centre_of(page, selector)
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + (tx - x) * far, y + (ty - y) * far, steps=5)
    await page.mouse.up()


def opened(bridge) -> list:
    """What the menu opened, sorted (controls, Agent Hub starts and dashboards are kept apart)."""
    return sorted(bridge.controls + [f"hub:{a}" for a in bridge.hub_actions] + [f"dashboard:{d}" for d in bridge.dashboards_opened])


async def test_hover_blooms_the_ring_and_every_item_opens_by_click(served, page) -> None:  # noqa: F811
    bridge, _, _, (x, y) = await open_orb(served, page)
    await bloom(page, x, y)
    assert [await b.get_attribute("data-go") for b in await page.locator("#radial button").all()] == MAIN
    for b in await page.locator("#radial button").all():                  # a ring around the orb, all of it inside
        bx, by = await centre_of(page, f'#radial [data-go="{await b.get_attribute("data-go")}"]')
        assert 70 < ((bx - x) ** 2 + (by - y) ** 2) ** 0.5 < 160
    expected = []
    for go, result in (("chat", "open_chat"), ("command", "open_command"), ("settings", "open_settings"), ("hub", "hub:start")):
        await page.locator(f'#radial [data-go="{go}"]').click()
        expected.append(result)
        assert await until(lambda: opened(bridge) == sorted(expected)), opened(bridge)
        await page.locator('#radial [data-go="chat"]').wait_for(state="hidden")              # an item opens and the menu folds away
        await bloom(page, x, y)
    await page.locator('#radial [data-go="dashboards"]').hover()           # hovering Dashboards opens its ring
    await page.locator("#subring button").first.wait_for(state="visible")
    assert await page.locator("#subring button").all_inner_texts() == ["PMO", "JobPilot"]
    await page.wait_for_timeout(400)
    for did in ("pmo", "jobpilot"):
        await page.locator(f'#subring [data-id="{did}"]').click()
        expected.append(f"dashboard:{did}")
        assert await until(lambda: opened(bridge) == sorted(expected)), opened(bridge)
        await bloom(page, x, y)
        await page.locator('#radial [data-go="dashboards"]').hover()
        await page.locator("#subring button").first.wait_for(state="visible")
        await page.wait_for_timeout(400)


async def test_every_item_opens_by_flick(served, page) -> None:  # noqa: F811
    bridge, lines, _, (x, y) = await open_orb(served, page)
    expected = []
    for go, result in (("chat", "open_chat"), ("command", "open_command"), ("hub", "hub:start"), ("settings", "open_settings")):
        await bloom(page, x, y)
        await flick(page, x, y, f'#radial [data-go="{go}"]')
        expected.append(result)
        assert await until(lambda: opened(bridge) == sorted(expected)), (go, opened(bridge))
    for did in ("pmo", "jobpilot"):
        await bloom(page, x, y)
        await flick(page, x, y, '#radial [data-go="dashboards"]')           # a flick to Dashboards opens its ring
        await page.locator("#subring button").first.wait_for(state="visible")
        await page.wait_for_timeout(400)
        await flick(page, x, y, f'#subring [data-id="{did}"]', far=0.3)      # then a flick picks from that ring
        expected.append(f"dashboard:{did}")
        assert await until(lambda: opened(bridge) == sorted(expected)), (did, opened(bridge))
    assert "jarvis:drag" not in lines and "mute" not in bridge.controls     # flicks never moved or muted the orb


async def test_gestures_dont_get_in_each_others_way(served, page) -> None:  # noqa: F811
    bridge, lines, _, (x, y) = await open_orb(served, page)
    await bloom(page, x, y)
    await page.mouse.dblclick(x, y)                                         # double-click: mute, the menu stays
    assert await until(lambda: bridge.controls == ["mute"])
    await page.wait_for_timeout(400)
    assert await page.locator('#radial [data-go="chat"]').is_visible()
    await page.mouse.move(x, y)                                             # a drag past the menu: the orb moves
    await page.mouse.down()
    await page.mouse.move(x + 260, y + 120, steps=8)
    assert await until(lambda: any(line.startswith('jarvis:drag:{"dx":') for line in lines))
    await page.mouse.up()
    assert await page.locator('#radial [data-go="chat"]').is_hidden() and bridge.controls == ["mute"]   # and nothing was chosen
    await bloom(page, x, y)
    await page.mouse.move(x + 3, y + 3, steps=2)                             # a wobble on the orb isn't a flick
    await page.mouse.down()
    await page.mouse.move(x + 8, y + 2)
    await page.mouse.up()
    await page.wait_for_timeout(400)
    assert bridge.controls == ["mute"] and not bridge.hub_actions and not bridge.dashboards_opened
    await page.mouse.move(5, 5)                                             # the mouse leaves: the menu folds away
    await page.locator('#radial [data-go="chat"]').wait_for(state="hidden", timeout=3000)


async def test_near_a_corner_the_menu_fans_out_into_the_screen(served, page) -> None:  # noqa: F811
    """Docked in the app's top-right dot, the orb has room only below and to the left: an arc, not a ring."""
    bridge, _, _, (x, y) = await open_orb(served, page)
    await bloom(page, x, y)
    size = await page.evaluate("[innerWidth, innerHeight]")
    await page.evaluate(f"window.jarvisPlace({{expanded: true, ring: true, x: {size[0] - 24}, y: 24, canvas: 59}})")
    await page.locator('#radial [data-go="dashboards"]').dispatch_event("mouseenter")
    await page.locator("#subring button").first.wait_for(state="visible")
    await page.wait_for_timeout(450)
    boxes = [await b.bounding_box() for b in await page.locator("#radial button, #subring button").all()]
    assert len(boxes) == 7
    for b in boxes:                                                         # every button on the screen
        assert b["x"] >= 0 and b["y"] >= 0 and b["x"] + b["width"] <= size[0] and b["y"] + b["height"] <= size[1]
    centres = [(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2) for b in boxes]
    for i, a in enumerate(centres):                                         # and none on top of another
        for c in centres[i + 1:]:
            assert ((a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2) ** 0.5 >= 56


async def test_shown_cards_close_by_button_by_time_and_on_clear(served, page) -> None:  # noqa: F811
    """Seen live 28 Oct: the screen JARVIS looked at and two prompts stayed next to the orb for twenty minutes
    of conversation. Each card now has a close button and its own time limit, and clear_screen removes them;
    an answer still being shown stays."""
    bridge, _, _, _ = await open_orb(served, page)
    b = bridge.bus
    cards = page.locator("#sections .card.media")
    b.publish("media", type="text", title="UI prompt", items=[], text="Move the channel name to the header.")
    await page.get_by_role("button", name="Close UI prompt").click()
    await cards.first.wait_for(state="detached")

    await page.evaluate("CARD_HOLD_MS.image = 400")
    b.publish("media", type="image", title="Looking at your screen", data="data:image/png;base64,iVBORw0KGgo=")
    await cards.first.wait_for()
    for _ in range(4):                                                     # talking doesn't keep it up
        b.publish("message", channel="voice", role="user", label="Krishna", text="and another thing")
        await page.wait_for_timeout(150)
    await cards.first.wait_for(state="detached")

    b.publish("message", channel="voice", role="jarvis", label="JARVIS", text="Here are the ideas.")
    b.publish("media", type="text", title="Ideas", items=["One", "Two"], text="")
    b.publish("media", type="image", title="Looking at your screen", data="data:image/png;base64,iVBORw0KGgo=")
    await page.get_by_role("button", name="Close Ideas").wait_for()
    b.publish("screen_clear")
    await cards.nth(1).wait_for(state="detached")
    await cards.first.wait_for(state="detached")
    assert await page.locator("#sections .card .reply").inner_text() == "Here are the ideas."   # the answer stays
