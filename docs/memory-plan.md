# Plan: the memory phase (general-assistant Phase 3)

18 October 2026. Decisions made (see the end); nothing is built yet.

## Where memory is today

- `actions/memory.py`: `remember` / `recall` / `forget`, stored in `data/memory.json` (2 facts now,
  100 max). `remember` is safe only when your own words asked for it; `forget` always asks.
  Secrets, passwords, PINs and payment details are refused.
- Every fact is pasted into Gemini Live's system instruction at connect. `recall` matches words,
  so "what's my wife's birthday" doesn't find "Priya was born on 3 March".
- Conversations aren't kept at all: once a session ends (or drops past the 8-line recap from
  586c7e8), JARVIS can't answer "what did we talk about yesterday?".
- `ask_brain` (Groq / Flash) and the chat window never see memory.

## Goal

One local memory shared by voice, chat and `ask_brain`:

1. **Facts you ask it to remember**, found by meaning, not exact words.
2. **Past conversations** (voice and chat), searchable: "what did you tell me about Rust vs Go?",
   "what was that song I asked for on Friday?".
3. Everything stays on this PC, in `data/`, never uploaded except the few lines a search returns
   to the model answering you.

## Design

**Storage: one SQLite file, `data/memory.sqlite`** (SQLite 3.49 in the venv, FTS5 built in).

| Table | What it holds |
|---|---|
| `facts` | id, text, created, updated, source (`voice` / `chat`), embedding |
| `conversations` | id, started, ended, channel(s), optional one-line title |
| `messages` | id, conversation, time, role (`user` / `assistant`), channel, text, embedding |
| `facts_fts`, `messages_fts` | FTS5 keyword indexes over the same text |

**Search: hybrid.** Keyword (FTS5 BM25) plus meaning (cosine similarity with numpy over the stored
vectors), merged by rank. A few thousand rows a year is small: plain numpy is a few milliseconds,
so no vector extension is needed.

**Embeddings: `all-MiniLM-L6-v2` (384 numbers per text), run on this PC.** Two ways to run it:

| | ONNX (recommended) | sentence-transformers |
|---|---|---|
| Adds to the venv | `tokenizers` (~10 MB) + the ONNX model (~90 MB) | torch CPU + transformers (~1 GB+) |
| Uses | onnxruntime, already installed for the wake word | its own stack |
| Speed per sentence | roughly 5–15 ms on CPU (estimate; measured in step 2) | similar |
| Same vectors | yes, same model | yes |

The model file lives in `data/models/` on E:, downloaded once by a `--setup-memory` command you run.
If it's missing, memory still works with keyword search only.

**What gets stored from conversations.** Only finished lines (the bus's `message` events): what you
said and what JARVIS said. Never tool output, web pages, screen or clipboard text, file contents,
or confirmations. Every line passes the existing secret check; a line with a password, PIN or card
number is dropped. Assistant lines can echo web text, so search results go back to the model
wrapped as untrusted data (same as the 586c7e8 recap).

**How the model uses it.**
- Live's system instruction gets your name and the ~20 most-used facts (not all of them).
- `recall` searches facts and conversations by meaning (`topic`, optional `when`: "yesterday",
  "last week").
- `ask_brain` gets read-only `recall` too, so chat and reasoning answers can use memory.
- The 586c7e8 recap can come from `messages`, so a drop can carry more than 8 lines.

**Your control.**
- "Forget that" / "forget today's conversation" / "forget everything": always asks first.
- "Don't remember this conversation" pauses logging until the next new chat.
- `memory.keep_conversations_days` (default 30): older messages are deleted automatically.
- A Memory page in the chat window: list, search, delete (delete still asks).

## Steps

Built in this order; automated tests and my own scripted checks after each, your voice tests
batched at the end (as agreed for this plan).

1. **SQLite store.** `core/memory_db.py`; move `MemoryStore` onto it with the same interface;
   one-time import of `memory.json` (kept as a backup); facts only. Back up `data/` first.
   *Done 18 October 2026.* Also: remembered wishes about how to talk now win over the persona
   ("don't call me sir or Krishna" means no name at all; the newer of two conflicting facts wins).
2. **Embeddings.** `core/embed.py` (ONNX MiniLM, loaded lazily in a background thread),
   `main.py --setup-memory`, keyword-only fallback. Measure load time and per-sentence latency.
3. **Meaning-based recall for facts.** Hybrid search; system instruction gets the top facts only.
   A labelled set of ~30 recall questions in `tests/fixtures/` (written before measuring), as was
   done for routing.
4. **Conversation log.** Record finished messages from voice and chat, secret filter, retention,
   pause command, "forget today's conversation". `recall` with `when`.
5. **Sharing.** `ask_brain` gets read-only `recall`; the drop recap reads from `messages`; Memory
   page in the chat window.
6. **Check and docs.** Recall accuracy and latency on the labelled set, full test suite,
   docs/architecture.md, configuration.md, safety.md, user-guide.md. Then your voice tests.

## Before starting

- **Disk:** C: was full; Playwright's browsers moved to E: (C: ~1.4 GB free). Installs and
  model downloads go to E: (`UV_CACHE_DIR`, `HF_HOME` on E:).
- Your two unpushed commits (wake margin, drop recap) should be voice-tested and pushed first, so
  this work starts from a known-good `main`.

## Decisions (Krishna, 18 October 2026: "go with recommended")

1. **Embedding runtime:** ONNX `all-MiniLM-L6-v2` on the onnxruntime already installed (~100 MB).
2. **Conversation memory:** past conversations are kept for 30 days
   (`memory.keep_conversations_days: 30`).
3. **Conversation titles:** none; search runs over the raw lines, so no Groq/Flash quota is used.
4. **Suggesting facts:** no "want me to remember that?" in this phase.

Work starts after the voice test of 6f3f46f (wake margin) and 586c7e8 (drop recap) passes and
they're pushed. C: now has ~1.4 GB free (Playwright moved to E:); all downloads and caches go to E:.
