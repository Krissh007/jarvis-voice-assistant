// The one connection to JARVIS, shared by every view of a page. The token comes from the URL's #fragment
// (t=...), so it never sits in a request line. Events from the bus arrive as {kind, ...}; requests
// ({type: "request" | "setting" | "forget"}) get one {kind: "reply", id} back, for this page only.
// A paired phone (the mobile app through Tailscale) has no launch token: it keeps a device token in its own
// storage and sends it in the WebSocket's subprotocol header and in uploads' headers, never in a URL.
"use strict";

const params = new URLSearchParams(location.hash.slice(1));
export const token = params.get("t") || "";
export const phone = !token;       // a page JARVIS didn't open itself: the phone app
const DEVICE_KEY = "jarvis.device";
const NOT_PAIRED = 4401;           // the server's close code for a device token that isn't valid (any more)

let socket = null, retryMs = 500, nextId = 1;
const listeners = new Set();       // (event) => void, for every event
const waiting = new Map();         // request id -> {resolve, timer}
const statusListeners = new Set(); // (connected: boolean) => void
const unpairedListeners = new Set(); // () => void: the phone has to pair (again)

export function onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function onConnection(fn) { statusListeners.add(fn); return () => statusListeners.delete(fn); }
export function onUnpaired(fn) { unpairedListeners.add(fn); return () => unpairedListeners.delete(fn); }

function setConnected(on) { for (const fn of statusListeners) fn(on); }

export function deviceToken() {
  try { return localStorage.getItem(DEVICE_KEY) || ""; } catch { return ""; }
}

function setDeviceToken(value) {
  try { if (value) localStorage.setItem(DEVICE_KEY, value); else localStorage.removeItem(DEVICE_KEY); } catch {}
}

export const paired = () => !!(token || deviceToken());

// The headers that let an upload through: the launch token on the PC, the device token on a phone.
export function authHeaders() {
  return token ? { "x-jarvis-token": token } : { "x-jarvis-device": deviceToken() };
}

// The phone trades a one-time pairing code (from the PC's Settings, Phone) for its device token.
export async function pair(code, name) {
  try {
    const r = await fetch("/pair", { method: "POST", headers: { "content-type": "application/json" },
                                     body: JSON.stringify({ code, name }) });
    const d = await r.json();
    if (d.ok && d.token) { setDeviceToken(d.token); return { ok: true, device: d.device }; }
    return { ok: false, message: d.message || "JARVIS refused the code." };
  } catch {
    return { ok: false, message: "Couldn't reach JARVIS. Is the PC on, with JARVIS and Tailscale running?" };
  }
}

export function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  if (token) {
    socket = new WebSocket(`${scheme}://${location.host}/ws?t=${encodeURIComponent(token)}`);
  } else {
    const device = deviceToken();
    if (!device) { for (const fn of unpairedListeners) fn(); return; }
    socket = new WebSocket(`${scheme}://${location.host}/ws`, ["jarvis", "jarvis-device." + device]);
  }
  socket.onopen = () => { retryMs = 500; setConnected(true); };
  socket.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.kind === "reply" && waiting.has(ev.id)) {
      const { resolve, timer } = waiting.get(ev.id);
      clearTimeout(timer);
      waiting.delete(ev.id);
      resolve(ev.data);
      return;
    }
    for (const fn of listeners) {
      try { fn(ev); } catch (err) { console.error(err); }
    }
  };
  socket.onclose = (e) => {
    setConnected(false);
    for (const { resolve, timer } of waiting.values()) { clearTimeout(timer); resolve(null); }
    waiting.clear();
    if (e.code === NOT_PAIRED) {   // removed on the PC (or never paired): pair again, don't retry
      setDeviceToken("");
      for (const fn of unpairedListeners) fn();
      return;
    }
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 8000);
  };
}

export function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) { socket.send(JSON.stringify(payload)); return true; }
  return false;
}

// A message that gets a reply: resolves with its data, or null if it couldn't be sent or took too long.
export function ask(payload, timeoutMs = 8000) {
  const id = nextId++;
  return new Promise((resolve) => {
    if (!send({ ...payload, id })) return resolve(null);
    const timer = setTimeout(() => { waiting.delete(id); resolve(null); }, timeoutMs);
    waiting.set(id, { resolve, timer });
  });
}

export const control = (action) => send({ type: "control", action });

// Messages for this page's own window (ui/window.py reads console lines starting "jarvis:").
export function toWindow(command, data) {
  console.log("jarvis:" + command + (data === undefined ? "" : ":" + JSON.stringify(data)));
}
