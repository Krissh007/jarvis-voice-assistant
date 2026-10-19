// JARVIS's orb: a quick-action control centre. Idle, only the orb is visible. Tap it to talk, drag it
// anywhere, double-tap it to open the control centre under it: the controls (talk, mute, new chat, stop,
// sleep), then the sections a reply needs (the answer, a confirmation, a picture, what's playing), then the
// typing box and quick-action chips, stacked under the orb. "New chat" opens the complete chat page in its
// own window. The orb is drawn in 3D (orb3d.js, Three.js) when WebGL works, else in 2D (orb.js).
//
// Three views: "closed" (just the orb), "voice" and "full". Talking to a closed orb opens the voice view:
// only the sections, with the answer as a short caption, and no controls, typing box or chips. It fades
// back to the orb a few seconds (web_ui.caption_seconds) after JARVIS finishes; a confirmation keeps it
// open until it's answered. Double-tap opens the full control centre, and double-tap, Esc or sleep closes it.
//
// Every text is set with textContent, never as HTML: replies and tool results can contain text from web
// pages and apps. The token comes from the URL's #fragment. Messages for the window itself go out as
// console lines starting "jarvis:" (see ui/window.py), and the window calls jarvisPlace() back.
"use strict";

const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("t") || "";
const preview = params.get("preview") === "1";   // a normal browser: a backdrop, and laid out by the page itself
if (params.get("bg") === "1" || preview) document.body.classList.add("backdrop");
const $ = (id) => document.getElementById(id);

const MAX_SECTIONS = 4;
const LIST_HOLD_MS = 45000;   // the voice view stays this long when JARVIS put a list on screen
const ACT = { starting: 0, asleep: 0, listening: 0.55, speaking: 1, working: 0.75, muted: 0.15 };

let ws = null, retryMs = 500, pendingId = null, state = "starting";
let view = "closed", captionMs = 8000;
let look = { orb_size: 112, core_size: 0.83, orb_brightness: 0.55, cloud_brightness: 0.45, ribbon_brightness: 0.5 }, lookDefaults = { ...look };
const place = { expanded: false, x: 0, y: 0, canvas: 235 };
const orbState = { act: 0, actTarget: 0, level: 0, levelTarget: 0 };

// ---------------------------------------------------------------- connection

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
  ws.onopen = () => { retryMs = 500; setStatus(""); };
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    setStatus("Not connected to JARVIS. Retrying…", true);
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 8000);
  };
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(payload)); return true; }
  return false;
}

function toWindow(command, data) {             // read by ui/window.py
  console.log("jarvis:" + command + (data === undefined ? "" : ":" + JSON.stringify(data)));
}

// ---------------------------------------------------------------- open and close

// The window resizes itself around the orb and then calls this with where the orb now is.
window.jarvisPlace = function (p) {
  Object.assign(place, p);
  sizeOrb();
  const opening = place.expanded && $("panel").hidden;
  $("panel").hidden = !place.expanded;
  layout();
  if (opening) [...$("panel").children].forEach((el, i) => animateIn(el, i * 45));
};

// Fades and slides are driven by animation frames, not CSS (see app.css): the Qt window doesn't advance
// CSS animations, but it does run frames (the orb draws on them).
const ease = (k) => 1 - Math.pow(1 - k, 3);
function animate(ms, delay, step, done) {
  const start = performance.now() + delay;
  (function frame(now) {
    const k = Math.min(1, Math.max(0, (now - start) / ms));
    step(ease(k));
    if (k < 1) requestAnimationFrame(frame); else if (done) done();
  })(performance.now());
}
function animateIn(el, delay = 0) {
  animate(380, delay, (e) => {
    el.style.opacity = e;
    el.style.transform = e < 1 ? `translateY(${-8 * (1 - e)}px) scale(${0.98 + 0.02 * e})` : "";
  }, () => { el.style.opacity = ""; sendShapeSoon(); });
}
function animateOut(el, done) {
  animate(240, 0, (e) => { el.style.opacity = 1 - e; el.style.transform = `scale(${1 - 0.03 * e})`; }, done);
}

function setView(next) {
  if (next === view) return;
  const prev = view;
  view = next;
  document.body.classList.toggle("voice", next === "voice");
  clearTimeout(captionTimer);
  if (next === "closed") { if (!$("look").hidden) showLook(false); return setExpanded(false); }
  if (prev === "closed") setExpanded(true);     // the window grows; jarvisPlace() fades the panel in
  else if (next === "full") { layout(); [$("controls"), $("composer"), $("quick")].forEach((el, i) => animateIn(el, i * 45)); }
  if (next === "voice") captionSoon();
}

// The voice view fades back to just the orb once JARVIS has finished and nothing's changed for a while.
let captionTimer = null, touched = 0;
function captionSoon() {
  touched = performance.now();
  clearTimeout(captionTimer);
  if (view !== "voice") return;
  captionTimer = setTimeout(() => {
    if (view !== "voice") return;
    if (pendingId || state === "speaking" || state === "working") return captionSoon();
    const panel = $("panel"), started = performance.now();
    animate(300, 0, (e) => { panel.style.opacity = 1 - e; }, () => {
      panel.style.opacity = "";
      if (view !== "voice") return;
      if (touched > started) return captionSoon();   // something new came while it faded
      setView("closed");
    });
  }, sections.has("list") ? Math.max(captionMs, LIST_HOLD_MS) : captionMs);   // a list needs time to read
}

function setExpanded(expanded) {
  if (expanded === place.expanded) return;
  if (!expanded) clearSections(true);
  if (preview) {                                // no window around the page: lay it out here
    window.jarvisPlace({ expanded, x: innerWidth / 2, y: expanded ? place.canvas / 2 + 6 : innerHeight / 2 });
  } else {
    toWindow("mode", { expanded });
  }
}

// ---------------------------------------------------------------- events

function handle(ev) {
  switch (ev.kind) {
    case "hello": return hello(ev);
    case "level": setState(ev.state); orbState.levelTarget = ev.level || 0; return;
    case "state": return setState(ev.state);
    case "partial": return partial(ev);
    case "message": return message(ev);
    case "tool_finished": return toolFinished(ev);
    case "confirm_requested": return confirmRequested(ev);
    case "confirm_resolved": return confirmResolved(ev);
    case "media": return media(ev);
    case "conversation": return clearSections();
    case "announce": return setAnnounce(ev.on);
    case "voice": return setSpeak(ev.on);
    case "status": return setStatus(ev.text);
    case "focus": if (!ev.target) toWindow("front"); return;
  }
}

function hello(ev) {
  const snap = ev.snapshot || {};
  setSpeak((snap.voice ?? snap.speak) !== false);
  setAnnounce(snap.announce !== false);
  if (snap.caption_s) captionMs = snap.caption_s * 1000;
  if (snap.look) look = { ...look, ...snap.look };
  if (snap.look_defaults) lookDefaults = { ...lookDefaults, ...snap.look_defaults };
  applyLook(false);
  setState(snap.state || "starting");
  const quick = $("quick");
  quick.replaceChildren();
  for (const text of snap.quick_actions || []) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = text;
    b.addEventListener("click", () => send({ type: "chat", text, speak: speakOn() }));
    quick.append(b);
  }
  if (snap.pending) confirmRequested({ id: snap.pending.id, summary: snap.pending.summary });
  layout();
}

function setState(next) {
  if (!next) return;
  const wasAwake = state !== "asleep" && state !== "starting", changed = next !== state;
  state = next;
  orbState.actTarget = ACT[next] ?? 0.55;
  orb && orb.setTint(next === "muted" ? "red" : next === "working" ? "amber" : "blue");
  $("mute").classList.toggle("on", next === "muted");
  $("mute").textContent = next === "muted" ? "Unmute" : "Mute";
  if (next === "asleep" && wasAwake) setView("closed");   // "go to sleep": back to just the orb
  else if (changed) captionSoon();
}

let statusTimer = null;
function setStatus(text, sticky) {
  $("status").textContent = text || "";
  clearTimeout(statusTimer);
  if (text && !sticky) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 5000);
}

// ---------------------------------------------------------------- sections around the orb

const sections = new Map();   // key -> element, in the order they appeared

function section(key, title, cls) {
  let el = sections.get(key);
  if (!el) {
    el = document.createElement("section");
    el.className = `card ${cls || ""}`;
    const h = document.createElement("h2");
    h.textContent = title;
    el.append(h);
    $("sections").append(el);                    // stacked under the orb, in the order they came
    sections.set(key, el);
    while (sections.size > MAX_SECTIONS) removeSection(sections.keys().next().value);
    if (view === "closed") setView("voice");   // something to show: the voice view, unless already open
    layout();
    animateIn(el);
  }
  return el;
}

function removeSection(key) {
  const el = sections.get(key);
  if (!el) return;
  sections.delete(key);
  animateOut(el, () => { el.remove(); sendShape(); });
  layout();
}

function clearSections(now) {
  for (const key of [...sections.keys()]) {
    if (now) { sections.get(key).remove(); sections.delete(key); } else removeSection(key);
  }
  layout();
}

function layout() {
  const w = innerWidth, cx = place.x || w / 2, cy = place.y || innerHeight / 2;
  const orbEl = $("orb");
  orbEl.style.left = cx + "px"; orbEl.style.top = cy + "px";
  const panel = $("panel");
  panel.style.left = cx + "px";
  panel.style.top = cy + place.canvas * 0.36 + "px";          // just under the orb's bands
  sendShape();
}

function answerSection() {
  const el = section("answer", "JARVIS");
  if (!el.querySelector(".reply")) {
    const you = document.createElement("p"); you.className = "you";
    const reply = document.createElement("p"); reply.className = "reply";
    el.append(you, reply);
  }
  return el;
}

let liveUser = "", liveReply = "";
function partial(ev) {
  if (ev.role === "user") {
    liveUser = (liveUser + ev.text).replace(/\s+/g, " ").trimStart();
    answerSection().querySelector(".you").textContent = "You: " + liveUser;
  } else {
    liveReply = (liveReply + ev.text).replace(/\s+/g, " ").trimStart();
    answerSection().querySelector(".reply").textContent = liveReply;
  }
  captionSoon();
  sendShapeSoon();
}

function message(ev) {
  const el = answerSection();
  if (ev.role === "user") {
    liveUser = ""; liveReply = "";
    el.querySelector(".you").textContent = "You: " + ev.text;
    el.querySelector(".reply").textContent = "";
  } else {
    liveReply = "";
    el.querySelector(".reply").textContent = ev.text;
  }
  captionSoon();
  sendShapeSoon();
}

const PLAY_TOOLS = new Set(["play_music", "play_youtube", "now_playing", "media_key"]);
const plain = (text) => String(text || "").replace(/<\/?untrusted_content[^>]*>/g, "").trim();

function toolFinished(ev) {
  const result = plain(ev.result);
  if (!PLAY_TOOLS.has(ev.tool) || !result || ev.paused) return;
  const card = section("playing", "Now playing");
  card.replaceChildren(card.firstChild);
  const p = document.createElement("p");
  const icon = document.createElement("span"); icon.className = "play";
  p.append(icon, document.createTextNode(result.slice(0, 200)));
  card.append(p);
  sendShapeSoon();
}

function media(ev) {                   // a picture a tool showed (e.g. the screen JARVIS just looked at)
  if (ev.type === "text") return shownText(ev);
  if (ev.type !== "image" || !String(ev.data || "").startsWith("data:image/")) return;
  const card = section("image", ev.title || "Picture", "media");
  card.replaceChildren(card.firstChild);
  const img = document.createElement("img");
  img.alt = ev.caption || "";
  img.src = ev.data;
  card.append(img);
  if (ev.caption) { const p = document.createElement("p"); p.className = "dim"; p.textContent = ev.caption; card.append(p); }
  img.onload = sendShape;
}

function shownText(ev) {               // a list or text JARVIS put on screen instead of reading it aloud
  const card = section("list", ev.title || "For you", "media list");
  card.replaceChildren(card.firstChild);
  card.firstChild.textContent = ev.title || "For you";
  const items = Array.isArray(ev.items) ? ev.items : [];
  if (items.length) {
    const ol = document.createElement("ol");
    for (const item of items) { const li = document.createElement("li"); li.textContent = String(item); ol.append(li); }
    card.append(ol);
  }
  if (ev.text) { const p = document.createElement("p"); p.className = "text"; p.textContent = String(ev.text); card.append(p); }
  captionSoon();
  sendShapeSoon();
}

function confirmRequested(ev) {
  pendingId = ev.id;
  const el = section("confirm", "Should I go ahead?", "warn");
  el.replaceChildren(el.firstChild);
  const p = document.createElement("p");
  p.textContent = ev.summary;
  const row = document.createElement("div");
  row.className = "row";
  for (const [label, action, cls] of [["Confirm", "confirm", "pill yes"], ["Cancel", "cancel", "pill"]]) {
    const b = document.createElement("button");
    b.className = cls; b.textContent = label;
    b.addEventListener("click", () => send({ type: "control", action }));
    row.append(b);
  }
  el.append(p, row);
  sendShapeSoon();
}

function confirmResolved(ev) {
  if (ev.id === pendingId) { pendingId = null; removeSection("confirm"); captionSoon(); }
}

// ---------------------------------------------------------------- buttons and the typing box

document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => send({ type: "control", action: b.dataset.action }));
});
const speakOn = () => $("speak").getAttribute("aria-pressed") === "true";
function setSpeak(on) {                    // the Voice setting: the speaker icon and the Voice button
  $("speak").setAttribute("aria-pressed", on ? "true" : "false");
  const b = $("voice");
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.textContent = on ? "Voice: on" : "Voice: off";
  b.classList.toggle("on", !on);
}
function setAnnounce(on) {                 // the Announce button shows what JARVIS says it is now
  const b = $("announce");
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.textContent = on ? "Announce: on" : "Announce: off";
  b.classList.toggle("on", !on);
}
$("speak").addEventListener("click", () => send({ type: "control", action: "voice" }));   // JARVIS answers with the new state
$("talk").addEventListener("click", () => send({ type: "control", action: "talk" }));
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (send({ type: "chat", text, speak: speakOn() })) $("input").value = "";
  else setStatus("Not connected: your message is still in the box.");
});
addEventListener("keydown", (e) => { if (e.key === "Escape" && view !== "closed") setView("closed"); });

// ---------------------------------------------------------------- Customize: the orb's size and brightness

// The sliders show a value now and keep it a moment later: the window resizes and the setting is saved
// (data/orb_look.json) once the slider stops moving.
const LOOK_PERCENT = new Set(["core_size", "orb_brightness", "cloud_brightness", "ribbon_brightness"]);
let sizeTimer = null, saveTimer = null;
function applyLook(changed) {
  if (orb && orb.setBrightness) orb.setBrightness(look.orb_brightness, look.cloud_brightness, look.ribbon_brightness);
  if (orb && orb.setCoreSize) orb.setCoreSize(look.core_size);
  for (const input of document.querySelectorAll("#look input")) {
    const key = input.dataset.key, value = look[key];
    input.value = LOOK_PERCENT.has(key) ? Math.round(value * 100) : value;
    input.nextElementSibling.textContent = LOOK_PERCENT.has(key) ? Math.round(value * 100) + "%" : value + " px";
  }
  const canvas = Math.round(look.orb_size / 0.68);   // ORB_SCALE in ui/window.py
  if (canvas !== place.canvas) {
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(() => {
      if (preview) { place.canvas = canvas; sizeOrb(); layout(); } else toWindow("size", { size: look.orb_size });
    }, changed ? 120 : 0);
  }
  if (changed) { clearTimeout(saveTimer); saveTimer = setTimeout(() => send({ type: "look", look }), 500); }
}
for (const input of document.querySelectorAll("#look input")) {
  input.addEventListener("input", () => {
    const key = input.dataset.key;
    look[key] = LOOK_PERCENT.has(key) ? input.value / 100 : Number(input.value);
    applyLook(true);
  });
}
function showLook(open) {
  $("look").hidden = !open;
  $("customize").setAttribute("aria-expanded", open ? "true" : "false");
  $("customize").classList.toggle("open", open);
  if (open) animateIn($("look"));
  layout();
}
$("customize").addEventListener("click", () => showLook($("look").hidden));
$("lookDone").addEventListener("click", () => showLook(false));
$("lookReset").addEventListener("click", () => { look = { ...lookDefaults }; applyLook(true); });

// ---------------------------------------------------------------- the orb: tap, double-tap, drag

let tapTimer = null, down = null;
const orbRadius = () => place.canvas * 0.36;
const onOrb = (e) => Math.hypot(e.clientX - (place.x || innerWidth / 2), e.clientY - (place.y || innerHeight / 2)) <= orbRadius();
$("orb").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !onOrb(e)) return;
  down = { x: e.clientX, y: e.clientY, dragged: false };
});
addEventListener("mousemove", (e) => {
  if (down && !down.dragged && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) {
    down.dragged = true;
    toWindow("drag");                          // the window moves with the mouse until it's released
  }
});
addEventListener("mouseup", (e) => {
  if (!down) return;
  const wasDrag = down.dragged;
  down = null;
  if (wasDrag || !onOrb(e)) return;
  if (tapTimer) {                              // double-tap: open or close the control centre
    clearTimeout(tapTimer); tapTimer = null;
    setView(view === "full" ? "closed" : "full");
  } else {
    tapTimer = setTimeout(() => { tapTimer = null; send({ type: "control", action: "talk" }); }, 280);
  }
});

// ---------------------------------------------------------------- the click-through shape

// The window lets clicks through everywhere except these shapes (ui/window.py checks the mouse against them).
let lastShape = "";
function sendShape() {
  const cx = Math.round(place.x || innerWidth / 2), cy = Math.round(place.y || innerHeight / 2);
  const shapes = [{ c: [cx, cy], r: Math.round(orbRadius()) }];
  if (place.expanded) {
    for (const el of document.querySelectorAll("#controls, #look, #composer, #quick .chip, #sections .card")) {
      const b = el.getBoundingClientRect();
      if (b.width && b.height && el.offsetParent !== null) shapes.push({ x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) });
    }
  }
  const json = JSON.stringify(shapes);
  if (json !== lastShape) { lastShape = json; toWindow("shape", shapes); }
}
let shapeTimer = null;
function sendShapeSoon() { clearTimeout(shapeTimer); shapeTimer = setTimeout(sendShape, 420); }   // after transitions
setInterval(sendShape, 1000);
addEventListener("resize", layout);

// ---------------------------------------------------------------- drawing

let orb = null, orbPixels = 0, orbMaking = null;
async function makeOrb(canvas) {
  if (params.get("orb") === "2d") return createOrb(canvas);   // #orb=2d: the 2D orb, for comparison
  try {
    const { createOrb3D } = await import("/static/orb3d.js");
    const o = await createOrb3D(canvas);
    if (o) return o;
  } catch (err) {
    console.warn("the 3D orb didn't load; using the 2D one", err);
  }
  return createOrb(canvas);
}
function sizeOrb() {
  const c = $("orb"), css = place.canvas, px = Math.round(css * (window.devicePixelRatio || 1));
  c.style.width = c.style.height = css + "px";
  if (px === orbPixels || orbMaking) return;
  orbPixels = px;
  if (orb) {                                  // a new size: the 3D orb fits its canvas itself, the 2D one is redrawn
    if (!orb.is3d) c.width = c.height = px;
    return;
  }
  if (!orb) c.width = c.height = px;          // the 3D orb sizes its own canvas
  orbMaking = makeOrb(c).then((o) => {
    orb = o; orbMaking = null;
    applyLook(false);
    setState(state);
  });
}

const t0 = performance.now();
let lastFrame = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const t = (now - t0) / 1000, dt = t - lastFrame;
  const calm = orbState.act < 0.3 && orbState.actTarget < 0.3;
  if (!orb || document.hidden || (calm && dt < 1 / 30)) return;     // idle: 30 frames a second is plenty
  lastFrame = t;
  orbState.act += (orbState.actTarget - orbState.act) * Math.min(1, dt * 3);
  orbState.level += (orbState.levelTarget - orbState.level) * Math.min(1, dt * 12);
  const lv = state === "speaking" || state === "listening" ? orbState.level : 0;
  orb.draw(t, orbState.act, lv, dt);
}

if (preview && params.get("open") === "1") setView("full");
else if (preview) window.jarvisPlace({ expanded: false, x: innerWidth / 2, y: innerHeight / 2 });
else sizeOrb();
layout();
requestAnimationFrame(frame);
connect();
