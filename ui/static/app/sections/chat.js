// Chat: the thread (voice and typing are one conversation), the composer, and the context panel showing
// what JARVIS used for an answer: the tools it called, web sources and memory. Plans and actions that need
// your yes appear as cards in the thread with Approve and Cancel (the same as saying "yes" or the hotkeys).
// Every text is rendered as text; links from the web are shown as site names, never as links.
"use strict";

import { useEffect, useRef, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, control, send, token } from "/static/shared/ws.js";
import { useChat } from "/static/app/sections/chat-model.js";

const TOOL_LABEL = {
  search_web: "Web search", read_page: "Read a page", get_weather: "Weather", recall: "Memory",
  remember: "Remember", look_at_screen: "Looked at the screen", agent_hub_request: "Agent Hub",
  ask_brain: "Reasoning", job_search_status: "Job search status", open_app: "Open an app",
  browser_open: "Browser", find_files: "Find files", read_app: "Read an app", type_in_app: "Type in an app",
};
const label = (tool) => TOOL_LABEL[tool] || tool.replace(/_/g, " ");
const DOT = { running: "warn", waiting: "warn", done: "ok", failed: "bad" };
const WEB = ["auto", "on", "off"];
const WEB_TITLE = { auto: "Web search: JARVIS decides", on: "Web search: on for this message",
                    off: "Web search: off for this message" };

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch {}
  const area = Object.assign(document.createElement("textarea"), { value: text });
  document.body.append(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  return ok;
}

// ---------------------------------------------------------------- one turn

export function PlanCard({ p }) {
  const steps = Array.isArray(p.step.plan) ? p.step.plan : [];
  const done = p.outcome !== null;
  const approved = done && String(p.outcome).startsWith("confirmed");   // confirmed_voice / _typed / _hotkey
  const why = { cancelled: "Cancelled", timeout: "No answer in time: cancelled" }[p.outcome] || "Not done";
  return html`<div class=${"plan" + (done ? " resolved" : "")}>
    <div class="plan-head"><${Icon} name=${done ? (approved ? "circle-check" : "circle-x") : "triangle-alert"} size=${16} />
      <span>${done ? (approved ? "Approved" : why) : "Should JARVIS go ahead?"}</span></div>
    <p class="plan-summary">${p.summary}</p>
    ${p.step.goal && html`<p class="dim small">Goal: ${p.step.goal}</p>`}
    ${steps.length > 0 && html`<ol class="plan-steps">${steps.map((s, i) => html`<li key=${i}
      class=${p.step.plan_step === i + 1 ? "now" : ""}>${label(s)}</li>`)}</ol>`}
    ${!done && html`<div class="plan-actions">
      <button class="btn primary" onClick=${() => control("confirm")}><${Icon} name="check" size=${15} /> Approve</button>
      <button class="btn" onClick=${() => control("cancel")}><${Icon} name="x" size=${15} /> Cancel</button></div>`}
  </div>`;
}

function Reply({ text, last, voice }) {
  const [note, setNote] = useState("");
  const ref = useRef(null);
  const flash = (m) => { setNote(m); setTimeout(() => setNote(""), 2500); };
  async function pin() {
    const sel = window.getSelection();
    const picked = sel && ref.current && ref.current.contains(sel.anchorNode) ? sel.toString().trim() : "";
    const d = await ask({ type: "pin", text: picked || text });
    flash(d ? d.message : "JARVIS didn't answer.");
  }
  return html`<div class="msg jarvis">
    <div class="bubble" ref=${ref}>${text}</div>
    <div class="msg-actions">
      <button title="Copy" aria-label="Copy" onClick=${async () => flash((await copyText(text)) ? "Copied." : "Couldn't copy.")}>
        <${Icon} name="copy" size=${14} /></button>
      ${last && html`<button title="Answer again, differently" aria-label="Regenerate"
        onClick=${() => send({ type: "regenerate", speak: voice })}><${Icon} name="refresh-cw" size=${14} /></button>`}
      <button title="Pin to memory (select part of the reply to pin just that)" aria-label="Pin to memory" onClick=${pin}>
        <${Icon} name="pin" size=${14} /></button>
      ${note && html`<span class="dim small" role="status">${note}</span>`}
    </div>
  </div>`;
}

function Turn({ t, last, selected, select, voice }) {
  return html`<div class=${"turn" + (selected ? " selected" : "")} onClick=${() => select(t.id)}>
    ${t.user && html`<div class="msg user"><div class="bubble">${t.user.text}</div>
      <div class="faint small">${t.user.channel === "voice" ? "spoken" : "typed"}</div></div>`}
    ${t.pictures.map((p, i) => html`<figure key=${i} class="pic"><img src=${p.data} alt=${p.caption || p.title} />
      <figcaption class="faint small">${p.title}${p.caption ? " · " + p.caption : ""}</figcaption></figure>`)}
    ${t.tools.length > 0 && html`<div class="tool-chips">${t.tools.map((x, i) => html`<span key=${i} class="chip">
      <span class=${"dot " + DOT[x.state]}></span>${label(x.tool)}</span>`)}</div>`}
    ${t.plans.map((p) => html`<${PlanCard} key=${p.id} p=${p} />`)}
    ${t.notes.map((n, i) => html`<div key=${i} class="shown"><div class="shown-title">${n.title}</div>
      ${n.items.length ? html`<ol>${n.items.map((x, j) => html`<li key=${j}>${x}</li>`)}</ol>` : html`<p>${n.text}</p>`}</div>`)}
    ${t.replies.map((r, i) => html`<${Reply} key=${i} text=${r} last=${last && i === t.replies.length - 1} voice=${voice} />`)}
  </div>`;
}

// ---------------------------------------------------------------- context panel

function Context({ t }) {
  if (!t) return html`<aside class="context"><p class="dim small">What JARVIS uses for an answer shows here.</p></aside>`;
  const memFacts = t.memory.flatMap((m) => m.facts);
  const memLines = t.memory.reduce((n, m) => n + m.lines, 0);
  return html`<aside class="context" aria-label="What this answer used">
    <h3><${Icon} name="cpu" size=${14} /> Tools</h3>
    ${t.tools.length ? html`<ul class="ctx">${t.tools.map((x, i) => html`<li key=${i}><span class=${"dot " + DOT[x.state]}></span>
      ${label(x.tool)}<span class="faint small">${x.state === "running" ? "running" : x.state === "waiting" ? "waiting for you" : x.state}</span></li>`)}</ul>`
      : html`<p class="faint small">None: answered directly.</p>`}
    <h3><${Icon} name="globe" size=${14} /> Web sources</h3>
    ${t.sources.length ? html`<ul class="ctx">${t.sources.map((s, i) => html`<li key=${i} title=${s.title}>
      <span class="src-title">${s.title}</span><span class="faint small">${s.site}</span></li>`)}</ul>`
      : html`<p class="faint small">None.</p>`}
    <h3><${Icon} name="brain" size=${14} /> Memory</h3>
    ${memFacts.length || memLines ? html`<ul class="ctx">
      ${memFacts.map((f, i) => html`<li key=${i}>${f}</li>`)}
      ${memLines > 0 && html`<li class="dim">${memLines} line${memLines === 1 ? "" : "s"} from past conversations</li>`}</ul>`
      : html`<p class="faint small">No memory looked up. Your remembered facts are always available to JARVIS.</p>`}
  </aside>`;
}

// ---------------------------------------------------------------- composer

export function Composer({ compact, placeholder = "Message JARVIS…", inputRef }) {
  const s = useStore();
  const [text, setText] = useState("");
  const [web, setWeb] = useState("auto");
  const [pics, setPics] = useState([]);       // {id, preview, name}
  const [note, setNote] = useState("");
  const fileRef = useRef(null);
  const busy = s.jarvis === "speaking" || s.jarvis === "working";

  async function upload(file) {
    if (!file) return;
    setNote("Adding the picture…");
    try {
      const r = await fetch("/upload", { method: "POST", body: file,
        headers: { "x-jarvis-token": token, "x-file-name": file.name.slice(0, 80), "content-type": file.type || "application/octet-stream" } });
      const d = await r.json();
      if (d.ok) { setPics((p) => [...p, d].slice(-4)); setNote(""); } else setNote(d.message);
    } catch { setNote("Couldn't add the picture."); }
  }

  async function snap() {
    setNote("Taking a picture of your screen…");
    try {
      const r = await fetch("/snap", { method: "POST", headers: { "x-jarvis-token": token } });
      const d = await r.json();
      if (d.ok) { setPics((p) => [...p, d].slice(-4)); setNote(d.note || ""); } else setNote(d.message);
    } catch { setNote("Couldn't take a picture of the screen."); }
  }

  function submit(e) {
    e && e.preventDefault();
    const t = text.trim();
    if (!t && !pics.length) return;
    const payload = { type: "chat", text: t, speak: s.voice };
    if (web !== "auto") payload.web = web;
    if (pics.length) payload.attachments = pics.map((p) => p.id);
    if (send(payload)) { setText(""); setPics([]); setWeb("auto"); setNote(""); }
    else setNote("Not connected: your message is still in the box.");
  }

  return html`<form class=${"composer" + (compact ? " compact" : "")} onSubmit=${submit}>
    ${pics.length > 0 && html`<div class="pics">${pics.map((p) => html`<span key=${p.id} class="pic-chip">
      <img src=${p.preview} alt=${p.name} /><button type="button" title="Remove" aria-label="Remove picture"
        onClick=${() => setPics(pics.filter((x) => x.id !== p.id))}><${Icon} name="x" size=${12} /></button></span>`)}</div>`}
    <textarea rows="1" maxlength="2000" placeholder=${placeholder} aria-label="Message to JARVIS" value=${text} ref=${inputRef}
      onInput=${(e) => { setText(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; }}
      onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey) submit(e); }}></textarea>
    <div class="composer-bar">
      <input type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" hidden ref=${fileRef}
        onChange=${(e) => { upload(e.target.files[0]); e.target.value = ""; }} />
      <button type="button" class="tool" title="Attach a picture" aria-label="Attach a picture" onClick=${() => fileRef.current.click()}>
        <${Icon} name="paperclip" size=${17} /></button>
      <button type="button" class="tool" title="Attach a picture of my screen (private windows are blacked out)"
        aria-label="Attach my screen" onClick=${snap}><${Icon} name="image" size=${17} /></button>
      <button type="button" class=${"tool web-" + web} title=${WEB_TITLE[web] + " (click to change)"} aria-label=${WEB_TITLE[web]}
        onClick=${() => setWeb(WEB[(WEB.indexOf(web) + 1) % WEB.length])}><${Icon} name="globe" size=${17} />
        <span>${web === "auto" ? "Web: auto" : web === "on" ? "Web: on" : "Web: off"}</span></button>
      <span class="grow note dim small" role="status">${note}</span>
      <button type="button" class="tool" title="Talk instead (Ctrl+Alt+Space)" aria-label="Talk" onClick=${() => control("talk")}>
        <${Icon} name="mic" size=${17} /></button>
      ${busy && html`<button type="button" class="btn" title="Stop (Ctrl+Alt+S)" onClick=${() => control("stop")}>
        <${Icon} name="square" size=${14} /> Stop</button>`}
      <button type="submit" class="btn primary" disabled=${!text.trim() && !pics.length}><${Icon} name="send" size=${15} /> Send</button>
    </div>
  </form>`;
}

// ---------------------------------------------------------------- the section

export function ChatSection({ compact }) {
  const s = useStore();
  const { turns, live } = useChat();
  const [selected, setSelected] = useState(null);
  const box = useRef(null);
  const atBottom = useRef(true);
  const lastId = turns.length ? turns[turns.length - 1].id : null;
  const shown = turns.find((t) => t.id === selected) || turns[turns.length - 1];

  useEffect(() => { setSelected(null); }, [lastId]);                     // a new turn: follow it
  useEffect(() => { const b = box.current; if (b && atBottom.current) b.scrollTop = b.scrollHeight; });

  return html`<div class=${"chat" + (compact ? " compact" : "")}>
    <div class="thread-col">
      <div class="thread" ref=${box} onScroll=${(e) => { const b = e.target; atBottom.current = b.scrollHeight - b.scrollTop - b.clientHeight < 80; }}>
        ${turns.length === 0 && !live.user && !live.jarvis && html`<div class="empty">
          <${Icon} name="message-square" size=${30} /><h2>Talk or type</h2>
          <p class="dim">Voice and typing share one conversation. Say "hey Jarvis" or type below.</p></div>`}
        ${turns.map((t) => html`<${Turn} key=${t.id} t=${t} last=${t.id === lastId} selected=${!compact && shown && t.id === shown.id}
          select=${setSelected} voice=${s.voice} />`)}
        ${live.user && html`<div class="msg user live"><div class="bubble">${live.user}</div><div class="faint small">listening…</div></div>`}
        ${live.jarvis && html`<div class="msg jarvis live"><div class="bubble">${live.jarvis}</div></div>`}
        ${s.activity && html`<div class="msg jarvis activity" role="status"><div class="bubble"><span class="pulse"></span>${s.activity}</div></div>`}
      </div>
      <${Composer} compact=${compact} />
    </div>
    ${!compact && html`<${Context} t=${shown} />`}
  </div>`;
}
