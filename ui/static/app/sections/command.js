// Command centre: a strip with the state of everything JARVIS depends on (voice session, Agent Hub and
// its agents, which model does what, the latest routing decision, tools loaded), and a live timeline of
// what JARVIS did, newest first, with Approve / Cancel on anything waiting for you.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, control } from "/static/shared/ws.js";
import { useTimeline } from "/static/app/sections/command-model.js";

const DOT = { running: "warn", waiting: "warn", done: "ok", failed: "bad", cancelled: "" };
const STATE = { running: "running", waiting: "waiting for you", done: "done", failed: "failed", cancelled: "cancelled" };
const REFRESH_MS = 30000;
const ICON = { tool: "cpu", confirm: "triangle-alert", agent: "bot", route: "sparkles" };

function clock(ms) {
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function tidy(name) { return String(name || "").replace(/_/g, " "); }

// ---------------------------------------------------------------- the strip

function Card({ icon, title, children }) {
  return html`<div class="strip-card"><div class="strip-title"><${Icon} name=${icon} size=${14} /> ${title}</div>${children}</div>`;
}

function Strip({ compact }) {
  const s = useStore();
  const [st, setSt] = useState(null);
  const [loading, setLoading] = useState(false);
  async function load() {
    setLoading(true);
    const d = await ask({ type: "request", what: "status" }, 12000);
    if (d) setSt(d);
    setLoading(false);
  }
  useEffect(() => {
    if (!s.connected) return;
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [s.connected]);
  if (!st) return html`<div class="strip"><p class="dim small">${s.connected ? "Checking…" : "Not connected to JARVIS."}</p></div>`;
  const hub = st.hub || {};
  const agents = hub.agents || [];
  const r = st.route;
  return html`<div class=${"strip" + (compact ? " compact" : "")}>
    <${Card} icon="plug" title="Agents">
      <div class="strip-row"><span class=${"dot " + (st.voice_session ? "ok" : "bad")}></span>Voice session
        <span class="faint small">${st.voice_session ? "connected" : "not connected"}</span></div>
      <div class="strip-row"><span class=${"dot " + (!hub.enabled ? "" : hub.reachable ? "ok" : "bad")}></span>Agent Hub
        <span class="faint small">${!hub.enabled ? "off" : hub.reachable ? (hub.planner ? "reachable · plans requests" : "reachable · rules only") : "not reachable"}</span></div>
      ${agents.map((a) => html`<div class="strip-row sub" key=${a.name} title=${a.description}>
        <span class=${"dot " + (a.reachable ? "ok" : a.optional ? "" : "bad")}></span>${a.name}
        <span class="faint small">${a.reachable ? `${a.tools} tools` : a.optional ? "not set up" : a.error || "not reachable"}</span></div>`)}
    </${Card}>
    <${Card} icon="cpu" title="Models">
      <div class="strip-row"><${Icon} name="mic" size=${13} />Voice<span class="mono small">${st.models.voice || "—"}</span></div>
      <div class="strip-row"><${Icon} name="brain" size=${13} />Reasoning<span class="mono small">${st.models.reasoning || st.models.reasoning_fallback || "—"}</span></div>
      ${st.models.reasoning && html`<div class="strip-row sub faint small">falls back to ${st.models.reasoning_fallback}</div>`}
      <div class="strip-row"><${Icon} name="sparkles" size=${13} />Last route
        <span class="small">${r ? `${r.route}${r.models && r.models.length ? " · " + r.models[0] : ""}${r.fallback ? " · fallback" : ""}` : "none yet"}</span></div>
    </${Card}>
    <${Card} icon="layout-dashboard" title="Tools">
      <div class="strip-big">${st.tools.loaded}<span class="dim small"> loaded in JARVIS</span></div>
      ${hub.registry_tools != null && html`<div class="dim small">${hub.registry_tools} available in the Agent Hub's registry</div>`}
      <button class="btn small-btn" disabled=${loading} onClick=${load}><${Icon} name="refresh-cw" size=${13} /> ${loading ? "Checking…" : "Check now"}</button>
    </${Card}>
  </div>`;
}

// ---------------------------------------------------------------- the timeline

function Entry({ it }) {
  const pending = it.type === "confirm" && it.state === "waiting";
  return html`<li class=${"entry " + it.state}>
    <span class=${"dot " + DOT[it.state]} title=${STATE[it.state]}></span>
    <div class="entry-body">
      <div class="entry-head"><${Icon} name=${ICON[it.type] || "cpu"} size=${14} />
        <span class="entry-title">${it.type === "confirm" ? "Asked to confirm" : tidy(it.tool)}</span>
        ${it.type === "tool" && it.step === true && html`<span class="badge">plan step</span>`}
        <span class="faint small">${STATE[it.state]}</span>
        <span class="grow"></span><span class="faint small mono">${clock(it.at)}</span></div>
      ${it.detail && html`<div class="entry-detail">${it.detail}</div>`}
      ${it.result && html`<div class="entry-result dim small">${it.result}</div>`}
      ${pending && html`<div class="plan-actions">
        <button class="btn primary" onClick=${() => control("confirm")}><${Icon} name="check" size=${15} /> Approve</button>
        <button class="btn" onClick=${() => control("cancel")}><${Icon} name="x" size=${15} /> Cancel</button></div>`}
    </div>
  </li>`;
}

export function CommandSection({ compact }) {
  const items = useTimeline();
  const [filter, setFilter] = useState("all");
  const shown = items.filter((x) => filter === "all" || (filter === "waiting" ? x.state === "waiting" || x.state === "running"
    : filter === "failed" ? x.state === "failed" : true));
  const waiting = items.filter((x) => x.type === "confirm" && x.state === "waiting").length;
  return html`<div class=${"command" + (compact ? " compact" : "")}>
    <${Strip} compact=${compact} />
    <div class="timeline-head">
      <h2><${Icon} name="activity" size=${17} /> Activity ${waiting > 0 && html`<span class="badge warn">${waiting} waiting for you</span>`}</h2>
      <div class="filters" role="group" aria-label="Show">
        ${[["all", "All"], ["waiting", "In progress"], ["failed", "Failed"]].map(([id, name]) => html`<button key=${id}
          class=${"filter" + (filter === id ? " active" : "")} aria-pressed=${filter === id ? "true" : "false"}
          onClick=${() => setFilter(id)}>${name}</button>`)}
      </div>
    </div>
    ${shown.length === 0
      ? html`<p class="dim timeline-empty">${items.length ? "Nothing here with this filter." : "What JARVIS does (tools, agent results, plans waiting for your yes) appears here as it happens."}</p>`
      : html`<ol class="timeline">${shown.map((it) => html`<${Entry} key=${it.id} it=${it} />`)}</ol>`}
  </div>`;
}
