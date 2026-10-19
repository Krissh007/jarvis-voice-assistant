// The command centre's timeline: one entry per thing JARVIS did, newest first, built from the bus's
// events from the moment the page loads. Each entry has a state for its dot: running or waiting for you
// (amber), done (green), failed (red), cancelled (grey).
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { onEvent } from "/static/shared/ws.js";

const MAX = 300;
let items = [];
let nextId = 1;
const subscribers = new Set();
const changed = () => { for (const fn of subscribers) fn(); };

function add(entry, ts) {
  items.unshift({ id: nextId++, at: ts ? ts * 1000 : Date.now(), ...entry });
  if (items.length > MAX) items.length = MAX;
}

function args(a) {
  if (!a || typeof a !== "object") return "";
  return Object.entries(a).filter(([, v]) => v !== "" && v !== null && v !== undefined)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · ").slice(0, 160);
}

function apply(ev) {
  switch (ev.kind) {
    case "tool_started":
      add({ type: "tool", tool: ev.tool, detail: args(ev.args), state: "running", step: !!ev.step, channel: ev.channel }, ev.ts);
      return true;
    case "tool_finished": {
      const it = items.find((x) => x.type === "tool" && x.tool === ev.tool && x.state === "running");
      const state = ev.paused ? "waiting" : ev.ok === false ? "failed" : "done";
      const result = typeof ev.result === "string"
        ? ev.result.replace(/<\/?untrusted_content[^>]*>/g, "").replace(/\s+/g, " ").trim().slice(0, 220) : "";
      if (it) { it.state = state; it.result = result; it.ended = ev.ts ? ev.ts * 1000 : Date.now(); }
      else add({ type: "tool", tool: ev.tool, state, result }, ev.ts);
      return true;
    }
    case "confirm_requested":
      add({ type: "confirm", pendingId: ev.id, tool: ev.tool, detail: ev.summary, step: ev.step || {}, state: "waiting" }, ev.ts);
      return true;
    case "confirm_resolved": {
      const it = items.find((x) => x.type === "confirm" && x.pendingId === ev.id);
      const approved = String(ev.outcome || "").startsWith("confirmed");
      if (it) { it.state = approved ? "done" : "cancelled"; it.outcome = ev.outcome; it.result = ev.result || ""; }
      return true;
    }
    case "agent_update":
      add({ type: "agent", tool: "Agent Hub", detail: ev.say || "", state: ev.status === "done" ? "done"
            : ev.status === "cancelled" ? "cancelled" : "failed" }, ev.ts);
      return true;
    case "route":
      add({ type: "route", tool: "Reasoning", detail: [ev.route, ...(ev.models || [])].filter(Boolean).join(" · "),
            result: (ev.tools || []).join(", "),
            // answered / fallback_flash: done; failed / budget (ran out of time or steps): failed; declined: cancelled
            state: { failed: "failed", budget: "failed", declined: "cancelled" }[ev.outcome] || "done" }, ev.ts);
      return true;
    case "hello":
      items = [];
      for (const old of ev.history || []) apply(old);
      return true;
  }
  return false;
}

onEvent((ev) => { if (apply(ev)) changed(); });

export function useTimeline() {
  const [, tick] = useState(0);
  useEffect(() => {
    const fn = () => tick((n) => n + 1);
    subscribers.add(fn);
    fn();
    return () => subscribers.delete(fn);
  }, []);
  return items;
}
