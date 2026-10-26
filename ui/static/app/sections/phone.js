// The phone app's pairing. On the PC (Settings, Phone): a QR code that opens the phone's page with a one-time
// code in the address's #fragment, and the paired devices with Remove. On the phone: the screen shown until
// it's paired, which trades the code (from the QR code, or typed) for a device token (shared/ws.js).
"use strict";

import { useEffect, useState } from "/static/vendor/preact/hooks.module.js";
import { html, Icon } from "/static/shared/ui.js";
import { useStore } from "/static/shared/store.js";
import { ask, pair } from "/static/shared/ws.js";

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never");
const spaced = (code) => code.replace(/^(.{5})/, "$1 ");

function deviceName() {
  const ua = navigator.userAgent;
  return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : "Phone";
}

// ---------------------------------------------------------------- on the phone

export function PairScreen({ code: fromLink, onPaired }) {
  const [code, setCode] = useState(fromLink || "");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  async function go(value) {
    const c = value.replace(/[^0-9a-z]/gi, "");
    if (!c) return;
    setBusy(true); setNote("");
    const r = await pair(c, deviceName());
    setBusy(false);
    if (r.ok) onPaired(); else setNote(r.message);
  }
  useEffect(() => { if (fromLink) go(fromLink); }, []);

  return html`<div class="pair-screen">
    <div class="pair-card">
      <div class="pair-logo"><span class="brand-dot big"></span></div>
      <h1>Pair this phone with JARVIS</h1>
      <p class="dim">On your PC, open JARVIS's Settings, then Phone, and press <b>Pair a phone</b>. Scan the QR
        code with the camera, or type the code shown under it here.</p>
      <form class="pair-form" onSubmit=${(e) => { e.preventDefault(); go(code); }}>
        <input type="text" inputmode="text" autocapitalize="characters" autocomplete="off" spellcheck="false"
          maxlength="14" placeholder="Pairing code" aria-label="Pairing code" value=${code}
          onInput=${(e) => setCode(e.target.value.toUpperCase())} />
        <button type="submit" class="btn primary big" disabled=${busy || !code.trim()}>
          <${Icon} name=${busy ? "loader" : "link"} size=${17} /> ${busy ? "Pairing…" : "Pair"}</button>
      </form>
      ${note && html`<p class="bad-text small" role="status">${note}</p>`}
      <p class="faint small">The code works once, for 5 minutes. After pairing, this phone stays paired until
        you remove it on the PC.</p>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- on the PC (Settings, Phone)

export function PhonePanel() {
  const s = useStore();
  const [view, setView] = useState(null);      // {remote, devices}
  const [pairing, setPairing] = useState(null);  // {url, code, qr, until}
  const [left, setLeft] = useState(0);
  const [note, setNote] = useState("");
  const [removing, setRemoving] = useState(null); // the device whose Remove asks "sure?"

  const load = () => ask({ type: "devices" }).then((d) => { if (d) setView(d); return d; });
  useEffect(() => { if (s.connected) load(); }, [s.connected]);

  // While a code is shown: count down, and look for the new phone every few seconds.
  useEffect(() => {
    if (!pairing) return;
    const before = view ? view.devices.length : 0;
    const tick = setInterval(() => {
      const secs = Math.max(0, Math.round((pairing.until - Date.now()) / 1000));
      setLeft(secs);
      if (secs === 0) { setPairing(null); setNote("The code ran out. Make a new one when you're ready."); }
    }, 1000);
    const look = setInterval(async () => {
      const d = await load();
      if (d && d.devices.length > before) {
        setPairing(null);
        setNote(`Paired: ${d.devices[d.devices.length - 1].name}.`);
      }
    }, 3000);
    return () => { clearInterval(tick); clearInterval(look); };
  }, [pairing]);

  async function start() {
    setNote("");
    const d = await ask({ type: "pair_start" });
    if (!d) { setNote("JARVIS didn't answer. Try again."); return; }
    if (!d.ok) { setNote(d.message); return; }
    setLeft(d.expires_in);
    setPairing({ ...d, until: Date.now() + d.expires_in * 1000 });
  }

  function stop() { ask({ type: "pair_cancel" }); setPairing(null); }

  async function remove(id) {
    setRemoving(null);
    const d = await ask({ type: "remove_device", device: id });
    if (d) { setView(d); setNote(d.ok ? "Removed: that phone can't connect any more." : d.message); }
  }

  if (!view) return html`<p class="dim">${s.connected ? "Loading…" : "Not connected to JARVIS."}</p>`;
  return html`<div class="phone-panel">
    <p class="dim small">Chat with JARVIS from your phone, from anywhere, through Tailscale. The address works
      only on your own Tailscale network, and each phone needs pairing once.</p>
    ${view.remote
      ? html`<div class="status-item"><span class="dot ok"></span><span>Phone address</span><code>https://${view.remote}/app</code></div>`
      : html`<div class="banner"><${Icon} name="info" size=${16} /> Phone access is off. Set web_ui.remote in config.yaml
          (enabled: true and your PC's Tailscale name), run tailscale serve, and restart JARVIS. See docs/mobile.md.</div>`}

    ${pairing
      ? html`<div class="pair-box">
          <img class="qr" alt="QR code that opens JARVIS on your phone" src=${"data:image/svg+xml;charset=utf-8," + encodeURIComponent(pairing.qr)} />
          <div class="pair-info">
            <h3>Scan with your phone's camera</h3>
            <p class="dim small">Or open <code>https://${view.remote}/app</code> on the phone and type this code:</p>
            <div class="pair-code">${spaced(pairing.code)}</div>
            <p class="dim small">Works once. Runs out in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}.</p>
            <button class="btn" onClick=${stop}><${Icon} name="x" size=${15} /> Cancel</button>
          </div>
        </div>`
      : html`<button class="btn primary" disabled=${!view.remote} onClick=${start}>
          <${Icon} name="smartphone" size=${15} /> Pair a phone</button>`}
    ${note && html`<p class="small" role="status">${note}</p>`}

    <h3>Paired devices</h3>
    ${view.devices.length === 0
      ? html`<p class="faint small">None yet.</p>`
      : html`<ul class="devices">${view.devices.map((d) => html`<li key=${d.id}>
          <${Icon} name="smartphone" size=${17} />
          <div class="device-text"><div>${d.name}</div>
            <div class="faint small">Paired ${when(d.created)} · last used ${when(d.last_seen)}</div></div>
          ${removing === d.id
            ? html`<button class="btn danger" onClick=${() => remove(d.id)}>Remove it</button>
                   <button class="btn" onClick=${() => setRemoving(null)}>Keep</button>`
            : html`<button class="btn" onClick=${() => setRemoving(d.id)}><${Icon} name="trash-2" size=${15} /> Remove</button>`}
        </li>`)}</ul>`}
  </div>`;
}
