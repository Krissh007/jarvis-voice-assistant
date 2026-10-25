// The Brain view's graph on a canvas: a d3-force layout (it cools and stops, so an idle brain costs
// nothing), zoom and pan, dragging nodes, focus (only a node and its links light up) and a traced path.
// Labels are drawn as canvas text, never HTML. Redraws only when something moved or changed.
"use strict";

import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from "/static/vendor/d3/force/index.js";
import { colorOf, isGroup, other } from "/static/app/sections/brain-model.js";

const MIN_K = 0.15, MAX_K = 4;
const LABEL_FONT = '11px "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif';
const HUB_FONT = '600 12px "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif';

export function radius(n) {
  if (n.type === "core") return 14;
  if (n.hub) return 6 + Math.min(10, Math.sqrt(n.degree) * 1.4);
  return 3 + Math.min(4, Math.sqrt(n.degree) * 0.9);
}

export class BrainCanvas {
  constructor(canvas, { onFocus, onHover, onSettled, inset } = {}) {
    this.cv = canvas;
    this.g = canvas.getContext("2d");
    this.onFocus = onFocus || (() => {});
    this.onHover = onHover || (() => {});
    this.onSettled = onSettled || (() => {});
    this.inset = inset || (() => 0);   // pixels on the right covered by something floating (the legend)
    this.right = 0;
    this.nodes = [];
    this.links = [];
    this.hidden = new Set();
    this.cam = { x: 0, y: 0, k: 1 };
    this.focused = null;
    this.trail = [];          // a traced path: nodes in order
    this.hover = null;
    this.labels = "hubs";     // "all" | "hubs" | "none"
    this.showLinks = true;
    this.forces = { repel: 60, distance: 40 };
    this.dirty = true;
    this.sim = forceSimulation([]).stop()
      .force("charge", forceManyBody().distanceMax(500))
      .force("link", forceLink([]))
      .force("collide", forceCollide((n) => radius(n) + 2))
      .force("x", forceX(0).strength(0.03))
      .force("y", forceY(0).strength(0.03))
      .on("tick", () => { this.dirty = true; if (this.autoFit && ++this._ticks % 8 === 0) this.fit(false); })
      .on("end", () => { if (this.autoFit) { this.autoFit = false; this.fit(); } this.onSettled(); });
    this.applyForces();
    this._bind();
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(canvas);
    this.resize();
    this._frame = requestAnimationFrame(this._draw);
  }

  destroy() {
    cancelAnimationFrame(this._frame);
    this.sim.stop();
    this._ro.disconnect();
    for (const [target, type, fn, opts] of this._listeners) target.removeEventListener(type, fn, opts);
  }

  // ---- data

  setGraph({ nodes, links, placed }) {
    const fresh = !this.nodes.length;
    this.nodes = nodes;
    this.links = links;
    this.focused = this.focused && nodes.find((n) => n.id === this.focused.id) || null;
    this.trail = this.trail.map((t) => nodes.find((n) => n.id === t.id)).filter(Boolean);
    if (this.trail.length < 2) this.trail = [];
    this.sim.nodes(nodes);
    this.sim.force("link").links(links);
    this.applyForces();
    // Already laid out (kept positions): a gentle settle. A new brain: the full layout.
    const known = nodes.length ? placed / nodes.length : 0;
    this.sim.alpha(known > 0.9 ? 0.08 : known > 0.5 ? 0.3 : 1).restart();
    if (fresh) {
      this.fit(false);
      this.autoFit = known <= 0.9;   // a new layout grows: keep it in view until it settles or you move it
      this._ticks = 0;
    }
    this.dirty = true;
  }

  applyForces() {
    const { repel, distance } = this.forces;
    this.sim.force("charge").strength((n) => -repel * (n.hub ? 3 : 1));
    this.sim.force("link")
      .distance((l) => (isGroup(l.source) || isGroup(l.target) ? distance * 1.6 : l.source.hub && l.target.hub ? distance * 2 : distance))
      .strength((l) => (l.kind === "structure" ? 0.5 : 0.15));
  }

  setForces(forces) {
    this.forces = { ...this.forces, ...forces };
    this.applyForces();
    this.sim.alpha(0.4).restart();
  }

  shake() { this.sim.alpha(1).restart(); }

  setHidden(hidden) { this.hidden = hidden; this.dirty = true; }

  setDisplay({ labels, showLinks }) {
    if (labels) this.labels = labels;
    if (showLinks !== undefined) this.showLinks = showLinks;
    this.dirty = true;
  }

  visible(n) { return n.type === "core" || !this.hidden.has(n.type); }

  // ---- focus, path, camera

  focus(node, { center = false } = {}) {
    this.focused = node;
    this.trail = [];
    if (node && center) this.centerOn(node);
    this.dirty = true;
  }

  trace(nodes) { this.trail = nodes.length > 1 ? nodes : []; this.dirty = true; }

  centerOn(node, k) {
    const from = { ...this.cam }, to = { x: node.x, y: node.y, k: k || Math.max(this.cam.k, 1.2) };
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 350), e = t * (2 - t);
      this.cam = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, k: from.k + (to.k - from.k) * e };
      this.dirty = true;
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  fit(animate = true) {
    const shown = this.nodes.filter((n) => this.visible(n) && Number.isFinite(n.x));
    if (!shown.length || !this.w) { this.cam = { x: 0, y: 0, k: 1 }; return; }
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of shown) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const k = Math.max(MIN_K, Math.min(1.6, Math.min((this.w - this.right) / (x1 - x0 + 120), this.h / (y1 - y0 + 120))));
    const target = { x: (x0 + x1) / 2, y: (y0 + y1) / 2 };
    if (animate) this.centerOn(target, k); else { this.cam = { ...target, k }; this.dirty = true; }
  }

  zoom(factor, sx = (this.w - this.right) / 2, sy = this.h / 2) {
    const before = this.toWorld(sx, sy);
    this.cam.k = Math.max(MIN_K, Math.min(MAX_K, this.cam.k * factor));
    const after = this.toWorld(sx, sy);
    this.cam.x += before.x - after.x;
    this.cam.y += before.y - after.y;
    this.dirty = true;
  }

  // the camera looks at the middle of the part of the canvas that isn't covered
  toScreen(n) { return [(this.w - this.right) / 2 + (n.x - this.cam.x) * this.cam.k, this.h / 2 + (n.y - this.cam.y) * this.cam.k]; }
  toWorld(sx, sy) { return { x: this.cam.x + (sx - (this.w - this.right) / 2) / this.cam.k, y: this.cam.y + (sy - this.h / 2) / this.cam.k }; }

  hit(sx, sy) {
    let best = null, bestD = Infinity;
    for (const n of this.nodes) {
      if (!this.visible(n) || !Number.isFinite(n.x)) continue;
      const [x, y] = this.toScreen(n);
      const d = Math.hypot(x - sx, y - sy);
      const reach = Math.max(8, radius(n) * Math.max(0.6, this.cam.k) + 4);
      if (d < reach && d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    this.w = this.cv.clientWidth;
    this.h = this.cv.clientHeight;
    this.right = Math.min(this.w / 2, Math.max(0, this.inset()));
    this.cv.width = Math.round(this.w * dpr);
    this.cv.height = Math.round(this.h * dpr);
    this.dpr = dpr;
    this.dirty = true;
  }

  // ---- input: drag the background to pan, drag a node to move it, click to focus, wheel to zoom

  _bind() {
    this._listeners = [];
    const on = (target, type, fn, opts) => { target.addEventListener(type, fn, opts); this._listeners.push([target, type, fn, opts]); };
    const pos = (e) => { const r = this.cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    on(this.cv, "pointerdown", (e) => {
      if (e.button !== 0) return;
      const [sx, sy] = pos(e);
      const node = this.hit(sx, sy);
      this.drag = { sx, sy, node, cam: { ...this.cam }, moved: false, shift: e.shiftKey };
      this.autoFit = false;
      this.cv.setPointerCapture(e.pointerId);
    });
    on(this.cv, "pointermove", (e) => {
      const [sx, sy] = pos(e);
      const d = this.drag;
      if (!d) {
        const h = this.hit(sx, sy);
        if (h !== this.hover) { this.hover = h; this.cv.style.cursor = h ? "pointer" : "grab"; this.onHover(h); this.dirty = true; }
        return;
      }
      if (!d.moved && Math.hypot(sx - d.sx, sy - d.sy) < 4) return;
      d.moved = true;
      if (d.node) {
        const w = this.toWorld(sx, sy);
        d.node.fx = w.x; d.node.fy = w.y;
        this.sim.alphaTarget(0.15).restart();
      } else {
        this.cv.style.cursor = "grabbing";
        this.cam.x = d.cam.x - (sx - d.sx) / this.cam.k;
        this.cam.y = d.cam.y - (sy - d.sy) / this.cam.k;
      }
      this.dirty = true;
    });
    const end = (e) => {
      const d = this.drag;
      this.drag = null;
      if (!d) return;
      this.cv.style.cursor = this.hover ? "pointer" : "grab";
      if (d.node && d.moved) {
        d.node.x = d.node.fx; d.node.y = d.node.fy;
        d.node.fx = d.node.fy = null;
        this.sim.alphaTarget(0);
        return;
      }
      if (!d.moved && e.type === "pointerup") this.onFocus(d.node, d.shift);
    };
    on(this.cv, "pointerup", end);
    on(this.cv, "pointercancel", end);
    on(this.cv, "pointerleave", () => { if (!this.drag && this.hover) { this.hover = null; this.onHover(null); this.dirty = true; } });
    on(this.cv, "dblclick", (e) => { const [sx, sy] = pos(e); const n = this.hit(sx, sy); if (n) this.centerOn(n, Math.max(this.cam.k, 2)); });
    on(this.cv, "wheel", (e) => {
      e.preventDefault();
      this.autoFit = false;
      const [sx, sy] = pos(e);
      this.zoom(Math.exp(-e.deltaY * 0.0015), sx, sy);
    }, { passive: false });
  }

  // ---- drawing

  _draw = () => {
    this._frame = requestAnimationFrame(this._draw);
    if (!this.dirty || !this.w) return;
    this.dirty = false;
    const g = this.g, k = this.cam.k;
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.clearRect(0, 0, this.w, this.h);

    const lit = new Set(), litLinks = new Set();
    if (this.trail.length) {
      this.trail.forEach((n, i) => {
        lit.add(n);
        const next = this.trail[i + 1];
        if (next) { const l = n.links.find((x) => other(x, n) === next); if (l) litLinks.add(l); }
      });
    } else if (this.focused) {
      lit.add(this.focused);
      for (const l of this.focused.links) { lit.add(other(l, this.focused)); litLinks.add(l); }
    }
    const dimming = lit.size > 0;

    if (this.showLinks || dimming) {
      g.lineWidth = 1;
      for (const l of this.links) {
        const a = l.source, b = l.target;
        if (!this.visible(a) || !this.visible(b) || !Number.isFinite(a.x) || !Number.isFinite(b.x)) continue;
        const on = litLinks.has(l);
        if (!on && !this.showLinks) continue;
        g.strokeStyle = on ? (this.trail.length ? "rgba(255, 220, 140, 0.9)" : "rgba(127, 224, 255, 0.6)")
          : dimming ? "rgba(120, 200, 255, 0.025)" : l.kind === "structure" ? "rgba(120, 200, 255, 0.08)" : "rgba(160, 170, 255, 0.16)";
        g.lineWidth = on ? (this.trail.length ? 2 : 1.4) : 1;
        const [x1, y1] = this.toScreen(a), [x2, y2] = this.toScreen(b);
        g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
      }
    }

    const labelled = [];
    for (const n of this.nodes) {
      if (!this.visible(n) || !Number.isFinite(n.x)) continue;
      const [x, y] = this.toScreen(n);
      if (x < -40 || y < -40 || x > this.w + 40 || y > this.h + 40) continue;
      const dim = dimming && !lit.has(n);
      const c = colorOf(n);
      const r = radius(n) * Math.max(0.6, Math.min(1.6, k));
      g.globalAlpha = dim ? 0.12 : 1;
      if ((n.hub || n === this.focused || n === this.hover) && !dim) { g.shadowColor = c; g.shadowBlur = n === this.focused ? 20 : 10; }
      g.fillStyle = c;
      g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
      g.shadowBlur = 0;
      if (n === this.focused || (this.trail.length && (n === this.trail[0] || n === this.trail[this.trail.length - 1]))) {
        g.strokeStyle = "rgba(230, 245, 255, 0.9)"; g.lineWidth = 1.5;
        g.beginPath(); g.arc(x, y, r + 4, 0, Math.PI * 2); g.stroke();
      }
      g.globalAlpha = 1;
      const wanted = n === this.hover || lit.has(n) ||
        (!dim && (this.labels === "all" ? k > 0.55 || n.hub : this.labels === "hubs" ? n.hub : false));
      if (wanted) labelled.push([n, x, y, r, dim]);
    }
    g.textAlign = "center";
    g.textBaseline = "bottom";
    for (const [n, x, y, r] of labelled) {
      g.font = n.hub ? HUB_FONT : LABEL_FONT;
      const text = short(n.label, n === this.hover || n === this.focused ? 70 : 34);
      g.lineWidth = 3; g.strokeStyle = "rgba(7, 11, 18, 0.85)";
      g.strokeText(text, x, y - r - 4);
      g.fillStyle = n.hub ? "rgba(228, 237, 247, 0.95)" : "rgba(200, 214, 230, 0.85)";
      g.fillText(text, x, y - r - 4);
    }
  };
}

function short(text, n) {
  const s = String(text || "");
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}
