// JARVIS's orb. Hover over it (or tap it) and its menu blooms out around it: Chat, Command centre, Agent Hub,
// Dashboards (a second ring: PMO, JobPilot, from web_ui.dashboards) and Settings; click an item or flick
// toward it (press the orb, drag a little toward the item, let go). Hold it to talk, double-tap to mute or
// unmute the microphone, drag it further to move it. Its colour shows the state: blue listening (still when
// idle, asleep), violet thinking, amber working on a task, grey with a mic-off badge muted, red when
// something JARVIS needs isn't working (the reason shows under it); it moves with JARVIS's voice when it
// speaks. Docked in the full-screen app it's the same orb, smaller: the same states and gestures, but it
// stays in the app's dot when dragged, and replies, pictures and lists show only in the app's chat.
// The older control centre below (opened only with #open=1 now) is kept for previews:
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
const ACT = { starting: 0, asleep: 0, listening: 0.55, thinking: 0.8, speaking: 1, working: 0.75, muted: 0.15 };
const STATE_WORDS = { starting: "starting", asleep: "idle", listening: "listening", thinking: "thinking",
  speaking: "speaking", working: "working on a task", muted: "microphone muted" };

let ws = null, retryMs = 500, pendingId = null, state = "starting";
let view = "closed", captionMs = 8000;
let look = { orb_size: 112, core_size: 0.83, orb_brightness: 0.55, cloud_brightness: 0.45, ribbon_brightness: 0.5 }, lookDefaults = { ...look };
const place = { expanded: false, ring: false, x: 0, y: 0, canvas: 235 };
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
  document.body.classList.toggle("menu", next === "menu");
  showMenu(next === "menu");
  clearTimeout(captionTimer);
  if (next === "closed") { if (!$("look").hidden) showLook(false); return setExpanded(false); }
  setExpanded(true, next === "menu");           // the window grows (a square for the menu); jarvisPlace() lays it out
  if (prev !== "closed" && next === "full") { layout(); [$("controls"), $("composer"), $("quick")].forEach((el, i) => animateIn(el, i * 45)); }
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
    if (pendingId || activityText || state === "speaking" || state === "working") return captionSoon();
    const panel = $("panel"), started = performance.now();
    animate(300, 0, (e) => { panel.style.opacity = 1 - e; }, () => {
      panel.style.opacity = "";
      if (view !== "voice") return;
      if (touched > started) return captionSoon();   // something new came while it faded
      setView("closed");
    });
  }, sections.has("list") ? Math.max(captionMs, LIST_HOLD_MS) : captionMs);   // a list needs time to read
}

function setExpanded(expanded, ring = false) {
  ring = expanded && ring;
  if (expanded === place.expanded && ring === !!place.ring) return;
  if (!expanded) clearSections(true);
  if (preview) {                                // no window around the page: lay it out here
    window.jarvisPlace({ expanded, ring, x: innerWidth / 2, y: expanded && !ring ? place.canvas / 2 + 6 : innerHeight / 2 });
  } else {
    toWindow("mode", { expanded, ring });
  }
}

// ---------------------------------------------------------------- events

function handle(ev) {
  switch (ev.kind) {
    case "hello": return hello(ev);
    case "activity": return activity(ev.text, ev.what);
    case "level": setProblem(ev.problem); setState(ev.state); orbState.levelTarget = ev.level || 0; return;
    case "orb_visibility": toWindow(ev.visible ? "show" : "hide"); return;
    case "orb_dock": return dock(ev);
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
    case "look": look = { ...look, ...(ev.look || {}) }; return applyLook(true);   // changed in the app's Settings
    case "status": return setStatus(ev.text);
    case "focus": if (!ev.target) toWindow("front"); return;
  }
}

// The app is full screen: the orb sits in the dot at its top right (the same orb, smaller), or floats again
// where it was. Docking closes anything open around the orb first.
let docked = false;
function dock(where) {
  docked = !!(where && where.docked);
  if (docked) setView("closed");
  showState();
  toWindow("dock", where && where.docked ? { docked: true, x: where.x, y: where.y, size: where.size, hwnd: where.hwnd }
                                          : { docked: false });
}

function hello(ev) {
  const snap = ev.snapshot || {};
  activity(snap.activity || "", snap.activity_kind);
  setProblem(snap.problem);
  if (snap.orb_visible === false) toWindow("hide");
  dock(snap.orb_dock ? { docked: true, ...snap.orb_dock } : { docked: false });
  setSpeak((snap.voice ?? snap.speak) !== false);
  setAnnounce(snap.announce !== false);
  if (snap.caption_s) captionMs = snap.caption_s * 1000;
  if (snap.look) look = { ...look, ...snap.look };
  if (snap.look_defaults) lookDefaults = { ...lookDefaults, ...snap.look_defaults };
  applyLook(false);
  setState(snap.state || "starting");
  setDashboards(snap.dashboards || []);
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

let problem = null;
function tint() {   // muted grey, thinking violet, working amber, a problem red, otherwise blue
  if (!orb) return;
  const s = shownState();
  orb.setTint(s === "muted" ? "muted" : s === "thinking" ? "thinking" : s === "working" ? "amber" : problem ? "error" : "blue");
}

// What the orb shows: muted, speaking, thinking (an answer or a tool on its way), working (a task), listening,
// or idle (asleep). JARVIS's own state, plus what it's doing right now.
function shownState() {
  if (state === "muted" || state === "speaking") return state;
  if (activityText && activityKind !== "background") return "thinking";
  return state;
}
function showState() {
  const s = shownState();
  orbState.actTarget = ACT[s] ?? 0.55;
  tint();
  $("mutedBadge").hidden = s !== "muted";
  $("orb").dataset.state = s;
  $("orb").setAttribute("aria-label", `JARVIS: ${STATE_WORDS[s] || s}. Tap for the menu, hold to talk, double-tap to ${
    s === "muted" ? "unmute" : "mute"}${docked ? "" : ", drag to move"}.`);
}
function setProblem(text) {
  const next = text || null;
  if (next === problem) return;
  problem = next;
  const el = $("problem");
  el.textContent = problem || "";
  el.hidden = !problem || view === "closed";
  $("orb").title = problem ? "JARVIS: " + problem : "";
  tint();
  layout();
}

function setState(next) {
  if (!next) return;
  const wasAwake = state !== "asleep" && state !== "starting", changed = next !== state;
  state = next;
  showState();
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
  // Docked in the app, the app's chat shows every reply, picture and list: the orb doesn't repeat them
  // (Krishna, 28 Oct: the same text in two places). Whatever is written to this goes nowhere.
  if (docked) return document.createElement("section");
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
  const badge = $("mutedBadge"), bs = Math.max(18, Math.round(orbRadius() * 1.1));
  badge.style.left = cx + "px"; badge.style.top = cy + "px";
  badge.style.width = badge.style.height = bs + "px";
  const panel = $("panel");
  panel.style.left = cx + "px";
  panel.style.top = cy + place.canvas * 0.36 + "px";          // just under the orb's bands
  placeMenu(cx, cy);
  const prob = $("problem");
  prob.style.left = cx + "px";
  prob.style.top = cy + place.canvas * 0.36 + (view === "menu" ? orbRadius() * 0.55 + 70 : 4) + "px";
  prob.hidden = !problem || view === "closed";
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

// What JARVIS is doing right now ("Searching the web…"): a small line at the top of the sections, so a pause
// never looks like nothing is happening. The dot pulses on animation frames (the see-through window doesn't
// run CSS animations).
let activityText = "", activityKind = "", activityDot = null;
function activity(text, what) {
  activityText = text || "";
  activityKind = activityText ? what || "thinking" : "";
  showState();
  if (!activityText) { activityDot = null; removeSection("status"); return; }
  const card = section("status", "", "status");
  if (card.parentElement && card.parentElement.firstChild !== card) card.parentElement.prepend(card);
  card.replaceChildren(card.firstChild);
  const dot = document.createElement("span");
  dot.className = "status-dot";
  const words = document.createElement("span");
  words.textContent = activityText;                        // written by JARVIS's code, still set as text
  card.append(dot, words);
  card.setAttribute("role", "status");
  if (!activityDot) requestAnimationFrame(function pulse(now) {
    if (activityDot !== dot || !dot.isConnected) return;
    dot.style.opacity = String(0.35 + 0.65 * (0.5 + 0.5 * Math.sin(now / 220)));
    requestAnimationFrame(pulse);
  });
  activityDot = dot;
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

// ---------------------------------------------------------------- the orb: hover, tap, double-tap, hold, flick, drag
//
// Hover: the menu blooms out around the orb. Tap: open or close it. Double-tap: mute or unmute. Hold: talk.
// With the menu open, press the orb and drag a little toward an item, then let go: that's a flick, and the item
// opens. Dragged past the menu (or before it bloomed), the orb moves; docked in the app it stays in its dot.
// ui/window.py says when the mouse comes over the orb or its menu and when it leaves (jarvisHover): outside
// them the window lets clicks through and the page gets no mouse events.

let tapTimer = null, holdTimer = null, down = null, bloomTimer = null, leaveTimer = null;
let hovering = false, hoverQuiet = false;     // quiet: closed by hand while the mouse is still on the orb
const HOLD_MS = 550, DOUBLE_MS = 280, BLOOM_MS = 140, LEAVE_MS = 650, DRAG_PX = 5;
const orbRadius = () => place.canvas * 0.36;
const orbCentre = () => [place.x || innerWidth / 2, place.y || innerHeight / 2];
const onOrb = (e) => Math.hypot(e.clientX - orbCentre()[0], e.clientY - orbCentre()[1]) <= orbRadius();

function bloomSoon() {
  if (bloomTimer || view !== "closed" || hoverQuiet || down) return;
  bloomTimer = setTimeout(() => {
    bloomTimer = null;
    if (view === "closed" && !hoverQuiet && !down && hovering) setView("menu");
  }, BLOOM_MS);
}
function hoverIn() {
  hovering = true;
  clearTimeout(leaveTimer);
  clearTimeout(menuTimer);
}
function hoverOut() {
  hovering = false;
  hoverQuiet = false;
  clearTimeout(bloomTimer); bloomTimer = null;
  clearTimeout(leaveTimer);
  if (view === "menu") leaveTimer = setTimeout(() => { if (view === "menu" && !down && !hovering) setView("closed"); }, LEAVE_MS);
}
window.jarvisHover = function (over) { if (over) hoverIn(); else hoverOut(); };

$("orb").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !onOrb(e)) return;
  clearTimeout(bloomTimer); bloomTimer = null;
  down = { x: e.clientX, y: e.clientY, sx: e.screenX, sy: e.screenY, dragged: false, held: false, moving: false,
           menu: view === "menu" };
  clearTimeout(holdTimer);
  holdTimer = setTimeout(() => {               // hold: talk
    if (!down || down.dragged) return;
    down.held = true;
    clearTimeout(tapTimer); tapTimer = null;
    if (view === "menu") setView("closed");
    send({ type: "control", action: "talk" });
  }, HOLD_MS);
});
addEventListener("mousedown", (e) => {        // a click in the menu's empty space folds it away
  if (view === "menu" && e.button === 0 && !onOrb(e) && !e.target.closest("button")) { hoverQuiet = true; setView("closed"); }
});
addEventListener("mousemove", (e) => {
  if (!down) {                                 // just the mouse going by
    const [cx, cy] = orbCentre(), d = Math.hypot(e.clientX - cx, e.clientY - cy);
    if (view === "menu") { if (d <= bloomReach()) hoverIn(); else if (hovering) hoverOut(); return; }
    if (onOrb(e)) { hoverIn(); bloomSoon(); } else if (hovering && view === "closed") hoverOut();
    return;
  }
  const d = Math.hypot(e.clientX - down.x, e.clientY - down.y);
  if (!down.dragged && d > DRAG_PX) {
    down.dragged = true;
    clearTimeout(holdTimer);
    clearTimeout(tapTimer); tapTimer = null;
    if (!down.menu) return startMove(e);       // no menu open: a drag moves the orb straight away
  }
  if (!down.dragged || down.moving) return;
  if (d > flickReach()) return startMove(e);   // past the menu: moving the orb after all
  aimAt(flickTarget(e.clientX - down.x, e.clientY - down.y));
});
function startMove(e) {
  down.moving = true;
  aimAt(null);
  if (docked) return;                          // docked: it stays in the app's dot
  if (!down.menu) return toWindow("drag");     // the window moves with the mouse until it's released
  setView("closed");                           // back to just the orb, then under the mouse again
  toWindow("drag", { dx: Math.round(e.screenX - down.sx), dy: Math.round(e.screenY - down.sy) });
}
addEventListener("mouseup", (e) => {
  clearTimeout(holdTimer);
  if (!down) return;
  const g = down;
  down = null;
  aimAt(null);
  if (g.held || g.moving) return;
  if (g.dragged) {                             // a flick
    if (view === "menu") {
      const target = flickTarget(e.clientX - g.x, e.clientY - g.y);
      if (target) choose(target);
      else if (subOpen) showSub(false);        // flicked away from the dashboards: back to the main ring
    }
    return;
  }
  if (!onOrb(e)) return;
  if (tapTimer) {                              // double-tap: mute or unmute the microphone
    clearTimeout(tapTimer); tapTimer = null;
    send({ type: "control", action: "mute" });
  } else {                                     // tap: open or close the menu (or close the old control centre)
    tapTimer = setTimeout(() => {
      tapTimer = null;
      const open = view === "menu" || view === "full";
      if (open) hoverQuiet = hovering;         // closed by hand: it doesn't bloom again until the mouse leaves
      setView(open ? "closed" : "menu");
    }, DOUBLE_MS);
  }
});

// ---------------------------------------------------------------- the radial menu

const MENU_CLOSE_MS = 8000;                    // opened by a tap and not hovered: it folds away after this
const ITEM_R = 29, ITEM_GAP = 66;              // a button's radius; the least distance between two buttons' centres
let menuTimer = null, subOpen = false, spots = [], subSpots = [];
function showMenu(on) {
  const menu = $("radial");
  clearTimeout(menuTimer);
  menu.hidden = !on;
  if (!on) { showSub(false, true); return; }
  layout();
  spots.forEach((s, i) => animateIn(s.el, i * 35));
  if (!hovering) menuTimer = setTimeout(() => { if (view === "menu" && !hovering) setView("closed"); }, MENU_CLOSE_MS);
}
function showSub(on, quiet) {
  subOpen = !!on && dashboardList.length > 0;
  $("subring").hidden = !subOpen;
  const dash = $("radial").querySelector('[data-go="dashboards"]');
  dash.setAttribute("aria-expanded", subOpen ? "true" : "false");
  if (quiet) return;
  layout();
  if (subOpen) subSpots.forEach((s, i) => animateIn(s.el, i * 35));
  sendShapeSoon();
}

// The directions (degrees, clockwise from the right) where a button `r` from the orb stays inside the window:
// all of them (a full ring, from the top), else the biggest unbroken stretch, near an edge or a corner.
function freeArc(cx, cy, r) {
  const fits = (a) => {
    const x = cx + r * Math.cos(a * Math.PI / 180), y = cy + r * Math.sin(a * Math.PI / 180);
    return x >= ITEM_R + 2 && x <= innerWidth - ITEM_R - 2 && y >= ITEM_R + 2 && y <= innerHeight - ITEM_R - 2;
  };
  const ok = Array.from({ length: 72 }, (_, i) => fits(i * 5));
  if (ok.every(Boolean)) return { full: true };
  let best = { start: 0, len: 0 };
  for (let i = 0; i < 72; i++) {
    if (!ok[i] || ok[(i + 71) % 72]) continue;           // the start of a stretch
    let len = 0;
    while (len < 72 && ok[(i + len) % 72]) len++;
    if (len > best.len) best = { start: i, len };
  }
  return best.len ? { full: false, from: best.start * 5, span: (best.len - 1) * 5 } : null;
}
function spread(n, arc) {
  if (arc.full) return Array.from({ length: n }, (_, i) => -90 + i * 360 / n);
  if (n === 1) return [arc.from + arc.span / 2];
  return Array.from({ length: n }, (_, i) => arc.from + arc.span * i / (n - 1));
}
function placeAt(el, cx, cy, r, deg) {
  const a = deg * Math.PI / 180, x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
  el.style.left = Math.round(x) + "px"; el.style.top = Math.round(y) + "px";
  return { el, go: el.dataset.go, id: el.dataset.id, x, y, deg, r };
}
function placeMenu(cx, cy) {
  const items = [...$("radial").children].filter((el) => !el.hidden);
  let r = orbRadius() + 44, arc = freeArc(cx, cy, r);
  for (let tries = 0; tries < 30 && arc && !arc.full; tries++) {   // near an edge: further out until they fit apart
    if (r * (arc.span * Math.PI / 180) / Math.max(1, items.length - 1) >= ITEM_GAP) break;
    const wider = freeArc(cx, cy, r + 8);
    if (!wider) break;
    r += 8; arc = wider;
  }
  const angles = spread(items.length, arc || { full: true });
  spots = items.map((el, i) => placeAt(el, cx, cy, r, angles[i]));
  placeSub(cx, cy, r);
}
// The Dashboards ring: just outside Dashboards, fanned around its direction (turned toward the side with room).
function placeSub(cx, cy, r) {
  const items = [...$("subring").children], dash = spots.find((s) => s.go === "dashboards");
  if (!dash || !items.length) { subSpots = []; return; }
  const r2 = r + ITEM_GAP + 4, step = (ITEM_GAP / r2) * 180 / Math.PI;
  const at = (turn) => items.map((_, i) => dash.deg + turn + (i - (items.length - 1) / 2) * step);
  const fits = (degs) => degs.every((d) => {
    const x = cx + r2 * Math.cos(d * Math.PI / 180), y = cy + r2 * Math.sin(d * Math.PI / 180);
    return x >= ITEM_R && x <= innerWidth - ITEM_R && y >= ITEM_R && y <= innerHeight - ITEM_R;
  });
  let degs = at(0);
  for (let t = 5; t <= 120 && !fits(degs); t += 5) {
    if (fits(at(t))) { degs = at(t); break; }
    if (fits(at(-t))) { degs = at(-t); break; }
  }
  subSpots = items.map((el, i) => placeAt(el, cx, cy, r2, degs[i]));
}
const visibleSpots = () => (subOpen ? [...spots, ...subSpots] : spots);
function bloomReach() {                        // how far the open menu reaches from the orb's centre
  const [cx, cy] = orbCentre();
  return Math.max(orbRadius(), ...visibleSpots().map((s) => Math.hypot(s.x - cx, s.y - cy))) + ITEM_R + 8;
}
const flickReach = () => bloomReach() + 12;    // a drag past this moves the orb
// A flick's item: the one in the direction of the drag (the Dashboards ring while it's open). Within 40°, the
// nearest; none if the drag points between items.
function flickTarget(dx, dy) {
  if (Math.hypot(dx, dy) < 12) return null;
  const want = Math.atan2(dy, dx) * 180 / Math.PI;
  const choices = subOpen && subSpots.length ? subSpots : spots;
  let best = null, bestOff = 41;
  for (const s of choices) {
    const off = Math.abs(((s.deg - want) % 360 + 540) % 360 - 180);
    if (off < bestOff) { best = s; bestOff = off; }
  }
  return best;
}
function aimAt(target) {
  for (const s of [...spots, ...subSpots]) s.el.classList.toggle("aim", !!target && s.el === target.el);
}
function choose(s) {
  if (s.go === "dashboards") return showSub(!subOpen);
  hoverQuiet = hovering;
  setView("closed");
  if (s.go === "chat") send({ type: "control", action: "open_chat" });
  else if (s.go === "command") send({ type: "control", action: "open_command" });
  else if (s.go === "settings") send({ type: "control", action: "open_settings" });
  else if (s.go === "hub") send({ type: "hub_control", action: "start", id: "orb-hub" });
  else if (s.go === "dashboard") send({ type: "open_dashboard", id: s.id });
}
for (const b of $("radial").children) {
  b.addEventListener("mouseenter", () => { hoverIn(); if (b.dataset.go === "dashboards" && !subOpen) showSub(true); });
  b.addEventListener("click", () => { const s = spots.find((x) => x.el === b); if (s) choose(s); });
}
// web_ui.dashboards, from JARVIS: [{id, label, short}]. Labels are set as text.
let dashboardList = [];
function setDashboards(list) {
  dashboardList = list.filter((d) => d && typeof d.id === "string" && typeof d.label === "string").slice(0, 8);
  const ring = $("subring");
  const model = $("radial").querySelector('[data-go="dashboards"]');   // its icon, copied for each dashboard
  ring.replaceChildren(...dashboardList.map((d) => {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.go = "dashboard"; b.dataset.id = d.id;
    b.title = d.label; b.setAttribute("aria-label", d.label);
    const words = document.createElement("span");
    words.textContent = d.short || d.label;
    b.append(model.querySelector("svg").cloneNode(true), words);
    b.addEventListener("mouseenter", hoverIn);
    b.addEventListener("click", () => { const s = subSpots.find((x) => x.el === b); if (s) choose(s); });
    return b;
  }));
  $("radial").querySelector('[data-go="dashboards"]').hidden = dashboardList.length === 0;
  if (view === "menu") layout();
}

// ---------------------------------------------------------------- the click-through shape

// The window lets clicks through everywhere except these shapes (ui/window.py checks the mouse against them).
let lastShape = "";
function sendShape() {
  const cx = Math.round(place.x || innerWidth / 2), cy = Math.round(place.y || innerHeight / 2);
  const shapes = [{ c: [cx, cy], r: Math.round(orbRadius()) }];
  if (place.expanded && view === "menu") {   // the whole bloom: going from the orb to an item keeps it open
    shapes.push({ c: [cx, cy], r: Math.round(bloomReach()) });
  } else if (place.expanded) {
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
