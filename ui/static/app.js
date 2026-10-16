// JARVIS web UI. Every text is set with textContent (never as HTML): replies and tool results can
// contain text from web pages and apps. The token comes from the URL's #fragment.
"use strict";

const token = new URLSearchParams(location.hash.slice(1)).get("t") || "";
const $ = (id) => document.getElementById(id);

const STATES = {
  starting: { ring: "#3a5568", core: "#6f8b9c", label: "Starting up…" },
  asleep: { ring: "#4a6b82", core: "#9fb4c4", label: "Asleep · say \"hey jarvis\"" },
  listening: { ring: "#00b4ff", core: "#e6f7ff", label: "Listening…" },
  speaking: { ring: "#00e0ff", core: "#ffffff", label: "Speaking" },
  working: { ring: "#f0a830", core: "#fff1d6", label: "Working on a task" },
  muted: { ring: "#d93a3a", core: "#f3c2c2", label: "Microphone muted" },
};
const MAX_MESSAGES = 200;
const MAX_STEPS = 100;

let ws = null;
let retryMs = 500;
let pendingId = null;
const live = { user: null, jarvis: null }; // bubbles for what's being said right now
const orb = { state: "starting", level: 0, shown: 0, angle: 0 };

// ---------------------------------------------------------------- connection

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
  ws.onopen = () => { retryMs = 500; $("conn").classList.remove("off"); };
  ws.onmessage = (e) => {
    try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  ws.onclose = () => {
    $("conn").classList.add("off");
    setStatus("Not connected to JARVIS. Retrying…", true);
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 8000);
  };
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- events

function handle(ev) {
  switch (ev.kind) {
    case "hello": return hello(ev);
    case "level": orb.state = ev.state; orb.level = ev.level; return;
    case "state": return setState(ev.state);
    case "partial": return partial(ev);
    case "message": return message(ev);
    case "tool_started": return toolStarted(ev);
    case "tool_finished": return toolFinished(ev);
    case "confirm_requested": return confirmRequested(ev);
    case "confirm_resolved": return confirmResolved(ev);
    case "status": return setStatus(ev.text);
    case "focus": return bringToFront();
  }
}

function hello(ev) {
  const snap = ev.snapshot || {};
  $("messages").replaceChildren();
  $("steps").replaceChildren();
  live.user = live.jarvis = null;
  pendingId = null;
  $("confirm").hidden = true;
  $("speak").checked = snap.speak !== false;
  setState(snap.state || "starting");
  for (const old of ev.history || []) handle(old);
  if (snap.pending) confirmRequested({ id: snap.pending.id, summary: snap.pending.summary, replay: true });
  if (!$("messages").children.length) showEmpty();
  setStatus("");
}

function setState(state) {
  orb.state = state;
  $("state").textContent = (STATES[state] || STATES.listening).label;
  $("mute").classList.toggle("on", state === "muted");
  $("mute").textContent = state === "muted" ? "Unmute" : "Mute";
}

let statusTimer = null;
function setStatus(text, sticky) {
  // Console lines such as "(awake)" or "[tool] -> ok" are passing news: they fade after a few seconds.
  $("status").textContent = text || "";
  clearTimeout(statusTimer);
  if (text && !sticky) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 6000);
}

// ---------------------------------------------------------------- chat

function showEmpty() {
  const p = document.createElement("p");
  p.className = "empty";
  p.id = "empty";
  p.textContent = "Say \"hey jarvis\", click the orb, or type below.";
  $("messages").append(p);
}

function bubble(role, label, channel, text, isLive) {
  const div = document.createElement("div");
  div.className = `msg ${role === "user" ? "user" : "jarvis"}${isLive ? " live" : ""}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  // How the user said it (typed or spoken); JARVIS's replies need no tag.
  const how = role === "user" && channel ? ` · ${channel === "chat" ? "typed" : "spoken"}` : "";
  meta.textContent = label + how;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  div.append(meta, body);
  return div;
}

function addToChat(node) {
  const box = $("messages");
  const empty = $("empty");
  if (empty) empty.remove();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.append(node);
  while (box.children.length > MAX_MESSAGES) box.firstChild.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function partial(ev) {
  const role = ev.role === "user" ? "user" : "jarvis";
  if (!live[role]) {
    live[role] = bubble(role, role === "user" ? "You" : "JARVIS", ev.channel, "", true);
    addToChat(live[role]);
  }
  const body = live[role].querySelector(".body");
  body.textContent = (body.textContent + ev.text).replace(/\s+/g, " ").trimStart();
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

function message(ev) {
  const role = ev.role === "user" ? "user" : "jarvis";
  if (live[role]) { live[role].remove(); live[role] = null; }
  addToChat(bubble(role, ev.label || (role === "user" ? "You" : "JARVIS"), ev.channel, ev.text, false));
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (send({ type: "chat", text, speak: $("speak").checked })) {
    $("input").value = "";
  } else {
    setStatus("Not connected: your message is still in the box.");
  }
});

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

// ---------------------------------------------------------------- activity (tools and plan steps)

function plain(text) {
  // Tool results keep their <untrusted_content> tags for the model; the reader doesn't need them.
  return String(text || "").replace(/<\/?untrusted_content[^>]*>/g, "").trim();
}

function addStep(cls, name, detail) {
  $("steps-empty").hidden = true;
  const li = document.createElement("li");
  li.className = `step ${cls}`;
  const n = document.createElement("div");
  n.className = "name";
  n.textContent = name;
  const d = document.createElement("div");
  d.className = "detail";
  d.textContent = detail || "";
  li.append(n, d);
  const list = $("steps");
  list.prepend(li); // newest first
  while (list.children.length > MAX_STEPS) list.lastChild.remove();
  return li;
}

function describeArgs(args) {
  const parts = Object.entries(args || {}).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`);
  return parts.join(", ").slice(0, 200);
}

function toolStarted(ev) {
  const li = addStep(`running${ev.step ? " plan" : ""}`, ev.tool, describeArgs(ev.args));
  li.dataset.tool = ev.tool;
}

function toolFinished(ev) {
  const li = [...$("steps").querySelectorAll("li.running")].find((x) => x.dataset.tool === ev.tool);
  const result = plain(ev.result);
  const failed = /^(That didn't work|Couldn't|I couldn't|Stopped\.)/.test(result);
  const cls = ev.paused ? "wait" : failed ? "failed" : "done";
  if (!li) { addStep(cls, ev.tool, result); return; }
  li.classList.remove("running");
  li.classList.add(cls);
  li.querySelector(".detail").textContent = (ev.paused ? "Waiting for your OK: " : "") + result.slice(0, 300);
}

function confirmRequested(ev) {
  pendingId = ev.id;
  $("confirm-text").textContent = ev.summary;
  $("confirm").hidden = false;
  if (!ev.replay) addStep("wait", "Asked for your OK", ev.summary);
}

function confirmResolved(ev) {
  if (ev.id === pendingId) { pendingId = null; $("confirm").hidden = true; }
  const yes = String(ev.outcome || "").startsWith("confirmed");
  addStep(yes ? "done" : "failed", yes ? "You confirmed" : "Not done", plain(ev.result));
}

// ---------------------------------------------------------------- buttons

document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => send({ type: "control", action: b.dataset.action }));
});
$("orb").addEventListener("click", () => send({ type: "control", action: "talk" }));

function bringToFront() {
  const api = window.pywebview && window.pywebview.api;
  if (api && api.bring_to_front) api.bring_to_front();
}

// ---------------------------------------------------------------- the orb

function drawOrb() {
  const canvas = $("orb");
  const ratio = window.devicePixelRatio || 1;
  const size = canvas.clientWidth;
  if (canvas.width !== Math.round(size * ratio)) {
    canvas.width = canvas.height = Math.round(size * ratio);
  }
  const ctx = canvas.getContext("2d");
  const s = canvas.width;
  const c = s / 2;
  const colors = STATES[orb.state] || STATES.listening;
  const asleep = orb.state === "asleep" || orb.state === "starting";
  orb.shown += (orb.level - orb.shown) * 0.25;
  orb.angle += asleep ? 0.003 : 0.012 + orb.shown * 0.05;

  ctx.clearRect(0, 0, s, s);
  ctx.lineCap = "round";
  // outer ring: segments
  ctx.strokeStyle = colors.ring;
  ctx.lineWidth = s * 0.025;
  for (let k = 0; k < 12; k++) {
    const a = orb.angle + (k * Math.PI) / 6;
    ctx.beginPath();
    ctx.arc(c, c, s * 0.44, a, a + Math.PI / 9);
    ctx.stroke();
  }
  // inner ring, turning the other way
  ctx.globalAlpha = 0.6;
  ctx.lineWidth = s * 0.012;
  for (let k = 0; k < 3; k++) {
    const a = -orb.angle * 1.6 + (k * 2 * Math.PI) / 3;
    ctx.beginPath();
    ctx.arc(c, c, s * 0.34, a, a + Math.PI / 2);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
  // core: pulses with the voice
  const pulse = asleep ? 0.02 * Math.sin(Date.now() / 900) : orb.shown;
  const r = s * (0.16 + 0.1 * pulse);
  const glow = ctx.createRadialGradient(c, c, r * 0.2, c, c, r * 1.8);
  glow.addColorStop(0, colors.core);
  glow.addColorStop(0.5, colors.ring);
  glow.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(c, c, r * 1.8, 0, 2 * Math.PI);
  ctx.fill();
  ctx.fillStyle = colors.core;
  ctx.beginPath();
  ctx.arc(c, c, r * 0.7, 0, 2 * Math.PI);
  ctx.fill();
  requestAnimationFrame(drawOrb);
}

showEmpty();
requestAnimationFrame(drawOrb);
connect();
