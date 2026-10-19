// Preact + htm (no build step: plain modules, allowed by the page's script-src 'self') and Lucide icons
// from the vendored sprite. Every text is rendered by Preact as text, never as HTML.
"use strict";

import { h, render } from "/static/vendor/preact/preact.module.js";
import htm from "/static/vendor/htm/htm.module.js";

export { render };
export const html = htm.bind(h);

const SPRITE = "/static/vendor/lucide/sprite.svg";

// <${Icon} name="mic" /> : a Lucide icon (vendor/lucide/sprite.svg holds the ones JARVIS uses).
export function Icon({ name, size = 18, label }) {
  return html`<svg class="icon" width=${size} height=${size} aria-hidden=${label ? undefined : "true"}
    role=${label ? "img" : undefined} aria-label=${label}><use href=${`${SPRITE}#i-${name}`} /></svg>`;
}
