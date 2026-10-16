// JARVIS's orb: a quick-action control centre. Idle, only the orb is visible. Tap it to talk, drag it
// anywhere, double-tap it to open the control centre around it: buttons (talk, mute, new chat, stop,
// sleep), a typing box under it and quick-action chips. While talking, the sections a reply needs (the
// answer, a confirmation, a picture, what's playing) appear around the orb at equal clock positions;
// they stay until the orb is double-tapped closed or JARVIS goes to sleep. "New chat" opens the complete
// chat page in its own window.
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

const MAX_SECTIONS = 6;
const ACT = { starting: 0, asleep: 0, listening: 0.55, speaking: 1, working: 0.75, muted: 0.15 };

let ws = null, retryMs = 500, pendingId = null, state = "starting";
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
  $("centre").hidden = !place.expanded;
  $("sections").hidden = !place.expanded;
  layout();
};

function setExpanded(expanded) {
  if (expanded === place.expanded) return;
  if (!expanded) clearSections(true);
  if (preview) {                                // no window around the page: lay it out here
    window.jarvisPlace({ expanded, x: innerWidth / 2, y: expanded ? innerHeight * 0.45 : innerHeight / 2 });
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
    case "status": return setStatus(ev.text);
    case "focus": if (!ev.target) toWindow("front"); return;
  }
}

function hello(ev) {
  const snap = ev.snapshot || {};
  setSpeak(snap.speak !== false);
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
  const wasAwake = state !== "asleep" && state !== "starting";
  state = next;
  orbState.actTarget = ACT[next] ?? 0.55;
  orb && orb.setTint(next === "muted" ? "red" : next === "working" ? "amber" : "blue");
  $("mute").classList.toggle("on", next === "muted");
  $("mute").textContent = next === "muted" ? "Unmute" : "Mute";
  if (next === "asleep" && wasAwake) setExpanded(false);   // "go to sleep": back to just the orb
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
    setExpanded(true);                         // something to show: open around the orb
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
  const w = innerWidth, h = innerHeight, cx = place.x || w / 2, cy = place.y || h / 2;
  const orbEl = $("orb");
  orbEl.style.left = cx + "px"; orbEl.style.top = cy + "px";
  if (place.expanded) {
    const ring = [...document.querySelectorAll("#ring .round")];
    [-72, -36, 0, 36, 72].forEach((deg, i) => {
      const r = (deg * Math.PI) / 180;
      ring[i].style.left = cx + 122 * Math.sin(r) + "px"; ring[i].style.top = cy - 122 * Math.cos(r) + "px";
    });
    $("composer").style.left = cx + "px"; $("composer").style.top = cy + 104 + "px";
    $("quick").style.left = cx + "px"; $("quick").style.top = cy + 152 + "px";
    $("status").style.left = cx + "px"; $("status").style.top = cy + 152 + $("quick").offsetHeight + 6 + "px";
    const rx = Math.min(270, w / 2 - 112), ry = Math.min(196, cy - 85);
    const keys = [...sections.keys()];
    clockAngles(keys.length).forEach((deg, i) => {
      const el = sections.get(keys[i]), r = (deg * Math.PI) / 180;
      el.style.left = Math.max(112, Math.min(w - 112, cx + rx * Math.sin(r))) + "px";
      el.style.top = Math.max(88, Math.min(h - 88, cy - ry * Math.cos(r))) + "px";
    });
  }
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

// ---------------------------------------------------------------- buttons and the typing box

document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => send({ type: "control", action: b.dataset.action }));
});
const speakOn = () => $("speak").getAttribute("aria-pressed") === "true";
function setSpeak(on) { $("speak").setAttribute("aria-pressed", on ? "true" : "false"); }
$("speak").addEventListener("click", () => setSpeak(!speakOn()));
$("talk").addEventListener("click", () => send({ type: "control", action: "talk" }));
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (send({ type: "chat", text, speak: speakOn() })) $("input").value = "";
  else setStatus("Not connected: your message is still in the box.");
});
addEventListener("keydown", (e) => { if (e.key === "Escape" && place.expanded) setExpanded(false); });

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
    setExpanded(!place.expanded);
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
    for (const el of document.querySelectorAll("#ring .round, #composer, #quick .chip, #sections .card:not(.leave)")) {
      const b = el.getBoundingClientRect();
      if (b.width && b.height) shapes.push({ x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) });
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

let orb = null, orbPixels = 0;
function sizeOrb() {
  const c = $("orb"), css = place.canvas, px = Math.round(css * (window.devicePixelRatio || 1));
  c.style.width = c.style.height = css + "px";
  if (px !== orbPixels) {                       // a new size: a new canvas resolution and a fresh orb
    orbPixels = px; c.width = c.height = px;
    orb = createOrb(c);
    setState(state);
  }
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

if (preview) window.jarvisPlace({ expanded: params.get("open") === "1", x: innerWidth / 2,
  y: params.get("open") === "1" ? innerHeight * 0.45 : innerHeight / 2 });
else sizeOrb();
layout();
requestAnimationFrame(frame);
connect();
