// Command centre. Phase 4 brings the live activity timeline (status dots, approve/cancel) and the status
// strip (Agent Hub and JobPilot, model routing, tools loaded); this is its place in the shell.
"use strict";

import { html, Icon } from "/static/shared/ui.js";

export function CommandSection({ compact }) {
  return html`<div class=${"placeholder" + (compact ? " compact" : "")}>
    <${Icon} name="activity" size=${compact ? 28 : 36} />
    <h2>Command centre</h2>
    <p class="dim">A live timeline of what JARVIS does, with approve and cancel for anything waiting, and the state of
      the Agent Hub, JobPilot and the models. Coming after the chat.</p>
  </div>`;
}
