// The one connection to JARVIS, shared by every view of a page. The token comes from the URL's #fragment
// (t=...), so it never sits in a request line. Events from the bus arrive as {kind, ...}; requests
// ({type: "request" | "setting" | "forget"}) get one {kind: "reply", id} back, for this page only.
"use strict";

const params = new URLSearchParams(location.hash.slice(1));
export const token = params.get("t") || "";

let socket = null, retryMs = 500, nextId = 1;
const listeners = new Set();       // (event) => void, for every event
const waiting = new Map();         // request id -> {resolve, timer}
const statusListeners = new Set(); // (connected: boolean) => void

export function onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function onConnection(fn) { statusListeners.add(fn); return () => statusListeners.delete(fn); }

function setConnected(on) { for (const fn of statusListeners) fn(on); }

export function connect() {
  socket = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
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
  socket.onclose = () => {
    setConnected(false);
    for (const { resolve, timer } of waiting.values()) { clearTimeout(timer); resolve(null); }
    waiting.clear();
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
