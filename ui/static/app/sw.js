// The phone app's service worker (served as /sw.js). It keeps only the app's own files (the page and /static/), so the app
// opens at once and shows "Not connected" when the PC can't be reached. Chat, uploads, pairing and the
// WebSocket never pass through it, and nothing JARVIS says is ever stored. Network first: an update of JARVIS
// reaches the phone the next time it opens the app.
"use strict";

const CACHE = "jarvis-shell-v2";
const SHELL = [
  "/app",
  "/static/shared/theme.css", "/static/shared/store.js", "/static/shared/ui.js", "/static/shared/ws.js",
  "/static/app/app.css", "/static/app/main.js", "/static/app/manifest.webmanifest",
  "/static/app/sections/chat.js", "/static/app/sections/chat-model.js", "/static/app/sections/command.js",
  "/static/app/sections/command-model.js", "/static/app/sections/facts.js", "/static/app/sections/memory.js",
  "/static/app/sections/phone.js", "/static/app/sections/settings.js", "/static/app/sections/brain.js",
  "/static/app/sections/brain-ask.js", "/static/app/sections/brain-canvas.js", "/static/app/sections/brain-hud.js",
  "/static/app/sections/brain-model.js",
  "/static/vendor/preact/preact.module.js", "/static/vendor/preact/hooks.module.js",
  "/static/vendor/htm/htm.module.js", "/static/vendor/lucide/sprite.svg",
  "/static/app/icons/icon-192.png", "/static/app/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  const shell = url.pathname === "/app" || url.pathname.startsWith("/static/");   // the app's own files only
  if (req.method !== "GET" || url.origin !== location.origin || !shell) return;
  event.respondWith(fetch(req).then((reply) => {
    if (reply.ok) {
      const copy = reply.clone();
      caches.open(CACHE).then((c) => c.put(url.pathname, copy));
    }
    return reply;
  }).catch(() => caches.match(url.pathname)));
});
