// Memory: the facts JARVIS keeps about you. Conversation history joins them once the conversation log
// (memory step 4) is built.
"use strict";

import { html, Icon } from "/static/shared/ui.js";
import { FactsList } from "/static/app/sections/facts.js";

export function MemorySection() {
  return html`<div class="page narrow">
    <section class="card">
      <h2><${Icon} name="brain" size=${18} /> Remembered facts</h2>
      <p class="dim">What you asked JARVIS to remember. It reads the most useful ones before every conversation.</p>
      <${FactsList} />
    </section>
    <section class="card">
      <h2><${Icon} name="history" size=${18} /> Conversations</h2>
      <p class="dim">Past conversations will be listed and searchable here once the conversation log is built.</p>
    </section>
  </div>`;
}
