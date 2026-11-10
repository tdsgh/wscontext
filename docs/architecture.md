<!-- kit:core/docs/architecture v1 -->
# Architecture

What wscontext is, today. Decisions behind it are in [adr.md](adr.md); the
reasoning behind those is in [design-rationale.md](design-rationale.md).

Stages 1 and 2 are built. § Stages tracks what each later stage delivers.
Parts planned for stages 3 to 5 are marked.

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

 raw/ + records/ ◀──── refs/wscontext/history ────▶ sync remote   (opt-in)
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
fires Claude Code hooks. The data directory holds `venv/`, `bin/ctx` (the stable
launcher `ctx init` writes), `plugin-root` and the build `lock` (→ ADR-023).
Claude Code creates the directory on first use in a session, not at install.
The plugin's `bin/` is on `PATH` only inside Claude Code's Bash tool and `!`
commands, not in the user's terminal. `CLAUDE_PLUGIN_*` variables reach hook
commands only, not the Bash tool. Uninstalling the plugin does not stop hooks in
sessions already running.

**Per repository — wiring and store.** `ctx init` installs Git hooks (chained to
existing ones) and the CLAUDE.md block. The store is split (→ ADR-004):

```
.wscontext/                                        committed, working tree
  config.toml                                      settings, redaction rules, sync remote
  decisions/<id>.md                                reviewed decision records (stage 4)

<git-common-dir>/wscontext/                        local, shared by all worktrees
  raw/sessions/<date>-<session-id>.jsonl           redacted transcripts           synced
  raw/sessions/<date>-pc<stamp>-<session-id>.jsonl PreCompact snapshots (opt-in)  host-local
  raw/subagents/<session-id>/<agent-id>.jsonl      redacted subagent transcripts  synced
  records/sessions/<session-id>.json               normalized sessions            synced
  records/checkpoints/<sha>.json                   commit ↔ session links         synced
  index/history.db                                 FTS5 (vectors: stage 4)        derived
  index/graph.db                                   code graph (stage 3)           derived
  state/    active.json, turns/<session>.json, materialized.json, pruned.json, last_fetch, last_sweep, locks/, rewrites/   host-local
  logs/     hooks.jsonl, ctx-calls.jsonl (one rotated generation each, .1)                                                  host-local
```

- For new captures, the stored transcript is reduced: a list for each kind of
  entry names what is dropped and what is stored only once (→ ADR-032).
  Copies captured in stage 1 are not trimmed again.
- `raw/` is a local cache of the history ref (→ ADR-033). The `raw/` files of
  sessions older than `retention.raw_days` are removed (pruned). A file is
  pruned only when its final copy is on the local ref and, with sync on, also
  on a remote's history as of the last fetch or push (a shared tip). The
  pruned files' paths on the ref go into `state/pruned.json`. The step that
  copies fetched history into `raw/` and `records/` (materialize) never writes
  these files back (src/wscontext/capture/worker.py:228-274). `ctx show`,
  `ctx why` and the index read a pruned copy from the ref. The index rows of a
  pruned session stay.

**Per sync remote — the history ref.** `refs/wscontext/history` holds `raw/`
and `records/` only, written with Git plumbing (→ ADR-007, ADR-031). It sits
outside `refs/heads/`, so a plain clone or fetch does not download it
(src/wscontext/history/branch.py:18). It is pushed only when sync is
configured, alongside the user's `git push` or on `ctx sync` (→ ADR-008).
Remote-tracking copies live under `refs/wscontext/remotes/<remote>/`
(src/wscontext/history/sync.py:33-41). A fresh clone recovers history with
`ctx init`, which fetches the ref; indexes rebuild on the first query
(→ ADR-010). Fetches and pushes run in background workers that cannot ask for
a credential. Sharing therefore needs a credential that works without a prompt:
an SSH key loaded in the SSH agent, or a Git credential helper.

### Hook events

| Hook | Fires when | Records |
|---|---|---|
| `SessionStart` | A session starts, resumes, clears or compacts | Session record (ID, branch, HEAD, start, author) kept on resume; marks the session active for the worktree; detached history fetch when sync is configured; spawns the sweep worker, which finalizes idle sessions, when a session is idle or the last sweep ran more than a day ago; returns the brief as additional context on `startup`, `clear` and `compact`, never on `resume` or `fork` (src/wscontext/capture/handlers.py:18) |
| `UserPromptSubmit` | A prompt is sent | Turn start time; if the session was finalized while idle, marks it active again |
| `PostToolUse` (all tools, async) | A tool call finishes | Tool name, file path for edits, `ctx` invocation and output size |
| `PreCompact` | Before context compaction | Transcript snapshot only while `capture.precompact_snapshot` is on (off by default, src/wscontext/config.py:34) |
| `Stop` | A turn ends | Turn end time; detached worker copies, redacts and reduces the transcript, then re-indexes the session |
| `SessionEnd` | A session ends | Deactivates the session; detached worker writes the final redacted copy, subagent copies, session record with counts and turns, commits them to the history ref, and re-indexes the session |
| Git `prepare-commit-msg`, `commit-msg` | A commit is being made | `AI-Session` trailer per active session in this worktree; skipped during rebase and `am`; `prepare-commit-msg` skips an empty message (→ ADR-025) |
| Git `post-commit` | After a commit | Detached worker writes the checkpoint and commits it to the history ref, when `HEAD` has trailers; spawns the sweep worker under the same condition as `SessionStart` |
| Git `post-rewrite` | After amend or rebase | Queues old to new pairs; detached worker writes checkpoints for the rewritten SHAs |
| Git `pre-push` | Before a push | Detached worker pushes `refs/wscontext/history` when sync is configured; the shim runs wscontext only if the chained original exited 0 |
| Git `post-merge` | Merge | A merge commit created here that carries `AI-Session` trailers is checkpointed like `post-commit`; a fast-forward and `--squash` write nothing. The graph refresh lands in stage 3 |
| Git `post-checkout` | Branch switch | Installed; the handler is a no-op until stage 3 |
| `ctx` read commands | `why`, `sessions`, `show`, `stats`, `find`, `brief` | Throttled history fetch when sync is configured; spawns the sweep worker under the same condition as `SessionStart` (src/wscontext/cli/main.py:31) |

### Search, brief and idle sessions

- **Index.** One SQLite file, `index/history.db`. One FTS5 table holds
  exchanges (prompt, reply, tool calls) and linked commit messages
  (src/wscontext/index/db.py:19-21). When a session's source changes, the
  whole session is re-indexed (src/wscontext/index/build.py:1-5). The capture
  worker re-indexes a session after each copy. The fetch worker refreshes the
  index after a fetch that changed history. `ctx find`, `ctx brief` and
  `ctx eval` first index any records the index does not have yet (→ ADR-010,
  ADR-013). `ctx rebuild` deletes the index and builds it again.
- **Ranking.** Search matches words only. bm25 ranks a match in the prompt
  highest, then in the commit message, then in the reply, then in tool calls
  (src/wscontext/index/search.py:8). Common English words are dropped from the
  query, unless no other word is left. A search first looks for rows that
  contain every word; words found in half the indexed rows or more are left
  out of this step. If that finds fewer rows than the limit, rows that match
  any word fill the rest. A matching commit is listed right after the
  best-ranked session it is linked to (→ ADR-037)
  (src/wscontext/index/search.py:47-80).
- **Brief.** Inside the `SessionStart` hook, `capture/brief.py` opens the index
  read-only and builds a brief of about 500 tokens. It returns nothing when the
  index is missing (→ ADR-035). The `[retrieval]` settings turn the brief and
  search off (→ ADR-036).
- **Idle sessions.** The `sweep` worker finalizes active sessions that have
  been idle longer than `capture.stale_session_minutes`. Then it prunes
  `raw/`. A sweep that starts while another one runs does nothing
  (src/wscontext/capture/worker.py:277-293; → ADR-034).
- **Eval.** `ctx eval` scores search on a fixed corpus in `eval/stage2/`: the
  sessions, the questions, and a Git bundle of the repository the scripted
  sessions worked in. `acceptance/record_corpus.py` recorded the corpus once.

## Data model

Every record carries a schema version and the Git author who produced it.

- **Session** — ID, agent name, branch, HEAD at start and end, start and end
  time, worktree, files touched, subagent IDs, and per-session counts: turns,
  tool calls by tool, `ctx` calls by command, turn tokens where available.
  Schema 2 adds: how the session ended (`session-end` or `idle`), whether
  retrieval was on, how many brief tokens it was given, and transcript sizes in
  bytes (source, stored, and stored per kind of entry). Readers still accept
  schema 1 (src/wscontext/store/records.py:36-52).
- **Event** — one prompt, assistant message or tool call in a session, with role
  and timestamp. Lives in the raw transcript; the index stores exchanges
  (prompt plus response).
- **Checkpoint** — commit SHA, the session IDs behind it, diff stats, and the
  SHA it was rewritten from, if any.
- **Decision** — what was chosen and why, rejected alternatives, linked files
  and symbols, status (active or superseded), the decision it replaces, and
  whether it is machine-written or reviewed (→ ADR-015).
- **Hook run** (host log) — event, duration, outcome.
- **`ctx` call** (host log) — timestamp, source (`cli` or `hook`), cwd, redacted
  argv, exit code, latency, output bytes, result count, flags. The hook-side
  line carries the session ID; stats join the two by argv and time (→ ADR-019).

## Components

| Component | What it is | Where | Stage |
|---|---|---|---|
| `ctx` CLI | Single entrypoint for agent and admin commands, including `find`, `brief`, `rebuild` and `eval` (→ ADR-011); internal `hook`, `worker`, `git-hook` (→ ADR-027) | `src/wscontext/cli/` | 1+ |
| Config | Loads `.wscontext/config.toml` into dataclasses (→ ADR-016) | `src/wscontext/config.py` | 1 |
| Store | Resolves the store under the Git common dir; reads and writes records | `src/wscontext/store/` | 1 |
| Hook handlers | One handler per Claude Code event; exit 0, bounded time (→ ADR-009) | `src/wscontext/capture/` | 1 |
| Transcript adapter | Versioned parser of Claude Code transcripts (→ ADR-002) | `src/wscontext/adapters/` | 1 |
| Redactor | Pattern rules applied before every write (→ ADR-006) | `src/wscontext/redact/` | 1 |
| History ref | Plumbing writer, union merge, fetch and push of `refs/wscontext/history` (→ ADR-007, ADR-008, ADR-031) | `src/wscontext/history/` | 1 |
| Git hooks | Shims plus handlers for trailer, checkpoint, rewrite, push; chaining installer | `src/wscontext/githooks/` | 1 |
| Stats | `ctx stats` views over records, logs and indexes, including stored volume, index, adoption and brief (→ ADR-018, ADR-019) | `src/wscontext/stats/` | 1+ |
| History index | SQLite FTS5, vectors in stage 4; updated incrementally, and each query first indexes anything missing (→ ADR-013) | `src/wscontext/index/` | 2 (FTS5), 4 (vectors) |
| Session brief | `SessionStart` context from prior work (→ ADR-012, ADR-035) | `src/wscontext/capture/brief.py` | 2 |
| Eval | `ctx eval`, which scores search on the recorded corpus, and the script that records the corpus | `src/wscontext/cli/eval.py`, `eval/`, `acceptance/record_corpus.py` | 2 |
| Code graph | tree-sitter nodes and edges, incremental by content hash (→ ADR-014) | `src/wscontext/graph/` (planned) | 3 |
| Distiller | Async LLM extraction of decision records (→ ADR-015) | `src/wscontext/distill/` (planned) | 4 |
| Plugin package | Marketplace and plugin manifests, `hooks/hooks.json`, `bin/ctx`, `bin/ctx.cmd`, stable launcher, skills (→ ADR-017) | repo root: `.claude-plugin/`, `hooks/`, `bin/`, `skills/` | 1 |

## Stages

Stages 1 and 2 are built and passed their acceptance gates. Stages and their gates
are in [roadmap.md](roadmap.md).

| Stage | Delivers | Requirements | Status |
|---|---|---|---|
| 1 — Capture and trace | Plugin, capture, redaction, commit linking, history sync, `ctx why/sessions/show/forget`, `ctx stats` | [wscontext-phase-1.md](hist/wscontext-phase-1.md) | built |
| 2 — History search | FTS5 index, `ctx find`, session brief, skill and CLAUDE.md block, adoption stats, idle-session finalization, stored-volume reduction, retention pruning, moving shared history out of the branches | [wscontext-phase-2.md](hist/wscontext-phase-2.md) | built |
| 3 — Code graph | Graph, `ctx code`, `ctx impact`, code stats | [wscontext-phase-3.md](wscontext-phase-3.md) | not built |
| 4 — Distillation and semantic search | Decision records with supersession, local embeddings, hybrid ranking | not written | not built |
| 5 — Finalize | Release, Windows verification, purge and uninstall polish | not written | not built |

## Known issues / accepted debt

- The transcript format is internal to Claude Code and changes between versions;
  derived fields disappear until the adapter catches up (→ ADR-002).
- `calls` edges are name-matched and ambiguous (→ ADR-014).
- Removing a session after it was pushed requires rewriting and force-pushing
  the history branch (→ ADR-007).
- Host squash-merges may drop `AI-Session` trailers; checkpoints then exist only
  for the pre-merge SHAs.
- Redaction hits found when materializing shared history are logged as a
  warning, not persisted, so `ctx stats data` does not count them
  (src/wscontext/history/sync.py:184-188).
- `ctx forget` does not edit host logs; their lines were redacted at write
  (src/wscontext/cli/forget.py).
- Page boundaries of `ctx show` are computed per call from the growing raw copy,
  so they shift while the session is active (src/wscontext/cli/show.py:16, 77).
- `post-checkout` is installed but does nothing until stage 3; `post-merge`
  checkpoints a merge commit made here and refreshes nothing until stage 3
  (src/wscontext/githooks/handlers.py:138).
- `bin/ctx.cmd` and the Windows path are untested until Finalize (bin/ctx.cmd:2;
  → ADR-021).
- `.claude-plugin/plugin.json` has no author field; plugin validation warns.
- Hooks keep firing in sessions that were already running when the plugin is
  uninstalled.
- Stored size is kept in check, but it has no upper limit. New captures drop
  Claude Code's bookkeeping lines and duplicated fields (→ ADR-032). Sessions
  captured in stage 1 keep their full copies. Tool results are kept whole
  unless a repository sets `capture.tool_result_max_chars`. Size warnings only
  warn; they enforce nothing (src/wscontext/config.py:54-57). Pruning removes
  only local `raw/` files. The shared ref only grows, and removing anything
  from it requires a history rewrite (→ ADR-007).
- The shared ref is not a branch on the Git host, so the host's branch
  protection does not apply to it.
- Teammates still on stage 1 who push to a remote recreate
  `refs/heads/wscontext/history` there. The next fetch merges it, and the next
  push deletes it again (→ ADR-031).
- An abandoned session is finalized only when wscontext next runs in the
  repository after the staleness window (`capture.stale_session_minutes`) has
  passed: at a session event, a commit or a read command. Nothing runs on a
  timer (→ ADR-034).
- If a prompt or tool call arrives while its session's final step runs, its
  counts can be lost. The final step deletes the turn state when it ends
  (src/wscontext/capture/worker.py:160).
- Search matches words only, so paraphrases are missed. Tool names are indexed
  together with their inputs, so a tool's name matches every session that
  called it (src/wscontext/index/build.py:84).
- Background fetches and pushes cannot ask for a credential. Without a
  credential that works with no prompt, they fail, and `ctx doctor` warns.
