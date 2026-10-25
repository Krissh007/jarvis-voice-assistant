// The Brain: everything JARVIS knows and has done, as one graph (docs/brain-ui-plan.md). Remembered
// facts, conversations, tools by category, the Agent Hub's agents and actions, reminders and the files
// JARVIS used; every link says why it's there. Left: search, the inspector (read-only), the busiest hubs
// and the layout controls. Right: JARVIS's HUD and capability switches (brain-hud.js), then the legend,
// which is also the filter. Below the graph: the command bar (brain-ask.js). Click a node to focus it,
// Shift-click another to trace the path between them. What JARVIS uses while it works pulses: the tool it
// calls, the facts it recalls, the Agent Hub when an agent reports. The graph is refreshed when JARVIS
// says it changed (brain_update), and where each node sits is kept by JARVIS, so the brain opens the way
// it was left.
"use strict";

import { useEffect, useMemo, useRef, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, control, onEvent, send } from "/static/shared/ws.js";
import { BrainCanvas } from "/static/app/sections/brain-canvas.js";
import { AskBar } from "/static/app/sections/brain-ask.js";
import { Hud } from "/static/app/sections/brain-hud.js";
import { TYPES, colorOf, details, layoutChunks, linkBetween, neighbours, path, prepare, search, topHubs,
         typeCounts, typeLabel } from "/static/app/sections/brain-model.js";

const SAVE_AFTER_MS = 1500;   // after the layout settles or a node is dragged
const MAX_NEIGHBOURS = 40;    // listed in the inspector
const MAX_LINES = 80;         // of a conversation, read in the inspector

function Dot({ color }) { return html`<span class="dot" style=${{ background: color }}></span>`; }

function NodeButton({ node, onPick, children }) {
  return html`<button class="brain-node" onClick=${() => onPick(node)} title=${node.label}>
    <${Dot} color=${colorOf(node)} /><span class="brain-node-label">${node.label}</span>${children}</button>`;
}

function Search({ graph, hidden, onPick }) {
  const [q, setQ] = useState("");
  const found = useMemo(() => (graph ? search(graph.nodes, q, hidden) : []), [graph, q, hidden]);
  return html`<div class="brain-search">
    <label class="brain-search-box"><${Icon} name="search" size=${15} />
      <input type="text" placeholder="Search the brain" aria-label="Search the brain" value=${q}
        onInput=${(e) => setQ(e.target.value)}
        onKeyDown=${(e) => { if (e.key === "Enter" && found[0]) onPick(found[0]); if (e.key === "Escape") setQ(""); }} /></label>
    ${q && html`<ul class="brain-list" aria-label="Search results">
      ${found.length ? found.map((n) => html`<li key=${n.id}><${NodeButton} node=${n} onPick=${onPick}>
          <span class="faint small">${typeLabel(n)}</span></${NodeButton}></li>`)
        : html`<li class="faint small brain-none">Nothing called that.</li>`}
    </ul>`}
  </div>`;
}

function Inspector({ focused, trail, onPick, onClear, onForgotten }) {
  if (trail.length > 1) {
    return html`<section class="brain-card" aria-label="Path">
      <div class="brain-card-head"><${Icon} name="route" size=${15} /> Path · ${trail.length - 1} step${trail.length > 2 ? "s" : ""}
        <span class="grow"></span><button class="icon-btn" title="Clear" aria-label="Clear the path" onClick=${onClear}><${Icon} name="x" size=${14} /></button></div>
      <ol class="brain-path">${trail.map((n, i) => html`<li key=${n.id}>
        <${NodeButton} node=${n} onPick=${onPick} />
        ${trail[i + 1] && html`<div class="brain-why small">${linkBetween(n, trail[i + 1])?.reason || ""}</div>`}</li>`)}</ol>
    </section>`;
  }
  if (!focused) {
    return html`<section class="brain-card brain-hint" aria-label="Inspector">
      <div class="brain-card-head"><${Icon} name="info" size=${15} /> Inspector</div>
      <p class="dim small">Click a node to see what it is and why it's linked. Shift-click a second node to trace the path
        between them. Drag to move, scroll to zoom, double-click to zoom in.</p></section>`;
  }
  const near = neighbours(focused).sort((a, b) => (a.kind === "structure") - (b.kind === "structure") || b.node.degree - a.node.degree);
  const d = focused.detail || {};
  return html`<section class="brain-card" aria-label="Inspector">
    <div class="brain-card-head"><${Dot} color=${colorOf(focused)} /> ${typeLabel(focused)}
      <span class="grow"></span><button class="icon-btn" title="Clear" aria-label="Clear the focus" onClick=${onClear}><${Icon} name="x" size=${14} /></button></div>
    <h2 class="brain-title">${focused.label}</h2>
    ${d.description && html`<p class="dim small brain-desc">${d.description}</p>`}
    ${d.day && html`<p class="faint small">${d.day}</p>`}
    <dl class="brain-facts">${details(focused).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>
    <${Actions} key=${focused.id} node=${focused} onForgotten=${onForgotten} />
    <h3>Linked to · ${near.length}</h3>
    <ul class="brain-list">${near.slice(0, MAX_NEIGHBOURS).map(({ node, reason }) => html`<li key=${node.id}>
      <${NodeButton} node=${node} onPick=${onPick} /><div class="brain-why small">${reason}</div></li>`)}
      ${near.length > MAX_NEIGHBOURS && html`<li class="faint small brain-none">and ${near.length - MAX_NEIGHBOURS} more</li>`}</ul>
  </section>`;
}

// What you can do with the focused node: forget a fact (a second click confirms), read a conversation,
// show a file JARVIS used in Explorer, open the Command centre for an agent. Each is the user's own click.
function Actions({ node, onForgotten }) {
  const [armed, setArmed] = useState(false);
  const [note, setNote] = useState("");
  const [lines, setLines] = useState(null);
  const id = node.id;
  const fact = node.type === "memory" && !node.hub && id.startsWith("fact:");
  const conv = node.type === "conversation" && !node.hub && id.startsWith("conv:");
  const file = node.type === "file" && !node.hub && id.startsWith("file:");
  const agent = node.type === "agent";
  if (!fact && !conv && !file && !agent) return null;

  async function forget() {
    if (!armed) { setArmed(true); return; }
    setArmed(false);
    const d = await ask({ type: "forget", fact: Number(id.slice(5)) });
    setNote(d ? d.message : "Not connected.");
    if (d && d.ok) onForgotten(node);
  }
  async function read() {
    if (lines) { setLines(null); return; }
    const d = await ask({ type: "request", what: "conversation", arg: Number(id.slice(5)) });
    setLines(d ? d.lines || [] : []);
    if (!d) setNote("Not connected.");
  }
  async function show() {
    setNote("Opening…");
    const d = await ask({ type: "show_file", node: id }, 12000);
    setNote(d ? d.message : "Not connected.");
  }
  return html`<div class="brain-actions">
    <div class="brain-action-row">
      ${fact && html`<button class=${"btn danger small-btn" + (armed ? " armed" : "")} onClick=${forget} onBlur=${() => setArmed(false)}
        title="Forget this fact (asks once more)"><${Icon} name="trash-2" size=${13} /> ${armed ? "Sure? Forget" : "Forget"}</button>`}
      ${conv && html`<button class="btn small-btn" onClick=${read} aria-expanded=${lines ? "true" : "false"}>
        <${Icon} name="message-square" size=${13} /> ${lines ? "Hide the conversation" : "Read the conversation"}</button>`}
      ${file && html`<button class="btn small-btn" onClick=${show} title="Open File Explorer at this file">
        <${Icon} name="search" size=${13} /> Show in folder</button>`}
      ${agent && html`<button class="btn small-btn" onClick=${() => control("open_command")}>
        <${Icon} name="activity" size=${13} /> Command centre</button>`}
    </div>
    ${note && html`<p class="small dim brain-note" role="status">${note}</p>`}
    ${lines && html`<ol class="conv-lines brain-lines" aria-label="The conversation">
      ${lines.length === 0 && html`<li class="faint small">No lines kept.</li>`}
      ${lines.slice(0, MAX_LINES).map((l) => html`<li key=${l.id} class=${l.role}>
        <span class="who">${l.role === "assistant" ? "JARVIS" : "You"}</span><span class="said">${l.text}</span></li>`)}
      ${lines.length > MAX_LINES && html`<li class="faint small">and ${lines.length - MAX_LINES} more lines (Memory section)</li>`}
    </ol>`}
  </div>`;
}

function Hubs({ graph, hidden, onPick }) {
  const hubs = graph ? topHubs(graph.nodes, hidden) : [];
  if (!hubs.length) return null;
  return html`<section class="brain-card" aria-label="Top hubs">
    <div class="brain-card-head"><${Icon} name="network" size=${15} /> Top hubs</div>
    <ul class="brain-list">${hubs.map((n) => html`<li key=${n.id}><${NodeButton} node=${n} onPick=${onPick}>
      <span class="faint small">${n.degree}</span></${NodeButton}></li>`)}</ul></section>`;
}

function Controls({ forces, setForces, labels, setLabels, showLinks, setShowLinks, view }) {
  const slider = (key, label, min, max) => html`<label class="brain-slider"><span>${label}</span>
    <input type="range" min=${min} max=${max} value=${forces[key]} aria-label=${label}
      onInput=${(e) => setForces({ ...forces, [key]: Number(e.target.value) })} /></label>`;
  return html`<section class="brain-card" aria-label="Layout">
    <div class="brain-card-head"><${Icon} name="sliders-horizontal" size=${15} /> Layout</div>
    ${slider("repel", "Push apart", 10, 200)}
    ${slider("distance", "Link length", 15, 120)}
    <div class="brain-toggles">
      <span class="small dim">Labels</span>
      <div class="filters">${[["hubs", "Hubs"], ["all", "All"], ["none", "None"]].map(([v, t]) => html`
        <button class=${"filter" + (labels === v ? " active" : "")} aria-pressed=${labels === v ? "true" : "false"}
          onClick=${() => setLabels(v)}>${t}</button>`)}</div>
    </div>
    <div class="brain-toggles">
      <span class="small dim">Links</span>
      <button class="switch" role="switch" aria-checked=${showLinks ? "true" : "false"} aria-label="Show links"
        onClick=${() => setShowLinks(!showLinks)}></button>
      <span class="grow"></span>
      <button class="btn small-btn" title="Lay the brain out again from the start" onClick=${() => view.current?.shake()}>
        <${Icon} name="refresh-cw" size=${13} /> Re-arrange</button>
    </div>
  </section>`;
}

function Legend({ graph, hidden, toggle }) {
  const counts = typeCounts(graph);
  return html`<aside class="brain-legend" aria-label="Legend: click a type to hide or show it">
    <h3>Show</h3>
    <ul>${Object.entries(TYPES).map(([t, info]) => html`<li key=${t}>
      <button class=${"brain-type" + (hidden.has(t) ? " off" : "")} aria-pressed=${hidden.has(t) ? "false" : "true"}
        aria-label=${`${hidden.has(t) ? "Show" : "Hide"} ${info.label.toLowerCase()} (${counts[t]})`}
        title=${hidden.has(t) ? `Show ${info.label.toLowerCase()}` : `Hide ${info.label.toLowerCase()}`} onClick=${() => toggle(t)}>
        <${Dot} color=${info.color} /><span>${info.label}</span><span class="faint">${counts[t]}</span></button></li>`)}</ul>
    ${graph && html`<p class="faint small brain-stamp">${graph.nodes.length} nodes · ${graph.links.length} links</p>`}
    ${graph?.capped?.length > 0 && html`<p class="faint small">Showing the newest ${graph.capped.join(" and ")} only.</p>`}
  </aside>`;
}

// What JARVIS is using right now, lit up on the graph (only nodes that are in it).
export function highlight(v, ev) {
  if (!v) return;
  if (ev.kind === "tool_started" || ev.kind === "confirm_requested") v.pulse("tool:" + ev.tool, ev.kind === "confirm_requested" ? 8 : 4);
  else if (ev.kind === "memory_used") {
    const said = (ev.facts || []).map((f) => norm(f).slice(0, 60)).filter(Boolean);
    for (const n of v.nodes) if (n.type === "memory" && !n.hub && said.some((f) => norm(n.label).startsWith(f.slice(0, 40)))) v.pulse(n.id, 5);
  } else if (ev.kind === "agent_update") {
    v.pulse("hub:agents", 5);
  }
}

function norm(text) { return String(text || "").toLowerCase().replace(/\s+/g, " ").trim(); }

export function BrainSection() {
  const s = useStore();
  const canvasRef = useRef(null);
  const view = useRef(null);
  const [graph, setGraph] = useState(null);       // prepare()'d graph
  const [state, setStateMsg] = useState("loading");
  const [hidden, setHidden] = useState(() => new Set());
  const [focused, setFocused] = useState(null);
  const [trail, setTrail] = useState([]);
  const [forces, setForcesState] = useState({ repel: 60, distance: 40 });
  const [labels, setLabelsState] = useState("hubs");
  const [showLinks, setShowLinksState] = useState(true);
  // what the canvas's callbacks need now, not as it was when they were made
  const live = useRef({ focused: null, hidden, saveTimer: 0, loading: false, again: false, loaded: false });

  // the canvas lives as long as the section
  useEffect(() => {
    const v = new BrainCanvas(canvasRef.current, {
      onFocus: (node, shift) => pick(node, { shift }),
      onSettled: () => scheduleSave(),
      inset: () => {   // the legend floats over the graph in a narrow window
        const legend = canvasRef.current?.closest(".brain")?.querySelector(".brain-legend");
        return legend && getComputedStyle(legend).position === "absolute" ? legend.offsetWidth + 20 : 0;
      },
    });
    view.current = v;
    canvasRef.current.brainView = v;   // for the browser tests: where a node is on screen
    return () => { clearTimeout(live.current.saveTimer); v.destroy(); view.current = null; };
  }, []);

  function scheduleSave() {
    clearTimeout(live.current.saveTimer);
    live.current.saveTimer = setTimeout(() => {
      const v = view.current;
      if (!v || !v.nodes.length) return;
      for (const chunk of layoutChunks(v.nodes)) send({ type: "brain_layout", ...chunk });
    }, SAVE_AFTER_MS);
  }

  async function load() {
    const L = live.current;
    if (L.loading) { L.again = true; return; }
    L.loading = true;
    const data = await ask({ type: "request", what: "brain" }, 20000);
    L.loading = false;
    if (data && Array.isArray(data.nodes)) {
      const v = view.current;
      const previous = new Map((v?.nodes || []).map((n) => [n.id, n]));
      const g = { ...prepare(data, previous, data.layout || {}), counts: data.counts, capped: data.capped || [],
                  generated: data.generated };
      v?.setGraph(g);
      setGraph(g);
      setStateMsg(g.nodes.length > 1 ? "ready" : "empty");
      const f = L.focused && g.byId.get(L.focused.id);
      L.focused = f || null;
      setFocused(f || null);
      setTrail((t) => t.map((n) => g.byId.get(n.id)).filter(Boolean));
      L.loaded = true;
    } else if (!L.loaded) {
      setStateMsg("failed");
    }
    if (L.again) { L.again = false; load(); }
  }

  useEffect(() => { if (s.connected) load(); }, [s.connected]);
  useEffect(() => onEvent((ev) => {
    if (ev.kind === "brain_update") load();
    else highlight(view.current, ev);
  }), []);

  function pick(node, { shift = false, center = false } = {}) {
    const v = view.current;
    const L = live.current;
    if (!node) { L.focused = null; setFocused(null); setTrail([]); v?.focus(null); return; }
    if (shift && L.focused && L.focused !== node) {
      const p = path(L.focused, node, L.hidden);
      setTrail(p.length > 1 ? p : []);
      v?.trace(p);
      return;
    }
    L.focused = node;
    setFocused(node);
    setTrail([]);
    v?.focus(node, { center });
  }

  const pickFromList = (node) => pick(node, { center: true });
  const clear = () => pick(null);
  const toggle = (t) => setHidden((h) => {
    const n = new Set(h);
    if (n.has(t)) n.delete(t); else n.add(t);
    live.current.hidden = n;
    view.current?.setHidden(n);
    return n;
  });
  const setForces = (f) => { setForcesState(f); view.current?.setForces(f); };
  const setLabels = (v) => { setLabelsState(v); view.current?.setDisplay({ labels: v }); };
  const setShowLinks = (v) => { setShowLinksState(v); view.current?.setDisplay({ showLinks: v }); };

  useEffect(() => {
    const key = (e) => {
      if (e.key === "Escape" && !e.target.closest?.("input, textarea")) pick(null);
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, []);

  const total = graph ? graph.nodes.length : 0;
  return html`<div class="brain">
    <div class="brain-left">
      <${Search} graph=${graph} hidden=${hidden} onPick=${pickFromList} />
      <${Inspector} focused=${focused} trail=${trail} onPick=${pickFromList} onClear=${clear} onForgotten=${() => setTimeout(clear, 1200)} />
      <${Hubs} graph=${graph} hidden=${hidden} onPick=${pickFromList} />
      <${Controls} forces=${forces} setForces=${setForces} labels=${labels} setLabels=${setLabels}
        showLinks=${showLinks} setShowLinks=${setShowLinks} view=${view} />
    </div>
    <div class="brain-stage">
      <canvas ref=${canvasRef} class="brain-canvas" role="img"
        aria-label=${`JARVIS's brain: ${total} nodes. Use the search and the inspector to explore it.`}></canvas>
      <div class="brain-zoom">
        <button class="icon-btn" title="Zoom in" aria-label="Zoom in" onClick=${() => view.current?.zoom(1.25)}><${Icon} name="zoom-in" size=${16} /></button>
        <button class="icon-btn" title="Zoom out" aria-label="Zoom out" onClick=${() => view.current?.zoom(0.8)}><${Icon} name="zoom-out" size=${16} /></button>
        <button class="icon-btn" title="Fit everything" aria-label="Fit everything" onClick=${() => view.current?.fit()}><${Icon} name="maximize-2" size=${16} /></button>
        ${focused && html`<button class="icon-btn" title="Go to the focused node" aria-label="Go to the focused node"
          onClick=${() => view.current?.centerOn(focused)}><${Icon} name="locate-fixed" size=${16} /></button>`}
      </div>
      <${AskBar} />
      ${state !== "ready" && html`<div class="brain-empty">
        ${state === "loading" && html`<p class="dim">${s.connected ? "Reading the brain…" : "Not connected to JARVIS."}</p>`}
        ${state === "empty" && html`<p class="dim">Nothing in the brain yet. Talk to JARVIS and it fills up.</p>`}
        ${state === "failed" && html`<p class="dim">The brain couldn't be read. <button class="btn small-btn" onClick=${load}>Try again</button></p>`}
      </div>`}
    </div>
    <div class="brain-right">
      <${Hud} />
      <${Legend} graph=${graph} hidden=${hidden} toggle=${toggle} />
    </div>
  </div>`;
}
