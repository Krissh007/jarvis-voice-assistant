// The JARVIS app: an icon sidebar (Chat, Brain, Command centre, Memory, Settings) and the section beside it.
// Split view puts Chat on the left and the Command centre on the right. The section is kept in the
// address's #fragment (s=...) next to the token, so JARVIS can open the window on a section, and a
// "focus" event from JARVIS (target "app") switches section and brings the window forward.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, render, Icon } from "/static/shared/ui.js";
import { applyEvent, setState, useStore } from "/static/shared/store.js";
import { connect, onConnection, onEvent, toWindow } from "/static/shared/ws.js";
import { BrainSection } from "/static/app/sections/brain.js";
import { ChatSection } from "/static/app/sections/chat.js";
import { CommandSection } from "/static/app/sections/command.js";
import { MemorySection } from "/static/app/sections/memory.js";
import { SettingsSection } from "/static/app/sections/settings.js";

export const SECTIONS = [
  { id: "chat", label: "Chat", icon: "message-square", view: ChatSection },
  { id: "brain", label: "Brain", icon: "network", view: BrainSection },
  { id: "command", label: "Command centre", icon: "activity", view: CommandSection },
  { id: "memory", label: "Memory", icon: "brain", view: MemorySection },
  { id: "settings", label: "Settings", icon: "settings", view: SettingsSection },
];
const IDS = SECTIONS.map((s) => s.id);
const STATE_LABEL = { starting: "Starting", asleep: "Asleep", listening: "Listening", speaking: "Speaking",
                      working: "Working", muted: "Microphone muted" };

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { section: IDS.includes(p.get("s")) ? p.get("s") : "chat", split: p.get("split") === "1" };
}

function writeHash(section, split) {
  const p = new URLSearchParams(location.hash.slice(1));
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
    <button class=${"nav" + (split ? " active" : "")} title="Split view: chat and command centre side by side"
      aria-label="Split view" aria-pressed=${split ? "true" : "false"} onClick=${toggleSplit}>
      <${Icon} name="columns-2" size=${20} /></button>
    <span class=${"conn dot " + (s.connected ? "ok" : "bad")} title=${s.connected ? "Connected to JARVIS" : "Not connected"}></span>
  </nav>`;
}

function Header({ title }) {
  const s = useStore();
  return html`<header class="top">
    <h1>${title}</h1>
    <div class="grow"></div>
    <span class="pill" title="What JARVIS is doing"><span class=${"dot " + (s.jarvis === "muted" ? "warn" : s.connected ? "ok" : "bad")}></span>
      ${s.connected ? s.activity || STATE_LABEL[s.jarvis] || s.jarvis : "Not connected"}</span>
    ${!s.voice && html`<span class="pill" title="Voice replies are off: JARVIS answers in text only">
      <${Icon} name="volume-x" size=${14} /> Text only</span>`}
    ${!s.announce && html`<span class="pill" title="Announcements are off"><${Icon} name="bell-off" size=${14} /> Quiet</span>`}
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

onEvent(applyEvent);
onConnection((on) => setState({ connected: on }));
render(html`<${App} />`, document.getElementById("root"));
connect();
