<!-- kit:core/docs/architecture v1 -->
# Architecture

What wscontext is, today. Decisions behind it are in [adr.md](adr.md); the
reasoning behind those is in [design-rationale.md](design-rationale.md).

Nothing is built yet. This file describes the designed system; § Not built yet
tracks which stage delivers each part.

## Purpose and data flow

wscontext is a local harness for Claude Code. It captures every agent session,
links sessions to the Git commits they produced, indexes them alongside a code
graph, and gives the agent one tool, `ctx`, to search both. Nothing leaves the
machine except through an opt-in Git remote.

```
 Claude Code session ──hooks──▶ capture ──▶ raw/ + records/ ──▶ history.db ─┐
        ▲                          ▲          (source of truth)              ├─▶ ctx ──▶ agent context
        │                          │                             graph.db ──┘          (push brief at
        │                 git hooks (trailer,        ▲                                  SessionStart,
        │                 checkpoint, rewrite)       └── repository working tree       pull via skill)
        │                                                + git log
        └───────────────────────────────── ctx output ◀────────────────────────────────┘

 raw/ + records/ ◀──── wscontext/history orphan branch ────▶ sync remote   (opt-in)
```

History flows down the left path, code structure down the right; they meet only
in `ctx`.

## Requirements

Goals:

- Capture every Claude Code session deterministically (→ ADR-001).
- Link each session to the commits it produced (→ ADR-003).
- Answer in one call each: what was done before (`ctx find`), why code exists
  (`ctx why`), what a change affects (`ctx impact`).
- Share history across hosts and teammates through Git alone, with every
  derived artifact rebuildable (→ ADR-005, ADR-007).
- Report statistics on collected data, code, Claude Code turns and `ctx` calls
  (→ ADR-018).
- Never block the agent or a commit (→ ADR-009).
- Run on macOS, Linux and Windows (→ ADR-020).

Non-goals:

- Agents other than Claude Code (→ ADR-002). Other agents in the same IDE, such
  as Copilot Chat, are not captured.
- Cross-repository search.
- A web UI (→ ADR-018).
- Compiler-grade code intelligence (→ ADR-014).

## Topology

Three places hold wscontext's parts.

**Per machine — the plugin.** Installed from the marketplace in this repository
(→ ADR-017). Claude Code keeps the plugin under its plugin cache and gives it a
persistent data directory, where the launcher keeps a uv-managed environment.
Works the same from the terminal, the VS Code extension and any client that
fires Claude Code hooks.

**Per repository — wiring and store.** `ctx init` installs Git hooks (chained to
existing ones) and the CLAUDE.md block. The store is split (→ ADR-004):

```
.wscontext/                                   committed, working tree
  config.toml                                 settings, redaction rules, sync remote
  decisions/<id>.md                           reviewed decision records

<git-common-dir>/wscontext/                   local, shared by all worktrees
  raw/sessions/<date>-<session-id>.jsonl      redacted transcripts          synced
  raw/subagents/<session-id>/<agent-id>.jsonl redacted subagent transcripts synced
  records/sessions/<session-id>.json          normalized sessions            synced
  records/checkpoints/<sha>.json              commit ↔ session links         synced
  index/history.db                            FTS5 (+ vectors)               derived
  index/graph.db                              code graph                     derived
  state/                                      active sessions, queue, locks  host-local
  logs/                                       hook runs, ctx calls           host-local
```

**Per sync remote — the history branch.** `wscontext/history` is an orphan
branch holding `raw/` and `records/` only, written with Git plumbing
(→ ADR-007). It is pushed only when sync is configured, alongside the user's
`git push` or on `ctx sync` (→ ADR-008). A fresh clone recovers history with
`ctx init`, which fetches the branch; indexes rebuild on the first query
(→ ADR-010).

### Hook events

| Hook | Fires when | Records |
|---|---|---|
| `SessionStart` | A session starts, resumes, clears or compacts | Session ID, branch, HEAD, start time, author; throttled history fetch; injects the brief (stage 2+) |
| `UserPromptSubmit` | A prompt is sent | Turn start time |
| `PostToolUse` (all tools, async) | A tool call finishes | Tool name, file path for edits, `ctx` invocation and output size |
| `PreCompact` | Before context compaction | Transcript snapshot — kept only if compaction drops entries from the transcript file (open question in [wscontext-phase-1.md](wscontext-phase-1.md)) |
| `Stop` | A turn ends | Turn end time; copies and redacts the transcript; enqueues indexing |
| `SessionEnd` | A session ends | Final transcript copy, session end, per-session counts |
| Git `prepare-commit-msg` | A commit is being made | `AI-Session` trailer per active session in this worktree |
| Git `post-commit` | After a commit | Checkpoint record; history-branch commit |
| Git `post-rewrite` | After amend or rebase | Checkpoints for rewritten SHAs |
| Git `pre-push` | Before a push | Pushes `wscontext/history` when sync is configured |
| Git `post-checkout`, `post-merge` | Branch switch, merge | Enqueues graph refresh (stage 3+) |

## Data model

Every record carries a schema version and the Git author who produced it.

- **Session** — ID, agent name, branch, HEAD at start and end, start and end
  time, worktree, files touched, subagent IDs, and per-session counts: turns,
  tool calls by tool, `ctx` calls by command, turn tokens where available.
- **Event** — one prompt, assistant message or tool call in a session, with role
  and timestamp. Lives in the raw transcript; the index stores exchanges
  (prompt plus response).
- **Checkpoint** — commit SHA, the session IDs behind it, diff stats, and the
  SHA it was rewritten from, if any.
- **Decision** — what was chosen and why, rejected alternatives, linked files
  and symbols, status (active or superseded), the decision it replaces, and
  whether it is machine-written or reviewed (→ ADR-015).
- **Hook run** (host log) — event, duration, outcome.
- **`ctx` call** (host log) — session, command, redacted query, result count,
  output tokens, latency, freshness flags.

## Components

| Component | What it is | Where (planned) | Stage |
|---|---|---|---|
| `ctx` CLI | Single entrypoint for agent and admin commands (→ ADR-011) | `src/wscontext/cli/` | 1+ |
| Config | Loads `.wscontext/config.toml` into dataclasses (→ ADR-016) | `src/wscontext/config.py` | 1 |
| Store | Resolves the store under the Git common dir; reads and writes records | `src/wscontext/store/` | 1 |
| Hook handlers | One handler per Claude Code event; exit 0, bounded time (→ ADR-009) | `src/wscontext/capture/` | 1 |
| Transcript adapter | Versioned parser of Claude Code transcripts (→ ADR-002) | `src/wscontext/adapters/` | 1 |
| Redactor | Pattern rules applied before every write (→ ADR-006) | `src/wscontext/redact/` | 1 |
| History branch | Plumbing writer, union merge, fetch and push (→ ADR-007, ADR-008) | `src/wscontext/history/` | 1 |
| Git hooks | Shims plus handlers for trailer, checkpoint, rewrite, pre-push; chaining installer | `src/wscontext/githooks/` | 1 |
| Stats | `ctx stats` views over records, logs and indexes (→ ADR-018, ADR-019) | `src/wscontext/stats/` | 1+ |
| History index | FTS5, then vectors; incremental with query-time gap check (→ ADR-013) | `src/wscontext/index/` | 2, 4 |
| Session brief | `SessionStart` context from prior work (→ ADR-012) | `src/wscontext/capture/` | 2 |
| Code graph | tree-sitter nodes and edges, incremental by content hash (→ ADR-014) | `src/wscontext/graph/` | 3 |
| Distiller | Async LLM extraction of decision records (→ ADR-015) | `src/wscontext/distill/` | 4 |
| Plugin package | Marketplace and plugin manifests, `hooks/hooks.json`, `bin/ctx` launchers, skills (→ ADR-017) | repo root: `.claude-plugin/`, `hooks/`, `bin/`, `skills/` | 1 |

## Not built yet

Everything. Stages and their gates are in [roadmap.md](roadmap.md).

| Stage | Delivers | Requirements |
|---|---|---|
| 1 — Capture and trace | Plugin, capture, redaction, commit linking, history sync, `ctx why/sessions/show/forget`, `ctx stats` | [wscontext-phase-1.md](wscontext-phase-1.md) |
| 2 — History search | FTS5 index, `ctx find`, session brief, skill and CLAUDE.md block, adoption stats | not written |
| 3 — Code graph | Graph, `ctx code`, `ctx impact`, code stats | not written |
| 4 — Distillation and semantic search | Decision records with supersession, local embeddings, hybrid ranking | not written |
| 5 — Finalize | Release, Windows verification, purge and uninstall polish | not written |

## Known issues / accepted debt

- The transcript format is internal to Claude Code and changes between versions;
  derived fields disappear until the adapter catches up (→ ADR-002).
- `calls` edges are name-matched and ambiguous (→ ADR-014).
- Removing a session after it was pushed requires rewriting and force-pushing
  the history branch (→ ADR-007).
- Host squash-merges may drop `AI-Session` trailers; checkpoints then exist only
  for the pre-merge SHAs.
