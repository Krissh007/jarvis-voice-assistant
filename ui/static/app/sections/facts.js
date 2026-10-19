// The remembered facts, each with a Forget button that asks once more before it deletes.
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask } from "/static/shared/ws.js";

function when(added) {
  const d = new Date(added);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function FactsList() {
  const s = useStore();
  const [facts, setFacts] = useState(null);
  const [armed, setArmed] = useState(null);       // the fact id whose Forget was pressed once
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!s.connected) return;
    ask({ type: "request", what: "facts" }).then((d) => setFacts(d ? d.facts : null));
  }, [s.connected]);

  async function forget(id) {
    if (armed !== id) { setArmed(id); return; }
    setArmed(null);
    const d = await ask({ type: "forget", fact: id });
    if (d) { setFacts(d.facts); setMessage(d.message); } else setMessage("JARVIS didn't answer. Try again.");
  }

  if (facts === null) return html`<p class="dim">${s.connected ? "Loading…" : "Not connected to JARVIS."}</p>`;
  if (!facts.length) return html`<p class="dim">Nothing remembered yet. Say "remember that…" to JARVIS.</p>`;
  return html`<div>
    <ul class="facts">
      ${facts.map((f) => html`<li key=${f.id}>
        <div class="fact-text">${f.text}<div class="faint small">${when(f.added)}${f.uses ? ` · used ${f.uses}×` : ""}</div></div>
        <button class=${"btn danger" + (armed === f.id ? " armed" : "")} onClick=${() => forget(f.id)}
          onBlur=${() => armed === f.id && setArmed(null)} title="Forget this fact">
          <${Icon} name="trash-2" size=${15} /> ${armed === f.id ? "Sure? Forget" : "Forget"}</button>
      </li>`)}
    </ul>
    ${message && html`<p class="dim small" role="status">${message}</p>`}
  </div>`;
}
