// The JARVIS app: an icon sidebar (Brain, Chat, Command centre, Memory, Settings) and the section beside it.
// The Brain opens first: the graph with JARVIS's HUD and the command bar.
// Split view puts Chat on the left and the Command centre on the right. The section is kept in the
// address's #fragment (s=...) next to the token, so JARVIS can open the window on a section, and a
// "focus" event from JARVIS (target "app") switches section and brings the window forward.
// The same page is the phone app (docs/mobile.md): on a phone the sidebar is a tab bar at the bottom, and
// until the phone is paired it shows the pairing screen (a code from the QR code's #pair=..., or typed).
"use strict";

import { useEffect, useRef, useState } from "/static/vendor/preact/hooks.module.js";
import { html, render, Icon } from "/static/shared/ui.js";
import { applyEvent, getState, setState, useStore } from "/static/shared/store.js";
import { connect, onConnection, onEvent, onUnpaired, paired, phone, send, toWindow } from "/static/shared/ws.js";
import { BrainSection } from "/static/app/sections/brain.js";
import { ChatSection } from "/static/app/sections/chat.js";
import { CommandSection } from "/static/app/sections/command.js";
import { MemorySection } from "/static/app/sections/memory.js";
import { PairScreen } from "/static/app/sections/phone.js";
import { SettingsSection } from "/static/app/sections/settings.js";

export const SECTIONS = [
  { id: "brain", label: "Brain", icon: "network", view: BrainSection },
  { id: "chat", label: "Chat", icon: "message-square", view: ChatSection },
  { id: "command", label: "Command centre", icon: "activity", view: CommandSection },
  { id: "memory", label: "Memory", icon: "brain", view: MemorySection },
  { id: "settings", label: "Settings", icon: "settings", view: SettingsSection },
];
const IDS = SECTIONS.map((s) => s.id);
const STATE_LABEL = { starting: "Starting", asleep: "Asleep", listening: "Listening", speaking: "Speaking",
                      working: "Working", muted: "Microphone muted" };

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const first = phone ? "chat" : "brain";   // the Brain graph is a big-screen view: the phone opens on Chat
  return { section: IDS.includes(p.get("s")) ? p.get("s") : first, split: p.get("split") === "1" };
}

function writeHash(section, split) {
  const p = new URLSearchParams(location.hash.slice(1));
  p.delete("pair");   // a pairing code is used once: never left in the address or the history
  p.set("s", section);
  if (split) p.set("split", "1"); else p.delete("split");
  history.replaceState(null, "", "#" + p.toString());   // the token stays in the fragment
}

function Sidebar({ section, split, go, toggleSplit }) {
  const s = useStore();
  return html`<nav class="side" aria-label="Sections">
    <div class="brand" title="JARVIS"><span class="brand-dot"></span></div>
    ${SECTIONS.map((x) => html`<button class=${"nav" + (x.id === section ? " active" : "")} title=${x.label}
        aria-label=${x.label} aria-current=${x.id === section ? "page" : undefined} onClick=${() => go(x.id)}>
        <${Icon} name=${x.icon} size=${20} /></button>`)}
    <div class="grow"></div>
    <button class=${"nav split-btn" + (split ? " active" : "")} title="Split view: chat and command centre side by side"
      aria-label="Split view" aria-pressed=${split ? "true" : "false"} onClick=${toggleSplit}>
      <${Icon} name="columns-2" size=${20} /></button>
    <span class=${"conn dot " + (s.connected ? "ok" : "bad")} title=${s.connected ? "Connected to JARVIS" : "Not connected"}></span>
  </nav>`;
}

// The dot at the top right of the header is where the orb docks while the app is full screen (ui/window.py):
// the page tells its window where the dot is; the window, while maximized, tells JARVIS where that is on
// the screen, and the orb moves into it. Not on a phone (there's no orb there).
const DOT_SIZE = 40;   // how big the docked orb looks, in pixels
function OrbDot() {
  const s = useStore();
  const ref = useRef(null);
  useEffect(() => {
    const report = () => {
      const r = ref.current && ref.current.getBoundingClientRect();
      if (r) toWindow("dock_slot", { x: r.left + r.width / 2, y: r.top + r.height / 2, size: DOT_SIZE });
    };
    report();
    const ro = new ResizeObserver(report);
    ro.observe(ref.current);
    ro.observe(document.body);
    return () => ro.disconnect();
  }, []);
  return html`<span class=${"orb-dot" + (s.orbDocked ? " docked" : "")} ref=${ref} aria-hidden="true"
    title=${s.orbDocked ? "" : "The orb sits here while the app is full screen"}></span>`;
}
// The window calls this with whether it's full screen, and where the dot is on the screen. The last word is
// sent again whenever the connection (re)opens: it can come before the page is connected, or JARVIS restarts.
let lastDock = null;
window.jarvisDock = (where) => {
  if (!where || typeof where.docked !== "boolean") return;
  lastDock = { type: "orb_dock", ...where };
  send(lastDock);
};
onConnection((on) => { if (on && lastDock) send(lastDock); });

function Header({ title }) {
  const s = useStore();
  return html`<header class="top">
    <h1>${title}</h1>
    <div class="grow"></div>
    <span class="pill" title="What JARVIS is doing">${!s.orbDocked && html`<span class=${"dot " + (s.jarvis === "muted" ? "warn" : s.connected ? "ok" : "bad")}></span>`}
      ${s.connected ? s.activity || STATE_LABEL[s.jarvis] || s.jarvis : "Not connected"}</span>
    ${!s.voice && html`<span class="pill" title="Voice replies are off: JARVIS answers in text only">
      <${Icon} name="volume-x" size=${14} /> Text only</span>`}
    ${!s.announce && html`<span class="pill" title="Announcements are off"><${Icon} name="bell-off" size=${14} /> Quiet</span>`}
    ${!phone && html`<${OrbDot} />`}
  </header>`;
}

function App() {
  const [{ section, split }, setNav] = useState(readHash);
  useEffect(() => writeHash(section, split), [section, split]);
  useEffect(() => onEvent((ev) => {
    if (ev.kind === "focus" && ev.target === "app") {
      if (IDS.includes(ev.section)) setNav((n) => ({ ...n, section: ev.section }));
      toWindow("front");
    }
  }), []);
  const go = (id) => setNav((n) => ({ ...n, section: id }));
  const toggleSplit = () => setNav((n) => ({ section: n.split ? n.section : "chat", split: !n.split }));
  const current = SECTIONS.find((x) => x.id === section);
  const body = split && (section === "chat" || section === "command")
    ? html`<div class="split"><section class="pane"><${ChatSection} compact /></section>
        <section class="pane"><${CommandSection} compact /></section></div>`
    : html`<${current.view} />`;
  return html`<div class="shell">
    <${Sidebar} section=${section} split=${split} go=${go} toggleSplit=${toggleSplit} />
    <main class="main">
      <${Header} title=${split && (section === "chat" || section === "command") ? "Chat and command centre" : current.label} />
      <div class="body">${body}</div>
    </main>
  </div>`;
}

// The phone app: paired, or the pairing screen. The code from a QR code's #pair=... is read once and
// taken out of the address at once.
let linkCode = new URLSearchParams(location.hash.slice(1)).get("pair") || "";   // ignored if already paired
if (linkCode) {
  const p = new URLSearchParams(location.hash.slice(1));
  p.delete("pair");
  history.replaceState(null, "", p.toString() ? "#" + p.toString() : location.pathname);
}

function Root() {
  const s = useStore();
  if (phone && !s.paired) {
    const code = linkCode;
    linkCode = "";   // used once, by the screen's first try
    return html`<${PairScreen} code=${code} onPaired=${() => { setState({ paired: true }); connect(); }} />`;
  }
  return html`<${App} />`;
}

onEvent(applyEvent);
onConnection((on) => setState({ connected: on }));
onUnpaired(() => setState({ paired: false, connected: false }));
setState({ paired: paired() });
if (phone) document.documentElement.classList.add("phone");
render(html`<${Root} />`, document.getElementById("root"));
if (getState().paired) connect();
// The phone app's service worker keeps only the app's own files (never chat data); HTTPS only, so never on the PC.
if (phone && location.protocol === "https:" && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
