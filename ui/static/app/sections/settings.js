// Settings: a category list and the chosen category's settings. Everything shown comes from JARVIS
// ({type: "request", what: "settings"}); each change goes back as {type: "setting", key, value} and
// JARVIS checks it (kind, range, hotkey clashes) before keeping it. Voice, Announce and the orb's look
// apply at once; the rest are kept for the next start and marked "Restart to apply".
// Keys and tokens are only ever shown as set / not set: their values never reach this page.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, onEvent } from "/static/shared/ws.js";
import { FactsList } from "/static/app/sections/facts.js";

const PERCENT = new Set(["look.core_size", "look.orb_brightness", "look.cloud_brightness", "look.ribbon_brightness"]);
const CHOICE_LABEL = { wake_word: "Wake word", push_to_talk: "Push to talk", always_on: "Always on" };

const SECRET_USE = { GOOGLE_API_KEY: "Gemini: voice and text models", GROQ_API_KEY: "Groq: reasoning models",
                     TAVILY_API_KEY: "Web search (else DuckDuckGo)", AGENT_HUB_TOKEN: "Agent Hub",
                     OPENROUTER_API_KEY: "OpenRouter fallback (optional)" };

function show(item, value) {
  if (item.kind === "bool" || typeof value === "boolean") return value ? "On" : "Off";
  if (PERCENT.has(item.key)) return Math.round(value * 100) + "%";
  if (item.key === "look.orb_size") return value + " px";
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined || value === "") return "Not set";
  return CHOICE_LABEL[value] || String(value);
}

// ---------------------------------------------------------------- one control per kind

function Toggle({ item, save }) {
  return html`<button class="switch" role="switch" aria-checked=${item.value ? "true" : "false"} aria-label=${item.label}
    disabled=${!item.editable} onClick=${() => save(item.key, !item.value)}></button>`;
}

function Range({ item, save }) {
  const [v, setV] = useState(item.value);
  useEffect(() => setV(item.value), [item.value]);
  return html`<div class="range">
    <input type="range" min=${item.low} max=${item.high} step=${item.step || 1} value=${v} aria-label=${item.label}
      onInput=${(e) => setV(Number(e.target.value))} onChange=${(e) => save(item.key, Number(e.target.value))} />
    <output>${show(item, v)}</output>
  </div>`;
}

function Choice({ item, save }) {
  return html`<select aria-label=${item.label} value=${item.value} onChange=${(e) => save(item.key, e.target.value)}>
    ${item.choices.map((c) => html`<option value=${c} selected=${c === item.value}>${CHOICE_LABEL[c] || c}</option>`)}
  </select>`;
}

function TextValue({ item, save }) {
  const list = item.kind === "models";
  const initial = list ? (item.value || []).join(", ") : item.value || "";
  const [v, setV] = useState(initial);
  useEffect(() => setV(initial), [initial]);
  const value = list ? v.split(",").map((x) => x.trim()).filter(Boolean) : v.trim();
  return html`<div class="text-value">
    <input type="text" value=${v} aria-label=${item.label} spellcheck="false"
      onInput=${(e) => setV(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && v !== initial && save(item.key, value)} />
    <button class="btn" disabled=${v === initial} onClick=${() => save(item.key, value)}><${Icon} name="save" size=${15} /> Save</button>
  </div>`;
}

const KEY_NAMES = { " ": "space", Enter: "enter", Tab: "tab", Backspace: "backspace" };

function comboFrom(e) {
  if (["Control", "Alt", "Shift", "Meta", "OS"].includes(e.key)) return null;   // still holding modifiers
  let key = KEY_NAMES[e.key] || (/^F([1-9]|1[0-2])$/.test(e.key) ? e.key.toLowerCase() : null);
  if (!key && /^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
  if (!key && /^Digit[0-9]$/.test(e.code)) key = e.code.slice(5);
  if (!key) return "";
  return [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "cmd", key].filter(Boolean).join("+");
}

function Hotkey({ item, save }) {
  const [listening, setListening] = useState(false);
  const [hint, setHint] = useState("");
  useEffect(() => {
    if (!listening) return;
    const onKey = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") { setListening(false); setHint(""); return; }
      const combo = comboFrom(e);
      if (combo === null) return;
      if (!combo) { setHint("JARVIS can't use that key. Letters, digits, F1–F12, Space, Enter, Tab."); return; }
      setListening(false);
      setHint("");
      save(item.key, combo);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [listening]);
  return html`<div class="hotkey">
    <kbd>${listening ? "Press the keys…" : item.value || "None"}</kbd>
    <button class="btn" onClick=${() => setListening(!listening)}>${listening ? "Cancel" : "Change"}</button>
    ${hint && html`<span class="dim small">${hint}</span>`}
  </div>`;
}

function Control({ item, save }) {
  if (!item.editable) return html`<span class="value">${show(item, item.value)}</span>`;
  switch (item.kind) {
    case "bool": return html`<${Toggle} item=${item} save=${save} />`;
    case "int": case "float": return html`<${Range} item=${item} save=${save} />`;
    case "choice": return html`<${Choice} item=${item} save=${save} />`;
    case "hotkey": return html`<${Hotkey} item=${item} save=${save} />`;
    case "model": case "models": return html`<${TextValue} item=${item} save=${save} />`;
    default: return html`<span class="value">${show(item, item.value)}</span>`;
  }
}

function Row({ item, save, note }) {
  return html`<div class="row" key=${item.key}>
    <div class="row-text">
      <div class="row-label">${item.label}
        ${item.restart && html`<span class="badge warn" title="Saved; JARVIS uses it after a restart">Restart to apply</span>`}
        ${item.applies === "now" && item.editable && html`<span class="badge" title="Takes effect at once">Live</span>`}</div>
      ${item.help && html`<div class="dim small">${item.help}</div>`}
      ${note && html`<div class=${"small " + (note.ok ? "ok-text" : "bad-text")} role="status">${note.message}</div>`}
    </div>
    <div class="row-control"><${Control} item=${item} save=${save} /></div>
  </div>`;
}

// ---------------------------------------------------------------- extra panels in some categories

function Connections({ status }) {
  const hub = status.agent_hub;
  return html`<div class="status-list">
    <div class="status-item"><span class=${"dot " + (status.voice_session ? "ok" : "bad")}></span>
      <span>Voice session (Gemini Live)</span><span class="dim">${status.voice_session ? "Connected" : "Not connected"}</span></div>
    <div class="status-item"><span class=${"dot " + (hub === true ? "ok" : hub === false ? "bad" : "")}></span>
      <span>Agent Hub</span><span class="dim">${hub === true ? "Reachable" : hub === false ? "Not reachable" : "Not checked yet"}</span></div>
    <h3>Keys and tokens</h3>
    <p class="dim small">Kept in JARVIS's .env file. Only whether each one is set is shown here, never its value.</p>
    ${status.secrets.map((k) => html`<div class="status-item" key=${k.name}><${Icon} name="key-round" size=${15} />
      <code>${k.name}</code><span class="dim small key-use">${SECRET_USE[k.name] || ""}</span>
      <span class=${"key-state " + (k.set ? "ok-text" : "faint")}>${k.set ? "Set" : "Not set"}</span></div>`)}
  </div>`;
}

// ---------------------------------------------------------------- the page

let lastCategory = "voice";   // kept while you visit other sections

export function SettingsSection() {
  const s = useStore();
  const [view, setView] = useState(null);
  const [category, setCategoryState] = useState(lastCategory);
  const setCategory = (id) => { lastCategory = id; setCategoryState(id); };
  const [notes, setNotes] = useState({});           // key -> {ok, message} of its last change

  const load = () => ask({ type: "request", what: "settings" }).then((d) => d && setView(d));
  useEffect(() => { if (s.connected) load(); }, [s.connected]);
  // Voice, Announce or the orb's look changed elsewhere (a button, the orb's sliders): show it.
  useEffect(() => onEvent((ev) => { if (["voice", "announce", "look"].includes(ev.kind)) load(); }), []);

  async function save(key, value) {
    const d = await ask({ type: "setting", key, value });
    if (!d) { setNotes((n) => ({ ...n, [key]: { ok: false, message: "JARVIS didn't answer. Try again." } })); return; }
    setNotes((n) => ({ ...n, [key]: { ok: d.ok, message: d.message } }));
    setView(d.settings);
  }

  if (!view) return html`<div class="page"><p class="dim">${s.connected ? "Loading settings…" : "Not connected to JARVIS."}</p></div>`;
  const cat = view.categories.find((c) => c.id === category) || view.categories[0];
  const restart = view.status.restart || [];
  return html`<div class="settings">
    <nav class="cats" aria-label="Settings categories">
      ${view.categories.map((c) => html`<button key=${c.id} class=${"cat" + (c.id === cat.id ? " active" : "")}
          aria-current=${c.id === cat.id ? "page" : undefined} onClick=${() => setCategory(c.id)}>
          <${Icon} name=${c.icon} size=${17} /> <span>${c.label}</span><${Icon} name="chevron-right" size=${15} /></button>`)}
    </nav>
    <div class="cat-body">
      ${restart.length > 0 && html`<div class="banner"><${Icon} name="rotate-ccw" size=${16} />
        ${restart.length === 1 ? "1 change applies" : `${restart.length} changes apply`} after you restart JARVIS.</div>`}
      <h2>${cat.label}</h2>
      ${cat.id === "connections" && html`<${Connections} status=${view.status} />`}
      <div class="rows">${cat.items.map((item) => html`<${Row} item=${item} save=${save} note=${notes[item.key]} />`)}</div>
      ${cat.id === "hotkeys" && html`<p class="dim small">While you choose a key, JARVIS's current keys still work, so
        pressing one (say Ctrl+Alt+M) also does what it does now.</p>`}
      ${cat.id === "memory" && html`<p class="dim small">Say "don't remember this conversation" to stop keeping the current
        one, or "forget today's conversation" to delete it. Past conversations are on the Memory page.</p>`}
      ${cat.id === "appearance" && html`<div class="row"><div class="row-text"><div class="row-label">Accent colour</div>
        <div class="dim small">One accent across the orb and the app.</div></div>
        <div class="row-control"><span class="swatch"></span><span class="value">${view.status.accent}</span></div></div>`}
      ${cat.id === "memory" && html`<h3>Remembered facts</h3><${FactsList} />`}
    </div>
  </div>`;
}
