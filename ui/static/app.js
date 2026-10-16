// JARVIS's window: just the orb and the typing box under it. While talking, only the sections a reply
// needs (the answer, a confirmation, what JARVIS is doing, a picture, what's playing) appear around the
// orb at equal clock positions. They stay until the orb is double-clicked or JARVIS goes to sleep.
// Clicking the orb opens the full panel (controls, the whole conversation, activity); click again to
// fold it away. Drag the orb to move the window.
//
// Every text is set with textContent, never as HTML: replies and tool results can contain text from web
// pages and apps. The token comes from the URL's #fragment. Messages for the window itself (its
// click-through shape, dragging, coming to the front) go out as console lines starting "jarvis:".
"use strict";

const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("t") || "";
if (params.get("bg") === "1") document.body.classList.add("backdrop");
const $ = (id) => document.getElementById(id);

const ORB_SIZE = 400;                 // CSS pixels of the orb canvas
const ORB_HIT = 150;                  // radius around the orb's centre that takes clicks
const MAX_SECTIONS = 6;
const MAX_HISTORY = 200;
const ACT = { starting: 0, asleep: 0, listening: 0.55, speaking: 1, working: 0.75, muted: 0.15 };

let ws = null, retryMs = 500, pendingId = null, state = "starting";
let panelOpen = false;
const view = { cx: 0, cy: 0 };
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

// ---------------------------------------------------------------- events

function handle(ev) {
  switch (ev.kind) {
    case "hello": return hello(ev);
    case "level": setState(ev.state); orbState.levelTarget = ev.level || 0; return;
    case "state": return setState(ev.state);
    case "partial": return partial(ev);
    case "message": return message(ev);
    case "tool_started": return toolStarted(ev);
    case "tool_finished": return toolFinished(ev);
    case "confirm_requested": return confirmRequested(ev);
    case "confirm_resolved": return confirmResolved(ev);
    case "media": return media(ev);
    case "status": return setStatus(ev.text);
    case "focus": return toWindow("front");
  }
}

function hello(ev) {
  const snap = ev.snapshot || {};
  clearSections(true);
  $("history").replaceChildren();
  $("activity").replaceChildren();
  pendingId = null;
  setSpeak(snap.speak !== false);
  state = snap.state || "starting";
  setState(state);
  replaying = true;
  for (const old of ev.history || []) {           // history fills the full panel, not the sections
    if (old.kind === "message") addHistory(old);
    else if (old.kind === "tool_started" || old.kind === "tool_finished") activityItem(old);
  }
  replaying = false;
  if (snap.pending) confirmRequested({ id: snap.pending.id, summary: snap.pending.summary });
}
let replaying = false;

function setState(next) {
  if (!next || next === state && orbState.actTarget === (ACT[next] ?? 0.55)) return;
  const wasAwake = state !== "asleep" && state !== "starting";
  state = next;
  orbState.actTarget = ACT[next] ?? 0.55;
  orb.setTint(next === "muted" ? "red" : next === "working" ? "amber" : "blue");
  $("mute").classList.toggle("on", next === "muted");
  $("mute").textContent = next === "muted" ? "Unmute" : "Mute";
  if (next === "asleep" && wasAwake) { clearSections(); setPanel(false); }   // "go to sleep" folds everything away
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
    el.className = `card enter ${cls || ""}`;
    const h = document.createElement("h2");
    h.textContent = title;
    el.append(h);
    $("sections").append(el);
    sections.set(key, el);
    while (sections.size > MAX_SECTIONS) removeSection(sections.keys().next().value);
    layout();
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("enter")));
  }
  return el;
}

function removeSection(key) {
  const el = sections.get(key);
  if (!el) return;
  sections.delete(key);
  el.classList.add("leave");
  setTimeout(() => el.remove(), 350);
  layout();
}

function clearSections(now) {
  for (const key of [...sections.keys()]) {
    if (now) { sections.get(key).remove(); sections.delete(key); } else removeSection(key);
  }
  layout();
}

// equal clock positions over the arc from 8 to 4 o'clock (through 12); the typing box sits at 6
function clockAngles(n) { return Array.from({ length: n }, (_, i) => -120 + (240 * (i + 0.5)) / n); }

function layout() {
  const w = innerWidth, h = innerHeight;
  view.cx = w / 2;
  view.cy = Math.round(Math.min(h * 0.42, h - 260));
  const orbEl = $("orb");
  orbEl.style.left = view.cx + "px"; orbEl.style.top = view.cy + "px";
  $("composer").style.left = view.cx + "px"; $("composer").style.top = view.cy + ORB_SIZE * 0.4 + "px";
  $("status").style.left = view.cx + "px"; $("status").style.top = view.cy + ORB_SIZE * 0.4 + 52 + "px";
  const rx = Math.min(390, w / 2 - 150), ry = Math.min(250, view.cy - 90);
  const keys = [...sections.keys()];
  clockAngles(keys.length).forEach((deg, i) => {
    const el = sections.get(keys[i]), r = (deg * Math.PI) / 180;
    el.style.left = Math.max(145, Math.min(w - 145, view.cx + rx * Math.sin(r))) + "px";
    el.style.top = Math.max(100, view.cy - ry * Math.cos(r)) + "px";
  });
  // the full panel: controls orbit the orb's top, history left, activity right, vision on top
  const ring = [...document.querySelectorAll(".ring-buttons .round")];
  [-52, -18, 18, 52].forEach((deg, i) => {
    const r = (deg * Math.PI) / 180;
    ring[i].style.left = view.cx + 205 * Math.sin(r) + "px"; ring[i].style.top = view.cy - 205 * Math.cos(r) + "px";
  });
  const side = Math.min(440, w / 2 - 160);
  place($("history-card"), view.cx - side, view.cy + 20);
  place($("activity-card"), view.cx + side, view.cy + 20);
  place($("vision-card"), view.cx, Math.max(60, view.cy - 290));
  sendShape();
}
function place(el, x, y) { el.style.left = x + "px"; el.style.top = y + "px"; }

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
    const el = answerSection();
    el.querySelector(".you").textContent = "You: " + liveUser;
  } else {
    liveReply = (liveReply + ev.text).replace(/\s+/g, " ").trimStart();
    answerSection().querySelector(".reply").textContent = liveReply;
  }
  sendShapeSoon();
}

function message(ev) {
  addHistory(ev);
  if (ev.role === "user") {
    liveUser = ""; liveReply = "";
    const el = answerSection();
    el.querySelector(".you").textContent = "You: " + ev.text;
    el.querySelector(".reply").textContent = "";
  } else {
    liveReply = "";
    answerSection().querySelector(".reply").textContent = ev.text;
  }
  sendShapeSoon();
}

function addHistory(ev) {
  const div = document.createElement("div");
  div.className = "msg " + (ev.role === "user" ? "user" : "jarvis");
  const meta = document.createElement("span");
  meta.className = "meta";
  const how = ev.role === "user" && ev.channel ? ` · ${ev.channel === "chat" ? "typed" : "spoken"}` : "";
  meta.textContent = (ev.label || (ev.role === "user" ? "You" : "JARVIS")) + how;
  div.append(meta, document.createTextNode(ev.text));
  const box = $("history");
  box.append(div);
  while (box.children.length > MAX_HISTORY) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- what JARVIS is doing

const PLAY_TOOLS = new Set(["play_music", "play_youtube", "now_playing", "media_key"]);

function plain(text) {
  return String(text || "").replace(/<\/?untrusted_content[^>]*>/g, "").trim();
}

function describeArgs(args) {
  return Object.entries(args || {}).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ").slice(0, 140);
}

function stepItem(list, cls, name, detail, max) {
  const li = document.createElement("li");
  li.className = cls;
  li.textContent = name;
  const d = document.createElement("span");
  d.className = "detail";
  d.textContent = detail || "";
  li.append(d);
  list.prepend(li);
  while (list.children.length > max) list.lastChild.remove();
  return li;
}

function activityItem(ev) {           // the full panel's activity list
  const list = $("activity");
  if (ev.kind === "tool_started") {
    stepItem(list, "running", (ev.step ? "plan step · " : "") + ev.tool, describeArgs(ev.args), 100).dataset.tool = ev.tool;
  } else {
    const li = [...list.querySelectorAll("li.running")].find((x) => x.dataset.tool === ev.tool);
    const result = plain(ev.result);
    const cls = ev.paused ? "wait" : /^(That didn't work|Couldn't|I couldn't|Stopped\.)/.test(result) ? "failed" : "done";
    if (li) { li.className = cls; li.querySelector(".detail").textContent = result.slice(0, 240); }
    else stepItem(list, cls, ev.tool, result.slice(0, 240), 100);
  }
}

function toolStarted(ev) {
  activityItem(ev);
  if (replaying) return;
  const el = section("doing", "Working on it");
  let list = el.querySelector(".steps");
  if (!list) { list = document.createElement("ol"); list.className = "steps"; el.append(list); }
  stepItem(list, "running", (ev.step ? "plan step · " : "") + ev.tool, describeArgs(ev.args), 4).dataset.tool = ev.tool;
  sendShapeSoon();
}

function toolFinished(ev) {
  activityItem(ev);
  if (replaying) return;
  const result = plain(ev.result);
  const el = sections.get("doing");
  const li = el && [...el.querySelectorAll("li.running")].find((x) => x.dataset.tool === ev.tool);
  if (li) {
    li.className = ev.paused ? "wait" : /^(That didn't work|Couldn't|I couldn't|Stopped\.)/.test(result) ? "failed" : "done";
    li.querySelector(".detail").textContent = result.slice(0, 140);
  }
  if (PLAY_TOOLS.has(ev.tool) && result && !ev.paused) {
    const card = section("playing", "Now playing");
    card.replaceChildren(card.firstChild);
    const p = document.createElement("p");
    const icon = document.createElement("span"); icon.className = "play";
    p.append(icon, document.createTextNode(result.slice(0, 200)));
    card.append(p);
  }
  sendShapeSoon();
}

function media(ev) {                   // a picture a tool showed (e.g. the screen JARVIS just looked at)
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
  if (ev.id === pendingId) { pendingId = null; removeSection("confirm"); }
}

// ---------------------------------------------------------------- the full panel

function setPanel(open) {
  panelOpen = open;
  $("panel").hidden = !open;
  $("sections").style.display = open ? "none" : "";
  sendShape();
}

document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => send({ type: "control", action: b.dataset.action }));
});

// ---------------------------------------------------------------- the typing box

function setSpeak(on) { $("speak").setAttribute("aria-pressed", on ? "true" : "false"); }
$("speak").addEventListener("click", () => setSpeak($("speak").getAttribute("aria-pressed") !== "true"));
$("talk").addEventListener("click", () => send({ type: "control", action: "talk" }));
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (send({ type: "chat", text, speak: $("speak").getAttribute("aria-pressed") === "true" })) $("input").value = "";
  else setStatus("Not connected: your message is still in the box.");
});

// ---------------------------------------------------------------- the orb: click, double-click, drag

let clickTimer = null, down = null;
const onOrb = (e) => Math.hypot(e.clientX - view.cx, e.clientY - view.cy) <= ORB_HIT;
$("orb").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !onOrb(e)) return;
  down = { x: e.clientX, y: e.clientY, dragged: false };
});
addEventListener("mousemove", (e) => {
  if (down && !down.dragged && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) {
    down.dragged = true;
    toWindow("drag");                       // the window moves with the mouse until it's released
  }
});
addEventListener("mouseup", (e) => {
  if (!down) return;
  const wasDrag = down.dragged;
  down = null;
  if (wasDrag || !onOrb(e)) return;
  if (clickTimer) {                           // second click: double-click clears the sections
    clearTimeout(clickTimer); clickTimer = null;
    clearSections(); setPanel(false);
  } else {
    clickTimer = setTimeout(() => { clickTimer = null; setPanel(!panelOpen); }, 260);
  }
});

// ---------------------------------------------------------------- the click-through shape

// The window lets clicks through everywhere except these shapes (ui/window.py checks the mouse against them).
let lastShape = "";
function sendShape() {
  const shapes = [{ c: [Math.round(view.cx), Math.round(view.cy)], r: ORB_HIT }];
  const boxes = [$("composer"), ...(panelOpen ? document.querySelectorAll("#panel .card, #panel .round")
    : document.querySelectorAll("#sections .card:not(.leave)"))];
  for (const el of boxes) {
    const b = el.getBoundingClientRect();
    if (b.width && b.height) shapes.push({ x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) });
  }
  const json = JSON.stringify(shapes);
  if (json !== lastShape) { lastShape = json; toWindow("shape", shapes); }
}
let shapeTimer = null;
function sendShapeSoon() { clearTimeout(shapeTimer); shapeTimer = setTimeout(sendShape, 420); }   // after transitions
setInterval(sendShape, 1000);
addEventListener("resize", layout);

// ---------------------------------------------------------------- drawing

const orb = createOrb($("orb"));
const t0 = performance.now();
let lastFrame = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const t = (now - t0) / 1000, dt = t - lastFrame;
  const calm = orbState.act < 0.3 && orbState.actTarget < 0.3;
  if (document.hidden || (calm && dt < 1 / 30)) return;     // idle: 30 frames a second is plenty
  lastFrame = t;
  orbState.act += (orbState.actTarget - orbState.act) * Math.min(1, dt * 3);
  orbState.level += (orbState.levelTarget - orbState.level) * Math.min(1, dt * 12);
  const lv = state === "speaking" || state === "listening" ? orbState.level : 0;
  orb.draw(t, orbState.act, lv, dt);
}

layout();
requestAnimationFrame(frame);
connect();
