// Chat. Phase 3 brings the thread, composer and context panel here; until then this points to the
// chat window JARVIS already has.
"use strict";

import { html, Icon } from "/static/shared/ui.js";
import { control } from "/static/shared/ws.js";

export function ChatSection({ compact }) {
  return html`<div class=${"placeholder" + (compact ? " compact" : "")}>
    <${Icon} name="message-square" size=${compact ? 28 : 36} />
    <h2>Chat moves here next</h2>
    <p class="dim">The thread, composer, plan cards and the panel showing which tools, sources and memory an answer
      used come in the next phase. Until then the chat window works as before.</p>
    <button class="btn" onClick=${() => control("open_chat")}><${Icon} name="message-square" size=${16} /> Open the chat window</button>
  </div>`;
}
