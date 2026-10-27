// The chat's data: the conversation as turns, built from JARVIS's events. A turn starts with what you
// said (spoken or typed) and collects everything JARVIS did for it: replies, the tools it called,
// web sources, memory it used, plans waiting for your yes, and pictures. It listens from the moment
// the page loads (before any view is drawn), so the "hello" history is never missed.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { onEvent } from "/static/shared/ws.js";

const MAX_TURNS = 120;
let turns = [];
let nextTurn = 1;
let live = { user: "", jarvis: "" };     // words still being spoken
const subscribers = new Set();

function changed() { for (const fn of subscribers) fn(); }

function newTurn(user) {
  const t = { id: nextTurn++, user, replies: [], tools: [], sources: [], memory: [], plans: [], pictures: [], notes: [] };
  turns.push(t);
  if (turns.length > MAX_TURNS) turns = turns.slice(-MAX_TURNS);
  return t;
}

function current() { return turns.length ? turns[turns.length - 1] : newTurn(null); }

function plan(id) {
  for (let i = turns.length - 1; i >= 0; i--) {
    const p = turns[i].plans.find((x) => x.id === id);
    if (p) return p;
  }
  return null;
}

function apply(ev) {
  switch (ev.kind) {
    case "message":
      if (ev.role === "user") {
        live.user = "";
        newTurn({ text: ev.text, channel: ev.channel || "", source: ev.source || "" });
      } else {
        live.jarvis = "";
        current().replies.push(ev.text);
      }
      return true;
    case "partial":
      if (ev.role === "user") live.user = (live.user + ev.text).replace(/\s+/g, " ").trimStart();
      else live.jarvis = (live.jarvis + ev.text).replace(/\s+/g, " ").trimStart();
      return true;
    case "tool_started":
      current().tools.push({ tool: ev.tool, state: "running", step: !!ev.step });
      return true;
    case "tool_finished": {
      const tools = current().tools;
      const t = [...tools].reverse().find((x) => x.tool === ev.tool && x.state === "running");
      const state = ev.paused ? "waiting" : ev.ok === false ? "failed" : "done";
      if (t) t.state = state; else tools.push({ tool: ev.tool, state });
      return true;
    }
    case "confirm_requested":
      current().plans.push({ id: ev.id, tool: ev.tool, summary: ev.summary, step: ev.step || {}, outcome: null });
      return true;
    case "confirm_resolved": {
      const p = plan(ev.id);
      if (p) p.outcome = ev.outcome || "done";
      return true;
    }
    case "sources": {
      const have = current().sources;
      for (const s of ev.sources || []) if (!have.some((h) => h.site === s.site && h.title === s.title)) have.push(s);
      return true;
    }
    case "memory_used":
      current().memory.push({ topic: ev.topic, when: ev.when, facts: ev.facts || [], lines: ev.conversation_lines || 0 });
      return true;
    case "media":
      if (ev.type === "image" && String(ev.data || "").startsWith("data:image/")) {
        current().pictures.push({ title: ev.title, caption: ev.caption || "", data: ev.data });
      } else if (ev.type === "text") {
        current().notes.push({ title: ev.title, items: ev.items || [], text: ev.text || "" });
      }
      return true;
    case "conversation":
      turns = []; live = { user: "", jarvis: "" };
      return true;
    case "hello":
      turns = []; live = { user: "", jarvis: "" };
      for (const old of ev.history || []) apply(old);
      if (ev.snapshot && ev.snapshot.pending && !plan(ev.snapshot.pending.id)) {
        current().plans.push({ id: ev.snapshot.pending.id, summary: ev.snapshot.pending.summary, step: {}, outcome: null });
      }
      return true;
  }
  return false;
}

onEvent((ev) => { if (apply(ev)) changed(); });

export function useChat() {
  const [, tick] = useState(0);
  useEffect(() => {
    const fn = () => tick((n) => n + 1);
    subscribers.add(fn);
    fn();
    return () => subscribers.delete(fn);
  }, []);
  return { turns, live };
}

// The channel you're on: voice while you're speaking or when your last message was spoken, else typing.
export function channelOf(turns, live) {
  if (live.user) return "voice";
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].user) return turns[i].user.channel === "voice" ? "voice" : "chat";
  return "voice";
}

export const _test = { apply, reset: () => { turns = []; live = { user: "", jarvis: "" }; } };
