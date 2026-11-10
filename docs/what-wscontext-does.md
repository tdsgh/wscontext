# What wscontext does

A short overview of the wscontext plugin: what a developer can do with it, how
it improves development, and how it affects Claude Code. The system itself is
described in [architecture.md](architecture.md); decisions are in
[adr.md](adr.md).

Stages 1 and 2 are built. Stages 3 and 4 are designed. Everything below marked
stage 3 or later describes the designed plugin. The effects on quality, speed and cost
are expected outcomes that the on/off benchmark has to prove at Alpha and Beta
([roadmap.md](roadmap.md)), not measured results.

## What a developer can do with it

**Set it up once, then it runs on its own** (stage 1)
- Install the plugin once per machine, and run `ctx init` once per repository.
  That installs the Git hooks and a short CLAUDE.md block.
- From then on, every Claude Code session is captured and redacted
  automatically, including subagents and sessions from any worktree. Neither
  the developer nor the agent has to remember anything.
- Every commit made during a session gets an `AI-Session` trailer. `git log`
  then shows which session produced it, on any Git host, including commits made
  from an IDE's source-control panel.

**Trace and review history** (stage 1)
- `ctx why src/auth.py:42` shows the commits that last changed a line, the
  sessions behind them, and the prompts that led to the change.
- `ctx sessions` lists captured sessions, filterable by branch, author, date or
  file.
- `ctx show <id>` pages through one session's redacted transcript.
- `ctx forget <id>` removes a session that captured something it shouldn't
  have. If the session was already shared, it reports that a rewrite of the
  shared history is needed.

**Share history with teammates and other machines** (stage 1, opt-in)
- `ctx sync --setup` names a remote, either `origin` or a separate private one.
  After that, history travels with each normal `git push`.
- A fresh clone plus `ctx init` recovers the whole shared history. A plain
  clone alone does not download it.

**Monitor everything** (stage 1, more views in later stages)
- `ctx stats` reports on collected data, Claude Code turns, `ctx` calls and
  hook health, as terminal tables or `--json`.
- From stage 3 it also reports on the indexed code: files, symbols and edges by
  language.

**Search past work** (stage 2)
- `ctx find "<topic>"` answers "has this been done or discussed before?" It
  lists the past sessions and commits that discuss the topic. You can narrow
  the list by branch, author, date, file or kind.
- When a session starts, the agent gets a short brief of recent related work.
- A conversation that was closed without a clean end is still finalized and
  shared. This happens once it has been idle for longer than the configured
  staleness window.
- Stored transcripts keep only what wscontext reads. Local copies older than
  the retention period are removed.

**Search code** (stages 3–4)
- `ctx code <name>` answers "where is it defined?"
- `ctx impact <symbol>` answers "what could this change break?"
- Stage 4 adds decision records with superseded/active status, plus
  meaning-based search.

## How it improves development

- **The reasoning behind code stops getting lost.** Without wscontext, the
  "why" behind agent-written code disappears when the session ends. With it,
  that reasoning is tied to the commit and lives in Git
  ([wscontext-phase-1.md](hist/wscontext-phase-1.md) §6–7).
- **Less rediscovery.** Past decisions and rejected approaches are searchable,
  so neither the developer nor the agent re-litigates them.
- **Team memory without a server.** History is shared through a Git ref
  that is not a branch, and every machine rebuilds its own indexes
  ([architecture.md](architecture.md#topology)).
- **Safe by default.**
  - Hooks never block a commit or a session; any failure just means missing
    context (ADR-009).
  - Nothing unredacted is stored (ADR-006).
  - Nothing leaves the machine unless sync is turned on (ADR-008).
- **Workflow unchanged.** It works the same from the terminal, the VS Code
  extension or any other client that runs Claude Code. There is no daemon and
  no web UI.

## How it affects Claude Code

### Quality

- **Better grounded changes.** The agent can check why code exists before
  editing it (`ctx why`) and what an edit affects (`ctx impact`).
- **Fewer repeated mistakes.** Past decisions come back at the moment they
  matter, both from the brief injected at session start (push) and from skill
  triggers (pull) (ADR-012).
- **Less risk of acting on stale reasoning.** Every result is flagged when the
  decision behind it was superseded or the code it refers to has changed
  (ADR-011).
- **Retrieved text can't steer the agent.** Results are framed as quoted data,
  not instructions.
- **Risk:** the agent may keep using grep out of habit. `ctx stats queries`
  measures how often it reaches for `ctx` first, so low use shows up instead of
  being assumed.

### Speed

- **Fewer exploration steps.** One `ctx` call is meant to replace a chain of
  grep, read and glob calls.
- **Negligible overhead.**
  - Hooks only start Python and exit; the per-tool-call hook runs
    asynchronously, so tool calls don't wait for it.
  - Heavy work runs in detached workers.
  - Before answering, each query first indexes whatever is new since the last
    run, so nothing has to stay running.
- **Proof:** at Alpha, the on/off benchmark shows fewer search-type tool calls
  per task with wscontext on, read from `ctx stats turns`.

### Costs

| Adds | Saves |
|---|---|
| About 500 tokens per session for the session-start brief | Tokens spent on file dumps and repeated grep/read rounds |
| Up to about 1,500 tokens per `ctx` call (budgeted, with `--limit` and `--full` overrides) | Turns spent rediscovering context or redoing rejected approaches |
| LLM calls for distilling decisions, after sessions end and off the critical path (stage 4 only) | |
| Local disk for the transcripts (only what wscontext reads) and the search index. By default, raw copies older than 180 days are removed locally | |

Embeddings run locally, so search adds no API spend.

The net effect is a hypothesis until measured. A fixed task set run with
wscontext's brief and search off, then on, compares turn, tool-call and token
figures from Alpha on, rather than impressions. The token figures are best
effort: they depend on Claude Code's transcript format, which is internal and
may change (ADR-002).
