// The Brain's command bar: the chat's composer (attach, screen picture, web auto/on/off, talk), with the
// caption above it (what you're saying, JARVIS's words as it speaks, then its reply for a few seconds), any
// plan waiting for your yes (Approve / Cancel, the same as saying "yes"), and the quick examples while
// nothing else is showing. Voice and typing are one conversation, shared with the Chat section.
"use strict";

import { useEffect, useRef, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { send } from "/static/shared/ws.js";
import { useChat } from "/static/app/sections/chat-model.js";
import { Composer, PlanCard } from "/static/app/sections/chat.js";

const CAPTION_MAX = 320;

function cut(text) {
  const s = String(text || "");
  return s.length <= CAPTION_MAX ? s : "…" + s.slice(-(CAPTION_MAX - 1)).trimStart();
}

// What the caption shows now: {who, text} or null.
export function captionOf(turns, live, recentReply) {
  if (live.jarvis) return { who: "JARVIS", text: cut(live.jarvis), live: true };
  if (live.user) return { who: "You", text: cut(live.user), live: true };
  if (recentReply) return { who: "JARVIS", text: cut(recentReply) };
  return null;
}

export function AskBar() {
  const s = useStore();
  const { turns, live } = useChat();
  const last = turns[turns.length - 1];
  const replies = last ? last.replies.length : 0;
  const [recent, setRecent] = useState(null);       // the newest reply, while it's fresh
  const seen = useRef({ turn: last?.id, replies });
  const inputRef = useRef(null);

  useEffect(() => {   // a new reply: show it for a while (the app's caption_seconds)
    const was = seen.current;
    seen.current = { turn: last?.id, replies };
    if (!last || !replies || (was.turn === last.id && was.replies === replies)) return;
    setRecent(last.replies[replies - 1]);
    const t = setTimeout(() => setRecent(null), Math.max(4, s.captionS || 8) * 1000);
    return () => clearTimeout(t);
  }, [last?.id, replies]);

  const waiting = [];
  for (let i = turns.length - 1; i >= Math.max(0, turns.length - 6); i--) {
    for (const p of turns[i].plans) if (p.outcome === null) waiting.push(p);
  }
  const caption = captionOf(turns, live, recent);
  const examples = !caption && !waiting.length ? (s.quickActions || []).slice(0, 4) : [];
  return html`<div class="ask">
    ${waiting.map((p) => html`<div class="ask-plan" key=${p.id}><${PlanCard} p=${p} /></div>`)}
    ${caption && html`<div class=${"caption" + (caption.live ? " live" : "")} role="status" aria-live="polite">
      <span class="caption-who">${caption.who}</span><span class="caption-text">${caption.text}</span></div>`}
    ${examples.length > 0 && html`<div class="ask-examples" aria-label="Examples">
      ${examples.map((text) => html`<button class="chip" key=${text} title="Send this to JARVIS"
        onClick=${() => send({ type: "chat", text, speak: s.voice })}><${Icon} name="sparkles" size=${12} /> ${text}</button>`)}</div>`}
    <div class="ask-box"><${Composer} compact placeholder="Ask JARVIS anything, or tell it what to do…" inputRef=${inputRef} /></div>
  </div>`;
}
