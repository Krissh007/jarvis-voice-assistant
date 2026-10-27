// The Brain's HUD: JARVIS's state as a ring reticle (or the 3D orb; its colour shows the state, the words
// are in the header's channel tag only), the capability switches, and the voice model and language folded
// under Details. Every switch is a real JARVIS control (the same as the orb's buttons and the
// hotkeys): Mic mutes the microphone, Voice speaks replies, Announce speaks background news, Memory keeps
// this conversation (or stops, as "don't remember this" does), Agents shows the Agent Hub and opens the
// Command centre. Colours follow the orb: blue listening, amber working, brighter while speaking, grey
// muted, red (with the reason) when something JARVIS needs isn't working.
"use strict";

import { useEffect, useRef, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, control, onEvent } from "/static/shared/ws.js";

const STATE_LABEL = { starting: "Starting", asleep: "Asleep", listening: "Listening", speaking: "Speaking",
                      working: "Working", muted: "Mic muted" };
const ACT = { starting: 0, asleep: 0, listening: 0.55, speaking: 1, working: 0.75, muted: 0.15 };   // as the orb (app.js)
const HUB_REFRESH_MS = 60000;
let chosenView = "ring";   // Ring or Orb, kept while the app is open

export function hudTone(state, problem) {
  if (state === "muted") return "muted";
  if (problem) return "problem";
  if (state === "working") return "working";
  if (state === "speaking") return "speaking";
  if (state === "asleep" || state === "starting") return "idle";
  return "listening";
}

// The ring reticle: ticks, two counter-rotating arcs and the name, drawn in SVG; the voice level swells it.
function Ring({ tone, label }) {
  const ref = useRef(null);
  useEffect(() => onEvent((ev) => {
    if (ev.kind === "level" && ref.current) ref.current.style.setProperty("--lv", String(Math.min(1, ev.level || 0)));
  }), []);
  const ticks = [];
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2, r0 = i % 5 ? 90 : 86;
    ticks.push(html`<line x1=${100 + Math.cos(a) * r0} y1=${100 + Math.sin(a) * r0} x2=${100 + Math.cos(a) * 94} y2=${100 + Math.sin(a) * 94} />`);
  }
  return html`<div class=${"hud-ring tone-" + tone} ref=${ref} role="img" aria-label=${"JARVIS: " + label}>
    <svg viewBox="0 0 200 200" aria-hidden="true">
      <g class="ring-ticks">${ticks}</g>
      <circle class="ring-arc a1" cx="100" cy="100" r="78" pathLength="100" />
      <circle class="ring-arc a2" cx="100" cy="100" r="68" pathLength="100" />
      <circle class="ring-core" cx="100" cy="100" r="50" />
      <text class="ring-name" x="100" y="104">J.A.R.V.I.S.</text>
    </svg>
  </div>`;
}

// Today's 3D orb (orb3d.js) in the HUD. It's made once and kept (WebGL contexts are few), drawn only
// while it's shown.
function Orb({ tone, shown }) {
  const canvas = useRef(null);
  const orb = useRef(null);
  const live = useRef({ act: 0, target: 0, level: 0, levelTarget: 0, shown, tone });
  live.current.shown = shown;
  live.current.tone = tone;
  const s = useStore();
  live.current.target = ACT[s.jarvis] ?? 0.55;
  useEffect(() => {
    let frame = 0, stopped = false, last = 0;
    const off = onEvent((ev) => { if (ev.kind === "level") live.current.levelTarget = ev.level || 0; });
    import("/static/orb3d.js").then(({ createOrb3D }) => createOrb3D(canvas.current)).then((o) => {
      if (stopped || !o) return;
      orb.current = o;
      const t0 = performance.now();
      const draw = (now) => {
        frame = requestAnimationFrame(draw);
        const L = live.current;
        if (!L.shown || document.hidden) return;
        const t = (now - t0) / 1000, dt = t - last;
        last = t;
        L.act += (L.target - L.act) * Math.min(1, dt * 3);
        L.level += (L.levelTarget - L.level) * Math.min(1, dt * 12);
        o.setTint({ muted: "muted", problem: "error", working: "amber" }[L.tone] || "blue");
        o.draw(t, L.act, L.level, dt);
      };
      frame = requestAnimationFrame(draw);
    }).catch((err) => console.warn("the 3D orb didn't load", err));
    return () => { stopped = true; cancelAnimationFrame(frame); off(); };
  }, []);
  return html`<canvas ref=${canvas} class="hud-orb" hidden=${!shown} aria-label="JARVIS's orb"></canvas>`;
}

function Switch({ icon, label, on, title, onClick, disabled, dot }) {
  return html`<button class=${"cap" + (on ? " on" : "")} role="switch" aria-checked=${on ? "true" : "false"} aria-label=${label}
      title=${title} onClick=${onClick} disabled=${disabled}>
    <${Icon} name=${icon} size=${16} /><span>${label}</span>${dot && html`<span class=${"dot " + dot}></span>`}</button>`;
}

function Capabilities() {
  const s = useStore();
  const [agents, setAgents] = useState(null);   // the Agent Hub's agents: reachable / total
  useEffect(() => {
    if (!s.connected || !s.hub.enabled) return;
    let gone = false;
    const load = async () => {
      const st = await ask({ type: "request", what: "status" }, 12000);
      if (gone || !st) return;
      const list = st.hub?.agents || [];
      setAgents({ reachable: !!st.hub?.reachable, up: list.filter((a) => a.reachable).length, total: list.length });
    };
    load();
    const t = setInterval(load, HUB_REFRESH_MS);
    return () => { gone = true; clearInterval(t); };
  }, [s.connected, s.hub.enabled]);
  const muted = s.jarvis === "muted";
  const hubUp = agents ? agents.reachable : s.hub.reachable;
  return html`<div class="caps" role="group" aria-label="Capabilities">
    <${Switch} icon=${muted ? "mic-off" : "mic"} label="Mic" on=${!muted} onClick=${() => control("mute")}
      title=${muted ? "The microphone is muted. Click to listen again (Ctrl+Alt+M)" : "Mute the microphone (Ctrl+Alt+M)"} />
    <${Switch} icon=${s.voice ? "volume-2" : "volume-x"} label="Voice" on=${s.voice} onClick=${() => control("voice")}
      title=${s.voice ? "Replies are spoken. Click for text only" : "Text only. Click to speak replies"} />
    <${Switch} icon=${s.announce ? "bell" : "bell-off"} label="Announce" on=${s.announce} onClick=${() => control("announce")}
      title=${s.announce ? "Agent results, reminders and the briefing are spoken. Click to only show them" : "Background news is shown, not spoken. Click to speak it"} />
    <${Switch} icon="brain" label="Memory" on=${s.keep.on} disabled=${!s.keep.enabled} onClick=${() => control("keep_memory")}
      title=${!s.keep.enabled ? "Conversations aren't kept (Settings, Memory)" : s.keep.on
        ? "This conversation is kept. Click to stop keeping it (until you click again or start a new chat)"
        : "This conversation isn't being kept. Click to keep it again from now on"} />
    <${Switch} icon="bot" label="Agents" on=${!!s.hub.enabled && !!hubUp} disabled=${!s.hub.enabled}
      dot=${!s.hub.enabled ? "" : hubUp ? "ok" : hubUp === false ? "bad" : ""} onClick=${() => control("open_command")}
      title=${!s.hub.enabled ? "The Agent Hub is off (Settings)" : `Agent Hub ${hubUp ? "reachable" : hubUp === false ? "not reachable" : "…"}${
        agents && agents.total ? ` · ${agents.up} of ${agents.total} agents answer` : ""}. Click for the Command centre`} />
  </div>`;
}

export function Hud() {
  const s = useStore();
  const [view, setViewState] = useState(chosenView);
  const setView = (v) => { chosenView = v; setViewState(v); };
  const [orbWanted, setOrbWanted] = useState(chosenView === "orb");
  const tone = hudTone(s.connected ? s.jarvis : "starting", s.connected ? s.problem : null);
  const label = !s.connected ? "Not connected" : STATE_LABEL[s.jarvis] || s.jarvis;
  return html`<section class="hud" aria-label="JARVIS">
    <div class="hud-views" role="group" aria-label="HUD view">
      ${[["ring", "Ring"], ["orb", "Orb"]].map(([v, t]) => html`<button class=${"filter" + (view === v ? " active" : "")}
        aria-pressed=${view === v ? "true" : "false"} onClick=${() => { setView(v); if (v === "orb") setOrbWanted(true); }}>${t}</button>`)}
    </div>
    <div class="hud-stage">
      ${view === "ring" && html`<${Ring} tone=${tone} label=${label} />`}
      ${orbWanted && html`<${Orb} tone=${tone} shown=${view === "orb"} />`}
    </div>
    ${s.connected && s.problem && html`<p class="hud-problem small" role="status"><${Icon} name="triangle-alert" size=${14} /> ${s.problem}</p>`}
    <${Capabilities} />
    ${(s.voiceModel || s.language) && html`<details class="hud-more">
      <summary class="small dim"><${Icon} name="info" size=${13} /> Details</summary>
      <div class="hud-pills">
        ${s.voiceModel && html`<span class="pill" title="The voice model">${s.voiceModel}</span>`}
        ${s.language && html`<span class="pill" title="The language JARVIS listens for">${s.language}</span>`}
      </div>
    </details>`}
  </section>`;
}
