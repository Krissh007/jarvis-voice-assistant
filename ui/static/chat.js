// JARVIS's complete chat page, opened by "New chat" on the orb. The same conversation as voice and the
// orb: messages (typed and spoken), confirmations and pictures inline, the activity list, vision.
//
// Every text is set with textContent, never as HTML: replies and tool results can contain text from web
// pages and apps. The token comes from the URL's #fragment.
"use strict";

const params = new URLSearchParams(location.hash.slice(1));
const token = params.get("t") || "";
const $ = (id) => document.getElementById(id);
const STATE_LABELS = { starting: "Starting up…", asleep: 'Asleep · say "hey jarvis"', listening: "Listening",
  speaking: "Speaking", working: "Working on a task", muted: "Microphone muted" };
const ACT = { starting: 0, asleep: 0, listening: 0.55, speaking: 1, working: 0.75, muted: 0.15 };
const MAX_MESSAGES = 300, MAX_STEPS = 100;

let ws = null, retryMs = 500, state = "starting";
const live = { user: null, jarvis: null };
const confirms = new Map();   // id -> bubble
const orbState = { act: 0, actTarget: 0, level: 0, levelTarget: 0 };

// ---------------------------------------------------------------- connection

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
  ws.onopen = () => { retryMs = 500; $("conn").classList.remove("off"); setStatus(""); };
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    $("conn").classList.add("off");
    setStatus("Not connected to JARVIS. Retrying…", true);
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 8000);
  };
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(payload)); return true; }
  return false;
}

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
    case "conversation": return newConversation();
    case "status": return setStatus(ev.text);
    case "focus": if (ev.target === "chat") console.log("jarvis:front"); return;
  }
}

function hello(ev) {
  const snap = ev.snapshot || {};
  newConversation();
  $("speak").checked = snap.speak !== false;
  setState(snap.state || "starting");
  for (const old of ev.history || []) handle(old);
  if (snap.pending && !confirms.has(snap.pending.id)) confirmRequested({ id: snap.pending.id, summary: snap.pending.summary });
}

function newConversation() {
  $("messages").replaceChildren();
  $("steps").replaceChildren();
  $("steps-empty").hidden = false;
  live.user = live.jarvis = null;
  confirms.clear();
  showEmpty();
}

function setState(next) {
  if (!next) return;
  state = next;
  orbState.actTarget = ACT[next] ?? 0.55;
  orb.setTint(next === "muted" ? "red" : next === "working" ? "amber" : "blue");
  $("state").textContent = STATE_LABELS[next] || next;
  $("mute").classList.toggle("on", next === "muted");
  $("mute").textContent = next === "muted" ? "Unmute" : "Mute";
}

let statusTimer = null;
function setStatus(text, sticky) {
  $("status").textContent = text || "";
  clearTimeout(statusTimer);
  if (text && !sticky) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 6000);
}

// ---------------------------------------------------------------- the conversation

function showEmpty() {
  const p = document.createElement("div");
  p.className = "empty"; p.id = "empty";
  const b = document.createElement("b"); b.textContent = "New chat";
  p.append(b, document.createTextNode('Type below, or just say "hey jarvis".'));
  $("messages").append(p);
}

function row(role) {
  const r = document.createElement("div");
  r.className = "row " + (role === "user" ? "user" : "jarvis");
  return r;
}

function bubble(role, meta, text, extra) {
  const r = row(role);
  const b = document.createElement("div");
  b.className = "bubble" + (extra ? " " + extra : "");
  const m = document.createElement("div"); m.className = "meta"; m.textContent = meta;
  const body = document.createElement("div"); body.className = "body"; body.textContent = text;
  b.append(m, body);
  r.append(b);
  return [r, b];
}

function add(node) {
  const box = $("messages");
  const empty = $("empty");
  if (empty) empty.remove();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(node);
  while (box.children.length > MAX_MESSAGES) box.firstChild.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

const label = (ev) => {
  if (ev.role !== "user") return ev.label || "JARVIS";
  return (ev.label || "You") + (ev.channel ? ` · ${ev.channel === "chat" ? "typed" : "spoken"}` : "");
};

function partial(ev) {
  const role = ev.role === "user" ? "user" : "jarvis";
  if (!live[role]) {
    const [r] = bubble(role, label(ev), "", "live");
    live[role] = r;
    add(r);
  }
  const body = live[role].querySelector(".body");
  body.textContent = (body.textContent + ev.text).replace(/\s+/g, " ").trimStart();
  $("messages").scrollTop = $("messages").scrollHeight;
}

function message(ev) {
  const role = ev.role === "user" ? "user" : "jarvis";
  if (live[role]) { live[role].remove(); live[role] = null; }
  add(bubble(role, label(ev), ev.text)[0]);
}

function media(ev) {
  if (ev.type === "text") {                    // a list or text JARVIS put on screen instead of reading it aloud
    const [r, b] = bubble("jarvis", ev.title || "For you", ev.text || "");
    const items = Array.isArray(ev.items) ? ev.items : [];
    if (items.length) {
      const ol = document.createElement("ol");
      for (const item of items) { const li = document.createElement("li"); li.textContent = String(item); ol.append(li); }
      b.append(ol);
    }
    return add(r);
  }
  if (ev.type !== "image" || !String(ev.data || "").startsWith("data:image/")) return;
  const [r, b] = bubble("jarvis", ev.title || "Picture", ev.caption || "");
  const img = document.createElement("img");
  img.alt = ev.caption || ""; img.src = ev.data;
  b.append(img);
  add(r);
}

function confirmRequested(ev) {
  const [r, b] = bubble("jarvis", "Should I go ahead?", ev.summary, "confirm");
  const actions = document.createElement("div");
  actions.className = "actions";
  for (const [text, action, cls] of [["Confirm", "confirm", "primary"], ["Cancel", "cancel", ""]]) {
    const btn = document.createElement("button");
    btn.textContent = text; btn.className = cls;
    btn.addEventListener("click", () => send({ type: "control", action }));
    actions.append(btn);
  }
  b.append(actions);
  confirms.set(ev.id, b);
  add(r);
  stepItem("wait", "Asked for your OK", ev.summary);
}

function confirmResolved(ev) {
  const b = confirms.get(ev.id);
  const yes = String(ev.outcome || "").startsWith("confirmed");
  if (b) {
    b.classList.add("done");
    const a = b.querySelector(".actions");
    if (a) a.replaceChildren(document.createTextNode(yes ? "Confirmed." : "Not done."));
    confirms.delete(ev.id);
  }
  stepItem(yes ? "done" : "failed", yes ? "You confirmed" : "Not done", plain(ev.result));
}

// ---------------------------------------------------------------- activity

const plain = (text) => String(text || "").replace(/<\/?untrusted_content[^>]*>/g, "").trim();

function stepItem(cls, name, detail) {
  $("steps-empty").hidden = true;
  const li = document.createElement("li");
  li.className = cls;
  li.textContent = name;
  const d = document.createElement("span"); d.className = "detail"; d.textContent = (detail || "").slice(0, 300);
  li.append(d);
  const list = $("steps");
  list.prepend(li);
  while (list.children.length > MAX_STEPS) list.lastChild.remove();
  return li;
}

function toolStarted(ev) {
  const args = Object.entries(ev.args || {}).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
  stepItem("running", (ev.step ? "plan step · " : "") + ev.tool, args.slice(0, 200)).dataset.tool = ev.tool;
}

function toolFinished(ev) {
  const li = [...$("steps").querySelectorAll("li.running")].find((x) => x.dataset.tool === ev.tool);
  const result = plain(ev.result);
  const cls = ev.paused ? "wait" : /^(That didn't work|Couldn't|I couldn't|Stopped\.)/.test(result) ? "failed" : "done";
  if (!li) { stepItem(cls, ev.tool, result); return; }
  li.className = cls;
  li.querySelector(".detail").textContent = (ev.paused ? "Waiting for your OK: " : "") + result.slice(0, 300);
}

// ---------------------------------------------------------------- typing and buttons

const input = $("input");
function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }
input.addEventListener("input", grow);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  if (send({ type: "chat", text, speak: $("speak").checked })) { input.value = ""; grow(); }
  else setStatus("Not connected: your message is still in the box.");
});
document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => send({ type: "control", action: b.dataset.action }));
});

// ---------------------------------------------------------------- the small orb

const orbCanvas = $("orb");
const px = Math.round(76 * (window.devicePixelRatio || 1));
orbCanvas.width = orbCanvas.height = px;
const orb = createOrb(orbCanvas);
const t0 = performance.now();
let last = 0;
function frame(now) {
  requestAnimationFrame(frame);
  const t = (now - t0) / 1000, dt = t - last;
  if (document.hidden || dt < 1 / 30) return;
  last = t;
  orbState.act += (orbState.actTarget - orbState.act) * Math.min(1, dt * 3);
  orbState.level += (orbState.levelTarget - orbState.level) * Math.min(1, dt * 12);
  orb.draw(t, orbState.act, state === "speaking" || state === "listening" ? orbState.level : 0, dt);
}

showEmpty();
requestAnimationFrame(frame);
connect();
input.focus();
