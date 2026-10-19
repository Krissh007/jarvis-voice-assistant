// Memory: the facts JARVIS keeps about you, and the conversations it keeps (finished lines only, for
// memory.keep_conversations_days). Open a conversation to read it; Delete asks once more first.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask } from "/static/shared/ws.js";
import { FactsList } from "/static/app/sections/facts.js";

function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function Conversation({ conv, onDeleted }) {
  const [open, setOpen] = useState(false);
  const [lines, setLines] = useState(null);
  const [armed, setArmed] = useState(false);

  async function toggle() {
    if (!open && lines === null) {
      const d = await ask({ type: "request", what: "conversation", arg: conv.id });
      setLines(d ? d.lines : []);
    }
    setOpen(!open);
  }

  async function remove() {
    if (!armed) { setArmed(true); return; }
    const d = await ask({ type: "delete_conversation", conversation: conv.id });
    if (d) onDeleted(d);
  }

  return html`<li class="conv">
    <div class="conv-head">
      <button class="conv-open" aria-expanded=${open ? "true" : "false"} onClick=${toggle}>
        <${Icon} name=${open ? "chevron-right" : "chevron-right"} size=${15} />
        <span class="conv-first">${conv.first || "(JARVIS spoke first)"}</span>
        <span class="faint small">${when(conv.started)} · ${conv.lines} line${conv.lines === 1 ? "" : "s"}
          ${conv.channels ? " · " + conv.channels.replace(",", " and ") : ""}</span>
      </button>
      <button class=${"btn danger" + (armed ? " armed" : "")} onClick=${remove} onBlur=${() => setArmed(false)}
        title="Delete this conversation"><${Icon} name="trash-2" size=${15} /> ${armed ? "Sure? Delete" : "Delete"}</button>
    </div>
    ${open && lines && html`<ol class="conv-lines">
      ${lines.map((l) => html`<li key=${l.id} class=${l.role}>
        <span class="who">${l.role === "user" ? "You" : "JARVIS"}</span><span class="said">${l.text}</span></li>`)}
    </ol>`}
  </li>`;
}

function Conversations() {
  const s = useStore();
  const [data, setData] = useState(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (s.connected) ask({ type: "request", what: "conversations" }).then(setData);
  }, [s.connected]);
  if (!data) return html`<p class="dim">${s.connected ? "Loading…" : "Not connected to JARVIS."}</p>`;
  if (!data.kept) return html`<p class="dim">JARVIS isn't keeping conversations (Settings › Memory › Keep conversations).</p>`;
  return html`<div>
    ${data.paused && html`<p class="small warn-text">This conversation isn't being kept ("don't remember this conversation"). Logging
      starts again with the next new chat.</p>`}
    ${data.conversations.length === 0
      ? html`<p class="dim">Nothing kept yet. What you and JARVIS say from now on appears here.</p>`
      : html`<ul class="convs">${data.conversations.map((c) => html`<${Conversation} key=${c.id} conv=${c}
          onDeleted=${(d) => { setData({ ...data, conversations: d.conversations }); setMessage(d.message); }} />`)}</ul>`}
    ${message && html`<p class="dim small" role="status">${message}</p>`}
  </div>`;
}

export function MemorySection() {
  return html`<div class="page narrow">
    <section class="card">
      <h2><${Icon} name="brain" size=${18} /> Remembered facts</h2>
      <p class="dim">What you asked JARVIS to remember. It reads the most useful ones before every conversation.</p>
      <${FactsList} />
    </section>
    <section class="card">
      <h2><${Icon} name="history" size=${18} /> Conversations</h2>
      <p class="dim">What you and JARVIS said, so you can ask "what did we talk about yesterday?". Only finished lines:
        never tool output, web pages or anything that looked like a password or card number.</p>
      <${Conversations} />
    </section>
  </div>`;
}
