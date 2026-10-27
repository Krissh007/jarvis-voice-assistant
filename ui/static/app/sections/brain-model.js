// The Brain view's data, without the drawing: node types and colours, positions kept between loads,
// neighbours, search, the path between two nodes and the busiest hubs. The graph itself comes from
// JARVIS (core/brain_graph.py) through the "brain" request; nothing here invents a node or a link.
"use strict";

export const TYPES = {
  tool: { label: "Tools", color: "#38c6f4" },
  agent: { label: "Agents", color: "#3ccf8e" },
  project: { label: "Projects", color: "#ffb347" },
  reminder: { label: "Reminders", color: "#ff7ab6" },
  file: { label: "Files", color: "#9aa7b8" },
};
export const CORE_COLOR = "#6fd8ff";   // JARVIS itself, in the middle

export function colorOf(node) { return node.type === "core" ? CORE_COLOR : (TYPES[node.type] || TYPES.file).color; }

export function typeLabel(node) {
  if (node.type === "core") return "JARVIS";
  if (node.type === "agent") {
    return node.id.startsWith("action:") ? "Agent action" : node.id.startsWith("work:") ? "Running request"
      : node.hub && node.id.startsWith("agent:") ? "Agent" : "Agents";
  }
  if (node.id.startsWith("project:")) return "Project";
  if (node.hub) return node.id.startsWith("cat:") ? "Tool category" : "Group";
  return { tool: "Tool", reminder: "Reminder", file: "File" }[node.type] || node.type;
}

// The graph from JARVIS, ready to draw: each link's ends resolved to node objects, every node's links,
// and positions taken from the previous load (same id) or the kept layout; a new node starts beside a
// neighbour that already has a place, so a refresh doesn't reshuffle the brain.
export function prepare(graph, previous = new Map(), layout = {}) {
  const nodes = (graph?.nodes || []).map((n) => ({ ...n, links: [] }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = [];
  for (const l of graph?.links || []) {
    const a = byId.get(l.source), b = byId.get(l.target);
    if (!a || !b || a === b) continue;
    const link = { source: a, target: b, reason: l.reason, kind: l.kind };
    links.push(link);
    a.links.push(link);
    b.links.push(link);
  }
  let placed = 0;
  for (const n of nodes) {
    const old = previous.get(n.id);
    const kept = layout[n.id];
    if (old && Number.isFinite(old.x)) { n.x = old.x; n.y = old.y; n.vx = old.vx || 0; n.vy = old.vy || 0; placed++; }
    else if (Array.isArray(kept) && kept.length === 2 && kept.every(Number.isFinite)) { [n.x, n.y] = kept; placed++; }
  }
  for (const n of nodes) {
    if (Number.isFinite(n.x)) continue;
    const near = n.links.map((l) => other(l, n)).find((m) => Number.isFinite(m.x));
    if (near) { n.x = near.x + jitter(n.id, 1) * 30; n.y = near.y + jitter(n.id, 2) * 30; }
  }
  for (const n of nodes) n.degree = n.links.length;
  return { nodes, links, byId, placed };
}

// A small, repeatable offset in [-1, 1] from a node's id (so the same graph lays out the same way).
export function jitter(id, salt) {
  let h = 2166136261 ^ salt;
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
  return ((h >>> 0) % 2001) / 1000 - 1;
}

export function other(link, node) { return link.source === node ? link.target : link.source; }

export function neighbours(node) { return node.links.map((l) => ({ node: other(l, node), reason: l.reason, kind: l.kind })); }

// Nodes whose label (or path / description) contains every word of the query, best first: label starts
// with the query, then hubs, then the most linked.
export function search(nodes, query, hidden = new Set(), limit = 8) {
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const scored = [];
  for (const n of nodes) {
    if (hidden.has(n.type)) continue;
    const label = String(n.label || "").toLowerCase();
    const text = [label, n.detail?.path, n.detail?.folder, n.detail?.description].filter(Boolean).join(" ").toLowerCase();
    if (!words.every((w) => text.includes(w))) continue;
    const score = (label.startsWith(words[0]) ? 4 : label.includes(words[0]) ? 2 : 0) + (n.hub ? 1 : 0) + Math.min(n.degree, 20) / 40;
    scored.push([score, n]);
  }
  return scored.sort((a, b) => b[0] - a[0]).slice(0, limit).map(([, n]) => n);
}

// The chain of links from a to b that says the most (Dijkstra), skipping hidden types; [] when there's
// none. Every link costs 1, and passing through a group hub costs more (JARVIS most), so a path goes
// through JARVIS or "Files used" only when nothing more specific joins the two.
export function path(a, b, hidden = new Set()) {
  if (!a || !b) return [];
  if (a === b) return [a];
  const cost = new Map([[a, 0]]), prev = new Map([[a, null]]), done = new Set();
  const heap = [[0, a]];
  while (heap.length) {
    const [c, n] = pop(heap);
    if (done.has(n)) continue;
    done.add(n);
    if (n === b) {
      const out = [];
      for (let m = b; m; m = prev.get(m)) out.unshift(m);
      return out;
    }
    const through = n === a ? 0 : n.type === "core" ? 6 : isGroup(n) ? 3 : 0;
    for (const l of n.links) {
      const m = other(l, n);
      if (done.has(m) || (hidden.has(m.type) && m !== b)) continue;
      const next = c + 1 + through;
      if (next < (cost.get(m) ?? Infinity)) { cost.set(m, next); prev.set(m, n); push(heap, [next, m]); }
    }
  }
  return [];
}

function push(heap, item) {
  heap.push(item);
  for (let i = heap.length - 1; i > 0;) {
    const up = (i - 1) >> 1;
    if (heap[up][0] <= heap[i][0]) break;
    [heap[up], heap[i]] = [heap[i], heap[up]];
    i = up;
  }
}

function pop(heap) {
  const top = heap[0], last = heap.pop();
  if (heap.length) {
    heap[0] = last;
    for (let i = 0; ;) {
      const l = 2 * i + 1, r = l + 1;
      let m = i;
      if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
      if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
      if (m === i) break;
      [heap[m], heap[i]] = [heap[i], heap[m]];
      i = m;
    }
  }
  return top;
}

// JARVIS and the "Files used"-style hubs only group things; agents, projects and categories say more.
export function isGroup(n) { return n.type === "core" || n.id.startsWith("hub:"); }

export function linkBetween(a, b) { return a.links.find((l) => other(l, a) === b) || null; }

export function topHubs(nodes, hidden = new Set(), limit = 8) {
  return nodes.filter((n) => n.hub && n.type !== "core" && !hidden.has(n.type))
    .sort((a, b) => b.degree - a.degree || String(a.label).localeCompare(String(b.label))).slice(0, limit);
}

export function typeCounts(graph) {
  return Object.fromEntries(Object.keys(TYPES).map((t) => [t, graph?.counts?.[t] || 0]));
}

// Positions to keep, as messages small enough for the page's socket (8 KB each): [{fresh, positions}].
export function layoutChunks(nodes, maxBytes = 6000) {
  const chunks = [];
  let cur = {}, size = 2;
  for (const n of nodes) {
    if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) continue;
    const xy = [Math.round(n.x * 10) / 10, Math.round(n.y * 10) / 10];
    const add = JSON.stringify(n.id).length + JSON.stringify(xy).length + 2;
    if (size + add > maxBytes && size > 2) { chunks.push(cur); cur = {}; size = 2; }
    cur[n.id] = xy;
    size += add;
  }
  if (size > 2 || !chunks.length) chunks.push(cur);
  return chunks.map((positions, i) => ({ fresh: i === 0, positions }));
}

// The inspector's facts about a node, as [label, value] rows (all text).
export function details(n) {
  const d = n.detail || {};
  const rows = [];
  const date = (s) => {
    const t = s ? new Date(s) : null;
    return t && !isNaN(t) ? t.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  };
  if (n.type === "tool" && !n.hub) {
    rows.push(["Does", d.read_only ? "Only reads" : "Can change things (asks first when it matters)"]);
    rows.push(["Used", d.calls ? `${d.calls} time${d.calls === 1 ? "" : "s"} in 30 days` : "Not in 30 days"]);
    if (d.last_used) rows.push(["Last used", date(d.last_used)]);
  } else if (n.type === "agent" && n.id.startsWith("work:")) {
    rows.push(["Running for", d.minutes ? `${d.minutes} min` : "under a minute"]);
  } else if (n.type === "project" && n.id.startsWith("project:")) {
    rows.push(["Folder", String(d.folder || "")]);
    rows.push(["Files used", String(d.files ?? 0)]);
  } else if (n.type === "agent" && n.id.startsWith("action:")) {
    rows.push(["Does", d.side_effect ? "Changes things (asks first)" : "Only reads"]);
  } else if (n.type === "agent" && n.hub && n.id.startsWith("agent:")) {
    rows.push(["State", d.reachable ? "Reachable" : d.optional ? "Not set up" : "Not reachable"]);
  } else if (n.type === "reminder" && !n.hub) {
    rows.push(["Due", date(d.due)]);
  } else if (n.type === "file" && !n.hub) {
    rows.push(["Path", String(d.path || "")]);
    if (d.last_used) rows.push(["Last used", date(d.last_used)]);
  }
  return rows.filter(([, v]) => v);
}
