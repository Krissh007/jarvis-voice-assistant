// One small state tree for a page, fed by the WebSocket's events. Views read it with useStore() and
// re-render when it changes; nothing else holds state about JARVIS.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";

const state = {
  connected: false,
  jarvis: "starting",    // asleep | listening | speaking | working | muted | starting
  voice: true,           // the Voice button: false = text-only replies
  announce: true,        // the Announce button
  status: "",            // the latest status line
  activity: "",          // what JARVIS is doing right now ("Searching the web…"); "" = nothing
  problem: null,         // something JARVIS needs that isn't working, in a few words
  keep: { enabled: false, on: false },   // is this conversation being kept (the Memory switch)
  hub: { enabled: false, reachable: null },
  voiceModel: "",
  language: "",
  quickActions: [],
  captionS: 8,           // how long a reply stays as the caption
};
const subscribers = new Set();
let version = 0;   // bumped on every change, so a view that subscribes late still catches up

export function getState() { return state; }

export function setState(patch) {
  let changed = false;
  for (const [k, v] of Object.entries(patch)) {
    if (state[k] !== v) { state[k] = v; changed = true; }
  }
  if (changed) {
    version += 1;
    for (const fn of subscribers) fn(state);
  }
}

// Re-render the calling component whenever the store changes.
export function useStore() {
  const [, tick] = useState(0);
  const seen = version;   // what this render showed
  useEffect(() => {
    const fn = () => tick((n) => n + 1);
    subscribers.add(fn);
    if (version !== seen) fn();   // it changed between this render and subscribing (e.g. the socket opened)
    return () => subscribers.delete(fn);
  }, []);
  return state;
}

// The events every page shares: hello (snapshot), level, state, voice, announce, status, activity, memory_keep.
export function applyEvent(ev) {
  switch (ev.kind) {
    case "hello": {
      const s = ev.snapshot || {};
      setState({ jarvis: s.state || "starting", voice: (s.voice ?? s.speak) !== false, announce: s.announce !== false,
                 activity: s.activity || "", problem: s.problem || null, keep: s.keep || state.keep,
                 hub: s.hub || state.hub, voiceModel: s.voice_model || "", language: s.language || "",
                 quickActions: Array.isArray(s.quick_actions) ? s.quick_actions : [], captionS: s.caption_s || 8 });
      return;
    }
    case "level": setState({ problem: ev.problem || null }); if (ev.state) setState({ jarvis: ev.state }); return;
    case "state": if (ev.state) setState({ jarvis: ev.state }); return;
    case "memory_keep": setState({ keep: { enabled: !!ev.enabled, on: !!ev.on } }); return;
    case "voice": setState({ voice: !!ev.on }); return;
    case "announce": setState({ announce: !!ev.on }); return;
    case "status": setState({ status: ev.text || "" }); return;
    case "activity": setState({ activity: ev.text || "" }); return;
  }
}
