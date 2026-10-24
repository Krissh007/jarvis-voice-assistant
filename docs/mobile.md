# JARVIS on your phone

The phone app is JARVIS's own app page (`/app`), sized for a phone and installable on the home screen (a
PWA). It reaches the PC through Tailscale, so it works from anywhere without opening JARVIS to the network:
JARVIS still listens on 127.0.0.1 only, and `tailscale serve` passes HTTPS from your PC's Tailscale name to
it. Only devices signed in to your own Tailscale network can reach that name.

Phase 1 covers chat: the conversation (spoken and typed), the live status line ("Searching the web…"), Yes
and No on confirmations, attaching a photo, and Settings. Talking from the phone, remote controls,
agents, tasks, reminders and notifications come in later phases.

## One-time setup

1. **Tailscale** (free plan) on the PC and the iPhone, both signed in to the same account.
2. In the Tailscale admin console, under **DNS**: turn on **MagicDNS** and **HTTPS Certificates**. iOS needs
   HTTPS for the app to install properly (and, later, for the microphone).
3. Find the PC's Tailscale name: run `tailscale status --self` or look in the admin console's Machines
   list. It looks like `mypc.tail1234.ts.net`.
4. In `config.yaml`, under `web_ui`:

   ```yaml
   web_ui:
     remote:
       enabled: true
       host: mypc.tail1234.ts.net   # your PC's Tailscale name
       port: 47821                  # the fixed local port (any free one from 1024 to 65535)
   ```

5. On the PC, once (it's remembered across restarts):

   ```bash
   tailscale serve --bg --https=443 http://127.0.0.1:47821
   ```

   `tailscale serve status` shows it; `tailscale serve --https=443 off` stops it. Never use
   `tailscale funnel` for JARVIS: that would put it on the public internet (JARVIS refuses requests that
   come through Funnel anyway).

6. Restart JARVIS. Its log says `web UI: phone access at https://mypc.tail1234.ts.net`.

## Pairing the phone

1. On the PC: open JARVIS's app, **Settings**, **Phone**, and press **Pair a phone**. A QR code and a
   10-character code appear; they work once, for 5 minutes.
2. On the iPhone: scan the QR code with the camera and open the link in Safari. It pairs at once and opens
   the chat.
3. Install it: in Safari, **Share**, **Add to Home Screen**. Open JARVIS from the home screen.
4. iOS keeps a home-screen app's storage apart from Safari's, so the installed app may ask to be paired
   again: press **Pair a phone** on the PC once more and type the code into the app (spaces and lower case
   are fine).

The phone keeps a long-lived device token in its own storage. The PC keeps only a SHA-256 hash of it, in
`data/devices.json`, with the phone's name and when it was paired and last used. To unpair a phone (lost,
replaced, or just to be safe), press **Remove** next to it in Settings, Phone: it disconnects at once and
can't connect again until it's paired anew.

## How it's kept safe

- JARVIS's server binds to 127.0.0.1 only. The phone's requests arrive through `tailscale serve`, and only
  the host named in `web_ui.remote.host` (with its `https://` origin and `wss://` WebSocket) is accepted,
  and only when `enabled` is true. Every other Host or Origin is refused, as before.
- The PC's own windows still use the per-launch token. A phone uses its device token, sent in a header
  (the WebSocket's subprotocol, or `x-jarvis-device` on photo uploads), never in a URL, and never logged.
  The pairing code travels in the address's `#fragment`, which browsers don't send to the server, and the
  page removes it from the address at once.
- A wrong pairing code counts against the current code: after 5 wrong tries it's cancelled. Codes are
  about 50 bits, last 5 minutes and work once; a new code replaces the old one.
- A phone can chat, answer confirmations, attach photos and change settings. It can't pair other phones,
  see or remove devices, take a picture of the PC's screen, or open windows on the PC.
- The app's service worker keeps only the app's own files (so it opens instantly and shows "Not
  connected" when the PC is off). Chat, uploads, pairing and the WebSocket never pass through it.

## Troubleshooting

- **"Couldn't reach JARVIS"**: the PC is asleep or off, JARVIS isn't running, or Tailscale is off on one of
  the devices. `tailscale serve status` on the PC should show the HTTPS line.
- **Settings, Phone says phone access is off**: `web_ui.remote.enabled` isn't true, or `host` isn't a plain
  host name (no `https://`, port or path). Fix `config.yaml` and restart JARVIS.
- **The page loads but stays "Not connected"**: the host in `config.yaml` doesn't match the name in the
  phone's address bar.
- **The log says the port is in use**: JARVIS still starts on the PC (on a free port), but the phone can't
  reach it. Pick another `web_ui.remote.port` and run the `tailscale serve` command again with it.
