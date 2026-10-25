# The Brain view (Option A, "Brain first") — build plan

Krishna picked Option A of the mock-ups (26 October 2026), inspired by the "AI Workshop OS" interface in
Zubair Trabzada's JARVIS video: a full-screen knowledge graph, a left inspector, a right HUD with status
and capability toggles, and a command bar with the reply as a caption. This is JARVIS's own build of
that layout, on JARVIS's own data; nothing is copied from the video.

## Where it lives

A new **Brain** section in the app (`/app#s=brain`), first in the sidebar and the section the orb's menu
opens. It's a normal window, so CSS animations work (unlike the see-through orb). The other sections
(Chat, Command centre, Memory, Settings) stay; the Brain's command bar and caption cover everyday use,
and the full thread is one click away in Chat.

## What the graph shows (all from JARVIS itself, nothing invented)

| Node type | Comes from | Label |
|---|---|---|
| **Memory** | `MemoryStore.facts()` | the fact |
| **Conversations** | `ConversationLog.conversations()` (kept 30 days) | first thing you said + date |
| **Tools** | `registry.visible()`, grouped by `core/catalog.py` category (hubs: Search, Files, Desktop…) | tool name |
| **Agents** | the Agent Hub's `agent_status` (extended to list each agent's actions) | agent / action |
| **Reminders** | `Reminders.pending()` | reminder text + time |
| **Files** | only files JARVIS actually used (audit log: open/read/write/edit/delete paths), never the 45k-file index | file name |

**Links**, each with a reason the inspector can show:
- every node to its type's hub (category for tools, agent for actions);
- conversation → tool: the tool was called during that conversation (audit log times inside the conversation);
- conversation → fact: the fact was recalled in it (`memory_used`), or its meaning is close (both already
  have meaning vectors; cosine ≥ a threshold chosen on real data);
- tool → file: the call's path argument; agent action → its agent; reminder → the conversation it was set in.

Sizes: about 100 facts, ~60 conversations a month, ~60 tools, ~20 agent actions, a few reminders and
files: a few hundred nodes, room for ~2,000.

**Live**: while JARVIS works, what it's using lights up: `tool_started` pulses the tool, `memory_used`
the facts, `agent_update` the agent, a new reply adds its conversation node. New facts and conversations
arrive as small `brain_update` events, so the graph never reloads.

**Privacy**: everything stays on this PC and is drawn as text (never HTML). Facts and conversation lines
already pass the secret filter; audit-log arguments are already redacted. No web page, screen or
clipboard text enters the graph.

## The layout (Option A)

- **Graph** (full bleed): force layout on a canvas, coloured by type, hub labels, zoom / pan, hover.
  Layout engine: **d3-force** (ISC licence, vendored like Preact, imports made relative) for a stable
  Barnes-Hut simulation that cools and stops, so it's light on the CPU.
- **Left panel**: search the brain (names, then facts and conversations by meaning through the bridge);
  **Inspector**: the focused node's details, why it's linked, and its actions (open the conversation,
  forget a fact — asks first, open a file's folder); **Shift-click** a second node to trace the path;
  **Top hubs** with link counts; **Forces** (repel, link length) and display switches (labels, links).
- **Right column**: the legend as a filter (click a type to hide it, with counts); the **HUD** — a ring
  reticle with J.A.R.V.I.S. that follows JARVIS's state (blue listening, amber working, brighter while
  speaking, grey muted, red with the reason when something's broken), pills for state, voice model,
  language and Voice on/off, and a view switch (**Ring** now; **Orb** = today's 3D orb; Face later);
  **capability toggles** that are real JARVIS switches: Mic (mute), Voice, Announce, Memory (keep /
  pause this conversation), Agents (Hub status; opens the Command centre). Eyes (camera) and Watch
  (screen) appear only when those capabilities are built.
- **Command bar**: Ask box with examples, round buttons (screen picture, attach, web auto/on/off,
  reminders, talk), and the **caption**: JARVIS's live words as it speaks, then the reply; plan cards
  (Approve / Cancel) appear above it when something waits for a yes.

## Phases (stop after each for Krishna's OK)

1. **Brain data.** `core/brain_graph.py` builds nodes and links from the sources above; the bridge answers
   `{type: "request", what: "brain"}` and publishes `brain_update`; the Hub's `agent_status` lists each
   agent's actions; the meaning-link threshold is measured on Krishna's real facts and conversations.
   Tests: every source, link reasons, privacy (no secrets, no outside text), size limits.
2. **Graph view.** Vendor d3-force; the Brain section with the canvas graph, legend filter, zoom / pan,
   focus, inspector (read-only), search, top hubs, forces, path tracing; layout positions remembered
   per node (`localStorage`) so the brain doesn't reshuffle each time. Browser tests (Playwright).
3. **HUD, toggles, command bar.** The ring HUD driven by state events, the real toggles, the command bar
   and caption (reusing the chat composer), plan cards, live highlighting; Brain becomes the default
   section and the orb's menu opens it.
4. **Actions and polish.** Inspector actions (open conversation, forget a fact with confirmation, open a
   file's folder), performance check at 2,000 nodes, docs, the full test suite, then Krishna's batched
   manual test list.

Later, separately: the Face HUD mode, Eyes (webcam) and Watch (screen) capabilities, projects as nodes
(from a short list in config.yaml).

## Decisions (Krishna, 26 Oct 2026)

1. The Brain becomes the app's default section (Phase 3).
2. Files in the graph: only the files JARVIS used (the audit log's path arguments).
3. HUD views now: Ring and the 3D Orb; Face later.

## Phase 1 result (done)

- `core/brain_graph.build()` returns `{nodes, links, counts, generated, capped}`; every link has a reason.
- Facts link to a conversation by the closest single user line (cosine >= 0.55, measured on the real
  14 facts x 21 conversations: a conversation's average matched everything). Shared words count only
  when there are no meaning vectors to compare (on real data they matched on "asking, not").
- The bridge answers `request: brain` off the loop (the Hub's agent list kept for 60 s) and publishes one
  `brain_update` (with `why`) at most every 2 s after messages, tool calls, conversations, recalls,
  agent results, pins, forgets and deletes.
- On the real data: 95 nodes, 210 links, 40 KB, built in about 0.1 s.
